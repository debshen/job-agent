"""
probe.py — Hits each candidate company's public ATS board to count open PM
roles. This is where Layer 01 actually goes to the network.

WHAT THIS DOES (in plain English):
  For one company, fetch their Greenhouse or Lever board (free, no API key)
  and return three numbers:
    - total_jobs    : every open job at the company right now
    - pm_total      : how many of those are PM-flavored (uses Layer 03's
                      shared filters.looks_like_pm_role so we get the same
                      definition as the rest of the agent)
    - pm_bay_area   : how many of THOSE are in Bay Area / remote / hybrid

  Those three numbers feed the score formula in score.py.

WHY WE REUSE LAYER 03's FILTER MODULE:
  If "PM role" means one thing during scrape and another during discovery,
  we'd discover companies whose jobs Layer 03 then drops — wasted work and
  confusing watchlist additions. So discovery uses the EXACT same filter.

ERROR HANDLING:
  Like Layer 03, a single broken slug or a network blip should never crash
  the whole run. Returns (0, 0, 0) and prints a one-line warning.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Reuse Layer 03's shared filter module so "is this a PM role?" is defined in
# exactly one place across the whole project.
SCRAPE_DIR = Path(__file__).resolve().parents[1] / "03_scrape"
if str(SCRAPE_DIR) not in sys.path:
    sys.path.insert(0, str(SCRAPE_DIR))

from filters import looks_like_pm_role, location_is_acceptable  # type: ignore

USER_AGENT = "JobAgent/0.1 (+https://github.com/debshen)"
TIMEOUT_SECONDS = 20


def probe_company(company: dict) -> dict:
    """
    Fetch this company's ATS board and count PM roles.

    Returns a stats dict the score module turns into a 0–100:
      {
        "ok": True,                         # False if the probe failed entirely
        "total_jobs":      <int>,            # all open jobs at the company
        "pm_total":        <int>,            # PM roles anywhere
        "pm_bay_area":     <int>,            # PM roles in Bay Area / remote / hybrid
        "sample_titles":   [str, ...],       # first 3 PM titles (for the JSON output)
        "error":           None or str,      # short reason if not ok
      }
    """
    ats = (company.get("ats") or "").lower()
    slug = company.get("ats_slug")
    name = company.get("name", "?")

    if ats == "custom":
        # Companies hosted on their own careers site (e.g. DoorDash, Notion,
        # Zoom, Apple): we have no free way to probe these. We deliberately
        # return ok=True with all-zero counts so the company still gets
        # scored on the metadata we DO know (HQ + headcount = 70/100) and
        # surfaces in the review queue with a "needs manual research" rationale.
        return {
            "ok": True,
            "total_jobs":   0,
            "pm_total":     0,
            "pm_bay_area":  0,
            "sample_titles": [],
            "error": None,
            "note": "custom careers site — not auto-probed",
        }

    if ats == "greenhouse":
        jobs, err = _fetch_greenhouse(slug)
    elif ats == "lever":
        jobs, err = _fetch_lever(slug)
    else:
        return _empty(error=f"unsupported ats: {ats}")

    if err is not None:
        return _empty(error=err)

    pm_total = 0
    pm_bay_area = 0
    sample_titles: list[str] = []

    for job in jobs:
        title, location = _extract_title_location(ats, job)
        if not looks_like_pm_role(title):
            continue
        pm_total += 1
        if location_is_acceptable(location):
            pm_bay_area += 1
        if len(sample_titles) < 3 and title:
            sample_titles.append(title)

    return {
        "ok": True,
        "total_jobs": len(jobs),
        "pm_total": pm_total,
        "pm_bay_area": pm_bay_area,
        "sample_titles": sample_titles,
        "error": None,
    }


# -----------------------------------------------------------------------------
# Per-ATS fetchers — same shape as Layer 03's, but without the database write.
# -----------------------------------------------------------------------------

def _fetch_greenhouse(slug: str) -> tuple[list[dict], str | None]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false"
    return _http_get_json(url, top_key="jobs")


def _fetch_lever(slug: str) -> tuple[list[dict], str | None]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    # Lever returns a top-level array, not a dict.
    payload, err = _http_get_raw(url)
    if err is not None:
        return [], err
    if isinstance(payload, list):
        return payload, None
    return [], "unexpected response shape (expected JSON array)"


def _http_get_raw(url: str):
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read()), None
    except HTTPError as e:
        return None, f"HTTP {e.code}"
    except URLError as e:
        return None, f"network error: {e.reason}"
    except Exception as e:  # pragma: no cover
        return None, f"unexpected: {e!r}"


def _http_get_json(url: str, *, top_key: str):
    payload, err = _http_get_raw(url)
    if err is not None:
        return [], err
    if isinstance(payload, dict):
        items = payload.get(top_key) or []
        if isinstance(items, list):
            return items, None
    return [], "unexpected response shape"


# -----------------------------------------------------------------------------
# Per-ATS field extraction — Greenhouse and Lever shape jobs differently.
# -----------------------------------------------------------------------------

def _extract_title_location(ats: str, job: dict) -> tuple[str | None, str | None]:
    if ats == "greenhouse":
        title = job.get("title")
        location = (job.get("location") or {}).get("name")
        return title, location
    if ats == "lever":
        title = job.get("text")
        categories = job.get("categories") or {}
        return title, categories.get("location")
    return None, None


def _empty(*, error: str) -> dict:
    return {
        "ok": False,
        "total_jobs": 0,
        "pm_total": 0,
        "pm_bay_area": 0,
        "sample_titles": [],
        "error": error,
    }
