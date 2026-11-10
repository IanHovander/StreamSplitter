"""Phase 0 scorecard for one run: recall, naming, false clips, boundaries (audio vs clock), latency, speed.

    .venv\\Scripts\\python.exe phase0\\score.py D1_run2 [D2_run1 ...]

Truth for which match a clip holds is TBA's actual_time joined to the found starts by phase0/truth.py, which allows
for minutes the saved video lost (stream drops); TBA matches the join cannot place need a by-eye verdict in
phase0/truth_notes.json (absent / partial / present) before they count. It locates matches to a few seconds: enough
for presence and naming, not for boundaries. The names under test are the ones the run gave live (teams, posted TBA times, order).
Boundaries are checked by comparing the two independent signals (field sounds, scoreboard clock) on every match where
both exist; the visual sample is separate (PLAN.md).
"""

from __future__ import annotations

import json
import math
import sys
from collections import Counter
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streamsplitter import config  # noqa: E402
from phase0 import truth as truth_mod  # noqa: E402
from phase0.fake_live import chunk_dir  # noqa: E402
from phase0.run_day import tba_matches, tba_schedule  # noqa: E402

NOTES = Path(__file__).resolve().parent / "truth_notes.json"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (math.nan, math.nan)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def rate(k: int, n: int) -> dict:
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "p": round(k / n, 4) if n else None, "ci95": [round(lo, 4), round(hi, 4)]}


def spread(xs: list[float]) -> dict | None:
    if not xs:
        return None
    xs = sorted(xs)
    return {"n": len(xs), "min": round(xs[0], 2), "median": round(median(xs), 2), "max": round(xs[-1], 2)}


def score(run: str) -> dict:
    rdir = config.data_root() / "phase0" / "runs" / run
    res = json.loads((rdir / "matches.json").read_text(encoding="utf-8"))
    day = res["day"]
    index = json.loads((chunk_dir(day) / "index.json").read_text(encoding="utf-8"))
    duration = index["chunks"][-1]["t1"]
    sched = tba_schedule(day, index["release_timestamp"], duration)
    found = res["matches"]
    notes = json.loads(NOTES.read_text(encoding="utf-8")).get(day, {})
    tj = truth_mod.join([m["start"] for m in found], sched, duration, pins=notes.get("_pins"))
    truth = tj["keys"]
    unresolved = [k for k in tj["in_gap"] if k not in notes]
    expected = set(tj["expected"]) | {k for k in tj["in_gap"] if notes.get(k, {}).get("verdict") == "present"}
    inside = [s for s in sched if s["key"] in expected]
    by_key = {truth[i]: (i, found[i]) for i in truth}
    hit, missed, bad_pad = [], [], []
    for s in inside:
        if s["key"] not in by_key:
            missed.append(s["key"])
            continue
        m = by_key[s["key"]][1]
        c = m.get("clip")
        if c is not None and not (2.95 <= c["pad_before"] <= 9.05 and abs(c["pad_after"] - 3.0) < 0.05):
            bad_pad.append((s["key"], c["pad_before"], c["pad_after"]))
        else:
            hit.append(s["key"])
    named_ok, named_wrong, unnamed = [], [], []
    for i, key in truth.items():
        nm = found[i].get("name")
        live_key = (found[i].get("naming") or {}).get("key") or (nm.split("_r")[0] if nm else None)
        if live_key is None:
            unnamed.append(key)
        elif live_key == key:
            named_ok.append(key)
        else:
            named_wrong.append((key, nm, (found[i].get("naming") or {}).get("source")))
    false = [{"start": round(m["start"], 1), "roles": sorted(m["roles"]), "clock": bool(m.get("clock")),
              "name": m.get("name")} for i, m in enumerate(found) if i not in truth]
    lag = res.get("clock_lag_s") or 0.0
    d_start, d_end, flagged = [], [], []
    for i, m in enumerate(found):
        ck = m.get("clock")
        if not ck or m.get("start_src") == "clock" or ck.get("auto_start") is None:
            continue
        ds = m["start"] - (ck["auto_start"] - lag)
        d_start.append(ds)
        if ck.get("match_end") is not None and "end" in m["roles"]:
            d_end.append(m["end"] - (ck["match_end"] - lag))
        if abs(ds) > 1.0:
            flagged.append((truth.get(i), round(m["start"], 1), round(ds, 2), m.get("start_src")))
    timing = json.loads((rdir / "timing.json").read_text(encoding="utf-8"))
    media = sum(t["chunk_s"] for t in timing)
    proc = sum(t.get("proc_s", t.get("audio_s", 0) + t.get("results_s", 0)) for t in timing)
    clips = [m["clip"] for m in found if m.get("clip")]
    out = {
        "run": run, "found": len(found), "played_in_video": len(inside), "truth_segments": tj["segments"],
        "in_gap": {k: (notes.get(k) or {}).get("verdict", "UNRESOLVED") for k in tj["in_gap"]},
        "unresolved_truth": unresolved, "unused_pins": tj["unused_pins"],
        "found_in_partial": [truth[i] for i in truth if (notes.get(truth[i]) or {}).get("verdict") == "partial"],
        "recall": rate(len(hit), len(inside)), "missed": missed, "bad_padding": bad_pad,
        "naming": rate(len(named_ok), len(truth)), "named_wrong": named_wrong, "unnamed": unnamed,
        "name_sources": dict(Counter((m.get("naming") or {}).get("source") for m in found)),
        "false_clips": false,
        "start_src": dict(Counter(m.get("start_src", "audio") for m in found)),
        "with_clock": sum(1 for m in found if m.get("clock")),
        "audio_roles_ge2": sum(1 for m in found if len(m["roles"]) >= 2),
        "role_counts": dict(Counter(r for m in found for r in m["roles"])),
        "clock_lag_s": lag,
        "boundary_start_audio_minus_clock_s": spread(d_start),
        "boundary_end_audio_minus_clock_s": spread(d_end),
        "boundary_flagged_over_1s": flagged,
        "speed_x_realtime": round(media / max(proc, 1e-9), 1),
        "proc_s_per_chunk": spread([t.get("proc_s", 0) for t in timing]),
    }
    if clips:
        out["latency_s"] = spread([c["latency_s"] for c in clips])
        out["pad_before_s"] = spread([c["pad_before"] for c in clips])
    pieces = [m for m in found if m.get("score_piece")]
    if any("score_piece" in m for m in found):
        out["score_pieces"] = {"found": len(pieces), "of": len(found),
                               "appear_after_end_s": spread([m["score_piece"]["appear"] - m["end"] for m in pieces]),
                               "length_s": spread([m["score_piece"]["end"] - m["score_piece"]["start"]
                                                   for m in pieces]),
                               "missing": [truth.get(i) or round(m["start"], 1) for i, m in enumerate(found)
                                           if not m.get("score_piece")]}
        # The results screen goes on air when the score is committed, so its appearance after the match start must
        # track TBA's post_result_time after actual_time, plus a constant broadcast lag: a piece far from the day's
        # lag shows some other screen (or the right one at the wrong time).
        tba = {m["key"]: m for m in tba_matches(day)}
        lag = {truth[i]: (m["score_piece"]["appear"] - m["start"]) - (tba[truth[i]]["post_result_time"]
                                                                       - tba[truth[i]]["actual_time"])
               for i, m in enumerate(found) if m.get("score_piece") and i in truth
               and tba.get(truth[i], {}).get("post_result_time") and tba[truth[i]].get("actual_time")}
        if lag:
            mid = median(lag.values())
            out["score_pieces"]["appear_vs_tba_post_s"] = spread(list(lag.values()))
            out["score_pieces"]["off_tba_post_by_5s"] = {k: round(v - mid, 1) for k, v in lag.items()
                                                          if abs(v - mid) > 5.0}
    return out


if __name__ == "__main__":
    for run in sys.argv[1:]:
        print(json.dumps(score(run), indent=1))
