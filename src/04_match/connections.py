"""
connections.py — Layer 04 helper: LinkedIn warm-intro lookup.

WHAT THIS DOES (in plain English):
  Loads your LinkedIn connections CSV once, indexes them by company, and
  exposes a single function: given a company name, return everyone in your
  network who currently works there.

  Used by the digest in Layer 05 to add a "Warm intros" section to each job
  card whenever you have a connection at the hiring company.

CSV FORMAT (LinkedIn export):
  - LinkedIn's export starts with a "Notes:" preamble (3 lines). We skip
    until we find the row that begins with "First Name".
  - Real columns: First Name, Last Name, URL, Email Address, Company,
    Position, Connected On.
  - Email is often blank — LinkedIn only exports it for contacts who
    opted to share. We don't rely on it.

MATCHING LOGIC:
  - Case-insensitive.
  - Both the digest's company name and the CSV's company name are
    "normalized" first: lowercased, stripped of trailing punctuation, and
    common corporate suffixes removed (Inc, LLC, Corp, Platforms, ...).
    So "Meta Platforms, Inc." and "Meta" both normalize to "meta".
  - A small alias map handles known renames (Facebook → Meta).
  - Unknown companies just return an empty list (digest renders nothing).

USAGE:
  from connections import find_connections
  for c in find_connections("Stripe"):
      print(c["name"], c["position"], c["url"])

CLI (handy for testing):
  python3 src/04_match/connections.py "Stripe"
"""

from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CSV_PATH = PROJECT_ROOT / "data" / "linkedin_connections.csv"

# Common corporate suffixes that we strip during normalization. Order matters:
# longer suffixes first so " inc" doesn't strip before ", inc.".
_SUFFIXES = (
    ", inc.",
    ", inc",
    " inc.",
    " inc",
    ", llc",
    " llc",
    ", ltd.",
    " ltd.",
    " ltd",
    " plc",
    " corporation",
    " corp.",
    " corp",
    " co.",
    " holdings",
    " group",
    " platforms",
    " technologies",
    " labs",
)

# Known company-name aliases — left side normalizes to right side. Useful when
# LinkedIn rows still say the old name long after a rebrand.
_ALIASES = {
    "facebook": "meta",
    "google llc": "google",
    "alphabet": "google",
    "x": "twitter",          # depending on which name you're using
}


def _normalize(name: str) -> str:
    """Lowercase, strip punctuation, drop a common corporate suffix.

    Examples:
      'Meta Platforms, Inc.'  -> 'meta'
      'Stripe'                -> 'stripe'
      'Apple Inc'             -> 'apple'
      'Google LLC'            -> 'google'   (via alias)
    """
    n = (name or "").strip().lower().rstrip(",. ")
    # Strip suffixes repeatedly so "Meta Platforms, Inc." chops both
    # ", inc." and " platforms".
    changed = True
    while changed:
        changed = False
        for suffix in _SUFFIXES:
            if n.endswith(suffix):
                n = n[: -len(suffix)].rstrip(",. ")
                changed = True
    # Apply aliases AFTER suffix stripping
    return _ALIASES.get(n, n)


@lru_cache(maxsize=1)
def _load_index(csv_path_str: str | None) -> dict:
    """Read the CSV once and build {normalized_company: [connection, ...]}.

    Cached so repeated calls during one digest run only do disk I/O once.
    Pass a different csv_path_str (or None) to bust the cache implicitly via
    a different argument.
    """
    path = Path(csv_path_str) if csv_path_str else DEFAULT_CSV_PATH
    by_company: dict[str, list[dict]] = {}
    if not path.exists():
        return by_company

    # LinkedIn's export has a 3-line "Notes:" preamble before the real header.
    # Skip lines until we find one starting with "First Name".
    with open(path, "r", encoding="utf-8", newline="") as fh:
        # Find the header line
        header_offset = 0
        while True:
            pos = fh.tell()
            line = fh.readline()
            if not line:
                # Empty / unreadable file
                return by_company
            if line.startswith("First Name"):
                header_offset = pos
                break
        fh.seek(header_offset)
        reader = csv.DictReader(fh)
        for row in reader:
            raw_company = (row.get("Company") or "").strip()
            if not raw_company:
                continue
            key = _normalize(raw_company)
            if not key:
                continue
            entry = {
                "name": (
                    f"{(row.get('First Name') or '').strip()} "
                    f"{(row.get('Last Name') or '').strip()}"
                ).strip(),
                "position": (row.get("Position") or "").strip(),
                "url": (row.get("URL") or "").strip(),
                "company": raw_company,
                "connected_on": (row.get("Connected On") or "").strip(),
            }
            by_company.setdefault(key, []).append(entry)
    return by_company


def find_connections(company: str, *, csv_path: str | Path | None = None) -> list[dict]:
    """Return all LinkedIn connections at the given company.

    Each entry is a dict with: name, position, url, company, connected_on.
    Empty list if no matches (caller should treat that as 'no warm intros').
    """
    target = _normalize(company)
    if not target:
        return []
    csv_key = str(csv_path) if csv_path else None
    return list(_load_index(csv_key).get(target, []))


def _self_test() -> None:
    """Tiny CLI: `python3 connections.py Stripe`."""
    import sys

    if len(sys.argv) < 2:
        print("Usage: python3 connections.py <company name>")
        print(f"CSV path: {DEFAULT_CSV_PATH}")
        index = _load_index(None)
        print(f"Indexed {sum(len(v) for v in index.values())} connections "
              f"across {len(index)} normalized companies.")
        if index:
            top = sorted(index.items(), key=lambda kv: -len(kv[1]))[:10]
            print("Top companies:")
            for company, entries in top:
                print(f"  {company:30s} {len(entries):3d}")
        return

    target = " ".join(sys.argv[1:])
    matches = find_connections(target)
    print(f"\nFound {len(matches)} connection(s) at '{target}':")
    for m in matches:
        line = f"  {m['name']:30s} — {m['position']}"
        if m["url"]:
            line += f"  ({m['url']})"
        print(line)


if __name__ == "__main__":
    _self_test()
