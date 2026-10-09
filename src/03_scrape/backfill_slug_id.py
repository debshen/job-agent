"""
backfill_slug_id.py — One-time script to populate ats_slug + ats_external_id
on existing rows in jobs.sqlite.

WHY THIS EXISTS:
  We added two new columns (ats_slug, ats_external_id) to fix the JD fetcher
  bug where Databricks/Stripe rows couldn't find their Greenhouse slug. Going
  forward, the scrapers write those at scrape time. But rows that were
  inserted BEFORE the fix don't have them yet. This script fills them in
  using watchlist.yaml + the raw_json we already saved.

WHEN TO RUN IT:
  Once, after the JD bug fix. After that you never need it again — it's
  designed to be safe to re-run if you ever do.

HOW TO RUN IT:
    cd "/path/to/job-agent"
    python3 src/03_scrape/backfill_slug_id.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from db import get_connection  # noqa: E402

PROJECT_ROOT = HERE.parent.parent
WATCHLIST = PROJECT_ROOT / "config" / "watchlist.yaml"


def _load_watchlist_slugs() -> dict[str, str]:
    """Map company name -> ats_slug from watchlist.yaml."""
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(WATCHLIST.read_text(encoding="utf-8")) or {}
    except ImportError:
        sys.stderr.write("PyYAML required: pip3 install --user pyyaml\n")
        raise
    out = {}
    for entry in data.get("include", []) or []:
        name = entry.get("name")
        slug = entry.get("ats_slug")
        if name and slug:
            out[name] = slug
        elif name:
            # custom adapters use lowercased company name as their slug
            out[name] = name.lower()
    return out


def main() -> int:
    slugs = _load_watchlist_slugs()
    print(f"Loaded {len(slugs)} company slugs from watchlist.yaml.\n")

    conn = get_connection()
    rows = conn.execute(
        """SELECT url, company, ats, raw_json, ats_slug, ats_external_id
             FROM jobs"""
    ).fetchall()

    filled = skipped = already = 0
    for r in rows:
        if r["ats_slug"] and r["ats_external_id"]:
            already += 1
            continue

        new_slug = r["ats_slug"] or slugs.get(r["company"])
        new_id = r["ats_external_id"]

        if not new_id and r["raw_json"]:
            try:
                raw = json.loads(r["raw_json"])
                new_id = (
                    raw.get("id")           # Greenhouse + Lever + Netflix
                    or raw.get("positionId")  # Apple
                    or raw.get("job_id")
                )
            except json.JSONDecodeError:
                pass

        if not (new_slug or new_id):
            skipped += 1
            continue

        conn.execute(
            "UPDATE jobs SET ats_slug = ?, ats_external_id = ? WHERE url = ?",
            (new_slug, str(new_id) if new_id is not None else None, r["url"]),
        )
        filled += 1

    conn.commit()

    print(f"  Already had both:  {already}")
    print(f"  Backfilled:        {filled}")
    print(f"  Could not resolve: {skipped}")
    print()
    print("Sample of backfilled rows:")
    for r in conn.execute(
        """SELECT company, title, ats_slug, ats_external_id
             FROM jobs WHERE ats_slug IS NOT NULL LIMIT 5"""
    ):
        print(f"    {r['company']:<10}  slug={r['ats_slug']:<12}  id={r['ats_external_id']}  — {r['title'][:50]}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
