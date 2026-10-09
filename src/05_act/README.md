# Layer 05 — Act

**What this layer does:** sends you a clean HTML email digest every morning with the top scored PM jobs from your watchlist.

**Status:** ✅ Built (Notify). Save is already handled by Layer 03's SQLite. Apply is a bonus stage for later.

---

## What's in this folder

| File | What it does |
|---|---|
| `digest.py` | **The one you actually run.** Pulls top-N scored jobs from `jobs.sqlite`, builds an HTML email, sends via Resend API. |

---

## First-time setup (one-time, ~3 minutes)

### 1. Get a Resend API key

1. Go to [https://resend.com/signup](https://resend.com/signup) — sign up (free, no credit card).
2. Click **API Keys** in the left nav, then **Create API Key**.
3. Give it a name like "Job Agent Digest" and pick "Sending access" → All domains.
4. Copy the value (starts with `re_`). You'll only see it once.

Resend's free tier is 3,000 emails/month, so a daily digest uses about 1% of it.

### 2. Add the key + your inbox to `.env`

Open `.env` at the project root and add these two lines:

```
RESEND_API_KEY=re_your_key_here
DIGEST_TO_EMAIL=you@example.com
```

(The `.env.example` template already lists both.)

That's it. No domain verification, no DKIM, no SMTP config — Resend lets you send from their `onboarding@resend.dev` address out of the box, which is fine for a personal digest.

---

## How to run it

**Preview the email without sending (free, no API call):**

```bash
cd "/path/to/job-agent"
python3 src/05_act/digest.py --dry-run
```

This prints the subject and the first chunk of HTML to your terminal. Useful for spotting issues with the layout or content before burning a real send.

**Send it for real:**

```bash
python3 src/05_act/digest.py
```

You should get the email within a minute. The terminal prints the Resend message ID on success.

**Useful flags:**

```bash
python3 src/05_act/digest.py --top 10           # 10 jobs instead of 5
python3 src/05_act/digest.py --to someone@example.com   # one-off recipient override
```

---

## What the email looks like

Each job is a card with:
- A coloured score chip (green ≥80, yellow-green ≥70, amber below)
- Company + title in bold
- Location (raw from the posting)
- The one-line rationale Sonnet produced during scoring
- A small breakdown row: `role 88 · comp 92 · AI moat 75 · WLB 68 · health 85 · model 80`
- A "View posting →" button that opens the original URL

Clean, mobile-friendly, no tracking pixels.

---

## How "top N" works

The selection rule is simple: anything with `score_total NOT NULL` and `triage_status = 'passed'`, ordered by `score_total DESC`, capped at `--top` (default 5). Triaged-out jobs are never included even if you ask for top 100.

Today this re-sends the same top 5 if you run it twice in a day. We chose this for predictability — easier to debug. A future enhancement is "send only what's new since the last digest," which would need a `digest_sent_at` column.

---

## End-to-end nightly flow (when you wire up cron)

```bash
cd "/path/to/job-agent"
python3 src/03_scrape/run_all.py    # 1. scrape watchlist
python3 src/02_score/run_score.py   # 2. score new jobs
python3 src/05_act/digest.py        # 3. email yourself the top 5
```

That's the entire product. Three commands. Wire them into a single shell script + a launchd / cron entry on your Mac (or run as a GitHub Actions workflow), and you have a true daily PM digest in your inbox.

---

## Known limits

- **Re-sends the same jobs** if run multiple times the same day (no dedup yet).
- **Sender is `onboarding@resend.dev`** until you verify a domain. Some email clients may put it in spam the first time — mark as "Not Spam" once and it'll stay in your inbox.
- **No Gmail / LinkedIn enrichment yet** — Layer 04 (Match) will eventually attach a tailored resume and flag any LinkedIn connections at the company.

---

## What's next

- **Layer 04 (Match)** — tailored resume per JD, attached to the digest.
- **Layer 01 (Discover)** — auto-discover new companies to add to the watchlist.
- **Layer 06 (UI)** — a simple dashboard if you want to browse history without opening SQLite.
- **Schedule it** — see `scripts/README.md` for the nightly launchd setup and cloud options.
