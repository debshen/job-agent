"""
workday.py — Workday scraper for Layer 03 (added 2026-09-24).

WHAT THIS DOES (in plain English):
  Many big companies (Salesforce, and others) host their careers site on
  Workday ("something.myworkdayjobs.com"). The page itself is rendered with
  JavaScript, but behind it sits a public JSON endpoint the page calls. We
  call that same endpoint directly, so no browser is needed.

HOW TO ADD A WORKDAY COMPANY TO watchlist.yaml:
  Look at the company's careers URL, e.g.
      https://salesforce.wd12.myworkdayjobs.com/en-US/External_Career_Site
  and fill in:
      ats: workday
      ats_slug: salesforce                          # the tenant (first word of the host)
      workday_host: salesforce.wd12.myworkdayjobs.com
      workday_site: External_Career_Site            # the path segment after /en-US/
      workday_search: product manager               # optional; defaults to "product manager"
  Entries with ats: workday but NO workday_host still go to the old
  best-effort path (custom_html.py), which logs and skips them.

THE WORKDAY API:
  List:    POST https://{host}/wday/cxs/{tenant}/{site}/jobs
           body {"appliedFacets": {}, "limit": 20, "offset": N, "searchText": "..."}
           → {"total": 34, "jobPostings": [{"title", "externalPath", "locationsText", ...}]}
  Detail:  GET  https://{host}/wday/cxs/{tenant}/{site}{externalPath}
           → {"jobPostingInfo": {"title", "location", "additionalLocations",
                                 "remoteType", "jobDescription", "externalUrl",
                                 "startDate", "jobReqId", ...}}
  The list only says "3 Locations" for multi-location jobs, so for jobs whose
  TITLE looks like a PM role we fetch the detail to get real locations (and
  the full description, which the JD fetcher then reuses).
"""

from __future__ import annotations

import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from db import upsert_job
from filters import keep_job, location_is_acceptable, looks_like_pm_role


def _is_ic_director_pm(title: str | None) -> bool:
    """'Director of Product Management' / 'Product Management Director', but
    not Senior Director or VP. Only used for companies flagged with
    include_ic_director: true (e.g. Salesforce, where these are usually IC)."""
    t = (title or "").lower()
    return (
        "director" in t
        and "product management" in t
        and "senior director" not in t
        and "sr. director" not in t
        and "vp" not in t
        and "vice president" not in t
    )

USER_AGENT = "JobAgent/0.1 (+https://github.com/debshen)"
TIMEOUT_SECONDS = 20
PAGE_SIZE = 20
MAX_POSTINGS = 400          # safety cap per company per run
DETAIL_PAUSE_SECONDS = 0.3  # be polite between detail calls
DEFAULT_SEARCH = "product manager"


def _request_json(url: str, *, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read())


def _base(host: str, tenant: str, site: str) -> str:
    return f"https://{host}/wday/cxs/{tenant}/{site}"


def _list_postings(host: str, tenant: str, site: str, search: str) -> list[dict]:
    """Page through the search results. Returns [] on any network error."""
    out: list[dict] = []
    offset = 0
    total = None
    while offset < MAX_POSTINGS:
        body = {"appliedFacets": {}, "limit": PAGE_SIZE, "offset": offset, "searchText": search}
        try:
            payload = _request_json(_base(host, tenant, site) + "/jobs", body=body)
        except HTTPError as e:
            print(f"    ⚠ HTTP {e.code} from Workday list for '{tenant}'")
            break
        except URLError as e:
            print(f"    ⚠ Network error listing Workday '{tenant}': {e.reason}")
            break
        except Exception as e:  # pragma: no cover
            print(f"    ⚠ Unexpected error listing Workday '{tenant}': {e!r}")
            break
        if total is None:
            total = payload.get("total") or 0
        page = payload.get("jobPostings") or []
        out.extend(page)
        offset += PAGE_SIZE
        if not page or len(page) < PAGE_SIZE or len(out) >= total:
            break
    return out


def _fetch_detail(host: str, tenant: str, site: str, external_path: str) -> dict | None:
    try:
        payload = _request_json(_base(host, tenant, site) + external_path)
        return payload.get("jobPostingInfo") or None
    except Exception as e:
        print(f"    ⚠ Could not fetch Workday detail {external_path}: {e!r}")
        return None


def _location_string(info: dict) -> str:
    locs = [info.get("location")] + list(info.get("additionalLocations") or [])
    text = "; ".join(l for l in locs if l)
    if info.get("remoteType"):
        text = f"{text} ({info['remoteType']})" if text else info["remoteType"]
    return text


def scrape_workday_companies(companies: list[dict], conn) -> dict:
    """Scrape every Workday company that has workday_host set. Same stats shape
    as the Greenhouse and Lever scrapers."""
    stats = {"checked": 0, "fetched": 0, "kept": 0, "new": 0}

    for company in companies:
        name = company.get("name", "?")
        host = company.get("workday_host")
        tenant = company.get("ats_slug") or (host.split(".")[0] if host else None)
        site = company.get("workday_site")
        search = company.get("workday_search") or DEFAULT_SEARCH
        ic_director = bool(company.get("include_ic_director"))
        if not (host and tenant and site):
            print(f"  ✗ {name}: needs workday_host, ats_slug, and workday_site — skipping")
            continue

        print(f"  → {name} (workday: {host}/{site}, search '{search}')")
        postings = _list_postings(host, tenant, site, search)
        stats["checked"] += 1
        stats["fetched"] += len(postings)

        kept_here = new_here = 0
        for p in postings:
            title = p.get("title")
            path = p.get("externalPath")
            is_director = ic_director and _is_ic_director_pm(title)
            if not path or not (looks_like_pm_role(title) or is_director):
                continue  # cheap title check before the detail call

            info = _fetch_detail(host, tenant, site, path)
            time.sleep(DETAIL_PAUSE_SECONDS)
            if info is None:
                location = p.get("locationsText")
                info = {}
            else:
                location = _location_string(info)

            if is_director:
                if not location_is_acceptable(location):
                    continue
            elif not keep_job(title=title, location=location):
                continue

            url = info.get("externalUrl") or f"https://{host}/en-US/{site}{path}"
            raw = {
                "title": title,
                "externalPath": path,
                "jobReqId": info.get("jobReqId"),
                "location": location,
                "remoteType": info.get("remoteType"),
                "startDate": info.get("startDate"),
                "jobDescription": info.get("jobDescription"),
            }
            is_new = upsert_job(
                conn,
                url=url,
                company=name,
                ats="workday",
                title=title or "(untitled)",
                location=location,
                department=None,
                posted_at=info.get("startDate"),
                raw=raw,
                ats_slug=tenant,
                ats_external_id=info.get("jobReqId") or path,
            )
            kept_here += 1
            if is_new:
                new_here += 1

        stats["kept"] += kept_here
        stats["new"] += new_here
        print(f"     {len(postings):>3} fetched · {kept_here:>2} kept · {new_here:>2} new")

    conn.commit()
    return stats
