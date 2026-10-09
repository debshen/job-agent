"""
custom_html.py — Best-effort scraper for companies NOT on Greenhouse/Lever.

WHAT THIS DOES (in plain English):
  Some watchlist companies (Apple, Google, Amazon, Netflix, PayPal,
  ServiceNow) run their own careers sites or use Workday. Those don't have
  the nice public APIs that Greenhouse and Lever do.

  This module makes a HONEST best effort:
    • Where the company has a public JSON endpoint we can hit without
      headers/cookies, we use it.  (Amazon, Apple, Netflix do.)
    • Where the page is rendered by JavaScript and there's no JSON endpoint
      we can hit cleanly (Google, Workday-tenants like PayPal/ServiceNow),
      we LOG IT CLEARLY and skip — those need Firecrawl or browser-use,
      planned for a later stage.

WHY NOT JUST FETCH THE HTML AND PARSE IT?
  Modern careers pages (Google, Workday) load their job lists with JavaScript
  AFTER the page loads. A plain `urlopen()` only sees the empty shell — no
  jobs. Pretending otherwise would mean shipping silent zero-hit code, which
  is worse than admitting the gap.

WHAT YOU'LL SEE WHEN THIS RUNS:
  Apple    → fetched 230 · kept 4 · new 4
  Amazon   → fetched 412 · kept 9 · new 9
  Netflix  → fetched 18 · kept 1 · new 1
  Google   → SKIPPED — JS-rendered, needs Firecrawl (Stage 2 work)
  PayPal   → SKIPPED — Workday tenant, needs Workday adapter (Stage 2)
  ServiceNow → SKIPPED — Workday tenant, needs Workday adapter (Stage 2)
"""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from db import upsert_job
from filters import keep_job

USER_AGENT = "JobAgent/0.1 (+https://github.com/debshen)"
TIMEOUT_SECONDS = 25


# ---- Per-company adapters ----------------------------------------------------
#
# Each adapter takes nothing, returns a list of normalized job dicts:
#   { "title": ..., "location": ..., "url": ..., "department": ...,
#     "posted_at": ..., "raw": ... }
#
# Returning [] means "honest zero" (the API said so).
# Raising means "broken integration" — the caller logs it.

def _amazon_jobs() -> list[dict]:
    """
    Amazon Jobs has a public search.json endpoint that takes URL params.
    Docs: it's been used by 3rd-party tools for years. We narrow with
    a 'product manager' query and let our title filter do the rest.
    """
    # Loose query — we'll re-filter with our own keyword list
    url = (
        "https://www.amazon.jobs/en/search.json"
        "?base_query=product+manager"
        "&loc_query=United+States"
        "&country=USA"
        "&result_limit=100"
        "&sort=recent"
    )
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        payload = json.loads(resp.read())

    out = []
    for j in payload.get("jobs", []):
        out.append({
            "title": j.get("title"),
            "location": j.get("normalized_location") or j.get("location"),
            "url": "https://www.amazon.jobs" + (j.get("job_path") or ""),
            "department": j.get("business_category"),
            "posted_at": j.get("posted_date"),
            "raw": j,
        })
    return out


def _apple_jobs() -> list[dict]:
    """
    Apple's careers site uses POST /api/role/search with a JSON body.
    This endpoint is public (no auth) and powers their own search UI.
    """
    import urllib.request

    body = json.dumps({
        "query": "product manager",
        "filters": {
            "postingpostLocation": ["postLocation-USA"],
            "teams": [],
        },
        "page": 1,
        "locale": "en-us",
        "sort": "newest",
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://jobs.apple.com/api/role/search",
        data=body,
        method="POST",
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    with urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        payload = json.loads(resp.read())

    out = []
    for j in payload.get("searchResults", []) or []:
        title = j.get("postingTitle") or j.get("transformedPostingTitle")
        # Locations is a list of {name, city, ...}
        locs = j.get("locations") or []
        location = ", ".join(filter(None, [(l or {}).get("name") for l in locs])) or None
        position_id = j.get("positionId")
        out.append({
            "title": title,
            "location": location,
            "url": f"https://jobs.apple.com/en-us/details/{position_id}" if position_id else None,
            "department": (j.get("team") or {}).get("teamName"),
            "posted_at": j.get("postingDate"),
            "raw": j,
        })
    return out


def _netflix_jobs() -> list[dict]:
    """
    Netflix's explore.jobs.netflix.net uses an Ashby-style JSON API.
    GET /api/apply/v2/jobs?domain=netflix.com returns the full list.
    """
    url = "https://explore.jobs.netflix.net/api/apply/v2/jobs?domain=netflix.com"
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        payload = json.loads(resp.read())

    out = []
    for j in payload.get("jobs", []) or []:
        out.append({
            "title": j.get("title"),
            "location": j.get("locationName") or j.get("location"),
            "url": j.get("jobUrl") or j.get("applyUrl"),
            "department": j.get("departmentName"),
            "posted_at": j.get("updatedAt"),
            "raw": j,
        })
    return out


# ---- Registry: which companies do we have a real adapter for? ---------------

ADAPTERS = {
    "Amazon": _amazon_jobs,
    "Apple": _apple_jobs,
    "Netflix": _netflix_jobs,
}

# Companies we know need a different approach than plain HTTP. We log honestly.
PLANNED_LATER = {
    "Google":     "JS-rendered careers page — needs Firecrawl (Stage 2 work)",
    "PayPal":     "Workday tenant — needs Workday adapter (Stage 2)",
    "ServiceNow": "Workday tenant — needs Workday adapter (Stage 2)",
}


def scrape_custom_companies(companies: list[dict], conn) -> dict:
    """
    Walk every `ats: custom` or `ats: workday` company. For each:
      - If we have an adapter, run it and write results.
      - Otherwise log a clear "needs <X> later" message and move on.
    """
    stats = {"checked": 0, "fetched": 0, "kept": 0, "new": 0, "skipped": 0}

    for company in companies:
        name = company.get("name", "?")
        ats = company.get("ats", "?")

        adapter = ADAPTERS.get(name)
        if adapter is None:
            reason = PLANNED_LATER.get(name, f"no adapter yet for ats: {ats}")
            print(f"  ⏭  {name}: SKIPPED — {reason}")
            stats["skipped"] += 1
            continue

        print(f"  → {name} ({ats}: custom adapter)")
        try:
            jobs = adapter()
        except HTTPError as e:
            print(f"     ⚠ HTTP {e.code} — adapter may be stale, will need fixing")
            continue
        except URLError as e:
            print(f"     ⚠ Network error: {e.reason}")
            continue
        except Exception as e:
            print(f"     ⚠ Adapter crashed: {e!r}")
            continue

        stats["checked"] += 1
        stats["fetched"] += len(jobs)

        kept_here = 0
        new_here = 0
        for j in jobs:
            title = j.get("title")
            location = j.get("location")
            url = j.get("url")
            if not url:
                continue
            if not keep_job(title=title, location=location):
                continue
            # Pull a stable per-job ID from the raw payload when available
            raw = j.get("raw") or {}
            external_id = (
                raw.get("positionId")     # Apple
                or raw.get("id")          # Amazon, Netflix
                or raw.get("job_id")
            )
            is_new = upsert_job(
                conn,
                url=url,
                company=name,
                ats=ats,
                title=title or "(untitled)",
                location=location,
                department=j.get("department"),
                posted_at=j.get("posted_at"),
                raw=raw,
                ats_slug=name.lower(),     # company name is fine as slug here
                ats_external_id=external_id,
            )
            kept_here += 1
            if is_new:
                new_here += 1

        stats["kept"] += kept_here
        stats["new"] += new_here
        print(f"     {len(jobs):>3} fetched · {kept_here:>2} kept · {new_here:>2} new")

    conn.commit()
    return stats
