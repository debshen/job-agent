#!/bin/bash
# =============================================================================
# install_schedule.sh — One-shot installer for the nightly schedule.
#
# WHAT THIS DOES:
#   1. Makes the wrapper script executable.
#   2. Copies BOTH launchd plists into ~/Library/LaunchAgents/:
#        - com.example.jobagent.plist        → 04:30 AM data run
#          (discover + scrape + score; the slow part, ~1.5–2.5h)
#        - com.example.jobagent.digest.plist → 07:00 AM digest email
#          (just reads SQLite + sends email; ~2 seconds)
#   3. Loads both into launchd.
#   4. Optionally schedules a wake event at 04:25 AM so the Mac wakes 5
#      minutes before the heavy data run starts. (You'll be asked for
#      your password; this is a system-level setting.)
#
# HOW TO RUN IT (one-time, or any time you change the plists):
#   bash "/path/to/job-agent/scripts/install_schedule.sh"
#
#   Re-running is safe: it unloads the old version first, then loads fresh.
#
# HOW TO TEST EACH HALF MANUALLY:
#   launchctl start com.example.jobagent          # heavy data run
#   launchctl start com.example.jobagent.digest   # digest email
# =============================================================================

set -e  # fail fast on any error

PROJECT_DIR="/path/to/job-agent"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"

DATA_PLIST="com.example.jobagent.plist"
DIGEST_PLIST="com.example.jobagent.digest.plist"

echo "→ Making run_nightly.sh executable..."
chmod +x "$PROJECT_DIR/scripts/run_nightly.sh"

echo "→ Copying both plists to ~/Library/LaunchAgents/..."
mkdir -p "$LAUNCH_AGENTS"
cp "$PROJECT_DIR/scripts/$DATA_PLIST"   "$LAUNCH_AGENTS/$DATA_PLIST"
cp "$PROJECT_DIR/scripts/$DIGEST_PLIST" "$LAUNCH_AGENTS/$DIGEST_PLIST"

# Unload first in case they're already loaded (lets us re-run this safely)
echo "→ Unloading any prior versions..."
launchctl unload "$LAUNCH_AGENTS/$DATA_PLIST"   2>/dev/null || true
launchctl unload "$LAUNCH_AGENTS/$DIGEST_PLIST" 2>/dev/null || true

echo "→ Loading fresh plists..."
launchctl load "$LAUNCH_AGENTS/$DATA_PLIST"
launchctl load "$LAUNCH_AGENTS/$DIGEST_PLIST"

echo
echo "✓ launchd schedule installed (split):"
echo "    04:30 AM   com.example.jobagent          (data: discover + scrape + score)"
echo "    07:00 AM   com.example.jobagent.digest   (digest email)"
echo

# ---- Optional: pmset wake schedule ------------------------------------------
echo "Do you want to ALSO schedule your Mac to wake from sleep at 04:25 AM?"
echo "(This is a one-line system command — requires your password.)"
echo
echo "Without this, the data run is skipped on days your Mac is fully asleep."
echo "With it, your Mac wakes briefly at 04:25, runs scrape+score, sleeps again."
echo "(The digest job at 07:00 AM also benefits from the same wake schedule;"
echo "if it doesn't fire on time, launchd catches up the next time the Mac"
echo "wakes — usually within minutes when you open the lid in the morning.)"
echo
read -p "Schedule wake at 04:25 AM? [y/N] " -r answer
if [[ "$answer" =~ ^[Yy]$ ]]; then
    echo "→ Setting wake schedule (you'll be prompted for your password)..."
    # `pmset repeat` only allows ONE recurring wake event. The data run's
    # 04:25 AM wake is the more important one — without it, scrape+score
    # is skipped entirely on closed-lid nights.
    sudo pmset repeat wake MTWRFSU 4:25:00
    echo
    echo "✓ Mac will now wake at 04:25 AM daily."
    echo "  Check it any time with:  pmset -g sched"
    echo "  Remove it any time with: sudo pmset repeat cancel"
else
    echo "Skipped wake schedule. The data run will only fire on days your Mac"
    echo "is awake at 04:30 AM. (You can re-run this script later to enable.)"
fi

echo
echo "============================================================"
echo " Installation complete."
echo "============================================================"
echo
echo "Test each half manually (will fire right now):"
echo "  launchctl start com.example.jobagent          # data run"
echo "  launchctl start com.example.jobagent.digest   # digest only"
echo
echo "Watch today's log:"
echo "  tail -f \"$PROJECT_DIR/data/logs/run-\$(date +%Y-%m-%d).log\""
echo
echo "Check schedule status:"
echo "  launchctl list | grep com.example.jobagent"
echo
echo "Uninstall:"
echo "  bash \"$PROJECT_DIR/scripts/uninstall_schedule.sh\""
echo
