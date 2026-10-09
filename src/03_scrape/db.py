"""
db.py — The "filing cabinet" for everything Layer 03 scrapes.

WHAT THIS DOES (in plain English):
  Every scraper (Greenhouse, Lever, custom HTML) hands its results to this
  module. This module writes them into a single SQLite database file at
  data/jobs.sqlite so we have ONE source of truth for "every PM job we've
  ever seen."

WHY SQLITE:
  - It's a single file on disk (no server to install or run).
  - It comes built into Python — zero dependencies.
  - You can open it in any free DB tool (DB Browser for SQLite, TablePlus, etc.)
    and look at the rows like a spreadsheet.
  - It handles deduplication for us: if the same job shows up two nights in a
    row, we just update the existing row instead of creating a duplicate.

JARGON DEFINED INLINE:
  - "schema"  = the shape of a database table (which columns exist, what types).
  - "upsert"  = INSERT a row if it's new, UPDATE it if it already exists.
                This is how we avoid duplicates when running nightly.
  - "PRIMARY KEY" = the column the database uses to tell rows apart uniquely.
                We use the job's source URL because URLs are guaranteed unique.

THE TABLE WE STORE:
  jobs (
    url          TEXT PRIMARY KEY,   -- the canonical job posting URL
    company      TEXT,               -- "Stripe", "Anthropic", etc.
    ats          TEXT,               -- "greenhouse" / "lever" / "custom" / "workday"
    title        TEXT,               -- the job title as posted
    location     TEXT,               -- raw location string from the source
    department   TEXT,               -- e.g. "Product" / "Engineering" (when given)
    posted_at    TEXT,               -- ISO date the company says it posted (if any)
    first_seen   TEXT,               -- ISO date WE first saw this job
    last_seen    TEXT,               -- ISO date WE last saw this job in a scrape
    raw_json     TEXT                -- the full original payload, for later layers
  )
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# ---- Where the database file lives -------------------------------------------

# Default: <project root>/data/jobs.sqlite
# Override: set the JOB_AGENT_DB environment variable to any absolute path.
#   - Useful when running in CI / cron with a different storage location
#   - Useful when testing in a sandbox whose filesystem doesn't support SQLite
#     locking (e.g. some FUSE mounts)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "jobs.sqlite"
DB_PATH = Path(os.environ.get("JOB_AGENT_DB", _DEFAULT_DB_PATH))


# ---- Schema setup ------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    url          TEXT PRIMARY KEY,
    company      TEXT NOT NULL,
    ats          TEXT NOT NULL,
    title        TEXT NOT NULL,
    location     TEXT,
    department   TEXT,
    posted_at    TEXT,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    raw_json     TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_company   ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_jobs_last_seen ON jobs(last_seen);
"""

# Layer 02 (Score) adds these columns to the same jobs row.
# We add them with ALTER TABLE so a database created by Layer 03 alone keeps
# working — no migration scripts to run by hand.
SCORE_COLUMNS = [
    # ATS provenance — set at scrape time, used by the JD fetcher to look up
    # the full description without having to reverse-engineer the URL.
    # (Some companies use custom careers domains in their public URLs even
    # when their ATS is Greenhouse, so URL parsing is unreliable.)
    ("ats_slug",           "TEXT"),    # company's ATS slug, e.g. "stripe"
    ("ats_external_id",    "TEXT"),    # job ID inside the ATS, e.g. "5181852008"

    ("jd_text",            "TEXT"),    # full job description text, fetched on demand
    ("jd_fetched_at",      "TEXT"),
    ("triage_status",      "TEXT"),    # 'passed', 'failed', or NULL
    ("triage_reason",      "TEXT"),    # short Haiku explanation
    ("triage_model",       "TEXT"),
    ("score_total",        "INTEGER"), # 0–100, NULL until Sonnet has run
    ("score_role_fit",     "INTEGER"),
    ("score_comp",         "INTEGER"),
    ("score_ai_moat",      "INTEGER"),
    ("score_wlb",          "INTEGER"),
    ("score_company_health","INTEGER"),
    ("score_work_model",   "INTEGER"),
    ("score_rationale",    "TEXT"),    # one-line "why" summary
    # Per-criterion notes added so the digest can show bullets instead of one
    # blob of prose. Each is a short one-liner from Sonnet.
    ("score_role_note",    "TEXT"),
    ("score_comp_note",    "TEXT"),
    ("score_ai_moat_note", "TEXT"),
    ("score_wlb_note",     "TEXT"),
    ("score_interview_note","TEXT"),   # optional; only present when Sonnet is confident
    ("score_model",        "TEXT"),    # which model produced the score
    ("scored_at",          "TEXT"),
]


def _ensure_score_columns(conn: sqlite3.Connection) -> None:
    """
    Add Layer 02 columns to an existing jobs table if they're not already there.
    SQLite doesn't have IF NOT EXISTS on ALTER TABLE, so we check PRAGMA first.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    for col, sql_type in SCORE_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {sql_type}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_score_total ON jobs(score_total DESC)"
    )
    conn.commit()


def _now_iso() -> str:
    """Today's date+time as a sortable ISO string (e.g. 2026-04-27T18:42:11Z)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_connection() -> sqlite3.Connection:
    """
    Open (or create) the SQLite database and make sure the table exists.

    Returns a live connection. Caller is responsible for closing it (or use
    `with get_connection() as conn:` to auto-close).
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    # When two runs overlap (e.g. the long nightly data run still scoring while
    # the morning digest run starts), one process can briefly hold a write lock.
    # Without this, the other process errors out instantly with "database is
    # locked". busy_timeout makes it wait up to 30s for the lock to free,
    # which is far longer than any single commit in this project holds it.
    conn.execute("PRAGMA busy_timeout=30000")
    # Row factory lets us access columns by name (row["title"] not row[3])
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    _ensure_score_columns(conn)
    conn.commit()
    return conn


# ---- The one function every scraper calls ------------------------------------

def upsert_job(
    conn: sqlite3.Connection,
    *,
    url: str,
    company: str,
    ats: str,
    title: str,
    location: str | None = None,
    department: str | None = None,
    posted_at: str | None = None,
    raw: dict | None = None,
    ats_slug: str | None = None,
    ats_external_id: str | None = None,
) -> bool:
    """
    Insert a job if we've never seen it, or update last_seen if we have.

    Returns True if this was a brand-new job (useful for printing "+12 new
    jobs tonight" in the summary).

    The url is the dedup key — Greenhouse/Lever/etc. always give us a unique
    posting URL, so this is the cleanest way to spot repeat jobs.

    `ats_slug` + `ats_external_id` are NEW: they let the JD fetcher look up
    the full description directly from the ATS without re-parsing the URL,
    which mattered because companies like Databricks and Stripe surface
    custom careers domains that hide the underlying Greenhouse routing.
    """
    now = _now_iso()
    raw_json = json.dumps(raw, ensure_ascii=False) if raw is not None else None
    ext_id_str = str(ats_external_id) if ats_external_id is not None else None

    cur = conn.execute("SELECT url FROM jobs WHERE url = ?", (url,))
    existing = cur.fetchone()

    if existing is None:
        conn.execute(
            """
            INSERT INTO jobs
              (url, company, ats, title, location, department,
               posted_at, first_seen, last_seen, raw_json,
               ats_slug, ats_external_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (url, company, ats, title, location, department,
             posted_at, now, now, raw_json, ats_slug, ext_id_str),
        )
        return True
    else:
        conn.execute(
            """
            UPDATE jobs
               SET title = ?, location = ?, department = ?, posted_at = ?,
                   last_seen = ?, raw_json = ?,
                   ats_slug = COALESCE(?, ats_slug),
                   ats_external_id = COALESCE(?, ats_external_id)
             WHERE url = ?
            """,
            (title, location, department, posted_at, now, raw_json,
             ats_slug, ext_id_str, url),
        )
        return False


def count_jobs(conn: sqlite3.Connection) -> int:
    """Total rows in the jobs table — used by the orchestrator's summary."""
    return conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]


def count_jobs_by_company(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """List of (company, count) pairs sorted by count descending."""
    rows = conn.execute(
        "SELECT company, COUNT(*) AS n FROM jobs GROUP BY company ORDER BY n DESC"
    ).fetchall()
    return [(r["company"], r["n"]) for r in rows]
