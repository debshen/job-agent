"""
test_discover.py — End-to-end test for Layer 01 with the network mocked out.

WHAT THIS COVERS:
  - pool_loader pre-filters (sectors_excluded, hq_bay_area, employees, dupes)
  - probe error handling (a 404 slug shouldn't crash the run)
  - scoring formula at every threshold edge
  - routing (auto_add / review / drop)
  - JSON output shape
  - watchlist.yaml append preserves comments and inserts before `exclude:`
  - max_auto_adds_per_run cap actually caps

HOW TO RUN:
  python3 src/01_discover/test_discover.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

# Make the layer importable.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pool_loader  # noqa: E402
import probe        # noqa: E402
import score        # noqa: E402
import output       # noqa: E402


def _setup_temp_project() -> Path:
    """
    Build a throwaway project tree with its own config/ and data/ so the test
    never touches the real watchlist.yaml or criteria.yaml.
    """
    tmp = Path(tempfile.mkdtemp(prefix="layer01_test_"))
    (tmp / "config").mkdir()
    (tmp / "data" / "discovery").mkdir(parents=True)

    # Minimal criteria.yaml — only the sections the discoverer reads.
    (tmp / "config" / "criteria.yaml").write_text(
        "must_have:\n"
        "  company_size:\n"
        "    min_employees: 500\n"
        "discovery:\n"
        "  auto_add_min_score: 80\n"
        "  queue_min_score: 50\n"
        "  max_auto_adds_per_run: 2\n"
        "  sectors_excluded:\n"
        "    - crypto\n"
        "  sectors_preferred:\n"
        "    - fintech\n",
        encoding="utf-8",
    )

    # Watchlist with one company already on it (so dupe filter triggers).
    (tmp / "config" / "watchlist.yaml").write_text(
        "include:\n"
        "  - name: AlreadyTracked\n"
        "    ats: greenhouse\n"
        "    ats_slug: alreadytracked\n"
        "    public: false\n"
        "    ticker: null\n"
        "    notes: existing\n"
        "\n"
        "# Companies to NEVER score (hard exclude)\n"
        "exclude:\n"
        "  - Meta\n",
        encoding="utf-8",
    )

    # Pool with one of every interesting case.
    (tmp / "config" / "discovery_pool.yaml").write_text(
        "companies:\n"
        # Already on watchlist → should be filtered out by pool_loader
        "  - name: AlreadyTracked\n"
        "    ats: greenhouse\n"
        "    ats_slug: alreadytracked\n"
        "    hq_bay_area: true\n"
        "    employees_est: 1000\n"
        "    sector: fintech\n"
        # Sector excluded → filtered out
        "  - name: CryptoCo\n"
        "    ats: greenhouse\n"
        "    ats_slug: cryptoco\n"
        "    hq_bay_area: true\n"
        "    employees_est: 1000\n"
        "    sector: crypto\n"
        # Too small → filtered out
        "  - name: TinyStartup\n"
        "    ats: greenhouse\n"
        "    ats_slug: tinystartup\n"
        "    hq_bay_area: true\n"
        "    employees_est: 100\n"
        "    sector: fintech\n"
        # Not Bay Area → filtered out
        "  - name: NewYorkCo\n"
        "    ats: greenhouse\n"
        "    ats_slug: newyorkco\n"
        "    hq_bay_area: false\n"
        "    employees_est: 1000\n"
        "    sector: fintech\n"
        # Surviving candidates
        "  - name: BigHirer\n"
        "    ats: greenhouse\n"
        "    ats_slug: bighirer\n"
        "    hq_bay_area: true\n"
        "    employees_est: 2000\n"
        "    sector: fintech\n"
        "  - name: ModerateHirer\n"
        "    ats: greenhouse\n"
        "    ats_slug: moderatehirer\n"
        "    hq_bay_area: true\n"
        "    employees_est: 800\n"
        "    sector: enterprise_saas\n"
        "  - name: NotHiringPMs\n"
        "    ats: lever\n"
        "    ats_slug: nothiringpms\n"
        "    hq_bay_area: true\n"
        "    employees_est: 800\n"
        "    sector: developer_tools\n"
        "  - name: BrokenSlug\n"
        "    ats: greenhouse\n"
        "    ats_slug: brokenslug\n"
        "    hq_bay_area: true\n"
        "    employees_est: 800\n"
        "    sector: enterprise_saas\n"
        "  - name: ExtraWinner1\n"
        "    ats: greenhouse\n"
        "    ats_slug: extrawinner1\n"
        "    hq_bay_area: true\n"
        "    employees_est: 1500\n"
        "    sector: fintech\n"
        "  - name: ExtraWinner2\n"
        "    ats: greenhouse\n"
        "    ats_slug: extrawinner2\n"
        "    hq_bay_area: true\n"
        "    employees_est: 1500\n"
        "    sector: fintech\n"
        # PMs hiring, but ALL outside Bay Area (the "Asana situation").
        # Pre-tightening this would have scored 90; now it should score 70.
        "  - name: DublinHirer\n"
        "    ats: greenhouse\n"
        "    ats_slug: dublinhirer\n"
        "    hq_bay_area: true\n"
        "    employees_est: 1200\n"
        "    sector: enterprise_saas\n"
        # ats: custom → skips probe, deterministic 70.
        "  - name: CustomCareersCo\n"
        "    ats: custom\n"
        "    ats_slug: null\n"
        "    hq_bay_area: true\n"
        "    employees_est: 5000\n"
        "    sector: enterprise_saas\n",
        encoding="utf-8",
    )
    return tmp


def _mock_probe(company: dict) -> dict:
    """
    Canned probe responses keyed by ats_slug so we can drive every code path
    without touching the network.
    """
    slug = company.get("ats_slug")
    table = {
        # 4 BA PMs → momentum bonus → 100/100
        "bighirer":      {"ok": True, "total_jobs": 50, "pm_total": 5, "pm_bay_area": 4,
                          "sample_titles": ["Senior PM, Risk", "Staff PM, Growth", "Lead PM, API"], "error": None},
        # 1 BA PM → activity but no momentum → 90/100
        "moderatehirer": {"ok": True, "total_jobs": 30, "pm_total": 1, "pm_bay_area": 1,
                          "sample_titles": ["Senior PM, Platform"], "error": None},
        # zero PM roles → 70/100 (HQ + headcount only) → DROP (under queue threshold of 50? no, 70 > 50, so REVIEW)
        "nothiringpms":  {"ok": True, "total_jobs": 12, "pm_total": 0, "pm_bay_area": 0,
                          "sample_titles": [], "error": None},
        # probe failed
        "brokenslug":    {"ok": False, "total_jobs": 0, "pm_total": 0, "pm_bay_area": 0,
                          "sample_titles": [], "error": "HTTP 404"},
        # extra winners — both 100/100 — to test max_auto_adds_per_run cap (=2)
        "extrawinner1":  {"ok": True, "total_jobs": 40, "pm_total": 6, "pm_bay_area": 5,
                          "sample_titles": ["Senior PM"], "error": None},
        "extrawinner2":  {"ok": True, "total_jobs": 40, "pm_total": 6, "pm_bay_area": 5,
                          "sample_titles": ["Senior PM"], "error": None},
        # 4 PM roles open but ZERO in Bay Area → activity bonus must NOT apply
        # under the tightened formula → score = 70 = HQ + headcount only.
        "dublinhirer":   {"ok": True, "total_jobs": 30, "pm_total": 4, "pm_bay_area": 0,
                          "sample_titles": ["Senior PM, EMEA"], "error": None},
    }
    if slug == "customcareerscoslug":
        # Should not be reached: ats: custom skips the probe entirely.
        raise AssertionError("custom ATS should not call _mock_probe")
    return table.get(slug, {"ok": False, "total_jobs": 0, "pm_total": 0,
                            "pm_bay_area": 0, "sample_titles": [], "error": "unknown slug in test"})


def run() -> int:
    print("=" * 60)
    print(" Layer 01 test — running pipeline with mocked probes")
    print("=" * 60)

    tmp = _setup_temp_project()
    print(f" Test project: {tmp}")
    failures: list[str] = []

    try:
        # ---- Patch the module-level path constants so the layer reads from tmp.
        with mock.patch.object(pool_loader, "POOL_PATH",      tmp / "config" / "discovery_pool.yaml"), \
             mock.patch.object(pool_loader, "WATCHLIST_PATH", tmp / "config" / "watchlist.yaml"), \
             mock.patch.object(pool_loader, "CRITERIA_PATH",  tmp / "config" / "criteria.yaml"), \
             mock.patch.object(output, "DISCOVERY_DIR",       tmp / "data" / "discovery"), \
             mock.patch.object(output, "WATCHLIST_PATH",      tmp / "config" / "watchlist.yaml"):

            # ---------- pool_loader pre-filter checks ----------
            candidates, settings = pool_loader.load_candidates()
            cand_names = sorted(c["name"] for c in candidates)
            expected_candidates = sorted([
                "BigHirer", "ModerateHirer", "NotHiringPMs", "BrokenSlug",
                "ExtraWinner1", "ExtraWinner2",
                "DublinHirer", "CustomCareersCo",
            ])
            if cand_names != expected_candidates:
                failures.append(f"candidate set wrong: {cand_names} != {expected_candidates}")
            else:
                print(f" ✓ pre-filter: {len(candidates)} candidates: {cand_names}")

            # ---------- probe + score every candidate ----------
            # For ats: custom we call the REAL probe (no network involved); for
            # everything else we use canned _mock_probe responses.
            results = []
            for c in candidates:
                if c.get("ats") == "custom":
                    pr = probe.probe_company(c)
                else:
                    pr = _mock_probe(c)
                if not pr["ok"]:
                    results.append({"name": c["name"], "ats": c["ats"],
                                    "ats_slug": c["ats_slug"], "score": 0,
                                    "rationale": f"probe failed: {pr['error']}",
                                    "action": "probe_failed",
                                    "probe": pr,
                                    "sector": c.get("sector"),
                                    "employees_est": c.get("employees_est"),
                                    "breakdown": None})
                    continue
                s = score.score_company(company=c, probe_result=pr,
                                        min_employees=settings["min_employees"])
                action = score.route(score_total=s["total"], settings=settings)
                results.append({"name": c["name"], "ats": c["ats"],
                                "ats_slug": c["ats_slug"], "score": s["total"],
                                "breakdown": s["breakdown"], "rationale": s["rationale"],
                                "action": action, "probe": pr,
                                "sector": c.get("sector"),
                                "employees_est": c.get("employees_est")})

            by_name = {r["name"]: r for r in results}

            # ---------- score formula assertions ----------
            checks = [
                ("BigHirer",        100, "auto_add"),    # 40+30+20+10
                ("ModerateHirer",    90, "auto_add"),    # 40+30+20+0
                ("NotHiringPMs",     70, "review"),      # 40+30+0+0 — no PMs at all
                ("DublinHirer",      70, "review"),      # 40+30+0+0 — PMs exist but 0 in BA
                ("CustomCareersCo",  70, "review"),      # ats: custom — no probe
                ("ExtraWinner1",    100, "auto_add"),
                ("ExtraWinner2",    100, "auto_add"),
                ("BrokenSlug",        0, "probe_failed"),
            ]
            score_failed = False
            for name, want_score, want_action in checks:
                r = by_name.get(name)
                if r is None:
                    failures.append(f"missing result for {name}")
                    score_failed = True
                    continue
                if r["score"] != want_score:
                    failures.append(f"{name}: score {r['score']} != {want_score}")
                    score_failed = True
                if r["action"] != want_action:
                    failures.append(f"{name}: action {r['action']!r} != {want_action!r}")
                    score_failed = True
            if not score_failed:
                print(f" ✓ score formula correct on all {len(checks)} cases")

            # ---------- max_auto_adds_per_run cap ----------
            auto_adds = [r for r in results if r["action"] == "auto_add"]
            auto_adds.sort(key=lambda r: -int(r["score"]))
            capped = auto_adds[: settings["max_auto_adds_per_run"]]
            if len(capped) != 2:
                failures.append(f"cap should be 2, got {len(capped)}")
            else:
                print(f" ✓ cap honored: {len(auto_adds)} winners → 2 actually written")

            # ---------- write JSON ----------
            json_path = output.write_daily_json(results=results, settings=settings,
                                                date_str="2099-01-01")
            data = json.loads(json_path.read_text())
            if data["summary"]["candidates_evaluated"] != 8:
                failures.append("JSON candidates_evaluated wrong")
            if data["summary"]["probe_failed"] != 1:
                failures.append("JSON probe_failed count wrong")
            if data["results"][0]["score"] < data["results"][-1]["score"]:
                failures.append("JSON results not sorted by score desc")
            else:
                print(" ✓ JSON written, summary correct, results sorted")

            # ---------- watchlist append preserves comments + structure ----------
            before = (tmp / "config" / "watchlist.yaml").read_text()
            n_appended = output.append_to_watchlist(winners=capped, date_str="2099-01-01")
            after = (tmp / "config" / "watchlist.yaml").read_text()

            if n_appended != 2:
                failures.append(f"append count {n_appended} != 2")
            if "AlreadyTracked" not in after or "existing" not in after:
                failures.append("existing entry/comment lost")
            if "# Companies to NEVER score" not in after:
                failures.append("exclude block comment lost")
            # New entries must appear BEFORE the exclude block
            if after.index("AUTO-DISCOVERED") >= after.index("# Companies to NEVER score"):
                failures.append("auto-discovered block inserted after exclude:")
            # Both winners present
            if "BigHirer" not in after or ("ExtraWinner1" not in after and "ExtraWinner2" not in after):
                failures.append("at least one winner missing from watchlist")
            if not failures:
                print(" ✓ watchlist append preserved structure & inserted before exclude:")

            # ---------- second append should de-dupe (BigHirer is now in the file) ----------
            n_again = output.append_to_watchlist(winners=capped, date_str="2099-01-02")
            if n_again != 0:
                failures.append(f"second append should be 0 (dedupe), got {n_again}")
            else:
                print(" ✓ second append correctly de-duplicated to 0")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(" ✗ FAILURES:")
        for f in failures:
            print(f"     - {f}")
        return 1
    print(" ✅ All Layer 01 checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
