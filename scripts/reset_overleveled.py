#!/usr/bin/env python3
"""
reset_overleveled.py — one-off cleanup for the "10+ years" filter change.

WHY THIS EXISTS:
  On 2026-09-15 we added a hard triage rule: any job whose description requires
  a MINIMUM of 10+ years of experience is filtered out (over-leveled for
  the candidate's 5-7 year target). But jobs that were ALREADY scored before that
  change keep their old "passed" status, so they'd keep showing in the digest
  until something re-checks them. This script finds those already-scored,
  over-leveled roles and clears their score + triage status so the next normal
  nightly run re-triages them under the new rule (the genuine 10+ ones get
  filtered out; anything actually 8-9 years simply comes back).

  Clearing a score is NOT destructive — the nightly run recomputes it. Worst
  case, a mistakenly-cleared role is re-scored for a few cents.

HOW TO USE (on your Mac, in Terminal):
  1. Preview what would change (safe, changes nothing):
       python3 "/path/to/job-agent/scripts/reset_overleveled.py"
  2. If the list looks right, apply it:
       python3 "/path/to/job-agent/scripts/reset_overleveled.py" --apply
  3. Let tonight's run happen (or run the pipeline now). The over-leveled roles
     will drop out of the digest.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "jobs.sqlite"

# Matches an experience figure like "10+ years", "at least 12 years",
# "minimum of 10 years", "10-15 years", capturing the LOWER bound number.
_YEARS_RE = re.compile(
    r"(?:(?:at least|minimum(?: of)?|min\.?)\s*)?"
    r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?(?:or more\s*)?years",
    re.I,
)
# Only count a match if it's clearly about experience, not company tenure/age.
_CONTEXT_WORDS = (
    "experience", "pm", "product management", "product manager",
    "building", "years of",
)
CEILING = 10  # minimum-years requirement at or above this is over-leveled


def find_overleveled(conn) -> list[tuple[str, str, str, int, int]]:
    rows = conn.execute(
        "SELECT url, company, title, score_total, jd_text "
        "FROM jobs WHERE triage_status='passed' AND score_total IS NOT NULL"
    ).fetchall()
    hits = []
    for url, company, title, score, jd_text in rows:
        jd = jd_text or ""
        mins = []
        for m in _YEARS_RE.finditer(jd):
            ctx = jd[max(0, m.start() - 40): m.end() + 40].lower()
            if any(w in ctx for w in _CONTEXT_WORDS):
                mins.append(int(m.group(1)))
        # Use the LOWER bound: a role passes only if its smallest stated
        # experience minimum is under the ceiling.
        if mins and min(mins) >= CEILING:
            hits.append((url, company, title, score, min(mins)))
    return hits


def main() -> int:
    apply = "--apply" in sys.argv
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}")
        return 2

    conn = sqlite3.connect(str(DB_PATH))
    hits = find_overleveled(conn)

    if not hits:
        print("No already-scored roles require 10+ years. Nothing to do.")
        return 0

    print(f"Found {len(hits)} already-scored role(s) requiring 10+ years:\n")
    for _url, company, title, score, mn in sorted(hits, key=lambda h: -h[3]):
        print(f"  {score:>3}  {company:<12} {title[:55]}  (min {mn} yrs)")

    if not apply:
        print("\nThis was a PREVIEW — nothing changed.")
        print("Re-run with --apply to clear these so the next run re-checks them.")
        return 0

    conn.executemany(
        "UPDATE jobs SET score_total=NULL, triage_status=NULL, triage_reason=NULL "
        "WHERE url=?",
        [(h[0],) for h in hits],
    )
    conn.commit()
    print(f"\nApplied. Cleared {len(hits)} role(s). They'll drop from the digest "
          "now and be re-checked under the 10+ rule on the next run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
