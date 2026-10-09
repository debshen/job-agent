"""
lever.py — Full Lever scraper for Layer 03.

WHAT THIS DOES (in plain English):
  Same shape as greenhouse.py, but for companies whose `ats:` is `lever` in
  watchlist.yaml. Lever is the second-most-common ATS in Bay Area tech, after
  Greenhouse.

THE LEVER API:
  Pattern:  https://api.lever.co/v0/postings/{slug}?mode=json
  Auth:     none — fully public
  Returns:  JSON array of job objects, each looking like:
            { "text": "Senior Product Manager",
              "categories": {"location": "San Francisco", "team": "Product",
                             "commitment": "Full-time"},
              "hostedUrl": "https://jobs.lever.co/..." }

  Note Lever returns a top-level array, not an object with a "jobs" key.
  That's the main shape difference from Greenhouse.
"""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from db import upsert_job
from filters import keep_job

USER_AGENT = "JobAgent/0.1 (+https://github.com/debshen)"
TIMEOUT_SECONDS = 20


def _fetch_lever_jobs(slug: str) -> list[dict]:
    """
    Hit Lever for one company. Returns the raw postings array, or [] on error.
    """
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read())
            if isinstance(payload, list):
                return payload
            return []
    except HTTPError as e:
        print(f"    ⚠ HTTP {e.code} from Lever for '{slug}'")
    except URLError as e:
        print(f"    ⚠ Network error fetching Lever '{slug}': {e.reason}")
    except Exception as e:  # pragma: no cover
        print(f"    ⚠ Unexpected error for Lever '{slug}': {e!r}")
    return []


def scrape_lever_companies(companies: list[dict], conn) -> dict:
    """
    Scrape every Lever company in `companies` and write to `conn`.

    Returns the same stats shape as Greenhouse: {checked, fetched, kept, new}.
    """
    stats = {"checked": 0, "fetched": 0, "kept": 0, "new": 0}

    for company in companies:
        slug = company.get("ats_slug")
        name = company.get("name", "?")
        if not slug:
            print(f"  ✗ {name}: no ats_slug set in watchlist.yaml — skipping")
            continue

        print(f"  → {name} (lever: {slug})")
        jobs = _fetch_lever_jobs(slug)
        stats["checked"] += 1
        stats["fetched"] += len(jobs)

        kept_here = 0
        new_here = 0
        for job in jobs:
            title = job.get("text")
            categories = job.get("categories") or {}
            location = categories.get("location")
            department = categories.get("team")

            if not keep_job(title=title, location=location):
                continue

            url = job.get("hostedUrl") or job.get("applyUrl")
            if not url:
                continue

            # Lever uses milliseconds since epoch
            posted_at = job.get("createdAt")
            if isinstance(posted_at, (int, float)):
                from datetime import datetime, timezone
                posted_at = datetime.fromtimestamp(
                    posted_at / 1000, tz=timezone.utc
                ).isoformat()

            is_new = upsert_job(
                conn,
                url=url,
                company=name,
                ats="lever",
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
