"""
mock.py — Interactive mock PM interview against the canonical seed bank.

WHAT THIS DOES (in plain English):
  Picks a question from `seed_questions.yaml` for the round you want to
  practice, prints it, reads your typed (or Wispr-dictated) answer,
  then has Claude play interviewer:
    - asks 1-2 focused follow-up probes like a real interviewer would
    - at the end of the round, gives structured feedback + a model answer

  Saves a markdown transcript to `data/interviews/mock_<round>_<ts>.md` and
  records a row in the `mock_sessions` table for later review.

HOW TO RUN IT:
    cd "/path/to/job-agent"
    python3 src/07_interview/mock.py --round behavioral

USEFUL FLAGS:
    --round X            recruiter | hiring_manager | behavioral |
                         product_sense | execution | strategy | technical
    --question ID        pick a specific question from seed_questions.yaml
    --count N            practice N questions in a row (default 1)
    --no-followups       skip Claude's follow-up probes (faster, cheaper)
    --max-followups N    cap follow-ups per question (default 2)
    --out path/to.md     custom transcript path (otherwise auto-named)
    --seed N             random seed for question selection (reproducibility)

INPUT FORMAT (Wispr Flow friendly):
  After Claude asks a question, type your answer over multiple lines if you
  want. End your answer with a line containing only `DONE`, or press Ctrl+D.
  Type `SKIP` to skip the question. Type `QUIT` to end the session early.

EXIT CODES:
    0 = ran successfully (at least one round completed)
    1 = ran but no questions matched the round / id given
    2 = could not start (missing API key, missing seed, etc.)
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---- sibling/Layer-02 imports ------------------------------------------------
HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
SCORE_DIR = HERE.parent / "02_score"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SCORE_DIR))

try:
    from client import get_client, CostTracker  # type: ignore  (Layer 02)
    from schema import ensure_schema            # type: ignore  (Layer 07's own; named
                                                 # schema.py to avoid clash with
                                                 # Layer 03's db.py module name)
except ImportError as e:
    sys.stderr.write(
        f"\nCould not import a required module. Run from project root:\n"
        f"    cd \"/path/to/job-agent\"\n"
        f"    python3 src/07_interview/mock.py --round behavioral\n\n"
        f"Original error: {e}\n"
    )
    sys.exit(2)


SEED_PATH = HERE / "seed_questions.yaml"
TRANSCRIPTS_DIR = PROJECT_ROOT / "data" / "interviews"

MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_FOLLOWUPS = 2

# Friendly heading for printing the round banner
ROUND_HEADINGS = {
    "recruiter":       "Recruiter screen",
    "hiring_manager":  "Hiring manager",
    "behavioral":      "Behavioral",
    "product_sense":   "Product sense / design",
    "execution":       "Execution / analytical",
    "strategy":        "Strategy",
    "technical":       "Lite technical PM",
}

VALID_ROUNDS = list(ROUND_HEADINGS.keys())


# ============================================================================
# Helpers
# ============================================================================

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_filename_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _load_seed() -> list[dict]:
    try:
        import yaml  # noqa: WPS433
    except ImportError:
        sys.stderr.write(
            "PyYAML is required. Install once with:\n"
            "    pip3 install --user pyyaml\n"
        )
        sys.exit(2)

    if not SEED_PATH.exists():
        sys.stderr.write(f"Seed bank not found at {SEED_PATH}\n")
        sys.exit(2)

    with SEED_PATH.open() as f:
        data = yaml.safe_load(f)
    return [q for q in (data or {}).get("questions", []) if isinstance(q, dict)]


def _pick_question(
    seed: list[dict], *, round_type: str | None,
    question_id: str | None, rng: random.Random,
    used_ids: set[str],
) -> dict | None:
    """
    Pick a question. If `question_id` is given, find it exactly. Otherwise
    pick a random un-used question matching round_type.
    """
    if question_id:
        for q in seed:
            if q["id"] == question_id:
                return q
        return None
    pool = [q for q in seed
            if q["round"] == round_type and q["id"] not in used_ids]
    if not pool:
        # Reset the "used" set if we've exhausted the round (lets long sessions continue)
        pool = [q for q in seed if q["round"] == round_type]
        if not pool:
            return None
    return rng.choice(pool)


def _read_multiline_answer() -> str | None:
    """
    Read user input until a line containing only DONE, SKIP, or QUIT, or until EOF.
    Returns None for SKIP, raises SystemExit for QUIT.
    """
    print("(Answer below. End with a line that's just DONE,")
    print(" or Ctrl+D. Type SKIP to skip this question, QUIT to end.)")
    print()
    lines: list[str] = []
    try:
        while True:
            line = input("> ")
            stripped = line.strip().upper()
            if stripped == "DONE":
                break
            if stripped == "SKIP":
                return None
            if stripped == "QUIT":
                raise SystemExit("session ended by user")
            lines.append(line)
    except EOFError:
        print()  # newline so the next print isn't on the same line as the prompt
    text = "\n".join(lines).strip()
    return text


# ============================================================================
# Prompting
# ============================================================================

INTERVIEWER_SYSTEM_PROMPT = """You are a senior Product Manager interviewer at an established tech company. You are conducting a {round_heading} round with an experienced candidate interviewing for a senior Product Manager role.

Your behavior:
- Push for specifics. If the candidate gives a vague answer ("we improved engagement"), probe for the actual mechanism, the actual metric, the actual decision they personally made.
- Ask exactly ONE focused follow-up at a time — never a list of questions.
- Stay in role as the interviewer until I send you a message containing "GIVE_FEEDBACK". Then drop the role and produce a structured debrief.
- Never reveal the rubric, hint at what you're looking for, or give a "model answer" until the GIVE_FEEDBACK signal.
- Be challenging but professional. No flattery, no fake encouragement.

Round context: {round_blurb}

The opening question for this round is:

{question_prompt}

Wait for the candidate's answer in the next user message, then respond as the interviewer would: ask ONE focused follow-up. Keep your follow-up to 1-2 sentences."""


FEEDBACK_TRIGGER = "GIVE_FEEDBACK"

FEEDBACK_INSTRUCTION = """{trigger}

End the round. Produce structured feedback in this exact format:

## What worked
- (2-4 short bullets — be specific, cite phrases the candidate used)

## Gaps
- (2-4 short bullets — what made the answer weaker than it could have been)

## Framework or structure that was missing
(2-3 sentences. Name a specific framework or structural move that would have improved the answer.)

## A model answer (concise reference)
(2-3 short paragraphs. Show, don't describe. The candidate should be able to compare their answer side-by-side with this one.)

Be honest, not flattering. If the answer was strong, say so directly and explain why; if it was weak, say so directly and show what would have been better.""".format(trigger=FEEDBACK_TRIGGER)


# ============================================================================
# Anthropic call wrapper with cost tracking
# ============================================================================

def _send(client, tracker: CostTracker, *,
          system: str, messages: list[dict], max_tokens: int = 1024) -> str:
    """Single Anthropic call. Records token usage. Returns assistant text."""
    response = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
    )
    usage = response.usage
    tracker.record(
        MODEL,
        input_tokens=getattr(usage, "input_tokens", 0),
        output_tokens=getattr(usage, "output_tokens", 0),
        cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
    )
    # Concatenate any text blocks (Sonnet always returns one in this flow).
    parts = [getattr(b, "text", "") for b in response.content if hasattr(b, "text")]
    return "".join(parts).strip()


# ============================================================================
# Run one full round (one question + follow-ups + feedback)
# ============================================================================

def _print_banner(round_type: str, q: dict, idx: int, total: int) -> None:
    heading = ROUND_HEADINGS.get(round_type, round_type)
    bar = "─" * 64
    print(f"\n{bar}")
    print(f"[ROUND] {heading}  ·  Question {idx} of {total}  ·  id: {q['id']}")
    print(bar)
    print()
    print("INTERVIEWER:")
    print(f"  {q['prompt']}")
    print()


def _format_transcript(round_type: str, q: dict, turns: list[dict],
                       feedback: str, started_at: str, ended_at: str) -> str:
    """Build the markdown transcript file."""
    heading = ROUND_HEADINGS.get(round_type, round_type)
    lines = [
        f"# Mock interview — {heading}",
        "",
        f"**Question id:** `{q['id']}`  ",
        f"**Started:** {started_at}  ",
        f"**Ended:** {ended_at}  ",
        "",
        "## Question",
        "",
        f"> {q['prompt']}",
        "",
    ]
    for t in turns:
        if t["role"] == "candidate":
            lines.append("### You")
            lines.append("")
            lines.append(t["content"])
            lines.append("")
        elif t["role"] == "interviewer_followup":
            lines.append("### Interviewer follow-up")
            lines.append("")
            lines.append(t["content"])
            lines.append("")
    lines.append("## Feedback")
    lines.append("")
    lines.append(feedback)
    lines.append("")
    return "\n".join(lines)


def _save_session_row(*, round_type: str, q: dict, started_at: str,
                      ended_at: str, transcript_path: Path,
                      feedback_summary: str, cost_usd: float) -> None:
    """Append one row to mock_sessions."""
    conn = sqlite3.connect(_db_path())
    try:
        ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO mock_sessions
              (job_id, round, question_id, question_prompt,
               started_at, ended_at, transcript_path,
               feedback_summary, cost_usd)
            VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (round_type, q["id"], q["prompt"],
             started_at, ended_at, str(transcript_path),
             feedback_summary[:1000], cost_usd),
        )
        conn.commit()
    finally:
        conn.close()


def _db_path() -> str:
    """Reach Layer 03's resolved DB_PATH via the same import path."""
    sys.path.insert(0, str(HERE.parent / "03_scrape"))
    from db import DB_PATH  # type: ignore
    return str(DB_PATH)


def run_one_round(
    client, tracker: CostTracker, *,
    round_type: str,
    question: dict,
    max_followups: int,
    no_followups: bool,
    out_path: Path | None,
    idx: int = 1,
    total: int = 1,
) -> dict | None:
    """
    Returns a dict summary of the round, or None if the user skipped.
    """
    started_at = _now_iso()
    _print_banner(round_type, question, idx, total)

    # First answer
    answer = _read_multiline_answer()
    if answer is None:
        print("\n(skipped)")
        return None
    if not answer:
        print("\n(empty answer — skipping)")
        return None

    # Conversation state — keep it small to control cost.
    system = INTERVIEWER_SYSTEM_PROMPT.format(
        round_heading=ROUND_HEADINGS.get(round_type, round_type),
        round_blurb={
            "recruiter":       "Fit, motivation, comp, logistics.",
            "hiring_manager":  "Background depth, role fit, working style.",
            "behavioral":      "STAR-format stories about past experience.",
            "product_sense":   "Open-ended product thinking.",
            "execution":       "Metric diagnostics, prioritization, estimation.",
            "strategy":        "Competitive, market, business model questions.",
            "technical":       "Communicating about technical concepts to non-technical partners.",
        }.get(round_type, ""),
        question_prompt=question["prompt"],
    )
    messages: list[dict] = [{"role": "user", "content": answer}]
    turns: list[dict] = [{"role": "candidate", "content": answer}]

    # Follow-up loop
    if not no_followups:
        for followup_num in range(max_followups):
            try:
                followup = _send(client, tracker, system=system, messages=messages,
                                 max_tokens=400)
            except Exception as e:  # noqa: BLE001 — catch-all here is intentional
                print(f"\n  ⚠ API error during follow-up {followup_num+1}: {e}")
                break

            print()
            print("INTERVIEWER (follow-up):")
            for line in followup.splitlines():
                print(f"  {line}")
            print()
            messages.append({"role": "assistant", "content": followup})
            turns.append({"role": "interviewer_followup", "content": followup})

            answer = _read_multiline_answer()
            if answer is None:
                print("\n(skipped follow-up)")
                break
            if not answer:
                break
            messages.append({"role": "user", "content": answer})
            turns.append({"role": "candidate", "content": answer})

    # Final feedback
    print("\n" + "─" * 64)
    print("Generating feedback…")
    messages.append({"role": "user", "content": FEEDBACK_INSTRUCTION})
    try:
        feedback = _send(client, tracker, system=system, messages=messages,
                         max_tokens=1500)
    except Exception as e:  # noqa: BLE001
        feedback = f"(Could not generate feedback — API error: {e})"

    ended_at = _now_iso()

    # Print + save
    print("\n" + "═" * 64)
    print("FEEDBACK")
    print("═" * 64)
    print()
    print(feedback)
    print()

    transcript = _format_transcript(round_type, question, turns, feedback,
                                    started_at, ended_at)
    if out_path is None:
        TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        out_path = TRANSCRIPTS_DIR / f"mock_{round_type}_{_now_filename_ts()}.md"
    else:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(transcript)
    print(f"Transcript saved: {out_path}")

    # Best-effort cost-per-round for the DB summary
    cost_so_far = tracker.cost_usd()
    _save_session_row(
        round_type=round_type, q=question,
        started_at=started_at, ended_at=ended_at,
        transcript_path=out_path,
        feedback_summary=feedback.split("\n\n", 1)[0] if feedback else "",
        cost_usd=cost_so_far,  # cumulative; per-round cost requires snapshotting before/after
    )

    return {"question_id": question["id"], "transcript": str(out_path)}


# ============================================================================
# Top-level orchestrator
# ============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run an interactive mock PM interview from the seed bank."
    )
    parser.add_argument("--round", choices=VALID_ROUNDS,
                        help="Which round type to practice.")
    parser.add_argument("--question", default=None,
                        help="Specific question id from seed_questions.yaml "
                             "(overrides --round selection).")
    parser.add_argument("--count", type=int, default=1,
                        help="How many questions to do in this session (default 1).")
    parser.add_argument("--max-followups", type=int, default=DEFAULT_MAX_FOLLOWUPS,
                        help=f"Max follow-ups per question (default {DEFAULT_MAX_FOLLOWUPS}).")
    parser.add_argument("--no-followups", action="store_true",
                        help="Skip follow-ups; one answer then straight to feedback.")
    parser.add_argument("--out", default=None,
                        help="Custom transcript path (only meaningful with --count 1).")
    parser.add_argument("--seed", type=int, default=None,
                        help="Random seed for question selection.")
    args = parser.parse_args()

    if not args.round and not args.question:
        parser.error("Pick one: --round <name> or --question <id>")

    seed = _load_seed()
    if args.question:
        # Single explicit question. Round is inferred.
        match = next((q for q in seed if q["id"] == args.question), None)
        if not match:
            sys.stderr.write(f"No question with id '{args.question}'.\n")
            return 1
        if args.round and match["round"] != args.round:
            sys.stderr.write(
                f"Question {args.question} is round '{match['round']}', "
                f"not '{args.round}'. Drop --round or pick a matching id.\n"
            )
            return 1
        target_round = match["round"]
        questions_planned = [match]
    else:
        target_round = args.round
        rng = random.Random(args.seed)
        used: set[str] = set()
        questions_planned = []
        for _ in range(args.count):
            q = _pick_question(seed, round_type=target_round, question_id=None,
                                rng=rng, used_ids=used)
            if q is None:
                sys.stderr.write(f"No questions for round '{target_round}'.\n")
                return 1
            questions_planned.append(q)
            used.add(q["id"])

    # Make sure the schema is in place (idempotent — first run after install)
    ensure_schema()

    # Spin up Anthropic client + cost tracker
    client = get_client()
    tracker = CostTracker()

    print(f"\nMock interview — {ROUND_HEADINGS[target_round]}  ·  "
          f"{len(questions_planned)} question(s)")
    print(f"Model: {MODEL}")
    if args.no_followups:
        print("Mode: single-answer (no follow-ups)")

    completed = 0
    summaries: list[dict] = []
    try:
        for idx, q in enumerate(questions_planned, start=1):
            out_path = Path(args.out) if (args.out and len(questions_planned) == 1) else None
            result = run_one_round(
                client, tracker,
                round_type=target_round,
                question=q,
                max_followups=args.max_followups,
                no_followups=args.no_followups,
                out_path=out_path,
                idx=idx, total=len(questions_planned),
            )
            if result:
                summaries.append(result)
                completed += 1
            if idx < len(questions_planned):
                print()
                go = input("Continue to next question? [Y/n] ").strip().lower()
                if go and go != "y":
                    break
    except SystemExit as exit_err:
        print(f"\n{exit_err}")
    except KeyboardInterrupt:
        print("\n\nSession interrupted (Ctrl+C).")

    # Final summary
    print("\n" + "═" * 64)
    print(f"Session complete — {completed} question(s) answered")
    print("═" * 64)
    for s in summaries:
        print(f"  {s['question_id']:<22}  →  {s['transcript']}")
    print()
    print(tracker.report())
    print()

    return 0 if completed > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
