"""
score.py — Deterministic 0–100 score for a candidate company.

WHY DETERMINISTIC (NOT CLAUDE):
  Layer 02 already does the smart 0–100 scoring against criteria.yaml — but
  it scores INDIVIDUAL JOBS, not companies. Layer 01's job is the cheaper
  upstream pass: "is this company even worth adding to the watchlist?" That
  question only needs three things: are they Bay Area, are they big enough,
  and are they actively hiring PMs. None of those need an LLM.

  When/if you want a smarter Layer 01 later, swap this module for one that
  calls Haiku — the function signature stays the same.

THE FORMULA:

  +40   HQ confidence       — pool entry has hq_bay_area: true
  +30   Headcount confidence — pool entry has employees_est >= min_employees
  +20   PM activity          — probe found ≥ 1 PM role in Bay Area / remote / hybrid
  +10   Hiring momentum      — probe found ≥ 3 PM roles in Bay Area / remote / hybrid
  ────
   100  Maximum

  By construction: any company that reaches probe.py has already cleared the
  HQ + headcount checks (pool_loader.py drops the rest), so they always start
  at 70 just for showing up. The remaining 30 points are earned by ACTUAL
  Bay Area PM hiring activity. A company with 4 PM roles all in Dublin scores
  the same 70 as a company with 0 PM roles — both land in the review queue
  rather than auto-add, because Layer 03 won't actually scrape useful jobs
  from them anyway.

  `ats: custom` entries skip the network probe entirely and always score 70
  (HQ + headcount only). They're surfaced for manual triage in the review queue.

ROUTING (driven by criteria.yaml → discovery: thresholds):
  score >= auto_add_min_score    → AUTO_ADD: append to watchlist.yaml
  score >= queue_min_score       → REVIEW:   write to discovery JSON for review
  otherwise                      → DROP:     not surfaced
"""

from __future__ import annotations

# Static contributions — change these and the formula updates everywhere.
PTS_HQ              = 40
PTS_HEADCOUNT       = 30
PTS_PM_ACTIVITY     = 20
PTS_HIRING_MOMENTUM = 10


def score_company(*, company: dict, probe_result: dict, min_employees: int) -> dict:
    """
    Compute the 0–100 score and a per-criterion breakdown.

    Returns:
      {
        "total": int,
        "breakdown": { "hq": int, "headcount": int, "pm_activity": int,
                       "hiring_momentum": int },
        "rationale": str,         # one-line, used in the JSON + watchlist note
      }
    """
    # Activity + momentum bonuses BOTH key off pm_bay_area (the count of PM
    # roles whose location passed the loose Bay Area / remote / hybrid filter
    # in filters.location_is_acceptable). PM roles in Dublin or NYC don't
    # earn either bonus, since Layer 03 wouldn't surface them in the digest
    # anyway.
    pm_ba = probe_result.get("pm_bay_area", 0)
    breakdown = {
        "hq":              PTS_HQ if company.get("hq_bay_area") is True else 0,
        "headcount":       PTS_HEADCOUNT if _hc(company) >= min_employees else 0,
        "pm_activity":     PTS_PM_ACTIVITY if pm_ba >= 1 else 0,
        "hiring_momentum": PTS_HIRING_MOMENTUM if pm_ba >= 3 else 0,
    }
    total = sum(breakdown.values())

    # Build a one-line rationale using the same words the breakdown uses, so
    # someone reading the JSON (or the watchlist comment) can connect the two.
    parts = []
    parts.append("Bay Area HQ" if breakdown["hq"] else "non-BA HQ")
    parts.append(f"~{_hc(company)} employees" if breakdown["headcount"] else "headcount below floor")
    pm_total = probe_result.get("pm_total", 0)
    pm_ba    = probe_result.get("pm_bay_area", 0)
    if pm_total == 0:
        parts.append("no PM roles open")
    else:
        parts.append(f"{pm_ba} BA / {pm_total} total PM roles")
    rationale = " · ".join(parts)

    return {"total": total, "breakdown": breakdown, "rationale": rationale}


def route(*, score_total: int, settings: dict) -> str:
    """
    Decide what to do with a scored company.

    Returns one of: "auto_add", "review", "drop".
    """
    if score_total >= settings["auto_add_min_score"]:
        return "auto_add"
    if score_total >= settings["queue_min_score"]:
        return "review"
    return "drop"


def _hc(company: dict) -> int:
    try:
        return int(company.get("employees_est") or 0)
    except (TypeError, ValueError):
        return 0
