"""
run_score.py — Orchestrator for Layer 02. The script you actually run.

WHAT THIS DOES (in plain English):
  1. Reads jobs from data/jobs.sqlite where score_total IS NULL (i.e., we
     haven't scored them yet).
  2. For each one:
       a. Fetches the full JD text (and caches it on the row so we never
          re-fetch the same JD).
       b. Runs the cheap Haiku triage. If it fails, marks triage_status='failed'
          and moves on — no Sonnet call, no waste.
       c. Runs the Sonnet scoring. Saves total + per-criterion + rationale.
  3. Prints a tidy summary including total estimated USD spent this run and
     the top-5 newly-scored jobs.

HOW TO RUN IT:
    cd "/path/to/job-agent"
    python3 src/02_score/run_score.py            # score all unscored jobs
    python3 src/02_score/run_score.py --limit 5  # cap at 5 (handy for testing)
    python3 src/02_score/run_score.py --rescore  # re-score everything

FIRST-TIME SETUP (one-time):
    pip3 install --user anthropic pyyaml
    cp .env.example .env
    # Open .env, paste your ANTHROPIC_API_KEY value, save.

EXIT CODES:
    0 = ran successfully
    1 = ran but couldn't score anything (e.g. zero unscored jobs in DB)
    2 = could not start (missing API key, missing DB, etc.)
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make sibling files importable + add src/03_scrape so we can reuse db.py
HERE = Path(__file__).resolve().parent
SCRAPE_DIR = HERE.parent / "03_scrape"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SCRAPE_DIR))

try:
    from db import get_connection, DB_PATH  # type: ignore  (Layer 03 module)
    from criteria_loader import load_criteria, format_criteria_for_prompt
    from jd_fetcher import fetch_jd
    from client import get_client, CostTracker
    from triage import triage_one
    from score import score_one
except ImportError as e:
    sys.stderr.write(
        f"\nCould not import a required module. Run from project root:\n"
        f"    cd \"/path/to/job-agent\"\n"
        f"    python3 src/02_score/run_score.py\n\n"
        f"Original error: {e}\n"
    )
    sys.exit(2)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _select_unscored(conn, *, rescore: bool, limit: int | None):
    """Pick the rows we should score on this run."""
    if rescore:
        sql = "SELECT * FROM jobs ORDER BY first_seen DESC"
    else:
        # Only jobs never triaged before. Jobs that already failed triage keep
        # score_total NULL forever, so filtering on score_total alone would
        # re-send them to the API every night (this drained the credit balance).
        sql = (
            "SELECT * FROM jobs WHERE score_total IS NULL "
            "AND triage_status IS NULL ORDER BY first_seen DESC"
        )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


def _save_jd(conn, url: str, jd_text: str | None) -> None:
    conn.execute(
        "UPDATE jobs SET jd_text = ?, jd_fetched_at = ? WHERE url = ?",
        (jd_text, _now_iso(), url),
    )
    conn.commit()


def _save_triage(conn, url: str, triage: dict, model: str) -> None:
    """
    Record triage decision. If triage FAILED, also clear any prior score
    columns — otherwise stale scores from earlier runs would leak into the
    digest after a rescore that newly disqualifies a row (e.g., a comp band
    being revealed as below floor once we got the real JD).
    """
    if triage["passed"]:
        conn.execute(
            """UPDATE jobs SET triage_status = ?, triage_reason = ?, triage_model = ?
                             WHERE url = ?""",
            ("passed", triage["reason"], model, url),
        )
    else:
        conn.execute(
            """UPDATE jobs
                  SET triage_status = ?, triage_reason = ?, triage_model = ?,
                      score_total = NULL, score_role_fit = NULL, score_comp = NULL,
                      score_ai_moat = NULL, score_wlb = NULL, score_company_health = NULL,
                      score_work_model = NULL, score_rationale = NULL,
                      score_role_note = NULL, score_comp_note = NULL,
                      score_ai_moat_note = NULL, score_wlb_note = NULL,
                      score_interview_note = NULL,
                      score_model = NULL, scored_at = NULL
                WHERE url = ?""",
            ("failed", triage["reason"], model, url),
        )
    conn.commit()


def _save_score(conn, url: str, scored: dict) -> None:
    conn.execute(
        """UPDATE jobs SET
              score_total = ?, score_role_fit = ?, score_comp = ?,
              score_ai_moat = ?, score_wlb = ?, score_company_health = ?,
              score_work_model = ?, score_rationale = ?,
              score_role_note = ?, score_comp_note = ?, score_ai_moat_note = ?,
              score_wlb_note = ?, score_interview_note = ?,
              score_model = ?, scored_at = ?
            WHERE url = ?""",
        (scored["score_total"], scored["score_role_fit"], scored["score_comp"],
         scored["score_ai_moat"], scored["score_wlb"], scored["score_company_health"],
         scored["score_work_model"], scored["score_rationale"],
         scored.get("score_role_note"), scored.get("score_comp_note"),
         scored.get("score_ai_moat_note"), scored.get("score_wlb_note"),
         scored.get("score_interview_note"),
         scored["score_model"], _now_iso(), url),
    )
    conn.commit()


def _print_top(conn, n: int = 5) -> None:
    rows = conn.execute(
        """SELECT company, title, location, score_total, score_rationale, url
             FROM jobs WHERE score_total IS NOT NULL
             ORDER BY score_total DESC LIMIT ?""", (n,),
    ).fetchall()
    if not rows:
        return
    print(f"\nTop {len(rows)} scored jobs in the database:")
    for r in rows:
        print(f"  [{r['score_total']:>3}] {r['company']:<10} {r['title']}")
        print(f"        {r['location'] or '(no loc)'}")
        if r["score_rationale"]:
            print(f"        \"{r['score_rationale']}\"")
        print(f"        {r['url']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Score unscored jobs in jobs.sqlite")
    parser.add_argument("--limit", type=int, default=None,
                        help="Score at most N jobs this run (handy for testing)")
    parser.add_argument("--rescore", action="store_true",
                        help="Re-score every job, even ones with an existing score")
    args = parser.parse_args()

    if not Path(DB_PATH).exists():
        print(f"jobs.sqlite not found at {DB_PATH}.")
        print("Run Layer 03 first:  python3 src/03_scrape/run_all.py")
        return 2

    print(f"Loading criteria...")
    criteria = load_criteria()
    criteria_block = format_criteria_for_prompt(criteria)
    weights = criteria.get("scoring_weights", {})

    client = get_client()
    cost = CostTracker()

    conn = get_connection()
    rows = _select_unscored(conn, rescore=args.rescore, limit=args.limit)
    print(f"Will process {len(rows)} job(s).\n")
    if not rows:
        print("Nothing to do — every job already has a score.")
        print("(Use --rescore to re-score everything.)")
        # No new jobs is a normal, healthy outcome on quiet days — exit 0 so the
        # nightly wrapper doesn't fire a spurious "score failed" email.
        return 0

    triaged_pass = triaged_fail = scored = jd_failed = errored = 0

    for i, row in enumerate(rows, start=1):
        print(f"[{i}/{len(rows)}] {row['company']} — {row['title']}")

        # Fetch JD if we don't already have it cached
        if row["jd_text"]:
            jd_text = row["jd_text"]
            jd_source = "cached"
        else:
            # Safety net: a network error while fetching ONE job's description
            # must never crash the whole nightly run (this happened 2026-09-02
            # via an uncaught ConnectionResetError). Treat any failure as a
            # missing JD and keep going.
            try:
                jd_text, jd_source = fetch_jd(row)
            except Exception as e:
                jd_text, jd_source = None, f"error: {e!r}"
            _save_jd(conn, row["url"], jd_text)
            if jd_text is None:
                jd_failed += 1
                print(f"     ⚠ JD fetch failed: {jd_source}")

        # Triage with Haiku
        try:
            triage = triage_one(client, cost, row, jd_text, criteria_block)
        except Exception as e:
            errored += 1
            print(f"     ⚠ Haiku triage error: {e!r}")
            continue
        _save_triage(conn, row["url"], triage, "claude-haiku-4-5-20251001")

        if not triage["passed"]:
            triaged_fail += 1
            print(f"     ✗ Triaged out: {triage['reason']}")
            continue
        triaged_pass += 1
        print(f"     ✓ Triage passed: {triage['reason']}")

        # Score with Sonnet
        try:
            result = score_one(client, cost, row, jd_text, criteria_block, weights)
        except Exception as e:
            errored += 1
            print(f"     ⚠ Sonnet scoring error: {e!r}")
            continue
        _save_score(conn, row["url"], result)
        scored += 1
        print(f"     → score: {result['score_total']:>3}/100 — {result['score_rationale']}")

    print("\n" + "=" * 60)
    print(" Layer 02 — Score — summary")
    print("=" * 60)
    print(f"  Processed:        {len(rows)}")
    print(f"  Triage passed:    {triaged_pass}")
    print(f"  Triage failed:    {triaged_fail}")
    print(f"  Scored fully:     {scored}")
    print(f"  JD fetch failed:  {jd_failed}")
    print(f"  Errored:          {errored}")
    print()
    print(" API usage:")
    print(cost.report())

    _print_top(conn, n=5)

    conn.close()
    return 0 if scored or triaged_fail else 1


if __name__ == "__main__":
    sys.exit(main())
