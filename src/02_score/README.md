# Layer 02 — Score

**What this layer does:** for every job that Layer 03 scraped, asks Claude to read the full job description and rate it 0–100 against your `criteria.yaml`. Saves the score, a per-criterion breakdown, and a one-line "why" rationale right back into `data/jobs.sqlite`.

**Status:** ✅ Built. Tiered scoring (Haiku triage → Sonnet score), only-new-jobs by default, cost tracker prints actual USD spent each run.

---

## How the tiered approach works (and why it saves money)

Calling Sonnet on every job in your database would burn cash on roles that obviously fail your hard filters (wrong city, $180k base, APM, etc.). So we do two passes:

1. **Triage with Claude Haiku 4.5.** Cheap (~$0.001 per job). Reads title + location + JD, asks "does this clear all the hard must-haves?" Answers yes/no with a one-line reason.
2. **Score with Claude Sonnet 4.6.** ~10× the cost of Haiku, only runs on triage survivors. Produces the full 0–100 with breakdown.

A typical nightly run on a watchlist of 16 companies scores ~30–60 jobs and costs roughly **$0.10–$0.50**. You'll see the exact cost printed at the end of every run.

---

## What's in this folder

| File | What it does |
|---|---|
| `run_score.py` | **The one you actually run.** Pulls unscored jobs, runs the pipeline, saves results, prints summary. |
| `criteria_loader.py` | Reads `config/criteria.yaml` and turns it into a Markdown block the prompts can use. |
| `jd_fetcher.py` | Pulls the full job description text for each row (Greenhouse single-job endpoint, Lever raw_json, Apple/Amazon/Netflix raw_json). |
| `client.py` | Loads your API key from `.env`, builds the Anthropic client, tracks token spend. |
| `triage.py` | The Haiku must-have filter. |
| `score.py` | The Sonnet 0–100 scorer with per-criterion breakdown. |

---

## First-time setup (one-time, ~5 minutes)

### 1. Install the two Python packages

```bash
pip3 install --user anthropic pyyaml
```

(`anthropic` is the official Python SDK that talks to the API. `pyyaml` is what reads your config files.)

### 2. Get your Anthropic API key

1. Go to https://console.anthropic.com → sign up (or log in if you have an account).
2. Click **Settings** in the left nav, then **API Keys**.
3. Click **Create Key**. Give it a name like "Job Agent". Copy the value (starts with `sk-ant-api03-`).
4. **Important:** you'll only see this value once. If you lose it, you make a new key.

If you've never used the Anthropic API: you'll also need to add **at least $5 of credit** under Settings → Billing. The job agent uses pennies per run, so $5 will last months.

### 3. Save the key to your `.env` file

```bash
cd "/path/to/job-agent"
cp .env.example .env
```

Open the new `.env` file in any text editor, paste your key after the `=`, and save:

```
ANTHROPIC_API_KEY=sk-ant-api03-your-key-here
```

The `.gitignore` already excludes `.env`, so your key will never be accidentally committed or shared.

---

## How to run it

```bash
cd "/path/to/job-agent"
python3 src/02_score/run_score.py
```

You'll see, per job, something like:

```
[12/47] Stripe — Senior Product Manager, Payments
     ✓ Triage passed: senior level, SF, comp likely above floor
     → score:  82/100 — Strong fit; payments PM is core to Stripe's roadmap
```

Then a summary box with totals and the day's API cost.

### Useful flags

```bash
# Score at most 5 jobs (handy when testing prompt tweaks)
python3 src/02_score/run_score.py --limit 5

# Re-score everything (e.g. after editing criteria.yaml)
python3 src/02_score/run_score.py --rescore
```

---

## What gets saved per scored job

Inside the same `jobs` table that Layer 03 writes to:

| Column | Meaning |
|---|---|
| `jd_text` | The full job description text we fetched (cached so we don't re-fetch) |
| `triage_status` | `passed`, `failed`, or `NULL` |
| `triage_reason` | Haiku's one-line "why" |
| `score_total` | 0–100 weighted score (NULL if triage failed) |
| `score_role_fit`, `score_comp`, `score_ai_moat`, `score_wlb`, `score_company_health`, `score_work_model` | Sub-scores 0–100 each |
| `score_rationale` | Sonnet's one-line "why" |
| `score_model` | Which model produced the score (so you can A/B prompt versions later) |
| `scored_at` | When this row was last scored |

To see your top-scored jobs from the terminal:

```bash
sqlite3 data/jobs.sqlite "SELECT score_total, company, title FROM jobs WHERE score_total IS NOT NULL ORDER BY score_total DESC LIMIT 10;"
```

---

## Tweaking the scorer

The two prompts live at the top of `triage.py` and `score.py`. They're plain English — you can edit them. After any change, re-score with `--rescore` so older rows reflect the new logic.

Same with `criteria.yaml`: edit weights, add must-haves, change AI-moat signals — then `python3 src/02_score/run_score.py --rescore`.

---

## What's tested

A mocked-Anthropic integration test verifies the full pipeline (fetch → triage → score → save) and the weighted-total math.

---

## Known limits

- **JD fetch failures** for Workday/Google jobs (we don't store JDs for those — Layer 03 skips them at scrape time).
- **No retries on API errors** — if Anthropic returns a 500, the affected job is logged and skipped, not retried. Re-running picks it up next time because `score_total` is still NULL.
- **No streaming** — every call waits for the full response. Fine for ~50 jobs/night; if your watchlist explodes to 500+ companies, we'll add concurrency.

---

## What's next

- **Layer 05 (Act)** — `digest.py` reads top-scored jobs each morning and emails you a 5-job summary. After this layer, you have the end-to-end product.
- **Layer 04 (Match)** — for the top 1–3 scores, tailor your resume per JD using Claude.
- Later: A/B test prompt variations and record application outcomes in the database so recalibration is automatic.
