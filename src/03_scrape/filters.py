"""
filters.py — Shared rules for "is this a PM job?" and "is this in the right
geography?" Used by every scraper so we never duplicate this logic.

DESIGN DECISION (2026-04-27):
  We intentionally use a LOOSE filter here at scrape time. The job of Layer 03
  is to gather candidates; Layer 02 (Score) does the real evaluation against
  criteria.yaml. So this module errs on the side of keeping a job rather than
  dropping it. Better to over-collect and let scoring discard, than to drop a
  great job because its location string was unusual.

WHAT COUNTS AS A "PM ROLE":
  Title must contain one of the PM_KEYWORDS and none of the title excludes.
  The excludes default to DEFAULT_PM_EXCLUDES below and can be replaced with
  scrape_filters.title_exclude_keywords in config/criteria.yaml.

WHAT COUNTS AS THE RIGHT GEOGRAPHY:
  Pass any of these checks:
    - Mentions one of your location keywords (scrape_filters.location_keywords
      in config/criteria.yaml; defaults to DEFAULT_LOCATION_TOKENS below,
      an example set for the San Francisco Bay Area)
    - Mentions remote / hybrid / distributed
    - Location is empty/None/"Multiple locations" (we keep these and let
      Layer 02 decide; many real PM postings have weird location strings)
"""

from __future__ import annotations

from pathlib import Path

_CRITERIA_PATH = Path(__file__).resolve().parents[2] / "config" / "criteria.yaml"


def _scrape_filter_config() -> dict:
    """Optional overrides from config/criteria.yaml → scrape_filters.
    Missing file, missing block, or missing pyyaml all fall back to defaults."""
    try:
        import yaml
        with open(_CRITERIA_PATH) as f:
            return (yaml.safe_load(f) or {}).get("scrape_filters") or {}
    except Exception:
        return {}


def _lower_list(values, default):
    if values is None:          # key missing → defaults; [] → no keywords
        return default
    return [str(v).lower() for v in values]

# ---- PM title filter ---------------------------------------------------------

PM_KEYWORDS = [
    "product manager",
    "product lead",
    "product management",
    " pm ",            # whitespace-padded so "spam" doesn't match
    " pm,",
    "pm)",
]

DEFAULT_PM_EXCLUDES = [
    "associate",
    "apm",
    "intern",
    "internship",
    "technical product",
    "technical pm",
    "tpm",
    "director",
    "vp ",
    "vice president",
    "head of",
    "chief product",
    "cpo",
    "engineering manager",
    "program manager",      # different role, often confused with PM
    "project manager",      # different role, often confused with PM
    "product marketing",    # PMM is a different role
    "product designer",
]


_CFG = _scrape_filter_config()
PM_EXCLUDES = _lower_list(_CFG.get("title_exclude_keywords"), DEFAULT_PM_EXCLUDES)

def looks_like_pm_role(title: str | None) -> bool:
    """
    Cheap keyword-based check for "is this a Product Manager role?"

    Returns True if the title contains a PM keyword AND none of the excludes.
    Layer 02 will do the real semantic scoring against criteria.yaml.
    """
    if not title:
        return False
    # Pad with spaces so leading/trailing keywords (" pm ") match correctly
    t = f" {title.lower()} "
    if not any(keyword in t for keyword in PM_KEYWORDS):
        return False
    if any(bad in t for bad in PM_EXCLUDES):
        return False
    return True


# ---- Location filter (loose mode) --------------------------------------------

DEFAULT_LOCATION_TOKENS = [   # example: San Francisco Bay Area
    "san francisco", "sf bay", "sf,", " sf ",
    "bay area",
    "oakland", "berkeley", "alameda",
    "san mateo", "burlingame", "redwood city", "redwood shores",
    "palo alto", "menlo park", "mountain view", "stanford",
    "sunnyvale", "santa clara", "san jose", "cupertino", "los gatos",
    "fremont", "milpitas",
    "california", " ca,", " ca ", " ca/", "ca, usa", "ca, united states",
]

LOCATION_TOKENS = _lower_list(_CFG.get("location_keywords"), DEFAULT_LOCATION_TOKENS)

REMOTE_TOKENS = [
    "remote", "anywhere", "distributed", "work from home", "wfh",
    "hybrid",   # we keep hybrid and let scoring sort out the geography
]


def location_is_acceptable(location: str | None) -> bool:
    """
    LOOSE check: does this location string suggest the job is reachable from
    the base location? Returns True if:
      - empty / None / unknown / "multiple locations"   → keep, decide later
      - mentions one of the configured location keywords
      - mentions remote, hybrid, or distributed
    """
    if not location:
        return True  # unknown — keep, let Layer 02 figure it out

    loc = f" {location.lower()} "

    # Wishy-washy/ambiguous → keep
    ambiguous_markers = ["multiple", "various", "global", "worldwide", "n/a"]
    if any(marker in loc for marker in ambiguous_markers):
        return True

    # Direct hit on a configured location keyword
    if any(tok in loc for tok in LOCATION_TOKENS):
        return True

    # Remote-ish?
    if any(tok in loc for tok in REMOTE_TOKENS):
        return True

    return False


def keep_job(*, title: str | None, location: str | None) -> bool:
    """The single function every scraper calls per candidate job."""
    return looks_like_pm_role(title) and location_is_acceptable(location)
