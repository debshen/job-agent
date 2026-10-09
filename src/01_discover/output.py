"""
output.py — Two responsibilities:
  1. Write the daily discovery JSON to data/discovery/YYYY-MM-DD.json
  2. Append AUTO_ADD winners to config/watchlist.yaml without trashing the
     existing comments and formatting.

WHY THE WATCHLIST APPEND IS HAND-WRITTEN (and not done with PyYAML):
  PyYAML round-trips lose all comments — and watchlist.yaml is a heavily
  commented config the candidate maintains by hand. So we treat watchlist.yaml as
  TEXT: find a stable insertion point (just before the `exclude:` block)
  and splice new entry blocks in. Existing entries and comments are never
  touched.

SAFETY RAILS:
  - We refuse to add a company whose name (case-insensitive) already appears
    on the watchlist. The orchestrator also pre-filters these in pool_loader,
    but a second check here is cheap insurance against double-runs.
  - We never write to watchlist.yaml in --dry-run mode.
  - max_auto_adds_per_run (from criteria.yaml) caps how many entries we
    splice in per night, so a buggy run can't carpet-bomb the watchlist.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DISCOVERY_DIR  = PROJECT_ROOT / "data" / "discovery"
WATCHLIST_PATH = PROJECT_ROOT / "config" / "watchlist.yaml"


# -----------------------------------------------------------------------------
# Daily JSON output
# -----------------------------------------------------------------------------

def write_daily_json(*, results: list[dict], settings: dict, date_str: str | None = None) -> Path:
    """
    Persist the full discovery run to data/discovery/YYYY-MM-DD.json.

    `results` is the list of per-company dicts the orchestrator built (see
    run_discover.py for the exact shape). We sort by score desc here so the
    JSON is easy to skim.
    """
    DISCOVERY_DIR.mkdir(parents=True, exist_ok=True)
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = DISCOVERY_DIR / f"{date_str}.json"

    sorted_results = sorted(results, key=lambda r: -int(r.get("score", 0)))
    summary = {
        "date":                 date_str,
        "candidates_evaluated": len(sorted_results),
        "auto_added":           sum(1 for r in sorted_results if r.get("action") == "auto_add"),
        "queued_for_review":    sum(1 for r in sorted_results if r.get("action") == "review"),
        "dropped":              sum(1 for r in sorted_results if r.get("action") == "drop"),
        "probe_failed":         sum(1 for r in sorted_results if r.get("action") == "probe_failed"),
        "thresholds": {
            "auto_add_min_score": settings["auto_add_min_score"],
            "queue_min_score":    settings["queue_min_score"],
            "max_auto_adds_per_run": settings["max_auto_adds_per_run"],
        },
    }

    out_path.write_text(
        json.dumps({"summary": summary, "results": sorted_results}, indent=2),
        encoding="utf-8",
    )
    return out_path


# -----------------------------------------------------------------------------
# watchlist.yaml append — text-level so comments are preserved.
# -----------------------------------------------------------------------------

def append_to_watchlist(*, winners: list[dict], date_str: str | None = None) -> int:
    """
    Splice a YAML block of `winners` into config/watchlist.yaml just before
    the `exclude:` section (or at end-of-file if there's no exclude block).

    Each winner dict needs:  name, ats, ats_slug, rationale (optional)

    Returns the number of entries actually appended (zero if nothing new).
    """
    if not winners:
        return 0
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    text = WATCHLIST_PATH.read_text(encoding="utf-8")

    # Last-line safety check: skip any winner whose name already appears
    # anywhere in the file. This protects against race conditions and
    # mistakes in the candidate filter upstream.
    text_lower = text.lower()
    fresh: list[dict] = []
    for w in winners:
        nm = (w.get("name") or "").strip()
        if not nm:
            continue
        if f"name: {nm.lower()}" in text_lower:
            continue
        fresh.append(w)

    if not fresh:
        return 0

    block = _build_watchlist_block(fresh, date_str)

    # Find the exclude: line so we splice before it. Match the start of a line
    # so we don't get fooled by the word "exclude" inside a comment.
    lines = text.splitlines(keepends=True)
    insert_at = None
    for i, ln in enumerate(lines):
        if ln.startswith("exclude:") or ln.startswith("# Companies to NEVER score"):
            insert_at = i
            break

    if insert_at is None:
        # No exclude block — append at end with a leading blank line.
        new_text = text.rstrip() + "\n\n" + block + "\n"
    else:
        new_text = "".join(lines[:insert_at]) + block + "\n" + "".join(lines[insert_at:])

    WATCHLIST_PATH.write_text(new_text, encoding="utf-8")
    return len(fresh)


def _build_watchlist_block(winners: list[dict], date_str: str) -> str:
    """
    Render a list of winner dicts as a YAML block matching the existing
    watchlist.yaml style (2-space indent, blank line between entries, banner
    comment so the user can spot auto-discoveries at a glance).
    """
    out_lines: list[str] = []
    out_lines.append(f"  # ---------- AUTO-DISCOVERED ({date_str} by Layer 01) ----------")
    for w in winners:
        name = (w.get("name") or "?").strip()
        ats = (w.get("ats") or "?").strip()
        slug = (w.get("ats_slug") or "?").strip()
        # The watchlist's downstream consumers (Layer 03) use these fields. We
        # mark public/ticker as null because we don't have that data in the
        # pool yet; the candidate can fill them in by hand later if they want.
        rationale = (w.get("rationale") or "").strip()
        score = w.get("score")
        notes = f"Auto-discovered {date_str}"
        if score is not None:
            notes += f" — score {score}/100"
        if rationale:
            notes += f" — {rationale}"
        out_lines.append("")
        out_lines.append(f"  - name: {name}")
        out_lines.append(f"    ats: {ats}")
        out_lines.append(f"    ats_slug: {slug}")
        out_lines.append("    public: false")
        out_lines.append("    ticker: null")
        out_lines.append(f"    notes: {notes}")
    out_lines.append("")  # trailing blank line
    return "\n".join(out_lines)
