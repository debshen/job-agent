# Layer 01 — Discover

**What this layer does:** finds *new* Bay Area companies worth adding to your watchlist that you'd never have thought to track yourself, scores them deterministically against your `criteria.yaml`, and either auto-adds the high scorers to `config/watchlist.yaml` or queues medium scorers in a daily JSON for you to review.

**Status:** ✅ Built (free MVP). Walks a curated discovery pool, probes their public Greenhouse / Lever boards for active PM hiring, writes results to `data/discovery/YYYY-MM-DD.json`, and (unless `--dry-run`) appends winners to `watchlist.yaml` directly.

---

## How it works (the 5-second version)

1. Load `config/discovery_pool.yaml` (the universe of candidate companies — currently ~32 hand-curated entries).
2. Drop any that are already on `watchlist.yaml`, in an excluded sector, not Bay Area, or below the headcount floor.
3. For each survivor, hit their public Greenhouse / Lever board (free, no API key) and count how many open PM roles they have right now.
4. Compute a deterministic 0–100 score (see formula below).
5. Route by score:
   - `≥ auto_add_min_score` (default 80) → appended to `watchlist.yaml` automatically.
   - `≥ queue_min_score` (default 50) → written to today's JSON for you to review and promote later by hand.
   - `< queue_min_score` → silently dropped (still in the JSON for full transparency).

That's the entire layer. It's intentionally cheaper than Layer 02 because it answers a cheaper question: *"is this company worth bothering with?"* not *"is this exact job a fit?"*

---

## What's in this folder

| File | What it does | Run directly? |
|---|---|---|
| `run_discover.py` | **The one you actually run.** Orchestrates the whole layer, prints a summary, exits 0/1/2. | Yes |
| `pool_loader.py` | Reads `discovery_pool.yaml` + `watchlist.yaml` + `criteria.yaml`'s `discovery:` block. Pre-filters obvious dupes / wrong sectors / too small. | No (helper) |
| `probe.py` | Hits each candidate's public Greenhouse / Lever board, counts PM roles in Bay Area / remote. Reuses Layer 03's `filters.py` so "PM role" means the same thing across the agent. | No (helper) |
| `score.py` | Pure-function 0–100 score (no LLM, no network). | No (helper) |
| `output.py` | Writes the daily JSON; appends winners to `watchlist.yaml` without losing existing comments. | No (helper) |
| `test_discover.py` | Mocked-network end-to-end test of the whole pipeline (pool filtering → scoring → cap → JSON → watchlist append → dedupe). | Yes |

---

## How to run it

```bash
cd "/path/to/job-agent"
python3 src/01_discover/run_discover.py
```

**First run only**, install the one Python dependency (you already have it if you've run Layer 02):

```bash
pip3 install --user pyyaml
```

(If PyYAML isn't installed, the script falls back to a built-in mini parser — same pattern as Layer 03.)

You'll see a per-company line like:

```
 [ 1/29] Brex (greenhouse: brex)
       ✅ score: 100/100 — Bay Area HQ · ~1100 employees · 5 BA / 7 total PM roles  [auto_add]
```

Then a summary box at the end with totals plus the JSON path.

### Useful flags

```bash
# Probe + score, but DON'T modify watchlist.yaml. Use this the first few runs.
python3 src/01_discover/run_discover.py --dry-run

# Only probe the first 5 candidates. Useful when you've added new pool entries
# and want to test fast before doing a full ~30-company run.
python3 src/01_discover/run_discover.py --limit 5

# Skip writing the JSON (printing-only).
python3 src/01_discover/run_discover.py --no-json
```

### Where this fits in the nightly loop

Conceptually Layer 01 should run **before** Layer 03 so newly auto-added companies get scraped the same night. Recommended cron order:

```bash
python3 src/01_discover/run_discover.py     # 0. find new companies
python3 src/03_scrape/run_all.py            # 1. scrape jobs (incl. new companies)
python3 src/02_score/run_score.py           # 2. score the new jobs
python3 src/05_act/digest.py                # 3. email yourself the digest
```

Running Layer 01 weekly or monthly is fine too — the pool changes slowly.

---

## How the score works

The 0–100 is a pure function of four things — no LLM, no API calls, no surprises:

```
+40  HQ confidence       — pool entry has hq_bay_area: true
+30  Headcount confidence — pool entry has employees_est >= criteria.must_have.company_size.min_employees
+20  PM activity          — probe found ≥ 1 PM role in Bay Area / remote / hybrid
+10  Hiring momentum      — probe found ≥ 3 PM roles in Bay Area / remote / hybrid
─────
 100  Maximum
```

Because pool_loader already drops anyone failing the HQ or headcount checks, every candidate that reaches scoring starts at **70/100**. The remaining 30 points are earned by **actual Bay Area PM hiring activity** — which is exactly the signal we care about.

Two important consequences of how the bonuses are gated:

- A company with 4 PM roles all in Dublin scores the same 70 as a company with 0 PMs open. Both end up in the review queue rather than auto-added — which is correct, because Layer 03 wouldn't surface useful jobs from either one anyway.
- `ats: custom` entries (companies with their own careers site, like DoorDash, Notion, Zoom, Apple) skip the probe entirely and **always score 70**. They surface in the review queue with a "needs manual research" rationale, so you can decide whether to hand-add them to `watchlist.yaml` (where they'll need a custom adapter in `src/03_scrape/custom_html.py` to actually scrape).

The formula constants live at the top of `score.py` and the routing thresholds live in `criteria.yaml → discovery:`. Edit either and the next run picks it up.

---

## Tuning thresholds

In `config/criteria.yaml`:

```yaml
discovery:
  auto_add_min_score: 80       # raise to be more conservative
  queue_min_score: 50          # raise to surface fewer candidates in the JSON
  max_auto_adds_per_run: 5     # safety net so a buggy run can't flood the watchlist
  sectors_excluded:
    - example_sector
```

After editing, just re-run discovery — no code change needed.

---

## Adding companies to the pool

Open `config/discovery_pool.yaml`, copy a block, fill in the fields, save. Next run picks it up:

```yaml
  - name: NewCompany
    ats: greenhouse                 # or "lever" (auto-probed) or "custom" (skipped)
    ats_slug: newcompany            # the slug in their public careers URL; null for custom
    hq_bay_area: true               # San Francisco, Peninsula, South Bay, East Bay
    employees_est: 1500             # rough headcount; conservative is fine
    sector: enterprise_saas         # one of the criteria.yaml preferred/excluded sectors
    notes: Seen on HN Who's Hiring 2026-04
```

If the slug is wrong the probe prints `HTTP 404 — check slug` and skips that company without crashing the run. If the company runs its own careers site (no Greenhouse/Lever board), set `ats: custom` and `ats_slug: null` — the entry will skip probing and land in the review queue at 70/100 so you can choose whether to manually add it to `watchlist.yaml`.

To temporarily disable an entry without deleting it, add `enabled: false`.

---

## Where the data goes

- `data/discovery/YYYY-MM-DD.json` — full per-company report from each run, sorted by score.
- `config/watchlist.yaml` — auto-appended in a clearly-labelled `# AUTO-DISCOVERED (date)` block, just before the `exclude:` section. Existing entries and comments are never touched.

The JSON shape is:

```json
{
  "summary": {
    "date": "2026-05-03",
    "candidates_evaluated": 29,
    "auto_added": 4,
    "queued_for_review": 12,
    "dropped": 11,
    "probe_failed": 2,
    "thresholds": { "auto_add_min_score": 80, "queue_min_score": 50, "max_auto_adds_per_run": 5 }
  },
  "results": [
    {
      "name": "Brex", "ats": "greenhouse", "ats_slug": "brex",
      "sector": "fintech", "employees_est": 1100,
      "score": 100,
      "breakdown": { "hq": 40, "headcount": 30, "pm_activity": 20, "hiring_momentum": 10 },
      "rationale": "Bay Area HQ · ~1100 employees · 5 BA / 7 total PM roles",
      "action": "auto_add",
      "probe": { "total_jobs": 84, "pm_total": 7, "pm_bay_area": 5,
                 "sample_titles": ["Senior PM, Risk", "Staff PM, Spend", "Lead PM, API"] }
    }
  ]
}
```

---

## What's tested

`test_discover.py` is a mocked-network end-to-end test. It builds a throwaway temp project (its own `criteria.yaml`, `watchlist.yaml`, `discovery_pool.yaml`), runs the entire pipeline against canned probe responses, and checks:

- Pre-filter drops dupes, excluded sectors, not-Bay-Area, too-small.
- Score formula is correct at every threshold (0, 70, 90, 100).
- Routing assigns `auto_add` / `review` / `drop` / `probe_failed` correctly.
- `max_auto_adds_per_run` cap actually caps and reclassifies the rest as `review`.
- JSON output is sorted by score and the summary counts add up.
- `watchlist.yaml` append preserves all existing comments, inserts before the `exclude:` block, and **dedupes on a second run** (no double-appending).

Run it with:

```bash
python3 src/01_discover/test_discover.py
```

Should finish in well under a second and print `✅ All Layer 01 checks passed.`

---

## Known limits (and what we'll fix later)

- **Hand-curated pool.** Stage-1 reality: there's no free API that lists every Bay Area 500+ tech company. Until we wire up a paid source, the pool is a YAML file you (or future Claude sessions) extend by hand. Stage-2 upgrade: Crunchbase API ($30/mo) auto-grows the pool.
- **Two ATSes auto-probed.** MVP supports Greenhouse + Lever (the two most common in Bay Area tech and the only two with truly free public APIs). Companies whose careers page is custom or Workday can be added with `ats: custom` so they show up in the review queue at 70/100, but the actual scraping (in Layer 03) needs a per-company adapter — same situation as the Apple/Amazon/Netflix custom adapters that already exist in `src/03_scrape/custom_html.py`.
- **No LLM in the score.** By design for the MVP — the score is fully deterministic so you can audit every number. When/if you want a smarter Layer 01, swap `score.py` for one that calls Haiku with the same `score_company()` signature.
- **Headcount is from the pool, not live data.** A company that 4× their headcount in 2026 still gets the `employees_est` from the YAML until you update it. Stage-2 upgrade with Crunchbase fixes this.
- **Won't catch genuinely brand-new companies.** Discovery only finds companies you've put in the pool. The "find ones I'd never have thought to track" promise from the original spec is a Stage-2 feature that needs Crunchbase or a hiring-signals service like Apify.

---

## What's next

- **Run it nightly** — `scripts/install_schedule.sh` already wires up the cron for Layers 02/03/05; add a line for `run_discover.py` so newly-auto-added companies get scraped the same night.
- **Stage 2: Crunchbase integration** — replace the hand-curated pool with a daily Crunchbase query. Same orchestrator, swap out `pool_loader.py`.
- **Stage 2: Apify hiring signals** — add an extra +10 momentum band for companies with sudden PM hiring spikes (vs. baseline).
- **Optional: smarter score** — drop in a Haiku call inside `score.py` to read each company's "About" page and score sector fit + AI moat.
