# Scheduling — running Job Agent nightly without lifting a finger

This folder turns the four manual commands (`discover → scrape → score → digest`) into a **split nightly schedule**: the heavy data work runs at 04:30 AM, and the digest email fires at 07:00 AM exactly. The schedule self-heals via launchd if your Mac was asleep, and emails you if anything breaks.

## Why split?

The score step takes ~1.5–2.5 hours on a ~30-company watchlist (one Anthropic API call per job, sequential — no concurrency yet). When the entire pipeline ran in one go at 06:50 AM, the digest email arrived at 08:30–09:30 AM, 1–2 hours late. Splitting it gives the heavy work plenty of runway and pins the email to a precise time.

## What's in this folder

| File | What it does |
|---|---|
| `run_nightly.sh` | The single wrapper. Takes one argument: `data` (discover + scrape + score), `digest` (digest only), or `all` (everything; for manual use). Logs to a daily file and emails you on failure. |
| `notify_failure.py` | Sends a short error email via Resend if any step fails. Reuses the same API key. |
| `com.example.jobagent.plist` | launchd schedule for the **04:30 AM data run**. |
| `com.example.jobagent.digest.plist` | launchd schedule for the **07:00 AM digest email**. |
| `install_schedule.sh` | One-shot installer — copies BOTH plists, loads them, optionally schedules `pmset` wake. |
| `uninstall_schedule.sh` | Reverses the install. Project files untouched. |

## One-time setup (5 minutes)

Open Terminal and run:

```bash
bash "/path/to/job-agent/scripts/install_schedule.sh"
```

The installer will:

1. Make `run_nightly.sh` executable.
2. Copy both plists into `~/Library/LaunchAgents/`.
3. Load them so they fire on schedule (04:30 AM data, 07:00 AM digest).
4. **Ask** if you also want to schedule your Mac to wake at 04:25 AM (recommended — without it, the data run is skipped on closed-lid nights). It'll prompt for your Mac password (the project itself never sees your password).

After install, you'll see help for testing and checking logs.

If you've already installed an older version (the single 06:50 AM schedule), just re-run the installer — it unloads the old job, removes any old plist, and installs both fresh. **One thing to do once after re-installing:** update the wake time from 06:45 to 04:25:

```bash
sudo pmset repeat cancel
sudo pmset repeat wake MTWRFSU 4:25:00
```

(The installer offers to do this for you when you opt-in.)

## How to test each half manually

```bash
# Heavy data run — discover + scrape + score (will take 1.5–2.5 hours):
launchctl start com.example.jobagent

# Just the digest email (~2 seconds):
launchctl start com.example.jobagent.digest
```

Watch today's log:

```bash
tail -f "/path/to/job-agent/data/logs/run-$(date +%Y-%m-%d).log"
```

Press Ctrl-C to stop tailing.

You can also run the wrapper directly without launchd:

```bash
bash scripts/run_nightly.sh data     # the slow stuff
bash scripts/run_nightly.sh digest   # just the email
bash scripts/run_nightly.sh all      # everything in one go (manual)
bash scripts/run_nightly.sh          # same as `all` (backward compat)
```

## How the wake-from-sleep works (if you opted in)

The installer optionally runs:

```bash
sudo pmset repeat wake MTWRFSU 4:25:00
```

That tells macOS to wake your Mac from sleep every day at 04:25 AM, 5 minutes before the data run starts. After scrape + score finishes (~06:30 AM at worst), your Mac goes back to sleep. The 07:00 AM digest fires whenever launchd next sees the Mac awake — usually within a minute or two of the scheduled time, because the previous wake event leaves the system in a state where the alarm fires reliably.

`pmset repeat` only supports ONE recurring wake event. Since the data run is the one that genuinely needs the Mac awake (the digest is just an SQLite read + an HTTP POST), we picked 04:25 AM.

**Power scenarios:**

- Lid open + plugged in → both jobs fire on schedule
- Lid open + on battery → both jobs fire (uses ~1–2% battery)
- Lid closed + plugged in (clamshell) → both jobs fire
- Lid closed + on battery → does NOT wake (Apple disables wake-on-battery to preserve battery; data run is skipped, digest fires when you open the lid in the morning)
- Powered fully off → does NOT wake (`pmset` can't turn on a Mac, only wake from sleep)

So as long as you don't fully shut down at night, you're covered.

**Check or change the wake schedule:**

```bash
pmset -g sched              # see current
sudo pmset repeat cancel    # remove it
sudo pmset repeat wake MTWRFSU 5:00:00   # change to 5:00 AM
```

## How the times work (if you want to change them)

Two places to update if you want different schedule times:

1. **Wake time** — update `pmset` (above)
2. **Job times** — edit one of the plists:
   - `~/Library/LaunchAgents/com.example.jobagent.plist` (data run)
   - `~/Library/LaunchAgents/com.example.jobagent.digest.plist` (digest)

   Change the `<integer>` values for `Hour` and `Minute`, then reload:

   ```bash
   launchctl unload ~/Library/LaunchAgents/com.example.jobagent.plist
   launchctl load ~/Library/LaunchAgents/com.example.jobagent.plist
   ```

Wake should be ~5 minutes before the data run start time. The digest can be any time, but make sure it's after the data run finishes — at the current 04:30 start, scoring up to 2.5 hours fits inside the window before 07:00.

## Where logs live

Three places, increasing detail:

- `data/logs/launchd-stdout.log` — anything launchd itself prints (rare, only on catastrophic failure)
- `data/logs/launchd-stderr.log` — same, but errors
- `data/logs/run-YYYY-MM-DD.log` — **the useful one.** One per day. Has timestamped output from every step. Both the 04:30 data run and the 07:00 digest run append to the same daily file, so you get a single timeline view.

Old logs accumulate forever — you can clean periodically:

```bash
find "/path/to/job-agent/data/logs" -name "run-*.log" -mtime +30 -delete
```

## Status checks

```bash
# Are both jobs loaded into launchd?
launchctl list | grep com.example.jobagent

# Is the wake schedule set?
pmset -g sched

# What did this morning's runs do?
cat "/path/to/job-agent/data/logs/run-$(date +%Y-%m-%d).log"
```

## Failure behavior

If any step (discover / scrape / score / digest) exits non-zero, `run_nightly.sh` calls `notify_failure.py`, which sends you an email via Resend with:

- Which step failed
- The exit code
- The last 80 lines of the day's log

You'll see it in your inbox within ~1 minute of the failure. Subject prefix: `⚠ Job Agent failed: ...`

The wrapper itself exits with a distinct code per step:

| Code | Meaning |
|---|---|
| 9  | discover failed |
| 10 | scrape   failed |
| 11 | score    failed |
| 12 | digest   failed |

Common failure causes & fixes:

| Symptom | Likely cause | Fix |
|---|---|---|
| `discover` fails repeatedly | Greenhouse / Lever rate-limited you (rare) | Wait 24h; reduce pool size |
| `scrape` fails with HTTP 403 / 503 | A company's ATS API is down | Wait, retry tomorrow |
| `score` fails with auth error | `ANTHROPIC_API_KEY` expired | Generate new key on console.anthropic.com, update `.env` |
| `digest` fails with Cloudflare 1010 | Resend bot detection (rare since httpx fix) | Re-run; if persistent, regenerate Resend key |
| Data run skipped silently | Mac was asleep at 04:30 AM | Set up `pmset` wake |
| Digest arrived 5+ minutes late | Mac was asleep at 07:00; launchd caught up at next wake | Normal on closed-lid days; for stricter timing move to cloud |

## Uninstall

```bash
bash "/path/to/job-agent/scripts/uninstall_schedule.sh"
```

Removes both launchd jobs and (if you confirm) the wake schedule. Your project files, database, and `.env` are untouched. You can still run any of the four commands manually.

## When you eventually want to move to the cloud

For when you outgrow laptop scheduling (e.g., traveling without your Mac, or just want bulletproof timing):

- **GitHub Actions**: free for public repos, ~2,000 free minutes/month for private. The whole pipeline costs about 5 minutes per run, well within free tier. Trade-off: requires committing the project to a Git repo and storing API keys as GitHub Secrets.
- **Railway / Render / Fly.io**: ~$5/month, runs as a cron job in a container. Most flexible, smallest infrastructure burden. The split-schedule pattern translates 1-to-1: two cron entries instead of one.
