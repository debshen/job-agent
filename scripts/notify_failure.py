"""
notify_failure.py — Sends a short error email when a nightly run breaks.

Called by run_nightly.sh whenever any pipeline step exits non-zero. Reuses
the Resend setup from src/05_act/digest.py so we don't duplicate config.

ARGS (positional/keyword via argparse):
  --step       Which step failed: "scrape" / "score" / "digest"
  --exit-code  The exit code that step produced
  --log        Path to the day's log file (we attach the last ~80 lines)
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Wire up imports the same way digest.py does
HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
SCORE_DIR = PROJECT_ROOT / "src" / "02_score"
ACT_DIR   = PROJECT_ROOT / "src" / "05_act"
sys.path.insert(0, str(SCORE_DIR))
sys.path.insert(0, str(ACT_DIR))

try:
    from client import _load_dotenv  # type: ignore
    from digest import send_via_resend, DEFAULT_FROM, RESEND_API_URL  # type: ignore
except ImportError as e:
    sys.stderr.write(f"notify_failure: import error: {e}\n")
    sys.exit(2)


def _tail(path: Path, n: int = 80) -> str:
    """Return the last n lines of a text file. Tiny implementation; no tail dep."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except FileNotFoundError:
        return "(log file not found)"
    except Exception as e:
        return f"(could not read log: {e})"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--step", required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--log", required=True)
    args = parser.parse_args()

    _load_dotenv()
    api_key = os.environ.get("RESEND_API_KEY")
    recipient = os.environ.get("DIGEST_TO_EMAIL")
    if not (api_key and recipient):
        sys.stderr.write(
            "notify_failure: RESEND_API_KEY and DIGEST_TO_EMAIL must be set in .env\n"
        )
        return 2

    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    subject = f"⚠ Job Agent failed: {args.step} (exit {args.exit_code})"

    log_tail = _tail(Path(args.log))
    html = f"""
    <html>
      <body style="font-family: -apple-system, BlinkMacSystemFont, 'Inter', sans-serif;
                   color: #1A1A1A; max-width: 640px; margin: 0 auto; padding: 24px;
                   background: #F8F4E6;">
        <h2 style="margin: 0 0 8px;">⚠ Nightly run failed</h2>
        <p style="color:#6B6B6B; font-size:13px; margin: 0 0 16px;">
          {when}
        </p>
        <p style="font-size:14px; line-height:1.5;">
          The <b>{args.step}</b> step exited with code <b>{args.exit_code}</b>.
        </p>
        <p style="font-size:14px; line-height:1.5;">
          Last 80 lines of today's log:
        </p>
        <pre style="background:#FEFCF5; border:1px solid #EAE4D2; border-radius:8px;
                    padding:16px; font-size:12px; line-height:1.4;
                    overflow-x:auto; white-space:pre-wrap;">
{log_tail}</pre>
        <p style="color:#6B6B6B; font-size:12px;">
          To debug locally, run:<br>
          <code>bash "{PROJECT_ROOT}/scripts/run_nightly.sh"</code>
        </p>
      </body>
    </html>
    """

    try:
        resp = send_via_resend(
            api_key=api_key, sender=DEFAULT_FROM, recipient=recipient,
            subject=subject, html=html,
        )
        print(f"Failure notification sent. Resend id: {resp.get('id', '?')}")
        return 0
    except SystemExit as e:
        # send_via_resend uses SystemExit on error; convert to a normal exit
        # so the wrapper still exits with the original failure code, not 2
        sys.stderr.write(f"notify_failure: send failed: {e}\n")
        return 0


if __name__ == "__main__":
    sys.exit(main())
