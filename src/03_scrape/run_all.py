"""
run_all.py — Orchestrator for Layer 03. The script you actually run nightly.

WHAT THIS DOES (in plain English):
  1. Reads config/watchlist.yaml (the list of companies to track).
  2. Groups them by ATS — Greenhouse, Lever, custom/Workday.
  3. Hands each group to its scraper module.
  4. The scrapers write rows into data/jobs.sqlite (deduped by URL).
  5. Prints a tidy summary so you can see what happened tonight.

HOW TO RUN IT:
  Open Terminal, then:

    cd "/path/to/job-agent"
    python3 src/03_scrape/run_all.py

  (Python automatically puts the script's folder onto its import path, so the
   plain `import db` / `import filters` inside the scrapers find their
   neighbours.)

FIRST-TIME SETUP:
  This script needs PyYAML to read the watchlist. One-time install:

    pip3 install --user pyyaml

  If pip3 isn't installed, run:
    python3 -m ensurepip --user
    python3 -m pip install --user pyyaml

  After that you never have to think about it again.

WHAT YOU'LL SEE:
  A per-company line like "→ Stripe (greenhouse: stripe)  102 fetched · 4 kept · 4 new"
  Then a summary box at the end with totals.

EXIT CODES (for cron / GitHub Actions later):
  0 = ran successfully and at least one job was scraped
  1 = ran but found zero PM jobs anywhere (probably a network or filter issue)
  2 = couldn't even start (missing watchlist, missing PyYAML, etc.)
"""

from __future__ import annotations

import sys
from pathlib import Path

# This package uses relative imports, so it must be run via `python3 -m src.03_scrape.run_all`
# from the project root. We do a friendly check up-front.
# Make sure THIS folder is on the import path before importing siblings.
# (Belt-and-suspenders: Python adds the script's folder automatically when run
#  as `python3 path/to/run_all.py`, but this also handles being imported.)
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from db import count_jobs, count_jobs_by_company, get_connection, DB_PATH
    from greenhouse import scrape_greenhouse_companies
    from lever import scrape_lever_companies
    from custom_html import scrape_custom_companies
    from workday import scrape_workday_companies
    from icims import scrape_icims_companies
    from atlassian import scrape_atlassian_companies
except ImportError as e:
    sys.stderr.write(
        "\nCould not import the scraper modules. Run from the project root:\n"
        "    cd \"/path/to/job-agent\"\n"
        "    python3 src/03_scrape/run_all.py\n\n"
        f"Original error: {e}\n"
    )
    sys.exit(2)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WATCHLIST_PATH = PROJECT_ROOT / "config" / "watchlist.yaml"


def _load_watchlist() -> list[dict]:
    """
    Parse config/watchlist.yaml into a list of company dicts.

    We try PyYAML first (the right tool). If it isn't installed, we fall back
    to a TINY hand-rolled parser that only understands our exact watchlist
    format (a top-level "include:" list of "- key: value" blocks). The fallback
    means the script still runs day-1, before you've installed anything.
    """
    if not WATCHLIST_PATH.exists():
        sys.stderr.write(f"watchlist.yaml not found at {WATCHLIST_PATH}\n")
        sys.exit(2)

    text = WATCHLIST_PATH.read_text(encoding="utf-8")

    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text) or {}
        return data.get("include", []) or []
    except ImportError:
        print("  (PyYAML not installed — using minimal built-in parser)")
        print("  (Recommended: pip3 install --user pyyaml)\n")
        return _minimal_yaml_parse(text)


def _minimal_yaml_parse(text: str) -> list[dict]:
    """
    Stripped-down parser for our watchlist's exact shape — just enough to
    avoid forcing PyYAML on day one. Handles:
      - top-level `include:` block
      - list items beginning with `- key: value`
      - subsequent lines `  key: value`
      - inline comments after `#`
      - null / true / false literals; everything else is a string
    """
    companies: list[dict] = []
    current: dict | None = None
    in_include = False

    for raw_line in text.splitlines():
        # Strip inline comments (but not '#' inside quoted strings — we have none)
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue

        if line.startswith("include:"):
            in_include = True
            continue
        if line.startswith("exclude:"):
            in_include = False
            continue
        if not in_include:
            continue

        stripped = line.lstrip()

        if stripped.startswith("- "):
            # New company block
            if current is not None:
                companies.append(current)
            current = {}
            stripped = stripped[2:]  # drop "- "

        if ":" in stripped and current is not None:
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip()
            if value.lower() in ("null", "~", ""):
                parsed = None
            elif value.lower() == "true":
                parsed = True
            elif value.lower() == "false":
                parsed = False
            else:
                parsed = value
            current[key] = parsed

    if current is not None:
        companies.append(current)
    return companies


def _bucket(companies: list[dict]) -> dict[str, list[dict]]:
    """Group companies by their `ats:` field."""
    buckets: dict[str, list[dict]] = {}
    for c in companies:
        ats = (c.get("ats") or "unknown").lower()
        buckets.setdefault(ats, []).append(c)
    return buckets


def _print_summary(stats_by_ats: dict, conn) -> None:
    print("\n" + "=" * 60)
    print(" Layer 03 — Scrape — summary")
    print("=" * 60)

    total_kept = total_new = 0
    for ats, s in stats_by_ats.items():
        print(
            f"  {ats:<12}  checked: {s.get('checked', 0):>2}  "
            f"fetched: {s.get('fetched', 0):>4}  "
            f"kept: {s.get('kept', 0):>3}  "
            f"new: {s.get('new', 0):>3}  "
            f"skipped: {s.get('skipped', 0):>2}"
        )
        total_kept += s.get("kept", 0)
        total_new += s.get("new", 0)

    print("-" * 60)
    print(f"  Total kept this run:  {total_kept}")
    print(f"  Brand-new tonight:    {total_new}")
    print(f"  Database total:       {count_jobs(conn)}  ({DB_PATH})")
    print()

    by_company = count_jobs_by_company(conn)
    if by_company:
        print(" Top companies in the database right now:")
        for company, n in by_company[:10]:
            print(f"    {n:>3}  {company}")
    print()


def main() -> int:
    print(f"Loading watchlist from {WATCHLIST_PATH.name}...")
    companies = _load_watchlist()
    print(f"  Found {len(companies)} companies\n")

    buckets = _bucket(companies)
    conn = get_connection()
    stats_by_ats: dict[str, dict] = {}

    if buckets.get("greenhouse"):
        print("--- Greenhouse ---")
        stats_by_ats["greenhouse"] = scrape_greenhouse_companies(buckets["greenhouse"], conn)
        print()

    if buckets.get("lever"):
        print("--- Lever ---")
        stats_by_ats["lever"] = scrape_lever_companies(buckets["lever"], conn)
        print()

    # Workday entries with workday_host set use the real Workday adapter
    # (workday.py, added 2026-09-24). Workday entries without it still fall
    # through to the best-effort custom path, which logs and skips them.
    workday_all = buckets.get("workday") or []
    workday_ready = [c for c in workday_all if c.get("workday_host")]
    workday_legacy = [c for c in workday_all if not c.get("workday_host")]
    if workday_ready:
        print("--- Workday ---")
        stats_by_ats["workday"] = scrape_workday_companies(workday_ready, conn)
        print()

    if buckets.get("atlassian"):
        print("--- Atlassian ---")
        stats_by_ats["atlassian"] = scrape_atlassian_companies(buckets["atlassian"], conn)
        print()

    if buckets.get("icims"):
        print("--- iCIMS ---")
        stats_by_ats["icims"] = scrape_icims_companies(buckets["icims"], conn)
        print()

    custom_like = (buckets.get("custom") or []) + workday_legacy
    if custom_like:
        print("--- Custom / Workday (best effort) ---")
        stats_by_ats["custom/workday"] = scrape_custom_companies(custom_like, conn)
        print()

    _print_summary(stats_by_ats, conn)
    conn.close()

    total_new = sum(s.get("new", 0) for s in stats_by_ats.values())
    total_kept = sum(s.get("kept", 0) for s in stats_by_ats.values())
    if total_kept == 0:
        print("⚠ Zero jobs kept. Possible causes: network blocked, filters too strict, ATS slugs stale.")
        return 1

    print(f"✅ Done. {total_new} new jobs added to {DB_PATH.name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
