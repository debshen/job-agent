"""
pool_loader.py — Reads the three config files Layer 01 needs and merges them
into a clean list of "companies to evaluate tonight."

WHAT THIS DOES (in plain English):
  Layer 01 looks at three places before it does any work:
    1. config/discovery_pool.yaml   — the universe of candidates
    2. config/watchlist.yaml        — companies we ALREADY track (skip these)
    3. config/criteria.yaml         — the discovery: section (thresholds, etc.)

  This module loads all three, drops anything already on the watchlist or in
  the exclude-sectors list, and hands the orchestrator a tidy list of
  candidate dicts.

WHY IT'S A SEPARATE MODULE:
  - The orchestrator (run_discover.py) stays focused on flow.
  - The output module (output.py) re-uses _load_watchlist_text() when it
    needs to append new entries to watchlist.yaml without losing comments.
  - The smoke test can call load_candidates() directly without subprocesses.

JARGON:
  - "candidate"  = a company that hasn't been ruled out by hard filters yet.
                   Still needs to clear the probe + score steps.
  - "pool"       = the curated universe (discovery_pool.yaml).
  - "watchlist"  = the companies whose jobs we already scrape every night.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
POOL_PATH      = PROJECT_ROOT / "config" / "discovery_pool.yaml"
WATCHLIST_PATH = PROJECT_ROOT / "config" / "watchlist.yaml"
CRITERIA_PATH  = PROJECT_ROOT / "config" / "criteria.yaml"


# -----------------------------------------------------------------------------
# YAML loading — PyYAML preferred, tiny fallback parser otherwise.
# We support TWO top-level shapes:
#   (a) Dict with a single list key: e.g. companies: [ ... ]  or  include: [ ... ]
#   (b) Full YAML doc                                  → use PyYAML
# -----------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict:
    """
    Load a YAML file as a dict. Returns {} if the file is empty / missing.

    Tries PyYAML first (the right tool). Falls back to a minimal parser that
    handles our exact file shapes if PyYAML isn't installed yet — so day-1
    works without `pip install`.
    """
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text) or {}
    except ImportError:
        return _minimal_yaml_parse(text)


def _minimal_yaml_parse(text: str) -> dict:
    """
    Hand-rolled parser for the project's exact YAML files. Handles:
      - top-level keys followed by either a list of dicts or a list of scalars
      - `- key: value` blocks with nested `  key: value` lines
      - `- value` scalar list items
      - inline `# comment` stripping
      - null / true / false / int literals

    Not a general-purpose YAML parser — only good enough for our own configs.
    """
    result: dict = {}
    current_key: str | None = None
    current_list: list | None = None
    current_item: dict | None = None

    for raw_line in text.splitlines():
        # Strip inline comments (no '#' inside any quoted strings in our files).
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue

        # Top-level key (no leading whitespace, ends with ":")
        if not line.startswith(" ") and line.rstrip().endswith(":"):
            # Save the previous item if any
            if current_item is not None and current_list is not None:
                current_list.append(current_item)
                current_item = None
            current_key = line.rstrip()[:-1].strip()
            current_list = []
            result[current_key] = current_list
            continue

        if current_list is None:
            continue   # outside any known section

        stripped = line.lstrip()

        # Scalar list item: "  - some_value"
        if stripped.startswith("- ") and ":" not in stripped[2:].split("#", 1)[0]:
            # save any previous dict item
            if current_item is not None:
                current_list.append(current_item)
                current_item = None
            current_list.append(_coerce(stripped[2:].strip()))
            continue

        # Dict-style list item start: "  - key: value"
        if stripped.startswith("- "):
            if current_item is not None:
                current_list.append(current_item)
            current_item = {}
            stripped = stripped[2:]

        if ":" in stripped and current_item is not None:
            key, _, value = stripped.partition(":")
            current_item[key.strip()] = _coerce(value.strip())

    if current_item is not None and current_list is not None:
        current_list.append(current_item)

    return result


def _coerce(value: str):
    """Turn a raw YAML scalar string into Python (None / bool / int / str)."""
    if value == "" or value.lower() in ("null", "~"):
        return None
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    try:
        return int(value)
    except ValueError:
        return value


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------

def load_discovery_settings() -> dict:
    """
    Pull the `discovery:` block out of criteria.yaml.

    Returns a dict with safe defaults so the orchestrator never crashes if
    someone deletes the section or runs against an old criteria.yaml.
    """
    criteria = _load_yaml(CRITERIA_PATH)
    discovery = (criteria.get("discovery") or {}) if isinstance(criteria, dict) else {}

    must_have = (criteria.get("must_have") or {}) if isinstance(criteria, dict) else {}
    company_size = (must_have.get("company_size") or {}) if isinstance(must_have, dict) else {}

    return {
        "auto_add_min_score":     int(discovery.get("auto_add_min_score", 80)),
        "queue_min_score":        int(discovery.get("queue_min_score", 50)),
        "max_auto_adds_per_run":  int(discovery.get("max_auto_adds_per_run", 5)),
        "sectors_excluded":       list(discovery.get("sectors_excluded") or []),
        "sectors_preferred":      list(discovery.get("sectors_preferred") or []),
        "min_employees":          int(company_size.get("min_employees", 500)),
    }


def load_watchlist_names() -> set[str]:
    """
    Lowercase set of every company name already on watchlist.yaml (include + exclude).
    Used to skip duplicates during discovery.
    """
    data = _load_yaml(WATCHLIST_PATH)
    if not isinstance(data, dict):
        return set()

    names: set[str] = set()
    for entry in (data.get("include") or []):
        if isinstance(entry, dict) and entry.get("name"):
            names.add(str(entry["name"]).strip().lower())

    # excludes are scalar strings, not dicts
    for entry in (data.get("exclude") or []):
        if isinstance(entry, str):
            names.add(entry.strip().lower())

    return names


def load_candidates() -> tuple[list[dict], dict]:
    """
    The one function run_discover.py imports.

    Returns (candidates, settings) where:
      candidates = list of pool entries that survived the cheap pre-filters:
                     - enabled is not False
                     - sector is NOT in sectors_excluded
                     - name is NOT already on watchlist.yaml
                     - employees_est >= settings.min_employees
                     - hq_bay_area is True
                     - ats is greenhouse or lever (MVP — extend later)
                     - ats_slug is non-empty
      settings   = the dict from load_discovery_settings(), threaded through
                   so callers don't have to load criteria twice.

    Cheap means: no network. Network probing happens in probe.py.
    """
    settings = load_discovery_settings()
    pool_data = _load_yaml(POOL_PATH)
    pool: list[dict] = pool_data.get("companies", []) if isinstance(pool_data, dict) else []
    already_tracked = load_watchlist_names()

    excluded_sectors = {s.lower() for s in settings["sectors_excluded"]}
    min_employees = settings["min_employees"]

    candidates: list[dict] = []
    for entry in pool:
        if not isinstance(entry, dict):
            continue
        if entry.get("enabled") is False:
            continue

        name = (entry.get("name") or "").strip()
        if not name:
            continue
        if name.lower() in already_tracked:
            continue

        sector = (entry.get("sector") or "").strip().lower()
        if sector in excluded_sectors:
            continue

        ats = (entry.get("ats") or "").strip().lower()
        if ats not in ("greenhouse", "lever", "custom"):
            continue

        # Slug is only required for ATSes we actually probe over the network.
        # `custom` entries deliberately have no slug because we don't auto-probe
        # them — they ride through scoring on metadata only and land in review.
        if ats in ("greenhouse", "lever") and not entry.get("ats_slug"):
            continue

        if entry.get("hq_bay_area") is not True:
            continue

        try:
            headcount = int(entry.get("employees_est") or 0)
        except (TypeError, ValueError):
            headcount = 0
        if headcount < min_employees:
            continue

        candidates.append(entry)

    return candidates, settings


def diagnostics() -> dict:
    """
    Useful for the orchestrator's summary box: how many entries got filtered
    out at the cheap pre-filter stage, and why.
    """
    pool_data = _load_yaml(POOL_PATH)
    pool: list[dict] = pool_data.get("companies", []) if isinstance(pool_data, dict) else []

    settings = load_discovery_settings()
    already_tracked = load_watchlist_names()
    excluded_sectors = {s.lower() for s in settings["sectors_excluded"]}
    min_employees = settings["min_employees"]

    counts = {
        "pool_total": len(pool),
        "disabled": 0,
        "already_on_watchlist": 0,
        "sector_excluded": 0,
        "wrong_ats": 0,
        "no_slug": 0,
        "not_bay_area": 0,
        "too_small": 0,
        "candidates": 0,
    }

    for entry in pool:
        if not isinstance(entry, dict):
            continue
        if entry.get("enabled") is False:
            counts["disabled"] += 1; continue
        name = (entry.get("name") or "").strip()
        if name.lower() in already_tracked:
            counts["already_on_watchlist"] += 1; continue
        if (entry.get("sector") or "").strip().lower() in excluded_sectors:
            counts["sector_excluded"] += 1; continue
        ats = (entry.get("ats") or "").strip().lower()
        if ats not in ("greenhouse", "lever", "custom"):
            counts["wrong_ats"] += 1; continue
        if ats in ("greenhouse", "lever") and not entry.get("ats_slug"):
            counts["no_slug"] += 1; continue
        if entry.get("hq_bay_area") is not True:
            counts["not_bay_area"] += 1; continue
        try:
            hc = int(entry.get("employees_est") or 0)
        except (TypeError, ValueError):
            hc = 0
        if hc < min_employees:
            counts["too_small"] += 1; continue
        counts["candidates"] += 1

    return counts
