"""
schema.py — Schema migrations for Layer 07 (Interview Prep).

NAMED schema.py NOT db.py: Layer 03 already has a `db.py` and we want to
import from it. If this file shared the name, Python's import resolver would
pick whichever appeared first on sys.path and we'd hit circular-import errors.
Different names, clean imports.

WHAT THIS DOES (in plain English):
  Layer 07 needs to record mock-interview sessions and track which jobs you're
  actively interviewing for. Both live in the same SQLite file Layer 03/02
  already use — `data/jobs.sqlite` — we just add a new table and a few new
  columns on the existing `jobs` table.

  This module is BOTH:
    1. A library: `ensure_schema(conn)` is called by mock.py at startup so the
       layer just works without you remembering to migrate first.
    2. A CLI: `python3 src/07_interview/schema.py --migrate` lets you run the
       migration explicitly the first time, with a friendly summary.

PHASE A (what this file does today):
  - Adds `mock_sessions` table for storing transcripts + feedback summaries.
  - Adds `interview_status` and `applied_at` columns on `jobs`.

PHASE B (what this file will add later):
  - `interview_intel` table   (cached company-level interview research)
  - `question_banks` table    (per-job tailored question banks)
  - `interview_debriefs` table (post-interview capture)

  Adding those later is a one-line code change — same pattern as the columns
  we add today.

JARGON DEFINED INLINE:
  - "schema migration" = changing the structure of a database (adding tables
                         or columns). Idempotent migrations are safe to run
                         repeatedly without breaking anything.
  - "PRAGMA table_info" = a SQLite-specific query that returns the columns
                          of a table. We use it to check what already exists
                          before adding new columns.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# Reuse Layer 03's database connection logic so there's exactly ONE source of
# truth for "where the SQLite file lives" (controlled by JOB_AGENT_DB env var).
HERE = Path(__file__).resolve().parent
SCRAPE_DIR = HERE.parent / "03_scrape"
sys.path.insert(0, str(SCRAPE_DIR))

try:
    from db import get_connection, DB_PATH  # type: ignore  (Layer 03 module)
except ImportError as e:
    sys.stderr.write(
        f"\nCould not import Layer 03's db module. Run from project root:\n"
        f"    cd \"/path/to/job-agent\"\n"
        f"    python3 src/07_interview/schema.py --migrate\n\n"
        f"Original error: {e}\n"
    )
    sys.exit(2)


# ---- New table: mock_sessions ------------------------------------------------

MOCK_SESSIONS_SQL = """
CREATE TABLE IF NOT EXISTS mock_sessions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id              INTEGER,           -- NULL for general practice; FK in Phase B
    round               TEXT NOT NULL,     -- e.g. 'behavioral', 'product_sense'
    question_id         TEXT,              -- e.g. 'behavioral_07' from seed_questions.yaml
    question_prompt     TEXT NOT NULL,     -- the question that was asked
    started_at          TEXT NOT NULL,
    ended_at            TEXT,
    transcript_path     TEXT,              -- file path to the saved markdown transcript
    feedback_summary    TEXT,              -- one-paragraph debrief from the interviewer
    cost_usd            REAL               -- approx API spend for this session
);

CREATE INDEX IF NOT EXISTS idx_mock_sessions_round   ON mock_sessions(round);
CREATE INDEX IF NOT EXISTS idx_mock_sessions_job_id  ON mock_sessions(job_id);
"""


# ---- New columns on the existing jobs table ----------------------------------
# These let Phase B mark which jobs you're actively interviewing for. Phase A
# doesn't read them yet but adding them now means we don't have to touch the
# schema again when Phase B ships.
INTERVIEW_COLUMNS = [
    ("interview_status",       "TEXT"),    # NULL / 'applied' / 'screening' /
                                            # 'onsite' / 'offer' / 'rejected' / 'withdrew'
    ("interview_round_current","TEXT"),    # which round you're prepping for now
    ("applied_at",             "TEXT"),    # ISO date you submitted the application
]


def _ensure_interview_columns(conn: sqlite3.Connection) -> list[str]:
    """
    Add interview-tracking columns to `jobs` if not already present.
    Returns the list of columns that were just added (for the CLI summary).
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    added: list[str] = []
    for col, sql_type in INTERVIEW_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {sql_type}")
            added.append(col)
    if added:
        conn.commit()
    return added


def ensure_schema(conn: sqlite3.Connection | None = None) -> dict:
    """
    Idempotent. Call from any Layer 07 script at startup.

    Returns a small report dict so callers (especially the CLI) can print
    what changed:
        {
            "table_created": bool,
            "columns_added": [list of column names just added to jobs],
            "db_path": str,
        }
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()  # also runs Layer 03 schema setup

    # Did mock_sessions already exist?
    pre = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='mock_sessions'"
    ).fetchone()
    table_existed = pre is not None

    conn.executescript(MOCK_SESSIONS_SQL)
    columns_added = _ensure_interview_columns(conn)
    conn.commit()

    if own_conn:
        conn.close()

    return {
        "table_created": not table_existed,
        "columns_added": columns_added,
        "db_path": str(DB_PATH),
    }


# ---- CLI ---------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply Layer 07 schema migrations to data/jobs.sqlite. "
                    "Safe to run repeatedly."
    )
    parser.add_argument(
        "--migrate", action="store_true",
        help="Run the migration. (Same effect as importing ensure_schema.)"
    )
    args = parser.parse_args()

    if not args.migrate:
        parser.print_help()
        return 0

    report = ensure_schema()

    print(f"\nDatabase: {report['db_path']}")
    if report["table_created"]:
        print("  ✅ Created table: mock_sessions")
    else:
        print("  • mock_sessions already existed (no change)")

    if report["columns_added"]:
        print(f"  ✅ Added columns to jobs: {', '.join(report['columns_added'])}")
    else:
        print("  • Interview columns already on jobs (no change)")

    print("\nLayer 07 schema is ready. Run a mock interview with:")
    print("    python3 src/07_interview/mock.py --round behavioral\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
