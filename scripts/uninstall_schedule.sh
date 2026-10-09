#!/bin/bash
# =============================================================================
# uninstall_schedule.sh — Removes the nightly schedule cleanly.
#
# WHAT THIS DOES:
#   1. Unloads BOTH launchd jobs (the 04:30 data run and the 07:00 digest).
#   2. Removes both plists from ~/Library/LaunchAgents/.
#   3. Optionally removes the pmset wake schedule.
#
# Project files (the scrapers, scorers, etc.) are NOT touched. Just the
# scheduling layer.
# =============================================================================

LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
DATA_PLIST="com.example.jobagent.plist"
DIGEST_PLIST="com.example.jobagent.digest.plist"

echo "→ Unloading launchd jobs..."
launchctl unload "$LAUNCH_AGENTS/$DATA_PLIST"   2>/dev/null || true
launchctl unload "$LAUNCH_AGENTS/$DIGEST_PLIST" 2>/dev/null || true

echo "→ Removing plists..."
rm -f "$LAUNCH_AGENTS/$DATA_PLIST" "$LAUNCH_AGENTS/$DIGEST_PLIST"

echo "✓ launchd schedule removed (both data + digest jobs)."
echo

# Show current pmset wake schedule, if any
echo "Current pmset schedule:"
pmset -g sched
echo

read -p "Also remove the pmset wake schedule? [y/N] " -r answer
if [[ "$answer" =~ ^[Yy]$ ]]; then
    echo "→ Cancelling wake schedule (will prompt for password)..."
    sudo pmset repeat cancel
    echo "✓ Wake schedule removed."
else
    echo "Wake schedule left alone. Remove later with:  sudo pmset repeat cancel"
fi

echo
echo "Done. The Job Agent will no longer run automatically."
echo "You can still run it manually any time:"
echo "  bash \"/path/to/job-agent/scripts/run_nightly.sh\" all"
echo "  bash \"/path/to/job-agent/scripts/run_nightly.sh\" data"
echo "  bash \"/path/to/job-agent/scripts/run_nightly.sh\" digest"
