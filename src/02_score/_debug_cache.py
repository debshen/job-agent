"""
_debug_cache.py — One-off diagnostic to figure out why prompt caching isn't
landing. Makes ONE Haiku call with cache_control set on the system block,
then dumps the full usage object so we can see what the API actually returned.

Run it once (~$0.001):
    cd "/path/to/job-agent"
    python3 src/02_score/_debug_cache.py

Delete this file once we've fixed caching.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from criteria_loader import load_criteria, format_criteria_for_prompt
from client import get_client
from triage import SYSTEM_PROMPT


def main() -> int:
    client = get_client()
    criteria = load_criteria()
    block = format_criteria_for_prompt(criteria)

    cached_text = SYSTEM_PROMPT + "\n\n# the candidate's criteria\n\n" + block
    print(f"Cached block length: {len(cached_text):,} characters")

    # Ask Anthropic exactly how many tokens this is — no more guessing
    try:
        token_check = client.messages.count_tokens(
            model="claude-haiku-4-5-20251001",
            system=[{
                "type": "text",
                "text": cached_text,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": "x"}],
        )
        actual = token_check.input_tokens
        # The user message "x" is 1 token; everything else is the system block
        cached_tokens = actual - 1
        print(f"Actual tokens (per Anthropic tokenizer): {cached_tokens:,}")
        if cached_tokens < 1024:
            print(f"  ✗ UNDER 1,024 — caching will silently fail by {1024 - cached_tokens} tokens")
        else:
            print(f"  ✓ over 1,024 by {cached_tokens - 1024} tokens — caching should work")
    except Exception as e:
        print(f"  (count_tokens call failed: {e})")
    print()

    # Test both models — if Sonnet caches but Haiku doesn't, then Haiku 4.5
    # has a higher cache-minimum than the documented 2,048 (Haiku 3.5's value).
    for model in ["claude-haiku-4-5-20251001", "claude-sonnet-4-6"]:
        print(f"========================================================")
        print(f"  Testing model: {model}")
        print(f"========================================================")
        for label in ["Call #1 (should WRITE cache)", "Call #2 (should READ cache)"]:
            print(f"\n--- {label} ---")
            try:
                resp = client.messages.create(
                    model=model,
                    max_tokens=16,
                    system=[{
                        "type": "text",
                        "text": cached_text,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    messages=[{"role": "user", "content": "Reply OK."}],
                )
            except Exception as e:
                print(f"   ✗ call failed: {e!r}")
                break
            u = resp.usage
            print(f"    input_tokens:               {u.input_tokens}")
            print(f"    cache_creation_input_tokens: {getattr(u, 'cache_creation_input_tokens', None)}")
            print(f"    cache_read_input_tokens:     {getattr(u, 'cache_read_input_tokens', None)}")
            print(f"    output_tokens:              {u.output_tokens}")
        print()

    import anthropic
    print(f"\nAnthropic SDK version: {anthropic.__version__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
