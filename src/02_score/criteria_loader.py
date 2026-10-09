"""
criteria_loader.py — Loads config/criteria.yaml into a Python dict the
scoring prompts can interpolate.

WHAT THIS DOES (in plain English):
  Reads your editable criteria.yaml file and hands back a clean dictionary.
  We isolate this so that:
    1. Every scorer (triage + full score) reads the SAME criteria
    2. If you ever change the file format, only this file needs updating
    3. Tests can pass in fake criteria without touching the real file

We also expose a `format_criteria_for_prompt()` helper that takes the dict and
turns it into a clean Markdown block we can paste into the system prompt.
That's what Claude actually sees when it scores a job.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Same trick as run_all.py: project root is two folders up from src/02_score/
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CRITERIA_PATH = PROJECT_ROOT / "config" / "criteria.yaml"


def load_criteria(path: Path | None = None) -> dict:
    """
    Parse criteria.yaml and return it as a nested dict.

    Tries PyYAML first (the right tool); raises a clear error if missing,
    because for Layer 02 we genuinely need YAML — there's too much nested
    structure for the minimal hand-rolled fallback we use in Layer 03.
    """
    target = path or CRITERIA_PATH
    if not target.exists():
        raise FileNotFoundError(
            f"criteria.yaml not found at {target}. "
            "Layer 02 needs this file to know what to score against."
        )
    text = target.read_text(encoding="utf-8")

    try:
        import yaml  # type: ignore
    except ImportError:
        sys.stderr.write(
            "PyYAML is required for Layer 02 scoring. Install it with:\n"
            "    pip3 install --user pyyaml\n"
        )
        raise

    return yaml.safe_load(text) or {}


def format_criteria_for_prompt(criteria: dict) -> str:
    """
    Turn the criteria dict into a clean Markdown block for the LLM.

    We hand-format this rather than dumping the YAML so the model sees a
    coherent prose-y description it can reason against, not raw config.
    """
    profile = criteria.get("candidate_profile", {})
    must = criteria.get("must_have", {})
    weights = criteria.get("scoring_weights", {})
    ai_moat = criteria.get("ai_moat", {})
    wlb = criteria.get("work_life_balance", {})
    health = criteria.get("company_health", {})
    work_model = criteria.get("work_model_preferences", {})

    lines: list[str] = []

    lines.append(f"# Candidate profile")
    lines.append(f"- Based in: {profile.get('base_location', '?')}")
    lines.append(f"- Target role: {profile.get('current_role_target', '?')}")
    lines.append(f"- Years experience: {profile.get('years_experience', '?')}")
    if profile.get("core_lane_summary"):
        lines.append(f"- Core lane: {' '.join(str(profile['core_lane_summary']).split())}")

    lines.append("\n# Must-have (hard filters — failing any drops the job)")
    role = must.get("role_levels", {})
    lines.append(f"- Role levels INCLUDE: {', '.join(role.get('include', []))}")
    lines.append(f"- Role levels EXCLUDE: {', '.join(role.get('exclude', []))}")

    comp = must.get("comp", {})
    lines.append(f"- Minimum base salary: ${comp.get('base_salary_min_usd', 0):,} USD")

    loc = must.get("location", {})
    lines.append(
        f"- Location: within {loc.get('max_miles', '?')} miles of "
        f"{loc.get('radius_miles_from', '?')} OR remote/hybrid"
    )
    lines.append(f"- Work models allowed: {', '.join(loc.get('work_models_allowed', []))}")

    exp = must.get("experience") or {}
    if exp.get("max_required_years"):
        lines.append(f"- Experience ceiling: fail postings whose stated MINIMUM "
                     f"years of experience is {exp['max_required_years']} or more")

    sz = must.get("company_size", {})
    lines.append(f"- Company size: {sz.get('min_employees', '?')}+ employees")

    # JD-content exclusions — newer addition; filters that read the JD body
    jd_excludes = must.get("jd_excludes") or {}
    if jd_excludes:
        lines.append("")
        lines.append("- JD-content exclusions (read the requirements/qualifications "
                     "section; only fail when listed as REQUIRED, not 'preferred'):")
        for key, block in jd_excludes.items():
            block = block or {}
            label = key.replace("_", " ").title()
            desc = (block.get("description") or "").strip().replace("\n", " ")
            lines.append(f"  • **{label}**: {desc}")
            for ex in block.get("examples_of_failures", []):
                lines.append(f"      ✗ Fail example: {ex}")
            for ex in block.get("examples_that_still_pass", []):
                lines.append(f"      ✓ Still passes:  {ex}")

    lines.append("\n# Scoring weights (sum to 100)")
    for k, v in weights.items():
        lines.append(f"- {k}: {v}")

    # Lane-first strategy (added 2026-09-23). This drives role_fit.
    lane = criteria.get("core_lane") or {}
    if lane:
        def _flat(s) -> str:
            return " ".join(str(s).split())

        lines.append("\n# Role fit: lane first (the most important dimension)")
        lines.append("Core question: is the problem at the CENTER of this role one "
                     "the candidate has owned, with AI as a lever?")
        if lane.get("in_lane_problems"):
            lines.append("In-lane problems (score role_fit up):")
            for s in lane["in_lane_problems"]:
                lines.append(f"  - {s}")
        if lane.get("out_of_lane_domains"):
            lines.append("Out-of-lane domains (when one of these is the CENTER of "
                         "the role, cap role_fit around 45, however strong the "
                         "company or AI angle):")
            for s in lane["out_of_lane_domains"]:
                lines.append(f"  - {s}")
        if lane.get("bridge_lane_note"):
            lines.append(f"Self-serve B2B bridge: {_flat(lane['bridge_lane_note'])}")
        if lane.get("ai_as_lever"):
            lines.append(f"AI as a lever: {_flat(lane['ai_as_lever'])}")
        if lane.get("ai_as_whole_role"):
            lines.append(f"AI as the whole role: {_flat(lane['ai_as_whole_role'])}")

    level = criteria.get("role_fit_level_guidance") or {}
    if level:
        lines.append("\n# Role fit: level")
        for k, v in level.items():
            lines.append(f"- {k.replace('_', ' ').title()}: {' '.join(str(v).split())}")

    years = (criteria.get("role_fit_guidance") or {}).get("jd_years_experience") or {}
    if years:
        lines.append("\n# Role fit: JD years-of-experience requirement")
        lines.append(f"- Ideal range: {years.get('ideal_range')} (full credit)")
        lines.append(f"- Acceptable range: {years.get('acceptable_range')} (partial credit)")
        lines.append(f"- Heavy penalty below {years.get('penalize_below')} "
                     f"or above {years.get('penalize_above')} years")
        lines.append("- If the JD states no number, treat as neutral.")

    lines.append("\n# AI moat — what counts as defensible")
    if ai_moat.get("definition"):
        lines.append(f"Definition: {ai_moat['definition'].strip()}")
    pos = ai_moat.get("signals_positive", [])
    neg = ai_moat.get("signals_negative", [])
    if pos:
        lines.append("Positive signals:")
        for s in pos:
            lines.append(f"  - {s}")
    if neg:
        lines.append("Negative signals:")
        for s in neg:
            lines.append(f"  - {s}")

    lines.append("\n# Work-life balance signals")
    if wlb.get("glassdoor_min_rating"):
        lines.append(f"- Employee-review rating below {wlb['glassdoor_min_rating']} loses points "
                     "(only if you have a sourced rating; otherwise treat as unknown)")
    if wlb.get("hard_no_signals"):
        lines.append("- Hard-no signals (penalize heavily):")
        for s in wlb["hard_no_signals"]:
            lines.append(f"  - {s}")
    if wlb.get("positive_signals"):
        lines.append("- Positive signals (boost):")
        for s in wlb["positive_signals"]:
            lines.append(f"  - {s}")

    lines.append("\n# Company health bonus")
    lines.append(f"- Public 5yr CAGR threshold: {health.get('public_stock_5yr_cagr_min_pct', '?')}%")
    if health.get("acceptable_for_private"):
        lines.append("- Acceptable for private companies:")
        for s in health["acceptable_for_private"]:
            lines.append(f"  - {s}")

    lines.append("\n# Work model preferences (within allowed)")
    for k, v in work_model.items():
        lines.append(f"- {k}: {v}")

    return "\n".join(lines)
