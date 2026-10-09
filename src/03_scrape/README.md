# Layer 03 — Scrape

**What this layer does:** pulls every PM-flavored job posting from each company on your watchlist into a single local database (`data/jobs.sqlite`).

**Status:** ✅ Built. Walks the full watchlist, dedupes across runs, ready for nightly cron.

---

## What's in this folder

| File | What it does | Run directly? |
|---|---|---|
| `run_all.py` | **The one you actually run.** Loads watchlist, dispatches to each scraper, prints summary. | Yes |
| `db.py` | SQLite "filing cabinet" — the schema, the `upsert_job()` helper, dedupe by URL. | No (helper) |
| `filters.py` | The shared "is this a PM role in our geography?" rules. Loose by design — Layer 02 does the real scoring. | No (helper) |
| `greenhouse.py` | Walks every `ats: greenhouse` company. | No (helper) |
| `lever.py` | Walks every `ats: lever` company. | No (helper) |
| `custom_html.py` | Best-effort adapters for non-GH/Lever companies (Apple, Amazon, Netflix). Honestly skips Google + any Workday entries that haven't been configured with `workday_host` yet. | No (helper) |
| `workday.py` | Real Workday adapter (added 2026-09-24, tenants configured 2026-09-28). Hits each tenant's public `wday/cxs/*/jobs` JSON endpoint, paginates, fetches per-job detail for real location + full JD text. | No (helper) |
| `icims.py` | iCIMS careers-site adapter. Calls the public `/api/jobs` feed behind iCIMS-hosted careers sites; full JD text and posting dates included. | No (helper) |
| `try_greenhouse.py` | Original smoke test. Still works, kept for sanity-check purposes. | Yes |

---

## How to run it

```bash
cd "/path/to/job-agent"
python3 src/03_scrape/run_all.py
```

**First run only**, install the one Python dependency:

```bash
pip3 install --user pyyaml
```

(If `pip3` isn't found: `python3 -m ensurepip --user` then retry.)

If PyYAML isn't installed, the script falls back to a minimal built-in YAML parser so it still runs day one — the install just makes it more robust to future watchlist changes.

You'll see a per-company line like:

```
→ Stripe (greenhouse: stripe)
   102 fetched ·  4 kept ·  4 new
```

…and a summary box at the end listing totals by ATS plus your top companies in the database.

---

## Where the data goes

`data/jobs.sqlite` — one SQLite file at the project root.

To poke at it without writing code:
- **TablePlus** or **DB Browser for SQLite** (both free) → open `data/jobs.sqlite` → see the `jobs` table like a spreadsheet.
- Or from terminal: `sqlite3 data/jobs.sqlite "SELECT company, title, location FROM jobs LIMIT 20;"`

The schema is documented at the top of `db.py`. Key thing to know: **the URL is the dedupe key**, so running the script twice in one night doesn't create duplicate rows — it just bumps the `last_seen` timestamp.

### Override the database path

Set `JOB_AGENT_DB=/some/other/path.sqlite` before running. Useful for cron jobs, GitHub Actions, or testing — no code change needed.

---

## How filtering works (loose mode)

Two questions get asked of every fetched job:

1. **Is the title a PM role?** → contains a PM keyword (`product manager`, `pm`, etc.) AND none of the excludes (`APM`, `TPM`, `Director`, `VP`, `PMM`, `program manager`, `project manager`, etc.).
2. **Is the location plausibly Bay Area or remote?** → mentions a Bay Area city/CA token, OR mentions remote/hybrid, OR is empty/ambiguous (we keep ambiguous ones and let Layer 02 decide).

We chose **loose over strict** so that we never silently drop a real opportunity because the location string was unusual. The real evaluation happens in Layer 02 (Score) using `criteria.yaml`.

---

## ATS coverage cheat sheet

| ATS | Status | Companies in your watchlist |
|---|---|---|
| Greenhouse | ✅ Working | Stripe, Plaid, Block, Affirm, Robinhood, Anthropic, Scale AI, Databricks, Snowflake |
| Lever | ✅ Working | Rippling |
| Apple custom API | ✅ Working | Apple |
| Amazon custom API | ✅ Working | Amazon |
| Netflix Ashby API | ✅ Working | Netflix |
| Workday tenants | ✅ Working | Salesforce, PayPal, ServiceNow (via `workday.py`) |
| Google careers | ⏭ Skipped — needs Firecrawl | Google |

Workday was a Stage 2 skip until 2026-09-28 — the adapter (`workday.py`) is now live and handles any tenant with `workday_host` + `workday_site` set in `watchlist.yaml`. If those fields are missing on a `ats: workday` entry, it still falls through to the best-effort custom path and is skipped with a clear log line, so nothing crashes.

Google remains a genuine Stage 2 gap — no public API, would need Firecrawl (~$20/mo) or a browser-based scraper.

### Adding a new Workday tenant

Open the company's careers URL in a browser. It'll look something like:
`https://salesforce.wd12.myworkdayjobs.com/en-US/External_Career_Site`

From that URL you can read off the three fields we need in `watchlist.yaml`:

```yaml
  - name: NewCompany
    ats: workday
    ats_slug: salesforce                          # the tenant subdomain
    workday_host: salesforce.wd12.myworkdayjobs.com
    workday_site: External_Career_Site            # the path after /en-US/
    workday_search: product manager               # optional; defaults to that
```

If host or site are wrong the first run prints `HTTP 404 from Workday list for '<tenant>'` and skips that company cleanly. Fix the field and re-run.

---

## What's tested

A small integration test at the repo's `/tmp` (re-runnable) verifies:

- 11 filter edge cases (positive matches, every kind of exclude, ambiguous locations)
- Upsert dedupes correctly across two runs
- SQLite reads back what was written

---

## What's next

- **Layer 02 (Score)** — wire Claude up to read each row in `jobs.sqlite` and rate it 0–100 against `criteria.yaml`. That's where this database becomes really valuable.
- **Layer 05 (Act/Notify)** — a `digest.py` that reads the top-scored jobs each morning and emails you a 5-job summary.
- **Stage 2 scraper upgrades** — Firecrawl integration for Google (Workday adapter shipped 2026-09-28).
- **Ashby generalization** — `custom_html.py` handles Netflix's Ashby feed as a one-off. Turning that into a general Ashby adapter unlocks Notion, Linear, Vercel, Ramp, Retool, and other AI-native startups.
- **Workday tenant expansion** — the adapter now supports arbitrary Workday tenants. Big enterprise fits worth adding: Adobe, Autodesk, Cisco, Visa, Wells Fargo (all Workday).
- **Schedule it** — see `scripts/README.md` for the nightly launchd setup and cloud options.
