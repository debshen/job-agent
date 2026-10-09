"""
score.py — The "real" 0–100 scoring pass with Claude Sonnet.

WHAT THIS DOES (in plain English):
  For every job that passed triage, Sonnet reads the full posting and the full
  criteria, and produces:
    - A 0–100 total score
    - Per-criterion sub-scores (role_fit, comp, ai_moat, wlb, company_health,
      work_model) — each on its own 0–100 scale
    - A one-line rationale you can read in the morning email digest

WHY THE SUB-SCORES ARE EACH 0–100 (NOT WEIGHTED):
  We let the LLM rate each dimension on its own 0–100 scale, then combine
  with the weights in criteria.yaml in OUR code. That separation has two
  benefits:
    1. You can tweak the weights in criteria.yaml without re-running scoring
    2. The sub-scores themselves stay interpretable ("ai_moat: 85" means
       "this is a strong AI moat" regardless of how much you weight it)

OUTPUT FORMAT:
  Sonnet returns JSON. We parse, validate, clamp to 0–100, and return a
  dict that run_score.py writes straight to the database.
"""

from __future__ import annotations

import json
import re

from client import create_message  # retry wrapper for transient API errors

SCORE_MODEL = "claude-sonnet-4-6"

_FENCE_RE = re.compile(r"^```(?:json)?|```$", re.MULTILINE)


SYSTEM_PROMPT = """You are a thoughtful career advisor scoring Product Manager
job openings for the candidate. You receive (1) their detailed criteria and (2) one
job posting that has already passed their hard filters.

Score it on EACH of these dimensions, 0–100:
  - role_fit:        LANE FIT first, then level. Is the problem at the center
                     of the role one the candidate has owned (see "Role fit: lane
                     first" in the criteria), with AI as a lever rather than
                     the whole job (see "AI as the whole role" in the
                     criteria)? Then: does the level match the level guidance
                     in the criteria? This is the most
                     heavily weighted dimension.
  - comp:            Posted base+equity+bonus likely meets/exceeds the floor.
                     If the posting omits comp, comp is unknown (see
                     "Evidence rules" below).
  - ai_moat:         How defensible is this company's product/distribution as
                     general-purpose AI gets cheaper? Use the criteria's
                     positive/negative signals.
  - wlb:             Team sustainability signals stated in the posting:
                     retention, PTO and parental-leave policies, and whether
                     PMs carry on-call duty. Outside facts only per the
                     "Evidence rules" below.
  - company_health:  For public, weight against the 5yr CAGR threshold. For
                     private, use stage / valuation / profitability. Unknown
                     is a valid answer (see "Evidence rules").
  - work_model:      Score the posting's work model using the
                     work_model_preferences ranking in the criteria.

Then output a SINGLE JSON object — nothing before or after — with these fields:
{
  "role_fit": int 0-100,
  "comp": int 0-100,
  "ai_moat": int 0-100,
  "wlb": int 0-100,
  "company_health": int 0-100,
  "work_model": int 0-100,
  "rationale": "one-sentence overall summary",
  "role_note": "≤20 words: the core problem, in-lane or out-of-lane, AI as lever or whole role, level fit",
  "comp_note": "≤20 words: posted base + equity, or unknown",
  "ai_moat_note": "≤20 words: the defensibility take, concrete",
  "wlb_note": "≤20 words: signals and concerns, not generic platitudes",
  "interview_note": "optional ≤25 words: interview process details stated
                     in the posting. OMIT this field if the posting has none."
}

## Evidence rules

Your only source is the material in this request: the criteria and the
job posting. Every factual claim about the company or role (comp, review
ratings, retention, funding, stock performance, profitability, interview
process) must be stated there. Do not use background knowledge or training
data for these facts, however confident you feel. When the posting doesn't
supply the evidence:
  - write "unknown" in the relevant note,
  - score that dimension at a neutral 50,
  - never invent figures, ratings, or quotes.
ai_moat is the one judgment dimension: assess it from the product and
business the posting describes, and label it as your assessment.
In each note, say where a fact came from ("posted range", "stated in
posting", "unknown").

Be direct and calibrated. 50 means 'fine'; 80+ means 'genuinely exciting';
20- means 'avoid'. Don't grade-inflate. Notes should be specific and useful
("Zone 1 caps below the floor") not vague ("comp is okay").


## Calibration anchors

Use these concrete examples to anchor your scoring. They assume the
priorities in the criteria block: the core problem is in the candidate's
lane (see core_lane) with AI as a lever; the target level; the preferred
work model; comp at or above the base floor; an AI moat that doesn't
depend on staying ahead of frontier models; a healthy, sustainable team.

A score of 90+ looks like:
  A role at the target level owning an in-lane problem at a healthy
  company, where AI is a real lever in the roadmap. The candidate's
  preferred work model near their base location. Posted base
  comfortably above the floor with meaningful equity. Strong team-retention
  and benefits signals.
  Scope is core to the company's roadmap.

A score of 75–85 looks like:
  A role at a strong company that clears most criteria but has one or two
  drags: a work model the criteria rank lower, comp clears floor by a
  thin margin, mixed team-retention signals, or AI moat depends
  on first-party data that is real but not yet defensible at scale. The role
  is genuinely interesting and the candidate should apply.

A score of 60–74 looks like:
  A role with comp at the floor, scope that is real but narrow, in a
  domain where AI commoditization is a non-trivial threat. One material
  concern (e.g. the lowest-ranked work model, a PM on-call rotation, or pre-IPO
  equity with uncertain liquidity).
  Worth a closer read, not a slam-dunk apply.

A score of 40–59 looks like:
  Borderline — title is ambiguous (could be IC4 or IC5), comp is unposted
  or posted at-or-below the floor, location is
  technically in-radius but the commute is long, OR the role is at a healthy
  company but in a function that is one rung off (PM-adjacent, not a true PM).
  Surface but de-prioritize.

A score below 40 looks like:
  Multiple criteria fail simultaneously — wrong level + below-floor comp,
  weak moat + poor retention signals, ambiguous scope + unhealthy company. Should
  rarely show up since triage already filters the obvious fails.

## What to look for in the JD

Comp signals: posted ranges, "Zone 1" / "Tier A" geographic bands, equity
language ("RSUs," "options," "ISOs," "fully-vested at signing"), bonus
structure. Be concrete in comp_note — quote the actual number.

Role-fit signals: years required (compare with the candidate's experience and
the JD years guidance in the criteria),
"founder profile" language (a stretch), team size (managing 0 ICs is IC, 3+
is people-management), reporting line (to a Director vs. VP signals seniority
of the role).

AI-moat signals: proprietary corpus, network effects, regulatory entrenchment,
two-sided marketplace liquidity, hardware integration. Distinguish between
"company that uses AI" (weak moat) and "company whose product is structurally
defensible regardless of AI" (strong moat).

Sustainability signals in JDs: documented PTO and parental-leave policies,
team tenure and retention, and clear scope are positive flags. A PM on-call
rotation is a drag. Pace and ambition language ("fast-paced," "high
ownership") is neutral on its own; don't penalize it.

Interview signals (only if the posting describes the process; otherwise
omit): stated interview steps, case studies, or work samples.

## Calibration discipline — common scoring mistakes to avoid

DO cap role_fit around 45 when the CENTER of the role is one of the
out_of_lane_domains in the criteria, no matter how famous the company or
how prominent the AI angle. Judge by the core problem, not shared words:
a compliance/KYC "onboarding" role is not the same as a growth
"onboarding" role.

DO follow any bridge_lane_note in the criteria when deciding whether an
adjacent market (for example, self-serve B2B versus sales-led enterprise)
is in or out of lane. For mixed roles, judge by the center of gravity.

DO NOT boost role_fit just because the role is AI-native. Apply the
criteria's ai_as_whole_role setting to roles where AI or agents ARE the
product. When the role owns an in-lane problem and AI is a lever or the
product happens to be AI-powered, judge it on the lane; that IS a boost.

DO NOT inflate scores because the company name is famous. A "Senior PM" at
a household-name company with no posted comp and a vague scope description
should score in the 60s, not the 80s. Famous-company-halo is the most common scoring error.

DO NOT score role_fit high just because the title contains "Senior" or
"Staff." Read the JD's experience requirement. A "Senior PM" role asking
well above the candidate's stated experience is functionally a higher
level, which should drag role_fit into the 60s.

DO NOT reward unposted comp. If the posting has no range and you have no
reliable band for this company and level, comp is "unknown": score it
around 50 and say so in comp_note. Don't guess a band from the company's
size or reputation.

DO calibrate the wlb score against the criteria's sustainability signals.
Clear PTO and parental-leave policies, strong retention, and no PM on-call
rotation score high. Don't penalize ambitious or fast-paced language by
itself; high standards and a sustainable team aren't in conflict.

DO be generous on AI moat for companies with structural distribution.
Embedded enterprise contracts and two-sided marketplaces are genuine
moats. Pure-play model companies score in the 60s on moat: the moat is a
frontier-model lead, which is real but can erode.

DO weigh location against the candidate's base location and the
work_model_preferences in the criteria: required office days far from the
base location are a work_model drag.

## Format reminders for output

- Total length: keep notes concise. Each note is one sentence, max 20 words.
- Don't repeat the rationale across notes — each note is for ITS dimension.
- "interview_note" is OPTIONAL. Omit the field entirely (don't include it as
  null or empty string) unless the posting describes the interview process.
- Numbers in notes are always preferred over adjectives. "Caps at $215K"
  beats "comp is borderline." "No PTO or leave policy in posting" beats
  "WLB is mid-pack."
- The rationale should be the ONE sentence that summarizes the trade-off.
  It's the line that goes into the email subject and the at-a-glance summary,
  so optimize it for skim-reading."""


def _build_user_message(job_row, jd_text: str | None) -> str:
    """Job-specific text. Criteria lives in the cached system prompt now."""
    return "\n".join([
        "## Job posting",
        f"Company: {job_row['company']}",
        f"Title: {job_row['title']}",
        f"Location: {job_row['location'] or '(unspecified)'}",
        f"Department: {job_row['department'] or '(unspecified)'}",
        f"URL: {job_row['url']}",
        "",
        "Description:",
        (jd_text or "(no JD text was fetched — judge from title/company alone)"),
    ])


def _build_system_blocks(criteria_block: str) -> list[dict]:
    """
    Cached system prompt + criteria. See triage.py for the rationale —
    same trick: tag the static slice with cache_control so Anthropic bills
    it at 10% of normal input cost on every call after the first.
    """
    return [
        {
            "type": "text",
            "text": SYSTEM_PROMPT + "\n\n# the candidate's criteria\n\n" + criteria_block,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _parse_response(text: str) -> dict:
    cleaned = _FENCE_RE.sub("", text).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {}


def _clamp(v, lo=0, hi=100) -> int:
    """Coerce to int in [lo, hi]; return lo on garbage input."""
    try:
        return max(lo, min(hi, int(round(float(v)))))
    except (TypeError, ValueError):
        return lo


def _weighted_total(subs: dict, weights: dict) -> int:
    """
    Weighted average of the sub-scores using criteria.yaml weights.
    Weights don't have to sum to 100 — we normalize.
    """
    keys = ["role_fit", "comp", "ai_moat", "wlb", "company_health", "work_model"]
    weight_map = {
        "role_fit":        weights.get("role_fit", 0),
        "comp":            weights.get("comp", 0),
        "ai_moat":         weights.get("ai_moat", 0),
        "wlb":             weights.get("work_life_balance", 0),
        "company_health":  weights.get("company_health", 0),
        "work_model":      weights.get("work_model", 0),
    }
    total_weight = sum(weight_map.values()) or 1
    weighted = sum(subs[k] * weight_map[k] for k in keys)
    return _clamp(weighted / total_weight)


def score_one(client, cost_tracker, job_row, jd_text: str | None,
              criteria_block: str, criteria_weights: dict) -> dict:
    """
    Run Sonnet on one job. Returns a dict with score_total + per-criterion +
    rationale, ready for run_score.py to write to the DB.
    """
    user = _build_user_message(job_row, jd_text)

    resp = create_message(
        client,
        model=SCORE_MODEL,
        max_tokens=512,
        system=_build_system_blocks(criteria_block),
        messages=[{"role": "user", "content": user}],
    )

    cost_tracker.record(
        SCORE_MODEL,
        input_tokens=getattr(resp.usage, "input_tokens", 0),
        output_tokens=getattr(resp.usage, "output_tokens", 0),
        cache_creation_tokens=getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
        cache_read_tokens=getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
    )

    text = ""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            text += getattr(block, "text", "")

    parsed = _parse_response(text)
    if not parsed:
        # Couldn't parse — return all zeros + a note rather than crashing the run
        return {
            "score_total": 0,
            "score_role_fit": 0, "score_comp": 0, "score_ai_moat": 0,
            "score_wlb": 0, "score_company_health": 0, "score_work_model": 0,
            "score_rationale": "(scorer returned unparseable output — review manually)",
            "score_role_note": None, "score_comp_note": None,
            "score_ai_moat_note": None, "score_wlb_note": None,
            "score_interview_note": None,
            "score_model": SCORE_MODEL,
        }

    subs = {
        "role_fit":       _clamp(parsed.get("role_fit")),
        "comp":           _clamp(parsed.get("comp")),
        "ai_moat":        _clamp(parsed.get("ai_moat")),
        "wlb":            _clamp(parsed.get("wlb")),
        "company_health": _clamp(parsed.get("company_health")),
        "work_model":     _clamp(parsed.get("work_model")),
    }

    def _short(field: str, cap: int = 200) -> str | None:
        v = parsed.get(field)
        if v is None:
            return None
        s = str(v).strip()
        return s[:cap] if s else None

    return {
        "score_total":          _weighted_total(subs, criteria_weights),
        "score_role_fit":       subs["role_fit"],
        "score_comp":           subs["comp"],
        "score_ai_moat":        subs["ai_moat"],
        "score_wlb":            subs["wlb"],
        "score_company_health": subs["company_health"],
        "score_work_model":     subs["work_model"],
        "score_rationale":      str(parsed.get("rationale", "")).strip()[:500],
        "score_role_note":      _short("role_note"),
        "score_comp_note":      _short("comp_note"),
        "score_ai_moat_note":   _short("ai_moat_note"),
        "score_wlb_note":       _short("wlb_note"),
        "score_interview_note": _short("interview_note", cap=300),
        "score_model":          SCORE_MODEL,
    }
