# Layer 07 — Interview Prep

**What this layer does:** lets you practice for PM interviews — first against a curated bank of canonical PM questions (so you can start today, before any application has come back), and later against questions tailored to a specific JD and what's publicly known about that company's interview process.

**Status:** 🟢 Phase A built. Phase B drafted, deferred until first real interview lands.

---

## How this layer is different from layers 01–06

Layers 01–06 are **batch and proactive** — a nightly cron does work for you while you sleep. Layer 07 is **interactive and reactive** — you invoke it when you want to practice or when you have an interview coming up. So:

- No cron entry for this one.
- Each script is a command you run on demand.
- Some commands (mock interviews) are conversational and run in your terminal as a back-and-forth.
- Output is per-session markdown files in `data/interviews/` plus rows in `jobs.sqlite`.

The shared plumbing (`.env`, Anthropic client, `criteria.yaml`, `jobs.sqlite`) is reused — you don't set anything new up.

---

## Phase A — General PM practice (✅ shipped)

Built this first so you can start practicing **before** any application has come back. The general bank is also the foundation Phase B builds on (JD/company-specific questions get added on top of the same data model).

### Files in Phase A

| File | What it does | Run directly? |
|---|---|---|
| `seed_questions.yaml` | Curated PM interview question corpus, ~70 questions across 6 round types | No (data) |
| `schema.py` | Light schema migration: adds `mock_sessions` table + `interview_status` column on jobs (named `schema.py` to avoid an import clash with Layer 03's `db.py`) | Yes (`--migrate`) |
| `bank.py` | Renders the seed bank into a browsable markdown file you can read offline | Yes |
| `mock.py` | Interactive mock interview: picks a question, you answer, Claude follows up like a real interviewer, gives structured feedback + a model answer | Yes |

### First-time setup (~30 seconds)

You're already set up if Layer 02 is running. The only Phase A addition is one schema migration:

```bash
cd "/path/to/job-agent"
python3 src/07_interview/schema.py --migrate
```

Idempotent — safe to re-run.

### Generate the browsable question bank

```bash
python3 src/07_interview/bank.py
```

Writes `data/interviews/general_bank.md` — categorized list of all ~70 seed questions you can read in any markdown viewer or paste into your notes app. Useful for skimming what kinds of questions exist before you start practicing live.

Free (no API call), runs instantly.

### Run a mock interview

```bash
python3 src/07_interview/mock.py --round behavioral
# round options: recruiter | hiring_manager | behavioral |
#                product_sense | execution | strategy | technical
```

What happens:

1. Script picks a question from the seed bank for that round (or generates a variant).
2. Prints the question.
3. You answer in your terminal — multi-line is fine. End your answer with a line containing only `DONE`, or press Ctrl+D. (For Wispr Flow: dictate, then type `DONE` on its own line.)
4. Claude pushes back like an interviewer would — one or two follow-up probes per question.
5. After the round, you get a structured debrief: what worked, what was unclear, what framework was missing, and a model answer to compare against.

Transcript saved to `data/interviews/mock_{round}_{timestamp}.md`.

### Useful flags

```bash
# Practice 3 questions in a row from the same round
python3 src/07_interview/mock.py --round behavioral --count 3

# Pick a specific question by ID from the seed bank
python3 src/07_interview/mock.py --question behavioral_07

# Skip Claude's follow-up probes (faster, cheaper, less realistic)
python3 src/07_interview/mock.py --round product_sense --no-followups

# Save transcript to a specific file
python3 src/07_interview/mock.py --round strategy --out my_practice.md
```

### Cost expectations

Per round (3-turn conversation + feedback): roughly **$0.05–$0.15** with Sonnet. A practice session of 5 rounds runs about **$0.25–$0.75**.

---

## Phase B — JD/company-tailored prep (🟡 drafted, deferred)

Build this when you have a real interview scheduled. Phase B layers on top of Phase A — same `mock.py`, same data model, just with extra context about *this specific role at this specific company* mixed into the question selection and feedback.

### Files Phase B will add

| File | What it does | Run directly? |
|---|---|---|
| `intel_fetcher.py` | Scrapes/caches publicly available interview intelligence per company (Glassdoor interviews page, public PM interview write-ups, Reddit). Cached for 30 days. | No (helper) |
| `questions.py` | Generates a JD+company-tailored question bank: combines seed canonical questions with JD-derived questions and company-specific intel. Saves per-job. | Yes |
| `prep.py` | Company + role deep-dive brief — recent news, leadership, product strategy, financial signals, what to ask them. Outputs `prep_brief.md`. | Yes |
| `stars.py` | STAR story matcher: reads your master resume, matches bullets to common behavioral prompts, drafts STAR-formatted answers. | Yes |
| `debrief.py` | Post-interview capture: conversational prompt that extracts what was asked, what went well, what to study. | Yes |

### How Phase B will extend the question bank

Each generated question gets tagged so you know how seriously to weight it:
- `[from intel]` — sourced from public reports of that company's actual interviews
- `[from JD]` — inferred from the role description
- `[from canon]` — pulled from `seed_questions.yaml` because it's universally common

Companies with thin public intel (smaller startups) will lean more heavily on `[from canon]` and `[from JD]` and the bank will say so.

### Phase B's heavier data model

Phase B will add three more tables to `jobs.sqlite`:

```sql
CREATE TABLE interview_intel (
  company TEXT PRIMARY KEY,
  intel_json TEXT,
  sources_json TEXT,
  fetched_at TEXT,
  expires_at TEXT
);

CREATE TABLE question_banks (
  id INTEGER PRIMARY KEY,
  job_id INTEGER REFERENCES jobs(id),
  generated_at TEXT,
  bank_json TEXT,
  intel_used BOOLEAN
);

CREATE TABLE interview_debriefs (
  id INTEGER PRIMARY KEY,
  job_id INTEGER REFERENCES jobs(id),
  round TEXT,
  interview_date TEXT,
  questions_asked TEXT,
  what_went_well TEXT,
  what_to_study TEXT,
  next_steps TEXT,
  created_at TEXT
);
```

Phase A's `mock_sessions` table accommodates both phases — it has a nullable `job_id` column so general practice sessions and job-specific mock interviews coexist.

### Phase B build order (when triggered)

1. `schema.py --migrate` (re-run; will idempotently add Phase B tables)
2. `intel_fetcher.py` end-to-end on one company (probably Stripe, rich public data)
3. `questions.py` using the cached intel + JD + Phase A seed
4. `prep.py` filled in fully
5. `stars.py` (independent of everything else)
6. `debrief.py`

### When to trigger Phase B

Any of these:
- A real interview gets scheduled and you want company-specific prep
- You want resume-driven STAR drafting before applying anywhere
- You want post-interview debriefs to start compounding into your prep corpus

Phase B is designed but not built yet.

---

## Known limits (Phase A)

- **Mock interview is text-only.** Wispr Flow handles the speaking practice on your end. No voice-in-voice-out from this script.
- **Seed bank is generic by design.** A generic "tell me about a time you influenced without authority" works at any company. JD/company-specific tailoring is what Phase B adds.
- **No transcript review across sessions yet.** Each mock round saves its own file. Phase B's debrief loop is what starts to compound learning across multiple sessions.

---

## Out of scope (for both phases)

- Live voice mock interviews
- Calendar integration / auto-detect what round you're prepping for
- Technical PM / SQL skill drilling
- Salary negotiation playbook (could be Layer 08 later)
