"""
triage.py — Cheap "is this even worth scoring?" pass with Claude Haiku.

WHAT THIS DOES (in plain English):
  Most jobs in the database won't pass your hard filters (comp floor, level,
  geography, exclusions). Spending Sonnet money to write nuanced essays about
  jobs that fail "wrong city" or "below the comp floor" is wasteful.

  Triage is a 2-cent first pass: Haiku reads the title + location + JD, checks
  it against the must_have block from criteria.yaml, and returns:
    - "passed" + a short reason  → job moves on to the Sonnet scorer
    - "failed" + a short reason  → job is shelved with the reason logged

WHY HAIKU AND NOT SONNET:
  Haiku is ~10× cheaper than Sonnet and plenty smart enough for "does this
  posting clear the comp floor AND the level is Senior+ AND it's reachable
  from the base location?" That's pattern matching, not deep judgment.

OUTPUT FORMAT:
  We ask Haiku to return JSON. Strict JSON-mode isn't available across all
  Anthropic models, so we ask explicitly and then parse — falling back to
  "passed=True" if the parse fails (better to over-score than to silently
  drop a real opportunity).
"""

from __future__ import annotations

import json
import re

from client import create_message  # retry wrapper for transient API errors

TRIAGE_MODEL = "claude-haiku-4-5-20251001"

# Strip markdown code fences sometimes returned by chatty models
_FENCE_RE = re.compile(r"^```(?:json)?|```$", re.MULTILINE)


SYSTEM_PROMPT = """You are a triage filter for a Product Manager job search agent.

You receive: (1) the candidate's hard-must-have criteria, and (2) one job posting.

Your ONLY job is to decide if the posting clears every must-have filter:
  - Role level (per role_levels include/exclude in the criteria)
  - Comp floor (base salary >= the minimum; if the posting doesn't mention comp
    pass it with uncertainty "high" rather than failing; don't guess a band —
    we'd rather over-include than miss a real role)
  - Location (within radius of base location, OR remote, OR hybrid acceptable)
  - Company size (>= the minimum employees; if size is unknown, pass with
    uncertainty "high")
  - Experience ceiling, if the criteria set one: if the JD's stated MINIMUM
    years-of-experience requirement is at or above it, FAIL (see the
    "Experience requirement" rule below)

Return ONLY a JSON object — no prose before or after — with these fields:
{
  "passed": true | false,
  "reason": "<one short sentence>",
  "uncertainty": "low" | "medium" | "high"
}

Be lenient on "uncertainty: high" cases — pass them through to detailed scoring.


## How to read the criteria

The criteria block below lists the candidate's hard filters in detail. Pay attention
to the role_levels.exclude list — anything matching it is an automatic fail
even if other criteria pass. Title variants count: a "Senior PM - Technical" title matches a
"Technical PM" exclusion if one is listed.

If the criteria exclude people-management titles (Director, VP, Head of),
a posting that explicitly says it is an individual contributor (IC) role
despite a Director title is NOT excluded by title alone. Pass it and let
scoring judge the level; the experience ceiling still applies.

Experience requirement: only applies if the criteria set an experience
ceiling (N years). If the job description explicitly states a MINIMUM of N
or more years (e.g. "N+ years", "minimum N years"), mark passed: false
with reason "over-leveled: requires N+ years." Judge by the LOWER bound of
any range: with N = 10, "8-12 years" still PASSES and "10-15 years" FAILS.
If the JD gives no explicit years number, do NOT filter on this —
leniency wins.

## Examples to anchor your decisions

These examples assume a SAMPLE configuration: target levels Senior through
Lead IC, Director excluded, "Technical PM" excluded, base floor $200K,
US-remote and in-radius hybrid allowed, experience ceiling 10 years. They
show the reasoning pattern only. Always decide using the actual criteria
block below; if it differs from the sample, follow the criteria.

PASS examples (mark passed: true):
  - "Staff Product Manager, Payments — San Francisco / Remote-US — base
    $245-310K." Clearly senior IC, in-radius via remote, comp clears floor.
  - "Senior Product Manager, Search — in-radius office — comp not posted."
    Comp unknown; level and location fit. Pass with uncertainty: high.
  - "Lead Product Manager, Growth — Hybrid SF — $260K base." Title maps to
    Senior+ IC, location in-radius, comp clears.

FAIL examples (mark passed: false):
  - "Senior Product Manager - Technical, Fire TV." Title contains 'Technical'
    which maps to TPM exclusion. Fail regardless of comp/location.
  - "Product Manager II, Card Experience — Remote Canada." PM II is mid-level
    (below Senior), AND Canada-only is outside US-remote.
  - "Staff Product Manager — posted base range tops out below the floor."
    The whole posted range is under the minimum, so comp fails.
  - "Director of Product, Growth." Director is above-target ceiling.
  - "Product Manager, Search — requires 10+ years of product experience,"
    when the criteria's experience ceiling is 10. Fail with reason
    "over-leveled: requires 10+ years," even if title/comp/location fit.

UNCERTAIN cases (mark passed: true with uncertainty: high):
  - Title is plain "Product Manager" but the years required suggest a
    senior-equivalent level under the experience ceiling. Let scoring
    evaluate.
  - Comp not posted. Pass if level looks right; scoring will treat comp as
    unknown.

When in doubt, lean pass — false negatives at triage (real opportunities
dropped) are more costly than false positives (borderline jobs given a low
score and never surfaced in the digest)."""


def _build_user_message(job_row, jd_text: str | None) -> str:
    """Job-specific text. Criteria lives in the cached system prompt now."""
    parts = [
        "## Job posting",
        f"Company: {job_row['company']}",
        f"Title: {job_row['title']}",
        f"Location: {job_row['location'] or '(unspecified)'}",
        f"Department: {job_row['department'] or '(unspecified)'}",
        f"URL: {job_row['url']}",
        "",
        "Description:",
        (jd_text or "(no JD text was fetched — judge from title/company alone)"),
    ]
    return "\n".join(parts)


def _build_system_blocks(criteria_block: str) -> list[dict]:
    """
    System prompt + criteria as a single cacheable block. Marked with
    cache_control: ephemeral so subsequent calls within ~5 minutes pay only
    10% of the input cost on this slice. Big savings across a batch run
    where every job sees the identical criteria.
    """
    return [
        {
            "type": "text",
            "text": SYSTEM_PROMPT + "\n\n# the candidate's criteria\n\n" + criteria_block,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _parse_response(text: str) -> dict:
    """Extract JSON from the model response, with a forgiving fallback."""
    cleaned = _FENCE_RE.sub("", text).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find a {...} block in the text
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    # Couldn't parse — fail open: pass it through to the scorer
    return {"passed": True, "reason": "triage parse fail — passing through", "uncertainty": "high"}


def triage_one(client, cost_tracker, job_row, jd_text: str | None,
               criteria_block: str) -> dict:
    """
    Run Haiku on one job. Returns dict with keys: passed, reason, uncertainty.

    `client` is the Anthropic client from client.get_client().
    `cost_tracker` is the CostTracker that accumulates spend across the run.
    """
    user = _build_user_message(job_row, jd_text)

    resp = create_message(
        client,
        model=TRIAGE_MODEL,
        max_tokens=256,
        system=_build_system_blocks(criteria_block),
        messages=[{"role": "user", "content": user}],
    )

    cost_tracker.record(
        TRIAGE_MODEL,
        input_tokens=getattr(resp.usage, "input_tokens", 0),
        output_tokens=getattr(resp.usage, "output_tokens", 0),
        cache_creation_tokens=getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
        cache_read_tokens=getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
    )

    text = ""
    for block in resp.content:
        # SDK returns a list of blocks; we only asked for text
        if getattr(block, "type", None) == "text":
            text += getattr(block, "text", "")

    parsed = _parse_response(text)
    # Normalize types defensively
    parsed["passed"] = bool(parsed.get("passed", True))
    parsed["reason"] = str(parsed.get("reason", "")).strip()[:300]
    parsed["uncertainty"] = parsed.get("uncertainty", "high")
    return parsed
