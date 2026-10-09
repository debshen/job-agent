#!/bin/bash
# =============================================================================
# run_nightly.sh — The single command launchd fires every morning.
#
# WHAT THIS DOES:
#   Runs some subset of (discover → scrape → score → digest), picked by the
#   first argument:
#
#     bash run_nightly.sh data     → discover + scrape + score (the SLOW work)
#     bash run_nightly.sh digest   → just the digest email
#     bash run_nightly.sh all      → everything in sequence (manual / fallback)
#     bash run_nightly.sh          → same as `all` — backward-compatible
#
#   Each step writes to a daily log (data/logs/run-YYYY-MM-DD.log). On any
#   failure the wrapper calls notify_failure.py to email a short alert and
#   exits with a step-specific code (so launchd records it).
#
# WHY WE SPLIT IT:
#   The score step takes 1.5–2.5 hours on a ~30-company watchlist (one
#   sequential Anthropic API call per job, no concurrency yet). Bundling it
#   with the digest meant the email arrived 1–2 hours after its scheduled
#   time. Splitting lets the heavy data work run early (4:30 AM) and the
#   tiny digest job (~2 seconds) hit your inbox at 7:00 AM exactly.
#
# HOW TO TEST IT MANUALLY (without waiting for launchd):
#   bash /path/to/job-agent/scripts/run_nightly.sh data
#   bash /path/to/job-agent/scripts/run_nightly.sh digest
#
# WHERE LOGS GO:
#   data/logs/run-YYYY-MM-DD.log    (one per day, both modes append to it)
#
# EXIT CODES:
#    0   all selected steps succeeded
#    2   couldn't even start (project dir missing, etc.)
#    9   discover failed
#   10   scrape   failed
#   11   score    failed
#   12   digest   failed
# =============================================================================

set -u  # error on unset variables

# ---- Configuration -----------------------------------------------------------

PROJECT_DIR="/path/to/job-agent"
PYTHON="/usr/bin/python3"

# First argument selects the mode; default is 'all' for backward compat with
# any existing manual workflows.
MODE="${1:-all}"

case "$MODE" in
  data|digest|all) ;;
  *)
    echo "Usage: $0 [data|digest|all]" >&2
    echo "  data   = discover + scrape + score (the heavy stuff)" >&2
    echo "  digest = digest only (the quick email)" >&2
    echo "  all    = everything (default; for manual one-off runs)" >&2
    exit 2
    ;;
esac

cd "$PROJECT_DIR" || {
  echo "FATAL: project dir not found: $PROJECT_DIR" >&2
  exit 2
}

LOG_DIR="$PROJECT_DIR/data/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/run-$(date +%Y-%m-%d).log"

# ---- Helpers -----------------------------------------------------------------

log() {
  # Prepend timestamp and write to both stdout and the day log
  echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

run_step() {
  # Usage: run_step "step name" command arg1 arg2 ...
  local name="$1"
  shift
  log "=== START: $name ==="
  if "$@" >> "$LOG_FILE" 2>&1; then
    log "=== OK:    $name ==="
    return 0
  else
    local code=$?
    log "=== FAIL ($code): $name ==="
    return "$code"
  fi
}

notify_failure() {
  local step="$1"
  local code="$2"
  log "Calling notify_failure for step '$step' (exit $code)..."
  # Don't let a notifier failure prevent the wrapper from exiting cleanly
  "$PYTHON" "$PROJECT_DIR/scripts/notify_failure.py" \
    --step "$step" \
    --exit-code "$code" \
    --log "$LOG_FILE" \
    >> "$LOG_FILE" 2>&1 || log "notify_failure itself errored — see log above"
}

# ---- The pipeline ------------------------------------------------------------

log "Job Agent run starting (mode: $MODE)"
log "  Project: $PROJECT_DIR"
log "  Python:  $PYTHON"
log "  Log:     $LOG_FILE"

# ---- DATA mode (or ALL): discover → scrape → score --------------------------
if [[ "$MODE" == "data" || "$MODE" == "all" ]]; then

  # Step 0: discover (find new companies, auto-add winners to watchlist.yaml)
  if ! run_step "discover" "$PYTHON" "src/01_discover/run_discover.py"; then
    notify_failure "discover" $?
    exit 9
  fi

  # Step 1: scrape (walk every company in watchlist.yaml)
  if ! run_step "scrape" "$PYTHON" "src/03_scrape/run_all.py"; then
    notify_failure "scrape" $?
    exit 10
  fi

  # Step 2: score (only new jobs by default)
  if ! run_step "score" "$PYTHON" "src/02_score/run_score.py"; then
    notify_failure "score" $?
    exit 11
  fi

fi

# ---- DIGEST mode (or ALL): send the email ----------------------------------
if [[ "$MODE" == "digest" || "$MODE" == "all" ]]; then

  # Step 3: send digest
  if ! run_step "digest" "$PYTHON" "src/05_act/digest.py"; then
    notify_failure "digest" $?
    exit 12
  fi

fi

log "All selected steps OK (mode: $MODE)"
exit 0
