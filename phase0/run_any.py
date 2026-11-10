"""Phase 0, event-agnostic: fake-live run of a day through streamsplitter/pipeline.py (OCR overlay timer, any game).

    .venv\\Scripts\\python.exe phase0\\run_any.py C1 [--tag t1] [--from-s S] [--limit-s S] [--stop-after N] [--pace X]

Chunks are fed one at a time, as the live recorder writes them. `--pace 0` (the default) reads them as fast as it
can. `--pace 1` releases each chunk when it would be complete live: when its last second has happened on a replay
clock started at the first chunk.

Timing per clip:
- "timer_s" (the user's definition, 2026-11-04): from the moment the program starts processing the minute (chunk)
  holding the match start, to the moment the clip (match + padding) is on disk.
- "after_end_s" (paced runs only): clip on disk minus the moment the match end happened on the replay clock.
- "projected_live_after_end_s" (any run): the wait live for the chunk holding end + padding to finish (it completes
  at its t1), plus the processing from then until the clip is on disk. YouTube's own delay comes on top.

Writes <data>/phase0/runs/<day>_<tag>/: matches.json (clips, naming, timing, episodes) and timing.json (per chunk).
A FINAL day (phase0/get_day.py) runs only with --final.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streamsplitter import config  # noqa: E402
from streamsplitter.pipeline import Pipeline  # noqa: E402
from phase0.fake_live import iter_chunks, make_chunks  # noqa: E402
from phase0.get_day import FINAL, SEALED  # noqa: E402


def load_schedule(event: str) -> list[dict] | None:
    p = config.data_root() / "phase0" / f"tba_{event}.json"
    return json.loads(p.read_text(encoding="utf-8"))["matches"] if p.is_file() else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("day")
    ap.add_argument("--tag", default="any1")
    ap.add_argument("--from-s", type=float, default=None)
    ap.add_argument("--limit-s", type=float, default=None)
    ap.add_argument("--stop-after", type=int, default=None, help="stop after this many clips")
    ap.add_argument("--pace", type=float, default=0.0, help="0 = as fast as possible; 1 = live speed")
    ap.add_argument("--deliver", default=None, help="also copy each clip here (e.g. a cloud-synced folder)")
    ap.add_argument("--final", action="store_true", help="allow a FINAL (held-out test) day")
    a = ap.parse_args(argv)
    if a.day in SEALED:
        sys.exit(f"{a.day} is sealed")
    if a.day in FINAL and not a.final:
        sys.exit(f"{a.day} is the final test day: only with --final")
    index = json.loads(make_chunks(a.day).read_text(encoding="utf-8"))
    event, release_ts = index["event"], index["release_timestamp"] or 0.0
    out = config.data_root() / "phase0" / "runs" / f"{a.day}_{a.tag}"
    out.mkdir(parents=True, exist_ok=True)
    pipe = Pipeline(out / "clips", event=event, schedule=load_schedule(event), release_ts=release_ts,
                    deliver_dir=Path(a.deliver) if a.deliver else None)
    started: dict[int, tuple[float, dict]] = {}
    timing, rows = [], []
    replay0 = first_t0 = None

    def save(final: bool) -> None:
        res = {"day": a.day, "event": event, "pace": a.pace, "from_s": a.from_s, "rows": rows,
               "learned": pipe.timeline.values(), "rejected": pipe.timeline.rejected,
               "reader": pipe.reader.stats, "final": final}
        if final:
            res["episodes"] = pipe.reader.episodes()
        (out / "matches.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
        (out / "timing.json").write_text(json.dumps(timing), encoding="utf-8")

    for path, c in iter_chunks(a.day):
        if a.limit_s is not None and c["t0"] >= a.limit_s:
            break
        if a.from_s is not None and c["t1"] <= a.from_s:
            continue
        if a.pace > 0:
            if replay0 is None:
                replay0, first_t0 = time.perf_counter() - (c["t1"] - c["t0"]) / a.pace, c["t0"]
            wait = replay0 + (c["t1"] - first_t0) / a.pace - time.perf_counter()
            if wait > 0:
                time.sleep(wait)
        t_p = time.perf_counter()
        started[c["seq"]] = (t_p, c)
        written = pipe.feed(path, c, now_wall=release_ts + c["t1"])
        t_done = time.perf_counter()
        timing.append({"seq": c["seq"], "t0": c["t0"], "t1": c["t1"], "proc_s": round(t_done - t_p, 2),
                       "places": len(pipe.reader.places)})
        print(f"chunk {c['seq']:4d} t={c['t0']:8.1f} proc {t_done - t_p:5.1f}s places {len(pipe.reader.places)}",
              flush=True)
        for r in written:
            s_chunk = next((sc for _, sc in started.values() if sc["t0"] <= r["start"] < sc["t1"]), None)
            tm = {"chunk_with_start": s_chunk["seq"] if s_chunk else None,
                  "timer_s": round(r["written_perf"] - started[s_chunk["seq"]][0], 1) if s_chunk else None,
                  "projected_live_after_end_s": round((c["t1"] - r["end"]) + (r["written_perf"] - t_p), 1)}
            if a.pace > 0:
                tm["after_end_s"] = round(r["written_perf"] - (replay0 + (r["end"] - first_t0) / a.pace), 1)
            r["timing"] = tm
            r.pop("written_perf", None)
            rows.append(r)
            print(f"CLIP {r['name']} {r['start']:.2f}-{r['end']:.2f} start_src={r['start_src']} "
                  f"end_src={r['end_src']} naming={r['naming'].get('source')} timing={tm}", flush=True)
        save(False)
        if a.stop_after is not None and len(rows) >= a.stop_after:
            break
    save(True)
    print(f"{a.day}: {len(rows)} clips", flush=True)
    print("DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
