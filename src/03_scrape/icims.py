"""
icims.py — iCIMS careers-site scraper for Layer 03 .

WHAT THIS DOES (in plain English):
  Many companies use iCIMS as their applicant tracking system. Their careers
  site (e.g. careers.rivian.com, careers.docusign.com) is iCIMS's modern
  front end, and behind it sits a public JSON endpoint the page itself calls:

      GET https://{careers site}/api/jobs?keywords=product%20manager&limit=100&page=1

  It returns every matching job WITH the full description, location, a
  Remote/Hybrid tag, and the real posting date. We call it directly, keep
  US-reachable PM roles, and store the description so the JD fetcher can
  reuse it (no second call).

  The old-style "something.icims.com/jobs/search" pages usually just redirect
  to that careers site, so the careers site is the one to configure.

HOW TO ADD AN iCIMS COMPANY TO watchlist.yaml:
      - name: Rivian
        ats: icims
        icims_site: careers.rivian.com        # the company's careers host
        icims_search: product manager         # optional; this is the default

  How to tell a company uses this: open one of their job postings. If the
  "Apply" button goes to a URL containing "icims.com", and the careers site
  answers https://{site}/api/jobs?keywords=x with JSON, it works here.
"""

from __future__ import annotations

import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from db import upsert_job
from filters import keep_job

USER_AGENT = "JobAgent/0.1 (+https://github.com/debshen)"
TIMEOUT_SECONDS = 30
PAGE_SIZE = 100
MAX_JOBS = 600              # safety cap per company per run
PAGE_PAUSE_SECONDS = 0.5    # be polite between pages
DEFAULT_SEARCH = "product manager"


def _get_page(site: str, search: str, page: int) -> dict | None:
    url = (f"https://{site}/api/jobs?keywords={quote(search)}"
           f"&limit={PAGE_SIZE}&page={page}")
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        print(f"    ⚠ HTTP {e.code} from iCIMS careers site '{site}'")
    except URLError as e:
        print(f"    ⚠ Network error reaching '{site}': {e.reason}")
    except Exception as e:  # pragma: no cover
        print(f"    ⚠ Unexpected error from '{site}': {e!r}")
    return None


def _list_jobs(site: str, search: str) -> list[dict]:
    """Page through results. Returns the list of job 'data' dicts."""
    out: list[dict] = []
    page = 1
    total = None
    while len(out) < MAX_JOBS:
        payload = _get_page(site, search, page)
        if not payload:
            break
        if total is None:
            total = payload.get("totalCount") or 0
        batch = [j.get("data") or {} for j in (payload.get("jobs") or [])]
        out.extend(batch)
        if not batch or len(batch) < PAGE_SIZE or len(out) >= total:
            break
        page += 1
        time.sleep(PAGE_PAUSE_SECONDS)
    return out


def _location_string(job: dict) -> str | None:
    """US jobs only: the city/state plus 'Remote (US)' when tagged remote.
    Returns None for non-US jobs so they're skipped."""
    country = (job.get("country_code") or "").upper()
    if country and country != "US":
        return None
    parts = []
    if job.get("full_location"):
        parts.append(job["full_location"])
    for extra in job.get("additional_locations") or []:
        if isinstance(extra, dict) and extra.get("full_location"):
            parts.append(extra["full_location"])
    tags = " ".join(str(t) for t in (job.get("tags2") or []) + [job.get("location_type") or ""])
    if "remote" in tags.lower():
        parts.append("Remote (US)")
    return "; ".join(parts) if parts else None


def scrape_icims_companies(companies: list[dict], conn) -> dict:
    """Same stats shape as the other scrapers."""
    stats = {"checked": 0, "fetched": 0, "kept": 0, "new": 0}

    for company in companies:
        name = company.get("name", "?")
        site = (company.get("icims_site") or "").strip().replace("https://", "").strip("/")
        search = company.get("icims_search") or DEFAULT_SEARCH
        if not site:
            print(f"  ✗ {name}: needs icims_site (e.g. careers.example.com) — skipping")
            continue

        print(f"  → {name} (icims: {site}, search '{search}')")
        jobs = _list_jobs(site, search)
        stats["checked"] += 1
        stats["fetched"] += len(jobs)

        kept_here = new_here = 0
        for j in jobs:
            title = j.get("title")
            location = _location_string(j)
            if location is None or not keep_job(title=title, location=location):
                continue
            meta = j.get("meta_data") or {}
            slug = j.get("slug") or j.get("req_id")
            url = meta.get("canonical_url") or (f"https://{site}/jobs/{slug}" if slug else None)
            if not url:
                continue
            categories = j.get("category") or []
            raw = {
                "req_id": j.get("req_id"),
                "title": title,
                "location": location,
                "tags2": j.get("tags2"),
                "posted_date": j.get("posted_date"),
                "apply_url": j.get("apply_url"),
                "description": j.get("description"),
                "responsibilities": j.get("responsibilities"),
                "qualifications": j.get("qualifications"),
            }
            is_new = upsert_job(
                conn,
                url=url,
                company=name,
                ats="icims",
                title=title or "(untitled)",
                location=location,
                department=(categories[0].strip() if categories else None),
                posted_at=j.get("posted_date") or j.get("create_date"),
                raw=raw,
                ats_slug=site,
                ats_external_id=str(j.get("req_id") or slug),
            )
            kept_here += 1
            if is_new:
                new_here += 1

        stats["kept"] += kept_here
        stats["new"] += new_here
        print(f"     {len(jobs):>3} fetched · {kept_here:>2} kept · {new_here:>2} new")

    conn.commit()
    return stats
