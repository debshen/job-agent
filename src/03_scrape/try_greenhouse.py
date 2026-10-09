"""
try_greenhouse.py — Smoke test for the Greenhouse public job board API.

WHAT THIS DOES (in plain English):
  Hits the Greenhouse careers API for two of your watchlist companies (Stripe
  and Anthropic), pulls down their open job postings, filters to roles that
  look like Product Manager positions, and prints the top hits to your terminal.

WHY THIS EXISTS:
  This is the smallest possible proof that Layer 03 (Scrape) works. If this
  script prints real PM jobs when you run it, you know we can build the full
  scraper on top of the same approach — just with more companies, filtering,
  and storage to a database.

HOW TO RUN IT:
  Open Terminal, then:
    cd "/path/to/job-agent"
    python3 src/03_scrape/try_greenhouse.py

  Expected: a list of PM-flavored job titles printed to your screen.

JARGON DEFINED INLINE:
  - "API endpoint" = a specific URL that returns data instead of a webpage
  - "JSON"         = a structured text format APIs return data in
  - "requests"     = the Python library we use to hit URLs
"""

import json
import sys
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# ---- Configuration -----------------------------------------------------------

# These are the company "slugs" Greenhouse uses in their URL.
# You can see one in your browser by visiting boards.greenhouse.io/stripe
COMPANIES_TO_TRY = ["stripe", "anthropic"]

# Words we look for in job titles to identify PM roles.
# (Layer 02 will do real scoring; this is just a quick keyword filter.)
PM_KEYWORDS = ["product manager", " pm ", "product lead"]

# Words that disqualify a role even if "product manager" appears.
# (We don't want APMs, technical PMs, directors, etc. — matches criteria.yaml)
PM_EXCLUDE = ["associate", "apm", "technical product", "director", "vp ", "head of"]


# ---- Helpers -----------------------------------------------------------------

def fetch_greenhouse_jobs(company_slug: str) -> list[dict]:
    """
    Hit the Greenhouse public API for one company and return its job list.

    The API URL pattern is:
      https://boards-api.greenhouse.io/v1/boards/{slug}/jobs

    No authentication required — this is a fully public endpoint.
    """
    url = f"https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs"
    request = Request(url, headers={"User-Agent": "JobAgent/0.1 (smoke-test)"})

    try:
        with urlopen(request, timeout=15) as response:
            data = json.loads(response.read())
            return data.get("jobs", [])
    except HTTPError as err:
        print(f"  ⚠ HTTP {err.code} from Greenhouse for '{company_slug}'")
        return []
    except URLError as err:
        print(f"  ⚠ Could not reach Greenhouse for '{company_slug}': {err.reason}")
        return []


def looks_like_pm_role(title: str) -> bool:
    """Cheap keyword match — Layer 02 will do the real scoring later."""
    t = title.lower()
    if not any(keyword in t for keyword in PM_KEYWORDS):
        return False
    if any(bad in t for bad in PM_EXCLUDE):
        return False
    return True


# ---- Main --------------------------------------------------------------------

def main() -> int:
    """Walk each company, print PM-looking jobs. Returns shell exit code."""
    grand_total = 0
    pm_total = 0

    for company in COMPANIES_TO_TRY:
        print(f"\n=== {company.upper()} ===")
        jobs = fetch_greenhouse_jobs(company)
        if not jobs:
            print("  (no jobs returned)")
            continue

        grand_total += len(jobs)
        pm_jobs = [j for j in jobs if looks_like_pm_role(j.get("title", ""))]
        pm_total += len(pm_jobs)

        print(f"  {len(jobs)} total openings, {len(pm_jobs)} PM-flavored")

        for job in pm_jobs[:10]:    # cap at 10 per company so output stays readable
            location = job.get("location", {}).get("name", "Unknown location")
            title = job.get("title", "Untitled")
            url = job.get("absolute_url", "")
            print(f"    • {title}  —  {location}")
            print(f"      {url}")

    print(f"\n--- Summary: {pm_total} PM roles across {grand_total} total openings ---")

    if pm_total == 0:
        print("\nHmm, zero PM hits. Possible reasons:")
        print("  1. Greenhouse changed slugs (check boards.greenhouse.io/<slug> in a browser)")
        print("  2. The companies aren't hiring PMs right now")
        print("  3. Network issue (try again in a minute)")
        return 1

    print("\n✅ Layer 03 smoke test passed. Greenhouse API is reachable and returning real jobs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
