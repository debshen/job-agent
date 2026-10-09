# Layer 06 — UI (Optional Dashboard)

**What this layer does:** gives you a clean web dashboard to browse jobs, see tailored resumes, and track your application status.

**Status:** 🟡 Stub. No code yet.

---

## Why this is optional

For most days, the morning email digest is enough. The dashboard exists for when you want to:
- Browse the full backlog (not just today's top 5)
- Tweak a tailored resume before sending
- Mark a job as "applied" / "rejected" / "interviewing" to track your pipeline

## Recommended stack

- **Lovable** or **v0** to generate the React frontend. Both let you describe the UI in English and get working code.
- **Supabase** as a shared database — replace the local SQLite when you're ready for the dashboard.
- The backend (Layers 01–05) writes to Supabase; the dashboard reads from it.

## What we'd build

- Job feed (sortable, filterable by score)
- Tailored resume preview (PDF inline)
- Network referral indicator
- Status tracker (saved → applied → interview → offer/reject)
- Company watchlist editor (so you don't have to edit YAML by hand)

## Why we're not building this yet

A dashboard with no data is useless. We need at least Layers 02 + 03 + 05 (Save) running before this earns its keep.
