"""
digest.py — Layer 05. Pulls the top-scored jobs from jobs.sqlite, formats a
nice HTML email, and sends it to your inbox via the Resend API.

WHAT THIS DOES (in plain English):
  1. Opens data/jobs.sqlite.
  2. Pulls the top 5 jobs by score_total (ignores triaged-out and unscored).
  3. Builds an HTML email — score chips, company, title, location, rationale,
     and a "View posting" link per job.
  4. POSTs it to Resend, which delivers it to your inbox.
  5. Records which jobs were sent so a future enhancement can avoid re-sending.

HOW TO RUN IT:
  cd "/path/to/job-agent"
  python3 src/05_act/digest.py                  # send the email
  python3 src/05_act/digest.py --dry-run         # print HTML to terminal
  python3 src/05_act/digest.py --to someone@example.com  # override recipient
  python3 src/05_act/digest.py --top 10          # change how many jobs

FIRST-TIME SETUP:
  1. Sign up at https://resend.com/signup (free).
  2. Settings → API Keys → Create API Key. Copy the value (starts with `re_`).
  3. Open `.env` and add:
       RESEND_API_KEY=re_your_key_here
       DIGEST_TO_EMAIL=you@example.com   # where to send the digest
  4. Run with --dry-run first to preview the HTML before spending a send.

WHY RESEND AND NOT GMAIL SMTP:
  Resend's free tier is 3,000 emails/month — more than enough for a daily
  digest. No 2FA-app-password dance, no SMTP config, no DKIM/SPF setup
  needed for personal use. Sends from `onboarding@resend.dev` until you
  verify your own domain (which we don't need).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Wire up imports the same way other layers do
HERE = Path(__file__).resolve().parent
SCRAPE_DIR = HERE.parent / "03_scrape"
SCORE_DIR = HERE.parent / "02_score"
MATCH_DIR = HERE.parent / "04_match"
sys.path.insert(0, str(SCRAPE_DIR))
sys.path.insert(0, str(SCORE_DIR))
sys.path.insert(0, str(MATCH_DIR))

try:
    from db import get_connection, DB_PATH  # noqa: E402
    from client import _load_dotenv  # reuse the .env loader  # noqa: E402
except ImportError as e:
    sys.stderr.write(f"\nCould not import required module: {e}\n")
    sys.exit(2)

# Layer 04 (Match) — optional. If the connections module isn't there yet, we
# silently disable the warm-intros section instead of crashing the digest.
try:
    from connections import find_connections  # noqa: E402
except ImportError:
    def find_connections(_company: str):  # type: ignore
        return []

# Layer 02 (Score) — used to read the warm_connection bonus config from
# criteria.yaml. Optional: if pyyaml or the file is missing, we fall back to
# sensible defaults so the digest still ships.
try:
    from criteria_loader import load_criteria  # noqa: E402
except ImportError:
    load_criteria = None  # type: ignore

# Defaults if criteria.yaml has no warm_connection block (or can't be read).
_DEFAULT_WARM_CONFIG = {
    "bonus_max": 10,
    "tiers": {
        "any_connection": 4,
        "relevant_role": 7,
        "multiple_relevant": 10,
    },
    "relevant_role_keywords": [
        "product", "pm", "engineer", "engineering", "director",
        "vp", "lead", "head", "principal", "staff",
    ],
}


def _load_warm_config() -> dict:
    """Read the warm_connection block from criteria.yaml, falling back to
    defaults if the block (or pyyaml) is missing. Cached after first call."""
    if load_criteria is None:
        return _DEFAULT_WARM_CONFIG
    try:
        criteria = load_criteria()
    except Exception:
        return _DEFAULT_WARM_CONFIG
    block = (criteria or {}).get("warm_connection") or {}
    # Merge with defaults so partial configs don't crash later code.
    merged = dict(_DEFAULT_WARM_CONFIG)
    merged.update(block)
    merged["tiers"] = {**_DEFAULT_WARM_CONFIG["tiers"], **(block.get("tiers") or {})}
    merged["relevant_role_keywords"] = (
        block.get("relevant_role_keywords") or _DEFAULT_WARM_CONFIG["relevant_role_keywords"]
    )
    return merged


def _warm_bonus(company: str, *, config: dict | None = None) -> int:
    """Compute the warm-connection bonus for a given company. Deterministic;
    uses your linkedin_connections.csv via Layer 04's find_connections.

    Returns an int 0..config['bonus_max']. Tiers (highest applicable wins):
      - 0 matches                                → 0
      - ≥1 match, none in a relevant role        → tiers.any_connection
      - exactly 1 in a relevant role             → tiers.relevant_role
      - ≥2 in relevant roles                     → tiers.multiple_relevant
    Capped at bonus_max so config tweaks can't blow past the intended ceiling.
    """
    cfg = config or _load_warm_config()
    matches = find_connections(company)
    if not matches:
        return 0

    keywords = [k.lower() for k in cfg.get("relevant_role_keywords", [])]
    relevant_count = 0
    for m in matches:
        position = (m.get("position") or "").lower()
        if any(k in position for k in keywords):
            relevant_count += 1

    tiers = cfg.get("tiers", {})
    if relevant_count >= 2:
        bonus = tiers.get("multiple_relevant", 10)
    elif relevant_count == 1:
        bonus = tiers.get("relevant_role", 7)
    else:
        bonus = tiers.get("any_connection", 4)

    cap = cfg.get("bonus_max", 10)
    return max(0, min(int(bonus), int(cap)))


# ---- Optional company deprioritization --------------------------------------
# Companies the user lists in criteria.yaml's company_cooldown block get a
# deterministic penalty at digest time. They sink in the ranking rather than
# disappearing. The list is empty by default, which turns this off.

_COOLDOWN_CACHE: dict | None = None


def _load_cooldown_config() -> dict:
    """Read company_cooldown from criteria.yaml (cached). Missing block or
    missing pyyaml means no cooldowns, so the digest still ships."""
    global _COOLDOWN_CACHE
    if _COOLDOWN_CACHE is not None:
        return _COOLDOWN_CACHE
    cfg: dict = {}
    if load_criteria is not None:
        try:
            cfg = (load_criteria() or {}).get("company_cooldown") or {}
        except Exception:
            cfg = {}
    _COOLDOWN_CACHE = cfg
    return cfg


def _cooldown_penalty(company: str, *, config: dict | None = None) -> tuple[int, str | None]:
    """Return (penalty_points, reason) for a company, or (0, None).

    Matches the company name or any alias as a whole word, case-insensitive,
    so "Google" matches "Google" and "Google LLC" but not "Googleplex Foods".
    """
    cfg = config if config is not None else _load_cooldown_config()
    name = (company or "").lower()
    if not name:
        return 0, None
    default = int(cfg.get("default_penalty", 15) or 0)
    for entry in cfg.get("companies") or []:
        if not isinstance(entry, dict):
            continue
        names = [entry.get("name") or ""] + list(entry.get("aliases") or [])
        for n in names:
            n = str(n).strip().lower()
            if n and re.search(r"\b" + re.escape(n) + r"\b", name):
                return int(entry.get("penalty", default) or 0), entry.get("reason")
    return 0, None


RESEND_API_URL = "https://api.resend.com/emails"
DEFAULT_FROM = "Job Agent <onboarding@resend.dev>"


# ---- Score chip color (visual cue in the email, Wispr Flow palette) --------

def _chip_color(score: int) -> str:
    """Warm chip colors that sit naturally against the cream background."""
    if score >= 80: return "#1F4F40"   # forest green (Wispr's banner color)
    if score >= 70: return "#5C7B3F"   # sage / olive
    if score >= 60: return "#B08B3E"   # warm amber
    return "#8B5A2B"                    # muted earthy brown


# ---- HTML rendering ---------------------------------------------------------
#
# Style language adapted from wisprflow.ai:
#   - Cream background (#F8F4E6), warm white cards (#FEFCF5)
#   - Forest green for accent text (#1F4F40)
#   - Lavender for the primary CTA button (#E5D5F5)
#   - Serif headline (Iowan / Palatino fallback — web-safe across Mail apps;
#     no @font-face since email clients are inconsistent about web fonts)
#   - Generous spacing, soft borders, rounded corners
#
EMAIL_CSS = """
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Inter', 'Segoe UI', sans-serif;
    color: #1A1A1A;
    max-width: 640px;
    margin: 0 auto;
    padding: 32px 24px;
    background: #F8F4E6;
    line-height: 1.5;
  }
  h1 {
    font-family: 'Iowan Old Style', 'Palatino Linotype', Palatino, Georgia, serif;
    font-size: 32px;
    font-weight: 500;
    letter-spacing: -0.01em;
    line-height: 1.15;
    margin: 0 0 6px;
    color: #1A1A1A;
  }
  h1 .accent { color: #1F4F40; }
  .sub {
    color: #6B6B6B;
    font-size: 13px;
    margin: 0 0 32px;
    letter-spacing: 0.01em;
  }
  .job {
    background: #FEFCF5;
    border-radius: 16px;
    padding: 24px 26px;
    margin-bottom: 16px;
    border: 1px solid #EAE4D2;
  }
  .row {
    display: flex;
    align-items: center;
    gap: 14px;
    margin-bottom: 10px;
  }
  .chip {
    display: inline-block;
    padding: 5px 13px;
    border-radius: 999px;
    color: #FEFCF5;
    font-weight: 600;
    font-size: 13px;
    min-width: 32px;
    text-align: center;
    letter-spacing: 0.02em;
  }
  .title {
    font-family: 'Iowan Old Style', 'Palatino Linotype', Palatino, Georgia, serif;
    font-size: 19px;
    font-weight: 500;
    line-height: 1.3;
    color: #1A1A1A;
    flex: 1;
  }
  .meta {
    color: #6B6B6B;
    font-size: 13px;
    margin-bottom: 16px;
    letter-spacing: 0.01em;
  }
  .breakdown {
    color: #6B6B6B;
    font-size: 12px;
    font-variant-numeric: tabular-nums;
    margin: -2px 0 10px 0;
    letter-spacing: 0.02em;
  }
  .breakdown .warm-bonus {
    color: #1F4F40;
    font-weight: 600;
  }
  .bullets {
    margin: 0 0 18px 0;
    padding: 0;
    list-style: none;
  }
  .bullets li {
    font-size: 14px;
    line-height: 1.55;
    color: #3A3A3A;
    margin-bottom: 8px;
    padding: 0;
  }
  .bullets li b {
    color: #1F4F40;
    font-weight: 600;
  }
  .interview {
    background: #F1ECD7;
    border-left: 3px solid #1F4F40;
    padding: 12px 16px;
    border-radius: 6px;
    font-size: 13px;
    line-height: 1.5;
    color: #3A3A3A;
    margin-bottom: 18px;
  }
  .interview b { color: #1F4F40; font-weight: 600; }
  .interview .caveat { color: #8A7E5A; font-size: 12px; }
  .warm-intros {
    background: #EDE7CC;
    border-left: 3px solid #1F4F40;
    padding: 12px 16px;
    border-radius: 6px;
    font-size: 13px;
    line-height: 1.55;
    color: #3A3A3A;
    margin-bottom: 18px;
  }
  .warm-intros .label { color: #1F4F40; font-weight: 600; display: block; margin-bottom: 6px; }
  .warm-intros ul { margin: 0; padding-left: 18px; }
  .warm-intros li { margin-bottom: 4px; }
  .warm-intros a { color: #1F4F40; text-decoration: underline; }
  .warm-intros .person { color: #1A1A1A; font-weight: 600; }
  .warm-intros .role { color: #6B6B6B; }
  .button {
    display: inline-block;
    background: #E5D5F5;
    color: #1A1A1A !important;
    padding: 10px 18px;
    border-radius: 999px;
    text-decoration: none;
    font-size: 14px;
    font-weight: 500;
    letter-spacing: 0.01em;
  }
  .footer {
    color: #8A7E5A;
    font-size: 12px;
    margin-top: 32px;
    text-align: center;
    letter-spacing: 0.02em;
  }
"""


def _get(row, name: str):
    """Safely read a sqlite Row column that may not exist on older schemas."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def _render_bullets(row) -> str:
    """Build the per-criterion bullet list. Each bullet pairs the sub-score
    with the short note Sonnet produced for that dimension. The score itself
    is color-coded using the same scale as the headline chip — so you can
    skim a card and immediately see which dimension is strong vs weak."""
    items = [
        ("Role fit", _get(row, "score_role_fit"), _get(row, "score_role_note")),
        ("Comp",     _get(row, "score_comp"),     _get(row, "score_comp_note")),
        ("AI moat",  _get(row, "score_ai_moat"),  _get(row, "score_ai_moat_note")),
        ("WLB",      _get(row, "score_wlb"),      _get(row, "score_wlb_note")),
    ]
    lis = []
    for label, score, note in items:
        if score is None:
            continue
        color = _chip_color(score)
        score_html = (
            f'<span style="color:{color}; font-weight:600;">'
            f'({score}/100)</span>'
        )
        if note:
            lis.append(
                f'<li><b>{escape(label)}</b> {score_html}: {escape(note)}</li>'
            )
        else:
            # Fall back to just the score if no note (e.g. pre-bullet-rescore data)
            lis.append(f'<li><b>{escape(label)}</b> {score_html}</li>')
    return f'<ul class="bullets">{"".join(lis)}</ul>' if lis else ""


def _render_interview(row) -> str:
    note = _get(row, "score_interview_note")
    if not note:
        return ""
    return (
        f'<div class="interview">'
        f'<b>Interview signal:</b> {escape(note)} '
        f'<span class="caveat">(from the posting)</span>'
        f'</div>'
    )


def _render_warm_intros(row) -> str:
    """If we have LinkedIn connections at this company, surface them as a
    'Warm intros' block. Hidden entirely when there are none — keeps cards
    for cold-application companies as clean as before.

    Each entry: bolded name, role, and a 'LinkedIn' link when we have a URL.
    """
    company = row["company"]
    matches = find_connections(company)
    if not matches:
        return ""

    items_html = []
    # Cap at 5 to keep the email scannable; mention the rest in a footer line.
    visible = matches[:5]
    for m in visible:
        name = escape(m.get("name") or "—")
        position = escape(m.get("position") or "")
        url = m.get("url") or ""
        link_html = (
            f'  <a href="{escape(url)}">LinkedIn</a>' if url else ""
        )
        role_html = (
            f' <span class="role">— {position}</span>' if position else ""
        )
        items_html.append(
            f'<li><span class="person">{name}</span>{role_html}{link_html}</li>'
        )

    label = "Warm intro" if len(matches) == 1 else f"Warm intros ({len(matches)})"
    overflow = (
        f'<li style="color:#8A7E5A;">+ {len(matches) - len(visible)} more — '
        f'check linkedin_connections.csv</li>'
        if len(matches) > len(visible) else ""
    )

    return (
        f'<div class="warm-intros">'
        f'<span class="label">🤝 {label} at {escape(company)}</span>'
        f'<ul>{"".join(items_html)}{overflow}</ul>'
        f'</div>'
    )


def _render_job(row, adjustment: int = 0) -> str:
    """`adjustment` is the net digest-time change (warm bonus minus cooldown
    penalty). We recompute the two parts here only to label the breakdown."""
    base = row["score_total"]
    effective = base + adjustment
    warm = _warm_bonus(row["company"])
    penalty, reason = _cooldown_penalty(row["company"])
    # Chip uses the effective total so the visual matches the sort order in
    # the digest. The breakdown caption shows the math when there's an adjustment.
    color = _chip_color(effective)
    parts = [str(base)]
    if warm > 0:
        parts.append(f'+ <span class="warm-bonus">{warm} warm</span>')
    if penalty > 0:
        title = f' title="{escape(reason)}"' if reason else ""
        parts.append(f'&minus; <span style="color:#8B5A2B;"{title}>{penalty} cooldown</span>')
    if len(parts) > 1:
        breakdown_html = (
            f'<div class="breakdown">{" ".join(parts)} = {effective}</div>'
        )
    else:
        breakdown_html = ""
    return f"""
      <div class="job">
        <div class="row">
          <span class="chip" style="background:{color};">{effective}</span>
          <div class="title">{escape(row["company"])} — {escape(row["title"])}</div>
        </div>
        {breakdown_html}
        <div class="meta">{escape(row["location"] or "Location not specified")}</div>
        {_render_bullets(row)}
        {_render_warm_intros(row)}
        {_render_interview(row)}
        <a class="button" href="{escape(row["url"])}">View posting →</a>
      </div>
    """


def render_email(picks, *, generated_at: str) -> tuple[str, str]:
    """Return (subject, html_body) for the digest email.

    `picks` is a list of (row, warm_bonus) tuples already sorted by effective
    total (score_total + warm_bonus) descending. _select_top builds it.
    """
    if not picks:
        subject = "Job digest — no jobs cleared the bar today"
        body = f"""
        <html><head><style>{EMAIL_CSS}</style></head><body>
        <h1>Quiet <span class="accent">today.</span></h1>
        <p class="sub">Generated {generated_at}</p>
        <p style="color:#3A3A3A; font-size:14px; line-height:1.6;">
          The scraper ran and the scorer ran, but nothing scored high enough
          to make the cut. Edit <code>config/criteria.yaml</code> if you want
          to loosen the bar, or wait for tomorrow's run.
        </p>
        </body></html>
        """
        return subject, body

    top_row, top_bonus = picks[0]
    top_effective = top_row["score_total"] + top_bonus
    subject = (
        f"PM jobs — top {top_effective}/100: {top_row['company']} {top_row['title']}"
    )[:120]

    job_html = "\n".join(_render_job(r, b) for (r, b) in picks)
    body = f"""
    <html>
      <head><style>{EMAIL_CSS}</style></head>
      <body>
        <h1>Top {len(picks)} PM <span class="accent">jobs for you.</span></h1>
        <p class="sub">Generated {generated_at}</p>
        {job_html}
        <p class="footer">Powered by your Job Agent  ·  edit criteria.yaml to retune</p>
      </body>
    </html>
    """
    return subject, body


# ---- Resend send ------------------------------------------------------------

def send_via_resend(*, api_key: str, sender: str, recipient: str,
                    subject: str, html: str) -> dict:
    """POST the email to Resend. Returns the parsed response on 2xx, else raises.

    NOTE: Uses httpx (already installed via the anthropic SDK) instead of
    Python's stdlib urllib because Resend sits behind Cloudflare, and
    Cloudflare's bot detection sometimes returns error 1010 for urllib's
    default User-Agent + TLS fingerprint. httpx handshakes look like a normal
    HTTP client, which Cloudflare lets through.
    """
    try:
        import httpx  # type: ignore
    except ImportError:
        raise SystemExit(
            "httpx is required to send the digest. Install with:\n"
            "    pip3 install --user httpx\n"
            "(it normally comes along with the anthropic SDK install)"
        )

    payload = {
        "from": sender,
        "to": [recipient],
        "subject": subject,
        "html": html,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "JobAgent-Digest/0.1",
    }

    try:
        resp = httpx.post(RESEND_API_URL, json=payload, headers=headers, timeout=30)
    except httpx.RequestError as e:
        raise SystemExit(f"Network error contacting Resend: {e!r}")

    if resp.status_code >= 400:
        raise SystemExit(
            f"Resend returned HTTP {resp.status_code}.\nResponse body:\n{resp.text}\n\n"
            "Common causes:\n"
            "  - Invalid or expired API key (check value in .env)\n"
            "  - Recipient address rejected by Resend\n"
            "  - Daily/monthly send quota exceeded\n"
            "  - Cloudflare error 1010 (bot detection) — usually a TLS/UA issue"
        )

    try:
        return resp.json()
    except json.JSONDecodeError:
        return {"id": "(no id returned)", "raw": resp.text}


# ---- Main -------------------------------------------------------------------

def _select_top(conn, n: int):
    """
    Top N (effective_total = score_total + warm_bonus) scored jobs, with at
    most ONE row per company.

    Returns a list of (row, warm_bonus) tuples, already sorted by effective
    total descending so render_email and main() can iterate without
    re-computing.

    Why we do this in Python instead of SQL: warm_bonus depends on
    linkedin_connections.csv (a file, not a table), so we'd have to bridge
    those two worlds anyway. A 2-pass loop on a few hundred rows is plenty
    fast and keeps the logic readable.

    Per-company tie-break order (when two postings at the same company both
    qualify): highest effective total → highest first_seen (most recent) →
    url alphabetically. This matches the SQL window function we used before,
    just with effective_total replacing raw score_total.
    """
    rows = conn.execute(
        """
        SELECT *
          FROM jobs
         WHERE score_total IS NOT NULL
           AND triage_status = 'passed'
        """
    ).fetchall()

    # One representative posting per company. When a company has more than one
    # qualifying posting, prefer a not-yet-sent (fresh) one, then higher
    # effective total, then more-recent first_seen, then url ascending.
    best_per_company: dict[str, dict] = {}
    for r in rows:
        # Net digest-time adjustment: warm bonus minus company cooldown.
        bonus = _warm_bonus(r["company"]) - _cooldown_penalty(r["company"])[0]
        cand = {
            "effective": (r["score_total"] or 0) + bonus,
            "first_seen": r["first_seen"] or "",
            "url": r["url"] or "",
            "row": r,
            "bonus": bonus,
            # A job is "fresh" until the day it first appears in a sent digest.
            "fresh": not _get(r, "digest_sent_at"),
        }
        cur = best_per_company.get(r["company"])
        if cur is None or _better_candidate(cand, cur):
            best_per_company[r["company"]] = cand

    cands = list(best_per_company.values())
    # "Fresh first, then fill": surface roles you haven't been emailed yet
    # (newest discovery first, higher score breaking ties); if there aren't
    # enough new ones, top up with the highest-scoring roles you've already
    # seen so the digest is always full and never repeats stale picks first.
    fresh = sorted(
        [c for c in cands if c["fresh"]],
        key=lambda c: (c["first_seen"], c["effective"]),
        reverse=True,
    )
    backfill = sorted(
        [c for c in cands if not c["fresh"]],
        key=lambda c: (c["effective"], c["first_seen"]),
        reverse=True,
    )
    chosen = (fresh + backfill)[:n]
    return [(c["row"], c["bonus"]) for c in chosen]


def _better_candidate(a: dict, b: dict) -> bool:
    """True if posting `a` should replace `b` as a company's representative.
    Prefer fresh over already-sent, then higher effective total, then newer
    first_seen, then url alphabetically."""
    if a["fresh"] != b["fresh"]:
        return a["fresh"]
    if a["effective"] != b["effective"]:
        return a["effective"] > b["effective"]
    if a["first_seen"] != b["first_seen"]:
        return a["first_seen"] > b["first_seen"]
    return a["url"] < b["url"]


def _ensure_digest_schema(conn) -> None:
    """Add the digest_sent_at column if an older database doesn't have it yet.
    Safe to call every run — it's a no-op once the column exists."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)")]
    if "digest_sent_at" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN digest_sent_at TEXT")
        conn.commit()


def _mark_sent(conn, picks, when: str) -> None:
    """Stamp digest_sent_at on the jobs we just emailed, but only the ones that
    were still fresh — so a job records the date of its FIRST appearance in a
    digest and naturally rotates out of the 'fresh' bucket afterward."""
    fresh_urls = [row["url"] for (row, _b) in picks if not _get(row, "digest_sent_at")]
    if not fresh_urls:
        return
    conn.executemany(
        "UPDATE jobs SET digest_sent_at = ? WHERE url = ?",
        [(when, u) for u in fresh_urls],
    )
    conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a top-N PM jobs digest by email")
    parser.add_argument("--top", type=int, default=5,
                        help="How many jobs to include (default 5)")
    parser.add_argument("--to", type=str, default=None,
                        help="Override recipient (default: DIGEST_TO_EMAIL from .env)")
    parser.add_argument("--from", dest="sender", type=str, default=DEFAULT_FROM,
                        help="Override sender (default Resend's onboarding address)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print HTML to terminal instead of sending")
    args = parser.parse_args()

    if not Path(DB_PATH).exists():
        print(f"jobs.sqlite not found at {DB_PATH}.")
        print("Run Layers 03 + 02 first (scrape, then score).")
        return 2

    _load_dotenv()
    recipient = args.to or os.environ.get("DIGEST_TO_EMAIL")
    if not recipient and not args.dry_run:
        sys.stderr.write(
            "\nNo recipient set. Either:\n"
            "  - pass --to your@email.com on the command line, OR\n"
            "  - add DIGEST_TO_EMAIL=your@email.com to .env\n"
        )
        return 2

    conn = get_connection()
    _ensure_digest_schema(conn)
    picks = _select_top(conn, args.top)
    # Keep `conn` open: on a real send we stamp digest_sent_at below so these
    # jobs rotate out of the "fresh" bucket next time.

    fresh_count = sum(1 for (row, _b) in picks if not _get(row, "digest_sent_at"))
    print(f"Selected {len(picks)} job(s) for the digest "
          f"({fresh_count} new, {len(picks) - fresh_count} backfill).")
    if picks:
        top_row, top_bonus = picks[0]
        top_effective = top_row["score_total"] + top_bonus
        bottom_row, bottom_bonus = picks[-1]
        bottom_effective = bottom_row["score_total"] + bottom_bonus
        warm_picks = sum(1 for (r, _) in picks if _warm_bonus(r["company"]) > 0)
        cooldown_picks = sum(1 for (r, _) in picks if _cooldown_penalty(r["company"])[0] > 0)
        print(
            f"  Top: {top_effective}/100 "
            f"({top_row['score_total']} {top_bonus:+d} adj) — "
            f"{top_row['company']} {top_row['title']}"
        )
        print(f"  Bottom of digest: {bottom_effective}/100")
        print(f"  Warm-intro picks: {warm_picks}/{len(picks)}")
        print(f"  Cooldown-company picks: {cooldown_picks}/{len(picks)}")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    subject, html = render_email(picks, generated_at=now)

    if args.dry_run:
        print("\n--- subject ---")
        print(subject)
        print("\n--- html (first 1500 chars) ---")
        print(html[:1500])
        if len(html) > 1500:
            print(f"\n... ({len(html) - 1500} more chars) ...")

        # Always also write the full HTML so you can open it in a browser to
        # actually SEE the rendered email, not just inspect the source.
        preview_path = Path(DB_PATH).parent / "digest_preview.html"
        preview_path.write_text(html, encoding="utf-8")
        print(f"\nFull HTML written to:\n  {preview_path}")
        print("Open it in any browser to see the rendered email.")
        print(f"\nDry run — nothing sent. Recipient would have been: {recipient or '(none configured)'}")
        print("(Dry run does not mark jobs as sent, so picks are unchanged.)")
        conn.close()
        return 0

    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        sys.stderr.write(
            "\nRESEND_API_KEY is not set.\n\n"
            "How to fix:\n"
            "  1. Sign up at https://resend.com/signup (free).\n"
            "  2. Settings -> API Keys -> Create API Key. Copy the value.\n"
            "  3. Add this line to .env (in the project root):\n"
            "       RESEND_API_KEY=re_your_key_here\n"
            "  4. Re-run this command.\n\n"
            "Or use --dry-run to preview the email without sending.\n"
        )
        return 2

    print(f"\nSending to {recipient} via Resend...")
    resp = send_via_resend(api_key=api_key, sender=args.sender,
                           recipient=recipient, subject=subject, html=html)
    msg_id = resp.get("id", "?")
    print(f"  ✓ Sent. Resend message id: {msg_id}")

    # Record that today's fresh picks have now been emailed, so tomorrow's
    # digest leads with the next batch of new roles instead of repeating these.
    # This is best-effort bookkeeping that runs AFTER the email already sent, so
    # a failure here (e.g. a transient "database is locked" from an overlapping
    # run) must NOT report the digest as failed — the user already got it. Worst
    # case, tomorrow may repeat today's picks once. Log it and exit cleanly.
    sent_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        _mark_sent(conn, picks, sent_day)
    except Exception as e:
        print(f"  ⚠ Email sent OK, but couldn't mark picks as sent ({e!r}). "
              "Tomorrow's digest may repeat today's picks; not treating this "
              "as a failure.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
