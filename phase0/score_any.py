"""Score an event-agnostic run (phase0/run_any.py) against TBA, as far as TBA can tell.

    .venv\\Scripts\\python.exe phase0\\score_any.py C1_any1 [--offset-tol 10]

TBA's actual_time is when the field started the match (it runs a few seconds ahead of the stream), so for every
clip named after a TBA match, offset = clip start - (actual_time - stream start) should be one constant per day.
A clip whose offset is far from the day's median is misnamed or misplaced. Expected matches are the TBA matches whose
actual_time falls inside the stretch the run read. A stream drop in a saved video hides matches, so a miss is only a
candidate until a picture confirms it. Days without TBA times (MadTown 2025, Kettering) get counts and timing only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streamsplitter import config  # noqa: E402
from phase0.run_any import load_schedule  # noqa: E402


def spread(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return {"n": len(xs), "min": round(xs[0], 2), "median": round(median(xs), 2), "max": round(xs[-1], 2)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--offset-tol", type=float, default=10.0)
    a = ap.parse_args(argv)
    out = config.data_root() / "phase0" / "runs" / a.run
    res = json.loads((out / "matches.json").read_text(encoding="utf-8"))
    rows = res["rows"]
    idx_path = config.work_root() / "phase0" / "chunks" / res["day"] / "index.json"
    index = json.loads(idx_path.read_text(encoding="utf-8"))
    rel = index["release_timestamp"] or 0.0
    timing = json.loads((out / "timing.json").read_text(encoding="utf-8"))
    read_a, read_b = timing[0]["t0"], timing[-1]["t1"]
    sched = load_schedule(res["event"]) or []
    tba = {m["key"]: m for m in sched}
    base = lambda n: re.sub(r"_r\d+$", "", n or "")
    offs = {}
    for r in rows:
        m = tba.get(base(r["name"]))
        if m and m.get("actual_time"):
            offs[r["name"]] = r["start"] - (m["actual_time"] - rel)
    med = median(offs.values()) if offs else None
    report = {"run": a.run, "read": [read_a, read_b], "clips": len(rows)}
    if med is not None:
        report["offset_median_s"] = round(med, 2)
        report["offset"] = spread(list(offs.values()))
        report["offset_outliers"] = {k: round(v - med, 1) for k, v in offs.items() if abs(v - med) > a.offset_tol}
        expected = sorted((m["actual_time"] - rel + med, k) for k, m in tba.items()
                          if m.get("actual_time") and read_a + 30 <= m["actual_time"] - rel + med <= read_b - 200)
        names = {base(r["name"]) for r in rows}
        report["expected"] = len(expected)
        report["found_by_name"] = sum(1 for _, k in expected if k in names)
        report["missed"] = [(round(t, 1), k) for t, k in expected if k not in names]
        report["extra"] = [r["name"] for r in rows if base(r["name"]) not in tba]
        report["replays"] = [r["name"] for r in rows if re.search(r"_r\d+$", r["name"] or "")]
    report["naming_sources"] = {}
    for r in rows:
        s = r["naming"].get("source")
        report["naming_sources"][s] = report["naming_sources"].get(s, 0) + 1
    report["start_src"] = {s: sum(1 for r in rows if r["start_src"] == s) for s in {r["start_src"] for r in rows}}
    report["end_src"] = {s: sum(1 for r in rows if r["end_src"] == s) for s in {r["end_src"] for r in rows}}
    report["length_s"] = spread([r["end"] - r["start"] for r in rows])
    report["pad_before_s"] = spread([r["clip"]["pad_before"] for r in rows])
    report["timer_s"] = spread([r["timing"]["timer_s"] for r in rows])
    report["projected_live_after_end_s"] = spread([r["timing"]["projected_live_after_end_s"] for r in rows])
    if any("after_end_s" in r["timing"] for r in rows):
        report["after_end_s"] = spread([r["timing"].get("after_end_s") for r in rows])
    sp = [r for r in rows if "score_piece" in r]
    report["score"] = {"settled": len(sp), "with_piece": sum(1 for r in sp if r["score_piece"]),
                       "appear_after_end_s": spread([r["score_piece"]["appear"] - r["end"] for r in sp if r["score_piece"]]),
                       "piece_s": spread([r["score_piece"]["end"] - r["score_piece"]["start"] for r in sp if r["score_piece"]]),
                       "no_piece": [r["name"] for r in sp if not r["score_piece"]],
                       "renamed_by_results": [(r["naming"].get("provisional"), r["name"]) for r in rows
                                              if r["naming"].get("source") == "results_title"]}
    report["proc_s"] = spread([t["proc_s"] for t in timing])
    print(json.dumps(report, indent=1))
    (out / "score.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
