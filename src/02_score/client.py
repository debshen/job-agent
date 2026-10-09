"""
client.py — Wraps the Anthropic API so the rest of Layer 02 stays clean.

WHAT THIS DOES (in plain English):
  - Loads your Anthropic API key from a `.env` file at the project root.
  - Builds an Anthropic client object that triage.py and score.py share.
  - Tracks how much each call costs, and prints a friendly summary at the end
    of the run (so you can see exactly what tonight's scoring will cost).

WHY A `.env` FILE?
  Your API key is a SECRET — anyone who gets it can spend money on your
  account. We never put it in code or commit it to git. Standard practice is
  to keep it in a file called `.env` at the project root, and add `.env` to
  your `.gitignore` so it's never accidentally shared.

  Example contents of `.env`:
      ANTHROPIC_API_KEY=sk-ant-api03-...

  See the `.env.example` file for the template.

JARGON DEFINED INLINE:
  - "API key" = a password-like string that proves an HTTP request is from you.
  - "Token"   = a chunk of text Claude reads or writes. Roughly 4 characters
                of English ≈ 1 token. Anthropic prices per million tokens.
  - "SDK"     = "software development kit" — the official Python library that
                wraps the Anthropic HTTP API so you write Python instead of
                hand-crafting HTTP requests.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# HTTP statuses worth retrying: rate limit (429), overloaded (529), and 5xx
# server errors. Everything else (400 bad request, 401 auth, etc.) is permanent
# and should surface immediately.
_RETRY_STATUSES = {429, 500, 502, 503, 529}


def create_message(client, *, max_retries: int = 4, **kwargs):
    """Wrapper around client.messages.create that retries transient API errors
    (rate-limit / overloaded / 5xx) with exponential backoff, then re-raises if
    it still fails. Permanent errors raise immediately so we don't waste time.

    Used by triage.py and score.py so a momentary '529 Overloaded' no longer
    means a job gets skipped for the night — it just waits and tries again.
    """
    delay = 2.0
    last_err = None
    for attempt in range(max_retries):
        try:
            return client.messages.create(**kwargs)
        except Exception as e:  # noqa: BLE001 — we re-raise below unless transient
            status = getattr(e, "status_code", None)
            msg = str(e).lower()
            transient = (
                status in _RETRY_STATUSES
                or "overloaded" in msg
                or "rate limit" in msg
                or "529" in msg
            )
            if not transient or attempt == max_retries - 1:
                raise
            last_err = e
            time.sleep(delay)
            delay *= 2
    raise last_err  # pragma: no cover — loop always returns or raises above


# Pricing as of 2026-04 (USD per million tokens). Update if Anthropic changes
# pricing — this only affects the cost summary printout, not actual billing.
PRICING = {
    "claude-haiku-4-5-20251001": {"input": 1.00,  "output":  5.00},
    "claude-sonnet-4-6":         {"input": 3.00,  "output": 15.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
}

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"


def _load_dotenv() -> None:
    """
    Tiny .env loader — sets os.environ from `KEY=value` lines in .env.
    We don't take a dependency on the `python-dotenv` package for this; the
    format is simple enough that 15 lines do the job.
    """
    if not ENV_PATH.exists():
        return
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Don't override an already-set env var (so CI overrides win)
        os.environ.setdefault(key, value)


def get_client():
    """
    Build and return an Anthropic client, with a friendly error if either the
    SDK isn't installed or the API key is missing.
    """
    _load_dotenv()
    try:
        from anthropic import Anthropic  # type: ignore
    except ImportError:
        sys.stderr.write(
            "\nThe Anthropic Python SDK isn't installed. One-time install:\n"
            "    pip3 install --user anthropic\n\n"
        )
        raise

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.stderr.write(
            "\nANTHROPIC_API_KEY is not set.\n\n"
            "How to fix:\n"
            "  1. Get a key at https://console.anthropic.com/settings/keys\n"
            "     (Sign up / log in, then 'Create Key'. Copy the value.)\n"
            f"  2. Open the file {ENV_PATH}\n"
            "     (Create it if it doesn't exist; copy from .env.example.)\n"
            "  3. Add this line:\n"
            "     ANTHROPIC_API_KEY=sk-ant-api03-...\n\n"
        )
        raise SystemExit(2)

    return Anthropic(api_key=key)


# ---- Cost tracking -----------------------------------------------------------

# Anthropic's prompt-caching pricing multipliers (vs. base input price):
#   - Cache WRITE  (first call that creates the cache):  1.25× normal input
#   - Cache READ   (subsequent calls that hit the cache): 0.10× normal input
#   - Non-cached input:                                   1.00× normal input
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER  = 0.10


class CostTracker:
    """
    Accumulates token counts per model and computes USD cost at print-time.

    Tracks four buckets per model:
      input            — fresh input tokens (charged at full rate)
      cache_creation   — tokens written to the cache (charged at 1.25×)
      cache_read       — tokens served from cache (charged at 0.10×)
      output           — model output tokens (charged at output rate)
    """

    def __init__(self) -> None:
        self.totals: dict[str, dict[str, int]] = {}

    def record(self, model: str, *,
               input_tokens: int = 0,
               output_tokens: int = 0,
               cache_creation_tokens: int = 0,
               cache_read_tokens: int = 0) -> None:
        bucket = self.totals.setdefault(model, {
            "input": 0, "output": 0,
            "cache_creation": 0, "cache_read": 0,
            "calls": 0,
        })
        bucket["input"]          += input_tokens
        bucket["output"]         += output_tokens
        bucket["cache_creation"] += cache_creation_tokens
        bucket["cache_read"]     += cache_read_tokens
        bucket["calls"]          += 1

    def cost_usd(self) -> float:
        total = 0.0
        for model, b in self.totals.items():
            price = PRICING.get(model)
            if not price:
                continue
            total += (b["input"]          / 1_000_000) * price["input"]
            total += (b["cache_creation"] / 1_000_000) * price["input"] * CACHE_WRITE_MULTIPLIER
            total += (b["cache_read"]     / 1_000_000) * price["input"] * CACHE_READ_MULTIPLIER
            total += (b["output"]         / 1_000_000) * price["output"]
        return total

    def report(self) -> str:
        if not self.totals:
            return "  (no API calls made)"
        lines = []
        for model, b in self.totals.items():
            cache_note = ""
            if b["cache_read"] or b["cache_creation"]:
                cache_note = (
                    f"   [cache: {b['cache_read']:>6,} read · "
                    f"{b['cache_creation']:>6,} written]"
                )
            lines.append(
                f"  {model:<35}  {b['calls']:>4} calls  "
                f"{b['input']:>7,} in / {b['output']:>6,} out{cache_note}"
            )
        lines.append(f"  Estimated cost this run: ${self.cost_usd():.4f}")
        return "\n".join(lines)
