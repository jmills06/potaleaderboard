#!/usr/bin/env python3
"""
One-time migration: GCS leaderboard-history.json -> repo-native data files.

Produces:
  data/history/<year>.jsonl   one line per operator per day, raw totals only
  data/baselines/2026.json    from the final 2025 snapshot
  data/awards/2025.json       final 2025 podium, scored with the ORIGINAL
                              formula (acts*5 + parks*5 + qsos*0.1), because
                              that was the game everyone played in 2025.

Safe to re-run: it rewrites the files from scratch each time.
Run via the 'Migrate GCS History' workflow (workflow_dispatch).
"""

import json
import urllib.request
from collections import defaultdict
from pathlib import Path

GCS_URL = "https://storage.googleapis.com/pota-activations0/leaderboard-history.json"
ROOT = Path(__file__).resolve().parent.parent
HISTORY_DIR = ROOT / "data" / "history"
BASELINE_DIR = ROOT / "data" / "baselines"
AWARDS_DIR = ROOT / "data" / "awards"

BASELINE_YEAR = 2026          # season the baseline feeds
FINALIZE_YEAR = 2025          # season to finalize awards for


def totals_of(op):
    a = op.get("activator") or {}
    h = op.get("hunter") or {}
    return {
        "activator": {
            "activations": a.get("activations", 0) or 0,
            "parks": a.get("parks", 0) or 0,
            "qsos": a.get("qsos", 0) or 0,
        },
        "hunter": {
            "parks": h.get("parks", 0) or 0,
            "qsos": h.get("qsos", 0) or 0,
        },
    }


def legacy_score(cur, base):
    """Original 2025-era formula, used only to finalize the 2025 season."""
    acts = cur["activator"]["activations"] - base["activator"]["activations"]
    parks = cur["activator"]["parks"] - base["activator"]["parks"]
    qsos = cur["activator"]["qsos"] - base["activator"]["qsos"]
    return round(acts * 5 + parks * 5 + qsos * 0.1)


def main():
    print(f"Fetching {GCS_URL}")
    with urllib.request.urlopen(GCS_URL, timeout=60) as r:
        snapshots = json.load(r)
    print(f"Loaded {len(snapshots)} snapshots")

    # Deduplicate: keep the LAST snapshot per calendar date, per operator.
    # Key: (date, callsign) -> record. Later snapshots on the same date win.
    by_year = defaultdict(dict)  # year -> {(date, call): line}
    last_of_year = {}            # year -> (timestamp, snapshot)

    for snap in snapshots:
        ts = snap.get("timestamp", "")
        if len(ts) < 10:
            continue
        date = ts[:10]
        year = int(date[:4])

        prev = last_of_year.get(year)
        if prev is None or ts > prev[0]:
            last_of_year[year] = (ts, snap)

        for op in snap.get("leaderboard", []):
            call = op.get("callsign")
            if not call:
                continue
            line = {
                "date": date,
                "callsign": call,
                "name": op.get("name") or "N/A",
                "qth": op.get("qth") or "",
                "gravatar": op.get("gravatar") or "",
                **totals_of(op),
                "awards": op.get("awards", 0) or 0,
                "endorsements": op.get("endorsements", 0) or 0,
            }
            by_year[year][(date, call)] = line

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    for year in sorted(by_year):
        path = HISTORY_DIR / f"{year}.jsonl"
        keys = sorted(by_year[year])
        with open(path, "w") as f:
            for k in keys:
                f.write(json.dumps(by_year[year][k], separators=(",", ":")) + "\n")
        print(f"Wrote {path.name}: {len(keys)} lines")

    # ---- Season boundaries ----
    # Rule: an operator's boundary for season N is their FIRST record dated in
    # year N (these 00:00 UTC snapshots represent standings at the end of
    # Dec 31), falling back to their LAST record of year N-1. The same
    # boundary is used as season N's baseline and season N-1's final totals,
    # so nothing is dropped or double counted.
    def per_call_sorted(year):
        out = {}
        for (d, c) in sorted(by_year.get(year, {})):
            out.setdefault(c, []).append(by_year[year][(d, c)])
        return out

    cur = per_call_sorted(BASELINE_YEAR)
    prev = per_call_sorted(BASELINE_YEAR - 1)
    prev2 = per_call_sorted(BASELINE_YEAR - 2)

    def boundary(call):
        if call in cur:
            return cur[call][0]
        if call in prev:
            return prev[call][-1]
        return None

    all_calls = sorted(set(cur) | set(prev))
    operators = {}
    for call in all_calls:
        rec = boundary(call)
        if rec:
            operators[call] = {**totals_of(rec),
                               "captured": rec["date"],
                               "source": "gcs-migration-boundary"}
    if operators:
        BASELINE_DIR.mkdir(parents=True, exist_ok=True)
        bpath = BASELINE_DIR / f"{BASELINE_YEAR}.json"
        bpath.write_text(json.dumps(
            {"season": BASELINE_YEAR,
             "created": f"{BASELINE_YEAR}-01-01",
             "operators": operators}, indent=2))
        print(f"Wrote {bpath.name} ({len(operators)} operators)")
    else:
        print("WARNING: no data found; baseline not written")

    # ---- Finalize the prior season with the legacy formula ----
    # Baseline for season N-1 = first record of N-1 (fallback last of N-2).
    # Operators first observed during N-1 therefore get a near-zero season:
    # we cannot reconstruct activity from before we started tracking them,
    # and crediting whole careers (the old behavior) is worse.
    standings = []
    for call in sorted(prev):
        first = (prev2[call][-1] if call in prev2 and prev2[call]
                 else prev[call][0])
        final = boundary(call) or prev[call][-1]
        base_t, final_t = totals_of(first), totals_of(final)
        standings.append({
            "callsign": call,
            "name": final.get("name", "N/A"),
            "gravatar": final.get("gravatar", ""),
            "score": legacy_score(final_t, base_t),
            "activations": final_t["activator"]["activations"] - base_t["activator"]["activations"],
            "parks": final_t["activator"]["parks"] - base_t["activator"]["parks"],
            "qsos": final_t["activator"]["qsos"] - base_t["activator"]["qsos"],
            "tracked_since": first["date"],
        })
    if standings:
        standings.sort(key=lambda s: s["score"], reverse=True)
        AWARDS_DIR.mkdir(parents=True, exist_ok=True)
        apath = AWARDS_DIR / f"{FINALIZE_YEAR}.json"
        apath.write_text(json.dumps({
            "season": FINALIZE_YEAR,
            "formula": "legacy: acts*5 + parks*5 + qsos*0.1",
            "note": "operators first tracked mid-season only get credit from tracked_since onward",
            "finalized_by": "gcs-migration",
            "podium": standings[:3],
            "full_standings": standings,
        }, indent=2))
        print(f"Wrote {apath.name}: podium = "
              + ", ".join(s["callsign"] for s in standings[:3]))

    print("Migration complete.")


if __name__ == "__main__":
    main()
