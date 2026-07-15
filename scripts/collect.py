#!/usr/bin/env python3
"""
POTA Achievement Board collector.

Each run:
  1. Fetches current stats for every roster operator with collect=true.
  2. Upserts one line per operator into data/history/<year>.jsonl (raw totals).
  3. Handles season rollover automatically: on the first run of a new year it
     finalizes last season's awards and captures this season's baseline.
  4. Gives any brand-new operator a baseline equal to their first observed
     totals, so they start at 0 instead of inheriting their whole career.
  5. Computes everything the board needs (scores, ranks, deltas, sparklines,
     activity tiers, 30-day movement, category leaders, group totals) and
     writes data/latest/board.json. The frontend never does math.

Raw totals live in history; scoring weights live in config/scoring.json.
Change the weights and the next run recomputes the entire season.
"""

import json
import sys
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "scoring.json"
ROSTER_PATH = ROOT / "data" / "roster.json"
HISTORY_DIR = ROOT / "data" / "history"
BASELINE_DIR = ROOT / "data" / "baselines"
AWARDS_DIR = ROOT / "data" / "awards"
LATEST_DIR = ROOT / "data" / "latest"

TZ = ZoneInfo("America/Detroit")
API = "https://api.pota.app/stats/user/"
UA = {"User-Agent": "pota-achievement-board (github.com/jmills06/potaleaderboard)"}

EMPTY = {
    "activator": {"activations": 0, "parks": 0, "qsos": 0},
    "hunter": {"parks": 0, "qsos": 0},
}


# ---------------------------------------------------------------- utilities

def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def totals_of(rec):
    a = rec.get("activator") or {}
    h = rec.get("hunter") or {}
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


def deltas(cur, base):
    return {
        "activations": cur["activator"]["activations"] - base["activator"]["activations"],
        "parks": cur["activator"]["parks"] - base["activator"]["parks"],
        "activator_qsos": cur["activator"]["qsos"] - base["activator"]["qsos"],
        "hunter_parks": cur["hunter"]["parks"] - base["hunter"]["parks"],
        "hunter_qsos": cur["hunter"]["qsos"] - base["hunter"]["qsos"],
    }


def score(d, weights):
    return round(
        d["activations"] * weights["activations"]
        + d["parks"] * weights["unique_parks"]
        + d["activator_qsos"] * weights["activator_qsos"]
        + d["hunter_qsos"] * weights["hunter_qsos"]
    )


def read_history(year):
    """Return {(date_str, callsign): record} for a season's JSONL file."""
    path = HISTORY_DIR / f"{year}.jsonl"
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            out[(rec["date"], rec["callsign"])] = rec
    return out


def write_history(year, records):
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = HISTORY_DIR / f"{year}.jsonl"
    with open(path, "w") as f:
        for key in sorted(records):
            f.write(json.dumps(records[key], separators=(",", ":")) + "\n")


def latest_asof(history_by_call, day_str):
    """Latest record on or before day_str, per the pre-sorted per-call list."""
    best = None
    for rec in history_by_call:
        if rec["date"] <= day_str:
            best = rec
        else:
            break
    return best


# ---------------------------------------------------------------- pipeline

def fetch_operator(call):
    try:
        req = urllib.request.Request(API + call, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except Exception as e:
        print(f"WARN: fetch failed for {call}: {e}")
        return None


def rollover(year, history, history_prev, config):
    """First run of a new season: finalize last year, capture this baseline.

    Boundary rule: an operator's baseline for this season is their FIRST
    record dated in this year. On the first run of Jan 1 that is today's
    fetch, which includes everything uploaded through the end of Dec 31.
    The same boundary is the previous season's final totals, so nothing is
    dropped or double counted at the year line.
    """
    baseline_path = BASELINE_DIR / f"{year}.json"
    if baseline_path.exists():
        return load_json(baseline_path)

    print(f"Season rollover: building baseline for {year}")
    prev_year = year - 1
    prev_baseline = load_json(BASELINE_DIR / f"{prev_year}.json") or {}

    cur_by_call, prev_by_call = {}, {}
    for (d, c), rec in sorted(history.items()):
        cur_by_call.setdefault(c, []).append(rec)
    for (d, c), rec in sorted(history_prev.items()):
        prev_by_call.setdefault(c, []).append(rec)

    def boundary(call):
        if call in cur_by_call:
            return cur_by_call[call][0]
        if call in prev_by_call:
            return prev_by_call[call][-1]
        return None

    now_iso = datetime.now(TZ).isoformat()
    operators = {}
    for call in sorted(set(cur_by_call) | set(prev_by_call)):
        rec = boundary(call)
        if rec:
            operators[call] = {**totals_of(rec),
                               "captured": rec["date"],
                               "source": "season-boundary"}

    baseline = {"season": year, "created": now_iso, "operators": operators}
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(json.dumps(baseline, indent=2))
    print(f"Baseline captured for {len(operators)} operators")

    # Finalize previous season's awards with the current configured formula
    awards_path = AWARDS_DIR / f"{prev_year}.json"
    if not awards_path.exists() and prev_by_call:
        weights = config["weights"]
        standings = []
        for call, series in prev_by_call.items():
            final = boundary(call) or series[-1]
            base = (prev_baseline.get("operators", {}).get(call)
                    or series[0])
            d = deltas(totals_of(final), totals_of(base))
            standings.append({
                "callsign": call,
                "name": final.get("name", "N/A"),
                "gravatar": final.get("gravatar", ""),
                "score": score(d, weights),
                "activations": d["activations"],
                "parks": d["parks"],
                "qsos": d["activator_qsos"],
            })
        standings.sort(key=lambda s: s["score"], reverse=True)
        AWARDS_DIR.mkdir(parents=True, exist_ok=True)
        awards_path.write_text(json.dumps({
            "season": prev_year,
            "formula": f"weights at finalization: {weights}",
            "finalized_by": "collector-rollover",
            "podium": standings[:3],
            "full_standings": standings,
        }, indent=2))
        print(f"Finalized {prev_year} awards: "
              + ", ".join(s["callsign"] for s in standings[:3]))
    return baseline


def activity_tier(hist_sorted, today):
    """Tier from activation-count increases over trailing windows."""
    def acts_at(day):
        rec = latest_asof(hist_sorted, day.isoformat())
        return rec["activator"]["activations"] if rec else None

    now_acts = acts_at(today)
    if now_acts is None:
        return "inactive"
    windows = [(7, "fire_or_week"), (14, "twoweek"), (30, "month"), (60, "sixty")]
    for days, label in windows:
        then = acts_at(today - timedelta(days=days))
        if then is None:
            continue
        gained = now_acts - then
        if gained > 0:
            if label == "fire_or_week":
                return "fire" if gained >= 2 else "week"
            return label
    return "inactive"


def weekly_streak(hist_sorted, today):
    """Consecutive weeks (ending this or last week) with >= 1 activation."""
    streak = 0
    week_end = today
    for _ in range(60):
        week_start = week_end - timedelta(days=7)
        a_end = latest_asof(hist_sorted, week_end.isoformat())
        a_start = latest_asof(hist_sorted, week_start.isoformat())
        if not a_end or not a_start:
            break
        gained = (a_end["activator"]["activations"]
                  - a_start["activator"]["activations"])
        if gained >= 1:
            streak += 1
            week_end = week_start
        elif streak == 0 and week_end == today:
            # allow the streak to have ended last week without zeroing it
            week_end = week_start
            if (today - week_end).days > 7:
                break
        else:
            break
    return streak


def main():
    config = load_json(CONFIG_PATH)
    roster = load_json(ROSTER_PATH)
    if not config or not roster:
        sys.exit("Missing config/scoring.json or data/roster.json")
    weights = config["weights"]

    now = datetime.now(TZ)
    today = now.date()
    year = today.year
    today_str = today.isoformat()

    history_prev = read_history(year - 1)
    history = read_history(year)

    # ---------------- fetch + upsert ----------------
    collect_calls = [o["callsign"] for o in roster["operators"] if o.get("collect")]
    display_calls = {o["callsign"] for o in roster["operators"]
                     if o.get("collect") and o.get("display")}

    fetched = 0
    new_records = []
    for call in collect_calls:
        data = fetch_operator(call)
        if data is None:
            # keep last known: leave existing history untouched for this op
            continue
        rec = {
            "date": today_str,
            "callsign": call,
            "name": data.get("name") or "N/A",
            "qth": data.get("qth") or "",
            "gravatar": data.get("gravatar") or "",
            **totals_of(data),
            "awards": data.get("awards", 0) or 0,
            "endorsements": data.get("endorsements", 0) or 0,
        }
        history[(today_str, call)] = rec
        fetched += 1
        new_records.append(rec)

    if fetched == 0:
        sys.exit("All fetches failed; aborting without writing anything")

    write_history(year, history)
    print(f"Upserted {fetched}/{len(collect_calls)} operators into {year}.jsonl")

    # ---------------- baseline / rollover (after upsert, so Jan 1's
    # fetch, including Dec 31 uploads, becomes the season boundary) --------
    baseline = rollover(year, history, history_prev, config)
    baseline_ops = baseline.setdefault("operators", {})
    baseline_dirty = False
    for rec in new_records:
        call = rec["callsign"]
        # New operator mid-season: baseline = first observed totals
        if call not in baseline_ops:
            baseline_ops[call] = {**totals_of(rec),
                                  "captured": now.isoformat(),
                                  "source": "first-observation"}
            baseline_dirty = True
            print(f"New operator {call}: baseline set to first observation")
    if baseline_dirty:
        (BASELINE_DIR / f"{year}.json").write_text(json.dumps(baseline, indent=2))

    # ---------------- per-operator series ----------------
    by_call = {}
    for (d, c), rec in sorted(history.items()):
        by_call.setdefault(c, []).append(rec)
    by_call_prev = {}
    for (d, c), rec in sorted(history_prev.items()):
        by_call_prev.setdefault(c, []).append(rec)

    def merged_series(call):
        return (by_call_prev.get(call, []) + by_call.get(call, []))

    def base_for(call):
        return totals_of(baseline_ops.get(call, EMPTY))

    def score_asof(call, day):
        rec = latest_asof(by_call.get(call, []), day.isoformat())
        if not rec:
            return None
        return score(deltas(totals_of(rec), base_for(call)), weights)

    # Sparkline sample dates: every Monday of the season plus today
    jan1 = date(year, 1, 1)
    samples = []
    d = jan1
    while d <= today:
        samples.append(d)
        d += timedelta(days=7)
    if samples[-1] != today:
        samples.append(today)

    # ---------------- build operator entries ----------------
    ops = []
    for call in sorted(display_calls):
        series = by_call.get(call, [])
        if not series:
            continue
        latest = series[-1]
        cur = totals_of(latest)
        d = deltas(cur, base_for(call))
        s = score(d, weights)
        spark = [v if (v := score_asof(call, sd)) is not None else 0
                 for sd in samples]
        acts = d["activations"]
        ops.append({
            "callsign": call,
            "name": latest.get("name", "N/A"),
            "qth": latest.get("qth", ""),
            "gravatar": latest.get("gravatar", ""),
            "score": s,
            "activations": acts,
            "parks": d["parks"],
            "qsos": d["activator_qsos"],
            "hunter_qsos": d["hunter_qsos"],
            "hunter_parks": d["hunter_parks"],
            "qsos_per_activation": round(d["activator_qsos"] / acts, 1) if acts else 0,
            "score_30d": (s - s30) if (s30 := score_asof(call, today - timedelta(days=30))) is not None else s,
            "activity": activity_tier(merged_series(call), today),
            "streak_weeks": weekly_streak(merged_series(call), today),
            "spark": spark,
        })

    # Ranks now and 30 days ago
    ops.sort(key=lambda o: o["score"], reverse=True)
    for i, o in enumerate(ops):
        o["rank"] = i + 1
        o["gap_to_next"] = (ops[i - 1]["score"] - o["score"]) if i else 0

    then = today - timedelta(days=30)
    old = sorted(
        ((c, sc) for c in display_calls
         if (sc := score_asof(c, then)) is not None),
        key=lambda t: t[1], reverse=True)
    old_rank = {c: i + 1 for i, (c, _) in enumerate(old)}
    for o in ops:
        pr = old_rank.get(o["callsign"])
        o["prev_rank_30d"] = pr
        o["movement"] = "new" if pr is None else (pr - o["rank"])

    # ---------------- category leaders ----------------
    def leader(key, valid=lambda o: True):
        pool = [o for o in ops if valid(o)]
        if not pool:
            return None
        best = max(pool, key=lambda o: o[key])
        return {"callsign": best["callsign"], "name": best["name"],
                "gravatar": best["gravatar"], "value": best[key]}

    min_acts = config.get("rate_machine_min_activations", 3)
    categories = {
        "park_explorer": leader("parks"),
        "rate_machine": leader("qsos_per_activation",
                               lambda o: o["activations"] >= min_acts),
        "top_hunter": leader("hunter_qsos"),
        "hot_hand": leader("score_30d"),
        "longest_streak": leader("streak_weeks"),
    }

    # ---------------- group totals + pace ----------------
    day_of_year = (today - jan1).days + 1
    days_in_year = (date(year, 12, 31) - jan1).days + 1
    g_acts = sum(o["activations"] for o in ops)
    g_parks = sum(o["parks"] for o in ops)
    g_qsos = sum(o["qsos"] for o in ops)
    group = {
        "activations": g_acts,
        "parks": g_parks,
        "qsos": g_qsos,
        "hunter_qsos": sum(o["hunter_qsos"] for o in ops),
        "pace_activations": round(g_acts / day_of_year * days_in_year),
        "pace_qsos": round(g_qsos / day_of_year * days_in_year),
    }

    # ---------------- past seasons ----------------
    awards = []
    if AWARDS_DIR.exists():
        for p in sorted(AWARDS_DIR.glob("*.json")):
            a = load_json(p)
            if a:
                awards.append({"season": a["season"], "podium": a["podium"]})

    LATEST_DIR.mkdir(parents=True, exist_ok=True)
    board = {
        "generated": now.isoformat(),
        "season": year,
        "days_remaining": (date(year, 12, 31) - today).days,
        "weights": weights,
        "sample_dates": [s.isoformat() for s in samples],
        "operators": ops,
        "categories": categories,
        "group": group,
        "past_seasons": awards,
    }
    (LATEST_DIR / "board.json").write_text(json.dumps(board, indent=1))
    print(f"Wrote board.json: {len(ops)} displayed operators, "
          f"leader = {ops[0]['callsign']} ({ops[0]['score']})" if ops else "no ops")


if __name__ == "__main__":
    main()
