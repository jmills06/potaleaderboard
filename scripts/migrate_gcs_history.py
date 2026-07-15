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

    # ---- Baseline for the current season: final snapshot of prior year ----
    prior = BASELINE_YEAR - 1
    if prior in last_of_year:
        ts, snap = last_of_year[prior]
        operators = {}
        for op in snap.get("leaderboard", []):
            if op.get("callsign"):
                operators[op["callsign"]] = {
                    **totals_of(op),
                    "captured": ts,
                    "source": "gcs-migration",
                }
        BASELINE_DIR.mkdir(parents=True, exist_ok=True)
        bpath = BASELINE_DIR / f"{BASELINE_YEAR}.json"
        bpath.write_text(json.dumps(
            {"season": BASELINE_YEAR, "created": ts, "operators": operators},
            indent=2))
        print(f"Wrote {bpath.name} from snapshot {ts} ({len(operators)} operators)")
    else:
        print(f"WARNING: no {prior} snapshot found; baseline not written")

    # ---- Finalize the prior season's awards with the legacy formula ----
    base_prior = FINALIZE_YEAR - 1
    if FINALIZE_YEAR in last_of_year and base_prior in last_of_year:
        _, final_snap = last_of_year[FINALIZE_YEAR]
        _, base_snap = last_of_year[base_prior]
        base_map = {o["callsign"]: totals_of(o)
                    for o in base_snap.get("leaderboard", []) if o.get("callsign")}
        standings = []
        for op in final_snap.get("leaderboard", []):
            call = op.get("callsign")
            if not call:
                continue
            cur = totals_of(op)
            base = base_map.get(call, {
                "activator": {"activations": 0, "parks": 0, "qsos": 0},
                "hunter": {"parks": 0, "qsos": 0},
            })
            standings.append({
                "callsign": call,
                "name": op.get("name") or "N/A",
                "gravatar": op.get("gravatar") or "",
                "score": legacy_score(cur, base),
                "activations": cur["activator"]["activations"] - base["activator"]["activations"],
                "parks": cur["activator"]["parks"] - base["activator"]["parks"],
                "qsos": cur["activator"]["qsos"] - base["activator"]["qsos"],
            })
        standings.sort(key=lambda s: s["score"], reverse=True)
        AWARDS_DIR.mkdir(parents=True, exist_ok=True)
        apath = AWARDS_DIR / f"{FINALIZE_YEAR}.json"
        apath.write_text(json.dumps({
            "season": FINALIZE_YEAR,
            "formula": "legacy: acts*5 + parks*5 + qsos*0.1",
            "finalized_by": "gcs-migration",
            "podium": standings[:3],
            "full_standings": standings,
        }, indent=2))
        print(f"Wrote {apath.name}: podium = "
              + ", ".join(s["callsign"] for s in standings[:3]))
    else:
        print(f"NOTE: cannot finalize {FINALIZE_YEAR} awards "
              f"(need both a {FINALIZE_YEAR} and a {base_prior} snapshot)")

    print("Migration complete.")


if __name__ == "__main__":
    main()
