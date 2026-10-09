"""
greenhouse.py — Full Greenhouse scraper for Layer 03.

WHAT THIS DOES (in plain English):
  Walks every company in config/watchlist.yaml whose `ats:` is `greenhouse`,
  fetches all their open jobs from Greenhouse's free public API, keeps the
  ones that look like PM roles in our geography, and writes them to the
  jobs.sqlite database.

THE GREENHOUSE API:
  Pattern:  https://boards-api.greenhouse.io/v1/boards/{slug}/jobs
  Auth:     none — fully public
  Returns:  JSON like { "jobs": [ {title, location, absolute_url, ...}, ... ] }

  The slug is whatever appears after boards.greenhouse.io/ in the company's
  careers URL. We store these in watchlist.yaml as `ats_slug:`.

USAGE FROM A SCRIPT (typical):
  from src.03_scrape.greenhouse import scrape_greenhouse_companies
  results = scrape_greenhouse_companies(companies, conn)
  # results: {"checked": 8, "fetched": 412, "kept": 17, "new": 3}

USAGE FROM THE COMMAND LINE:
  Don't run this directly — use run_all.py. This module is a building block.
"""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from db import upsert_job
from filters import keep_job

USER_AGENT = "JobAgent/0.1 (+https://github.com/debshen)"
TIMEOUT_SECONDS = 20


def _fetch_greenhouse_jobs(slug: str) -> list[dict]:
    """
    Hit Greenhouse for one company. Returns the raw `jobs` array, or [] on
    any error (so a single broken company doesn't crash the whole run).
    """
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false"
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read())
            return payload.get("jobs", []) or []
    except HTTPError as e:
        print(f"    ⚠ HTTP {e.code} from Greenhouse for '{slug}'")
    except URLError as e:
        print(f"    ⚠ Network error fetching Greenhouse '{slug}': {e.reason}")
    except Exception as e:  # pragma: no cover — last-ditch safety net
        print(f"    ⚠ Unexpected error for Greenhouse '{slug}': {e!r}")
    return []


def scrape_greenhouse_companies(companies: list[dict], conn) -> dict:
    """
    Scrape every Greenhouse company in `companies` and write to `conn`.

    `companies` is the subset of watchlist.yaml entries where ats == "greenhouse".
    Returns a stats dict the orchestrator uses for the summary line.
    """
    stats = {"checked": 0, "fetched": 0, "kept": 0, "new": 0}

    for company in companies:
        slug = company.get("ats_slug")
        name = company.get("name", "?")
        if not slug:
            print(f"  ✗ {name}: no ats_slug set in watchlist.yaml — skipping")
            continue

        print(f"  → {name} (greenhouse: {slug})")
        jobs = _fetch_greenhouse_jobs(slug)
        stats["checked"] += 1
        stats["fetched"] += len(jobs)

        kept_here = 0
        new_here = 0
        for job in jobs:
            title = job.get("title")
            location = (job.get("location") or {}).get("name")
            if not keep_job(title=title, location=location):
                continue

            url = job.get("absolute_url")
            if not url:
                continue   # No URL means we can't dedupe → skip

            posted_at = job.get("updated_at") or job.get("first_published")
            department = None
            depts = job.get("departments") or []
            if depts and isinstance(depts, list):
                department = (depts[0] or {}).get("name")

            is_new = upsert_job(
                conn,
                url=url,
                company=name,
                ats="greenhouse",
                title=title or "(untitled)",
                location=location,
                department=department,
                posted_at=posted_at,
                raw=job,
                ats_slug=slug,
                ats_external_id=job.get("id"),
            )
            kept_here += 1
            if is_new:
                new_here += 1

        stats["kept"] += kept_here
        stats["new"] += new_here
        print(f"     {len(jobs):>3} fetched · {kept_here:>2} kept · {new_here:>2} new")

    conn.commit()
    return stats
