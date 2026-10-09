"""
jd_fetcher.py — Fetches the full job description text for a row in jobs.sqlite.

WHY THIS EXISTS:
  Layer 03 stores enough about each job to dedupe and triage by title/location,
  but for actual scoring we need the full job description (responsibilities,
  qualifications, comp range, work model, etc.). That text lives in different
  places depending on the source:

    - Greenhouse: the listing endpoint we hit (with content=false) doesn't
      include the JD. We re-hit the SINGLE-job endpoint with content=true.
    - Lever:      the description IS in the row's raw_json under "descriptionPlain"
      or "description" — no extra fetch needed.
    - Apple/Amazon/Netflix: their listing payloads include enough description
      text. We pull it from raw_json and clean up HTML.
    - Workday/custom (skipped at scrape time): no JD available — return None.

DESIGN:
  fetch_jd(row) returns a (text, source) tuple, or (None, reason) if it can't
  get one. The caller writes both the text and a fetched-at timestamp to the
  jobs row so we never re-fetch the same JD twice.
"""

from __future__ import annotations

import json
import re
import time
from html import unescape
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

USER_AGENT = "JobAgent/0.1 (+https://github.com/debshen)"
TIMEOUT_SECONDS = 25

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(html: str) -> str:
    """Remove HTML tags + collapse whitespace + decode entities."""
    if not html:
        return ""
    text = unescape(_HTML_TAG_RE.sub(" ", html))
    return _WS_RE.sub(" ", text).strip()


# ---- per-source fetchers -----------------------------------------------------

def _fetch_greenhouse_jd(row) -> tuple[str | None, str]:
    """
    Greenhouse exposes a single-job endpoint that includes the full description:
        https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{id}?content=true

    Slug + job ID come from the dedicated columns we set at scrape time.
    Falls back to URL parsing + raw_json for older rows that pre-date those
    columns, so existing data keeps working.
    """
    # Preferred path: use the columns the scraper writes.
    slug = _get_col(row, "ats_slug")
    job_id = _get_col(row, "ats_external_id")

    # Fallback for rows scraped before we added the columns
    if not (slug and job_id):
        try:
            raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
        except (TypeError, json.JSONDecodeError):
            raw = {}
        if not job_id:
            job_id = raw.get("id")
        if not slug:
            url = row["url"] or ""
            m = re.search(r"greenhouse\.io/([^/]+)/jobs/", url)
            slug = m.group(1) if m else None

    if not (job_id and slug):
        return None, "no slug/id available (need backfill)"

    api = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}?content=true"
    req = Request(api, headers={"User-Agent": USER_AGENT})
    # Retry transient network blips (timeouts, connection resets, 5xx). A reset
    # is a plain OSError raised while READING the response, so it slips past a
    # bare (HTTPError, URLError) catch — we must catch OSError too. 4xx errors
    # (bad slug/id) are permanent, so we return immediately without retrying.
    last_err = None
    for attempt in range(3):
        try:
            with urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                payload = json.loads(resp.read())
            text = _strip_html(payload.get("content") or "")
            return (text or None, "greenhouse single-job endpoint")
        except HTTPError as e:
            if 400 <= e.code < 500:
                return None, f"greenhouse fetch failed: {e}"
            last_err = e  # 5xx — retry
        except (URLError, OSError) as e:
            last_err = e  # timeout / connection reset — retry
        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))
    return None, f"greenhouse fetch failed after retries: {last_err}"


def _get_col(row, name: str):
    """Safely read a column that might not exist on older sqlite Row objects."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def _fetch_lever_jd(row) -> tuple[str | None, str]:
    """Lever already has the description in raw_json from the scrape pass."""
    try:
        raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
    except (TypeError, json.JSONDecodeError):
        return None, "raw_json unparseable"

    # Lever fields: descriptionPlain (preferred), description (HTML),
    # plus an array `lists` of section objects with text fields.
    parts: list[str] = []
    if raw.get("descriptionPlain"):
        parts.append(raw["descriptionPlain"])
    elif raw.get("description"):
        parts.append(_strip_html(raw["description"]))
    for section in raw.get("lists") or []:
        if section.get("text"):
            parts.append(_strip_html(section["text"]))
    if raw.get("additionalPlain"):
        parts.append(raw["additionalPlain"])
    elif raw.get("additional"):
        parts.append(_strip_html(raw["additional"]))

    text = "\n\n".join(p for p in parts if p).strip()
    return (text or None, "lever raw_json")


def _fetch_apple_jd(row) -> tuple[str | None, str]:
    """Apple's search payload contains enough description for triage/scoring."""
    try:
        raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
    except (TypeError, json.JSONDecodeError):
        return None, "raw_json unparseable"
    # Fields seen in the wild: jobSummary, transformedJobSummary, keyQualifications,
    # description, minimumQualifications, etc.
    parts = [
        raw.get("jobSummary"),
        raw.get("description"),
        raw.get("keyQualifications"),
        raw.get("minimumQualifications"),
        raw.get("preferredQualifications"),
    ]
    text = "\n\n".join(_strip_html(p) for p in parts if p).strip()
    return (text or None, "apple raw_json")


def _fetch_amazon_jd(row) -> tuple[str | None, str]:
    """Amazon's search.json includes description / basic_qualifications."""
    try:
        raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
    except (TypeError, json.JSONDecodeError):
        return None, "raw_json unparseable"
    parts = [
        raw.get("description"),
        raw.get("description_short"),
        raw.get("basic_qualifications"),
        raw.get("preferred_qualifications"),
    ]
    text = "\n\n".join(_strip_html(p) for p in parts if p).strip()
    return (text or None, "amazon raw_json")


def _fetch_netflix_jd(row) -> tuple[str | None, str]:
    """Netflix Ashby payload includes description + bullets."""
    try:
        raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
    except (TypeError, json.JSONDecodeError):
        return None, "raw_json unparseable"
    parts = [raw.get("descriptionPlain"), raw.get("description")]
    text = "\n\n".join(_strip_html(p) for p in parts if p).strip()
    return (text or None, "netflix raw_json")


def _fetch_workday_jd(row) -> tuple[str | None, str]:
    """Workday scraper stores the full jobDescription HTML in raw_json."""
    try:
        raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
    except (TypeError, json.JSONDecodeError):
        return None, "raw_json unparseable"
    text = _strip_html(raw.get("jobDescription") or "").strip()
    return (text or None, "workday raw_json")


def _fetch_atlassian_jd(row) -> tuple[str | None, str]:
    """Atlassian scraper stores overview/responsibilities/qualifications HTML in raw_json."""
    try:
        raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
    except (TypeError, json.JSONDecodeError):
        return None, "raw_json unparseable"
    parts = [raw.get("overview"), raw.get("responsibilities"), raw.get("qualifications")]
    text = "\n\n".join(_strip_html(p) for p in parts if p).strip()
    return (text or None, "atlassian raw_json")


def _fetch_icims_jd(row) -> tuple[str | None, str]:
    """iCIMS scraper stores the full description HTML/text in raw_json."""
    try:
        raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
    except (TypeError, json.JSONDecodeError):
        return None, "raw_json unparseable"
    parts = [raw.get("description")] if raw.get("description") else [raw.get("responsibilities"), raw.get("qualifications")]
    text = "\n\n".join(_strip_html(p) for p in parts if p).strip()
    return (text or None, "icims raw_json")


# ---- dispatch ----------------------------------------------------------------

_FETCHERS_BY_ATS = {
    "greenhouse": _fetch_greenhouse_jd,
    "lever": _fetch_lever_jd,
    "workday": _fetch_workday_jd,
    "atlassian": _fetch_atlassian_jd,
    "icims": _fetch_icims_jd,
}

_FETCHERS_BY_COMPANY = {
    "Apple": _fetch_apple_jd,
    "Amazon": _fetch_amazon_jd,
    "Netflix": _fetch_netflix_jd,
}


def fetch_jd(row) -> tuple[str | None, str]:
    """
    Look up the right fetcher for this row and run it.

    Returns (jd_text, source_label). On failure, returns (None, reason).
    """
    company = row["company"]
    ats = (row["ats"] or "").lower()

    fetcher = _FETCHERS_BY_COMPANY.get(company) or _FETCHERS_BY_ATS.get(ats)
    if fetcher is None:
        return None, f"no JD fetcher for ats={ats} / company={company}"
    return fetcher(row)
