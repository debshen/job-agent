# Job Agent

A personal AI agent that finds Product Manager roles, scores each one against my criteria with an LLM, and emails me a ranked digest every morning. I built it end-to-end with Claude Code while running my own job search, and I've used it daily since April 2026.

The interesting part isn't the scraping. It's the evaluation loop: I designed the scoring rubric, hand-labeled a golden set to check it, and then used real application outcomes to find where the scoring was wrong and recalibrate it.

---

## How it works

Every night a scheduled job runs four steps:

1. **Discover** companies worth tracking from a curated pool, by probing their public job boards for active PM hiring.
2. **Scrape** PM postings from each company's applicant tracking system (Greenhouse, Lever, Workday, iCIMS, and a few custom career feeds) into a local SQLite database.
3. **Score** each new posting with Claude: a fast, cheap triage pass (Haiku) drops clear mismatches, then a detailed pass (Sonnet) scores the rest 0–100 across weighted criteria and writes a short rationale.
4. **Send** a morning digest email with the top roles, adjusted for warm connections and any companies the user has chosen to deprioritize.

```
src/
├── 01_discover/   finds new companies from a curated pool
├── 02_score/      LLM triage + scoring against config/criteria.yaml
├── 03_scrape/     Greenhouse, Lever, Workday, iCIMS, and custom-feed scrapers
├── 04_match/      matches companies to your LinkedIn connections (warm bonus)
├── 05_act/        builds and emails the daily digest
├── 06_ui/         optional dashboard (planned)
└── 07_interview/  PM interview practice: question bank + mock interviews
config/
├── criteria.example.yaml   your preferences and the scoring rubric (copy to criteria.yaml)
├── watchlist.yaml          companies to track and how to reach their job boards
└── discovery_pool.yaml     candidate companies for discovery
scripts/          nightly run + macOS launchd schedule
```

---

## The scoring rubric

Each posting gets six weighted sub-scores: role fit, comp, work model, AI moat, team sustainability, and company health. Role fit carries the most weight and is "lane-first": the scorer asks whether the core problem of the role is one the candidate has actually owned, rather than how buzzworthy the company or the AI angle is.

The rubric lives in `config/criteria.yaml` (start from `criteria.example.yaml`), so changing what "a good fit" means is a config edit, not a code change.

## Evaluating and recalibrating the scorer

- **Golden set.** I hand-labeled a set of real postings and compared the model's scores with my own judgment to check and tune the scoring prompt.
- **Outcome data.** I compared model rankings with my own judgments and with application outcomes, then re-weighted the rubric: lane fit over AI-forwardness, and an explicit level check.
- **Calibration anchors.** The scoring prompt includes concrete examples of what a 90, a 75, and a 50 look like, plus a list of common scoring mistakes to avoid, such as rewarding a famous company name over the actual fit.
- **Evidence rules.** Company-level claims (comp bands, ratings, funding) must come from the posting or well-documented facts. When evidence is missing, the scorer says "unknown" and scores that dimension neutral instead of guessing.

## Cost

Triage runs on Haiku and the full score on Sonnet, with prompt caching on the shared criteria block. A cost tracker prints USD spend per run.

---

## Setup

Requires Python 3.

```bash
git clone https://github.com/debshen/job-agent.git
cd job-agent
pip3 install --user pyyaml anthropic

cp .env.example .env                              # then add your API keys
cp config/criteria.example.yaml config/criteria.yaml   # then edit to your preferences
```

`.env` needs:

- `ANTHROPIC_API_KEY` from console.anthropic.com
- `RESEND_API_KEY` from resend.com (free tier) for the digest email
- `DIGEST_TO_EMAIL`, the inbox that receives the digest

Run the loop by hand:

```bash
python3 src/01_discover/run_discover.py
python3 src/03_scrape/run_all.py
python3 src/02_score/run_score.py
python3 src/05_act/digest.py
```

Or schedule it nightly on macOS with `scripts/install_schedule.sh` (update the project path in the scripts first).

## Intended use

A personal tool for one job seeker to triage public job postings for themselves. It reads public job-board data at a polite rate and is not built for bulk scraping, recruiting, or screening other people. Scores are one input to your own judgment, not a decision.

## Data and privacy

What stays local (and is listed in `.gitignore`): your API keys (`.env`), your criteria (`config/criteria.yaml`), the job database, and any LinkedIn export.

What leaves your machine:

- **Anthropic API:** your criteria and each job posting are sent to Claude for triage and scoring. If you use the interview practice tool, your answers are sent too.
- **Resend:** the digest email, including matched jobs and the names of any LinkedIn connections used for the warm bonus.
- **Job boards:** requests to public ATS endpoints to read postings.

`config/criteria.example.yaml` is a fictional example profile; replace it with your own.
