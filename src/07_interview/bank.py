"""
bank.py — Render the seed PM question bank as browsable markdown.

WHAT THIS DOES (in plain English):
  Reads `seed_questions.yaml`, groups questions by round, and writes a
  categorized markdown file at `data/interviews/general_bank.md` that you can
  open in any markdown viewer (Obsidian, VS Code, your notes app) and skim
  whenever you want to see what kind of questions exist before practicing
  live with mock.py.

  This is the cheapest, fastest way to get value from Layer 07: zero API
  calls, zero cost, runs in under a second.

HOW TO RUN IT:
    cd "/path/to/job-agent"
    python3 src/07_interview/bank.py

USEFUL FLAGS:
    --out path/to/file.md     Write to a different file
    --rounds behavioral,strategy   Only include certain rounds
    --no-notes                Hide the interviewer-hint notes (cleaner for
                              candidate-facing browsing)

EXIT CODES:
    0 = wrote the file successfully
    1 = no questions matched the requested rounds
    2 = could not start (missing yaml file or pyyaml dep)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
SEED_PATH = HERE / "seed_questions.yaml"
DEFAULT_OUT = PROJECT_ROOT / "data" / "interviews" / "general_bank.md"

# Display order + friendly headings for each round type.
ROUND_HEADINGS = [
    ("recruiter",       "Recruiter screen",
     "Fit, motivation, comp, logistics. Usually 30-45 minutes."),
    ("hiring_manager",  "Hiring manager",
     "Deeper background dive, role fit, working style."),
    ("behavioral",      "Behavioral",
     "STAR-format stories about past experience. Universal across companies."),
    ("product_sense",   "Product sense / design",
     "Open-ended product thinking. 45-60 minutes."),
    ("execution",       "Execution / analytical",
     "Metric diagnostics, prioritization, estimation."),
    ("strategy",        "Strategy",
     "Competitive, market, business model questions."),
    ("technical",       "Lite technical PM",
     "Communicating about technical concepts, not coding."),
]

ROUND_LOOKUP = {key: (heading, blurb) for key, heading, blurb in ROUND_HEADINGS}


def _load_questions() -> list[dict]:
    """Load and validate the seed YAML."""
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

    questions = (data or {}).get("questions", [])
    if not questions:
        sys.stderr.write("Seed bank is empty.\n")
        sys.exit(2)

    # Validate required fields without crashing on one bad row.
    valid: list[dict] = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        if not all(k in q for k in ("id", "round", "prompt")):
            sys.stderr.write(f"Skipping malformed question: {q!r}\n")
            continue
        valid.append(q)
    return valid


def _filter_rounds(questions: list[dict], allowed: list[str] | None) -> list[dict]:
    if allowed is None:
        return questions
    allow_set = {r.strip().lower() for r in allowed if r.strip()}
    return [q for q in questions if q.get("round", "").lower() in allow_set]


def _render_markdown(questions: list[dict], *, show_notes: bool) -> str:
    """Group by round in display order, render to markdown."""
    by_round: dict[str, list[dict]] = {}
    for q in questions:
        by_round.setdefault(q["round"], []).append(q)

    lines: list[str] = [
        "# PM Interview Question Bank — General",
        "",
        "Canonical PM interview questions across the rounds you'll typically see "
        "at Bay Area tech companies. Generated from "
        "`src/07_interview/seed_questions.yaml`.",
        "",
        f"Total questions: **{len(questions)}**",
        "",
        "Practice live against any of these with:",
        "",
        "```bash",
        "python3 src/07_interview/mock.py --round behavioral",
        "# or pick a specific question:",
        "python3 src/07_interview/mock.py --question behavioral_07",
        "```",
        "",
        "---",
        "",
    ]

    for round_key, heading, blurb in ROUND_HEADINGS:
        bucket = by_round.get(round_key, [])
        if not bucket:
            continue
        lines.append(f"## {heading}")
        lines.append("")
        lines.append(f"_{blurb}_  ·  **{len(bucket)} questions**")
        lines.append("")
        for q in bucket:
            lines.append(f"### `{q['id']}`")
            lines.append("")
            lines.append(f"> {q['prompt']}")
            lines.append("")
            if show_notes and q.get("notes"):
                lines.append(f"_What they're really probing: {q['notes']}_")
                lines.append("")
        lines.append("---")
        lines.append("")

    # Catch any rounds that exist in the YAML but aren't in our display order
    # so we never silently drop questions.
    extras = [q for q in questions if q["round"] not in ROUND_LOOKUP]
    if extras:
        lines.append("## Other")
        lines.append("")
        for q in extras:
            lines.append(f"- **`{q['id']}`** ({q['round']}): {q['prompt']}")
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render the canonical PM question bank as markdown."
    )
    parser.add_argument(
        "--out", default=str(DEFAULT_OUT),
        help=f"Output path (default: {DEFAULT_OUT.relative_to(PROJECT_ROOT)})",
    )
    parser.add_argument(
        "--rounds", default=None,
        help="Comma-separated rounds to include (default: all). "
             "Choices: " + ", ".join(k for k, _, _ in ROUND_HEADINGS),
    )
    parser.add_argument(
        "--no-notes", action="store_true",
        help="Hide interviewer-hint notes (cleaner for candidate-side browsing)",
    )
    args = parser.parse_args()

    questions = _load_questions()
    if args.rounds:
        questions = _filter_rounds(questions, args.rounds.split(","))
        if not questions:
            sys.stderr.write(f"No questions matched rounds: {args.rounds}\n")
            return 1

    md = _render_markdown(questions, show_notes=not args.no_notes)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md)

    print(f"Wrote {len(questions)} questions to {out_path}")
    print(f"Open it with: open \"{out_path}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
