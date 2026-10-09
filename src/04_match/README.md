# Layer 04 — Match

**What this layer does:** turns a high-scoring job into something you can actually act on — a tailored resume and a list of people in your network at that company.

**Status:** 🟡 Partial. `connections.py` matches companies to your LinkedIn connections export; the digest uses it for the warm-connection bonus. Resume tailoring isn't built yet.

---

## Two things happen here

### Resume tailoring

For each top-scoring job:
1. Load your master resume (you'll save it to `data/resume_master.docx` later).
2. Load the job description.
3. Send both to Claude with a "rewrite my resume to emphasize the parts that match this JD" prompt.
4. Output a tailored PDF to `data/tailored_resumes/{company}_{role}.pdf`.

### Network matching

1. You export your LinkedIn connections to CSV (LinkedIn → Settings → Data Privacy → Get a copy → Connections).
2. Save the CSV to `data/linkedin_connections.csv`.
3. For each company in the digest, check if any of your connections currently work there.
4. Surface those names in the digest as potential warm intros.

## What's next

Automated resume tailoring per job description.
