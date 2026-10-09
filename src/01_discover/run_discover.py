"""
run_discover.py — Orchestrator for Layer 01. The script you actually run.

WHAT THIS DOES (in plain English):
  1. Loads the discovery pool, the watchlist, and the discovery thresholds.
  2. For every candidate (companies in the pool that aren't already on the
     watchlist), probes their public Greenhouse / Lever board to count open
     PM roles right now.
  3. Scores each candidate 0–100 (deterministic — see score.py).
  4. Sorts each into one of three buckets:
       AUTO_ADD → appended to watchlist.yaml so Layer 03 picks them up tonight
       REVIEW   → written to the daily JSON for you to review and promote later
       DROP     → silently below the queue threshold
  5. Writes data/discovery/YYYY-MM-DD.json with the full results.
  6. Prints a tidy summary.

HOW TO RUN IT:

  cd "/path/to/job-agent"
  python3 src/01_discover/run_discover.py

  Useful flags:
    --dry-run         # Probe + score, but DON'T modify watchlist.yaml.
                      # The JSON still gets written. Use this the first few
                      # times you run discovery to see what it WOULD do.
    --limit N         # Only probe the first N candidates. Handy when adding
                      # entries to the pool and you want to test fast.
    --no-json         # Skip writing the JSON file (printing-only).

WHERE THIS FITS IN THE NIGHTLY LOOP:
  Conceptually Layer 01 should run BEFORE Layer 03 so newly auto-added
  companies get scraped the same night. So the recommended cron order is:

    python3 src/01_discover/run_discover.py
    python3 src/03_scrape/run_all.py
    python3 src/02_score/run_score.py
    python3 src/05_act/digest.py

  But running it weekly (or even monthly) is fine too — most of the discovery
  pool changes slowly. The auto-add cap in criteria.yaml prevents surprises.

EXIT CODES:
  0 = ran cleanly (whether or not anything was added)
  1 = ran but every candidate's probe failed (suggests network blocked)
  2 = couldn't even start (missing config, missing PyYAML, etc.)
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from pool_loader import load_candidates, diagnostics
    from probe import probe_company
    from score import score_company, route
    from output import write_daily_json, append_to_watchlist
except ImportError as e:
    sys.stderr.write(
        "\nCould not import the Layer 01 modules. Run from the project root:\n"
        "    cd \"/path/to/job-agent\"\n"
        "    python3 src/01_discover/run_discover.py\n\n"
        f"Original error: {e}\n"
    )
    sys.exit(2)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Layer 01 — Discover new companies.")
    p.add_argument("--dry-run", action="store_true",
                   help="Don't modify watchlist.yaml; only write the JSON report.")
    p.add_argument("--limit", type=int, default=None,
                   help="Only probe the first N candidates (for fast testing).")
    p.add_argument("--no-json", action="store_true",
                   help="Skip writing the daily JSON file.")
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    print("=" * 60)
    print(" Layer 01 — Discover")
    print("=" * 60)

    candidates, settings = load_candidates()
    diag = diagnostics()

    print(
        f" Pool: {diag['pool_total']} entries · "
        f"{diag['candidates']} candidates after pre-filters\n"
        f"   (filtered out — disabled: {diag['disabled']}, "
        f"already_tracked: {diag['already_on_watchlist']}, "
        f"sector_excluded: {diag['sector_excluded']}, "
        f"wrong_ats: {diag['wrong_ats']}, "
        f"no_slug: {diag['no_slug']}, "
        f"not_bay_area: {diag['not_bay_area']}, "
        f"too_small: {diag['too_small']})"
    )
    print(
        f" Thresholds: auto-add ≥ {settings['auto_add_min_score']}, "
        f"queue ≥ {settings['queue_min_score']}, "
        f"max-auto-adds = {settings['max_auto_adds_per_run']}"
    )
    if args.dry_run:
        print(" Mode: DRY RUN — watchlist.yaml will NOT be modified.")
    print()

    if not candidates:
        print(" No candidates to probe. Nothing to do.\n")
        if not args.no_json:
            out = write_daily_json(results=[], settings=settings)
            print(f" Empty discovery JSON written to {out}\n")
        return 0

    if args.limit is not None:
        candidates = candidates[: max(0, args.limit)]
        print(f" --limit {args.limit} → probing {len(candidates)} candidates this run.\n")

    # ---------- Probe + score every candidate ----------
    results: list[dict] = []
    for i, company in enumerate(candidates, 1):
        name = company.get("name", "?")
        ats = company.get("ats", "?")
        slug = company.get("ats_slug", "?")
        print(f" [{i:>2}/{len(candidates)}] {name} ({ats}: {slug})")

        probe = probe_company(company)
        if not probe["ok"]:
            print(f"      ⚠ probe failed: {probe['error']} — skipping scoring")
            results.append({
                "name": name, "ats": ats, "ats_slug": slug,
                "sector": company.get("sector"),
                "employees_est": company.get("employees_est"),
                "score": 0,
                "breakdown": None,
                "rationale": f"probe failed: {probe['error']}",
                "action": "probe_failed",
                "probe": probe,
            })
            continue

        scored = score_company(
            company=company, probe_result=probe, min_employees=settings["min_employees"]
        )
        action = route(score_total=scored["total"], settings=settings)

        results.append({
            "name": name, "ats": ats, "ats_slug": slug,
            "sector": company.get("sector"),
            "employees_est": company.get("employees_est"),
            "score": scored["total"],
            "breakdown": scored["breakdown"],
            "rationale": scored["rationale"],
            "action": action,
            "probe": {
                "total_jobs":   probe["total_jobs"],
                "pm_total":     probe["pm_total"],
                "pm_bay_area":  probe["pm_bay_area"],
                "sample_titles": probe["sample_titles"],
            },
        })

        emoji = {"auto_add": "✅", "review": "📝", "drop": "—"}.get(action, "?")
        print(f"      {emoji} score: {scored['total']:>3}/100 — {scored['rationale']}  [{action}]")

    # ---------- Decide which winners to actually add ----------
    auto_adds = [r for r in results if r["action"] == "auto_add"]
    auto_adds.sort(key=lambda r: -int(r["score"]))
    capped_winners = auto_adds[: settings["max_auto_adds_per_run"]]

    if len(auto_adds) > len(capped_winners):
        skipped = len(auto_adds) - len(capped_winners)
        print(f"\n ⚠ {skipped} more company(ies) cleared the auto-add bar but were "
              f"capped by max_auto_adds_per_run={settings['max_auto_adds_per_run']}. "
              f"They land in the JSON for review next time.")
        # Re-tag the capped ones as 'review' in the results so the JSON is honest.
        winner_names = {w["name"] for w in capped_winners}
        for r in results:
            if r["action"] == "auto_add" and r["name"] not in winner_names:
                r["action"] = "review"
                r["rationale"] += " (over auto-add cap — reclassified to review)"

    # ---------- Side effects ----------
    appended = 0
    if not args.dry_run and capped_winners:
        appended = append_to_watchlist(winners=capped_winners)

    out_path = None
    if not args.no_json:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out_path = write_daily_json(results=results, settings=settings, date_str=date_str)

    # ---------- Summary ----------
    print("\n" + "-" * 60)
    print(" Summary")
    print("-" * 60)
    print(f"  Probed:           {len(results)}")
    print(f"  Auto-add winners: {len(capped_winners)}"
          f"  ({'WROTE TO WATCHLIST' if appended else 'dry-run / nothing to write'})")
    print(f"  Queued (review):  {sum(1 for r in results if r['action'] == 'review')}")
    print(f"  Dropped:          {sum(1 for r in results if r['action'] == 'drop')}")
    print(f"  Probe failures:   {sum(1 for r in results if r['action'] == 'probe_failed')}")
    if out_path:
        print(f"  JSON written to:  {out_path}")
    print()

    if appended:
        print(f" ✅ Appended {appended} company(ies) to watchlist.yaml. "
              f"Layer 03 will pick them up on the next scrape.\n")

    # If literally every probe failed → return 1 so cron emails on failure.
    if results and all(r["action"] == "probe_failed" for r in results):
        print(" ⚠ Every probe failed — likely a network/DNS issue. Exit 1.\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
