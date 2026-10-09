"""
atlassian.py — Atlassian careers scraper for Layer 03 (added 2026-09-28).

WHAT THIS DOES (in plain English):
  Atlassian's careers page (atlassian.com/company/careers/all-jobs) is rendered
  with JavaScript and backed by iCIMS, which our other scrapers can't read.
  Behind the page sits one public JSON feed with EVERY open job and its full
  description. We call that feed directly, keep US-reachable PM roles, and
  store the description so the JD fetcher can reuse it (no second call).

HOW TO ADD IT TO watchlist.yaml:
      - name: Atlassian
        ats: atlassian

THE FEED:
  GET https://www.atlassian.com/endpoint/careers/listings
  → [{"id", "title", "category", "locations": [...], "overview",
      "responsibilities", "qualifications", "portalJobPost": {"portalUrl",
      "updatedDate"}, ...}, ...]

LOCATION NOTE:
  Nearly every Atlassian job lists "Remote - Remote", including roles meant
  for India or Australia. So we only keep a job if at least one location is in
  the United States, and we pass only the US locations on to the filters.
"""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from db import upsert_job
from filters import keep_job

FEED_URL = "https://www.atlassian.com/endpoint/careers/listings"
DETAIL_URL = "https://www.atlassian.com/company/careers/details/{id}"
USER_AGENT = "JobAgent/0.1 (+https://github.com/debshen)"
TIMEOUT_SECONDS = 30


def _fetch_feed() -> list[dict]:
    req = Request(FEED_URL, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read())
        return data if isinstance(data, list) else []
    except HTTPError as e:
        print(f"    ⚠ HTTP {e.code} from Atlassian careers feed")
    except URLError as e:
        print(f"    ⚠ Network error fetching Atlassian feed: {e.reason}")
    except Exception as e:  # pragma: no cover
        print(f"    ⚠ Unexpected error fetching Atlassian feed: {e!r}")
    return []


def _us_locations(locations: list[str] | None) -> list[str]:
    """Keep only US locations. 'Remote - Remote' alone is ambiguous, so it
    counts only when a US office is also listed (then 'Remote' is appended)."""
    locs = [l for l in (locations or []) if l]
    us = [" ".join(l.split()) for l in locs if "united states" in l.lower()]
    if us and any(l.strip().lower().startswith("remote") for l in locs):
        us.append("Remote (US)")
    return us


def scrape_atlassian_companies(companies: list[dict], conn) -> dict:
    """Same stats shape as the other scrapers. `companies` is the watchlist
    entries with ats: atlassian (normally just one)."""
    stats = {"checked": 0, "fetched": 0, "kept": 0, "new": 0}
    if not companies:
        return stats

    name = companies[0].get("name", "Atlassian")
    print(f"  → {name} (atlassian careers feed)")
    jobs = _fetch_feed()
    stats["checked"] += 1
    stats["fetched"] += len(jobs)

    kept_here = new_here = 0
    for j in jobs:
        title = j.get("title")
        us_locs = _us_locations(j.get("locations"))
        if not us_locs:
            continue
        location = "; ".join(us_locs)
        if not keep_job(title=title, location=location):
            continue

        job_id = j.get("id")
        portal = j.get("portalJobPost") or {}
        url = DETAIL_URL.format(id=job_id) if job_id else portal.get("portalUrl")
        if not url:
            continue
        raw = {
            "id": job_id,
            "title": title,
            "category": j.get("category"),
            "locations": j.get("locations"),
            "portalUrl": portal.get("portalUrl"),
            "updatedDate": portal.get("updatedDate"),
            "overview": j.get("overview"),
            "responsibilities": j.get("responsibilities"),
            "qualifications": j.get("qualifications"),
        }
        is_new = upsert_job(
            conn,
            url=url,
            company=name,
            ats="atlassian",
            title=title or "(untitled)",
            location=location,
            department=j.get("category"),
            posted_at=portal.get("updatedDate"),
            raw=raw,
            ats_slug="atlassian",
            ats_external_id=str(job_id) if job_id else None,
        )
        kept_here += 1
        if is_new:
            new_here += 1

    stats["kept"] += kept_here
    stats["new"] += new_here
    print(f"     {len(jobs):>3} fetched · {kept_here:>2} kept · {new_here:>2} new")
    conn.commit()
    return stats
