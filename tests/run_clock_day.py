"""Feed a range of a full-day stream video to the clock reader as 60 s MPEG-TS chunks, fake live: an ffmpeg segment
muxer cuts the chunks (stream copy, -copyts, so TS timestamps are the source video's) while the reader consumes each
chunk as soon as the next one has started.  Pieces stop at a wall-clock budget and pickle the reader, so a long range
is processed in several short foreground runs with state carried over as in one live run.

    python tests/run_clock_day.py <day.mp4> --start 0 --end 18000 --state F:/.../d1.pkl --out-dir F:/.../d1 \
        [--budget 540]

Re-running with the same --state continues where the last piece stopped (the state records the next start).
Each piece writes <out-dir>/piece_<start>.json with its readings, per-chunk stats, and the reader's matches/events.
"""

from __future__ import annotations

import argparse
import json
import logging
import pickle
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from streamsplitter import ffmpeg as _ffmpeg  # noqa: E402
from streamsplitter.detect.clock import ClockReader, probe  # noqa: E402

SCRATCH = Path(r"F:\StreamSplitterData\phase0\scratch_clock")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--start", type=float, required=True)
    ap.add_argument("--end", type=float, required=True)
    ap.add_argument("--state", required=True, help="pickle {'reader', 'next_start'} (created if missing)")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--budget", type=float, default=540.0, help="stop after this many wall seconds")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    wall0 = time.time()
    src = Path(args.video)
    state = Path(args.state)
    if state.is_file():
        st = pickle.loads(state.read_bytes())
        reader, start = st["reader"], st["next_start"]
    else:
        reader, start = ClockReader(), args.start
    if start >= args.end - 1:
        print("range already done")
        return
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = SCRATCH / "day_chunks" / f"{src.stem}_{int(start)}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    cmd = [_ffmpeg.find("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-to", f"{args.end:.3f}",
           "-copyts", "-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
           "-muxdelay", "0", "-muxpreload", "0", "-f", "segment", "-segment_time", "60",
           "-segment_format", "mpegts", str(work / "c%05d.ts")]
    cutter = subprocess.Popen(cmd)
    readings, per_chunk = [], []
    k = 0
    next_start = start
    stopped = "range end"
    try:
        while True:
            cur, nxt = work / f"c{k:05d}.ts", work / f"c{k + 1:05d}.ts"
            while not nxt.exists() and cutter.poll() is None:
                time.sleep(0.2)
            if not cur.exists():
                break
            info = probe(cur)
            if info is None:
                break
            t0 = info["start"]
            r = reader.feed(cur, t0)
            readings += r
            s = dict(reader.last_stats)
            per_chunk.append(s)
            vals = sorted({x["value_s"] for x in r})
            print(f"  t0={t0:9.2f} {s['mode']:6s} wall={s['wall_s']:5.2f} "
                  f"cpu={s['python_cpu_s'] + s['ffmpeg_cpu_s']:5.2f} n={len(r):4d} {vals[:1]}..{vals[-1:]}", flush=True)
            next_start = t0 + info["duration"]
            if k >= 3:
                (work / f"c{k - 3:05d}.ts").unlink(missing_ok=True)     # keep 3 for the reader's back-fill
            k += 1
            if time.time() - wall0 > args.budget:
                stopped = "budget"
                break
    finally:
        if cutter.poll() is None:
            cutter.kill()
            cutter.wait()
    # the reader keeps paths of its last chunks for back-fill: those files are about to go
    reader._recent.clear()
    state.write_bytes(pickle.dumps({"reader": reader, "next_start": next_start}))
    piece = {"range": [start, next_start], "stopped": stopped, "readings": readings, "per_chunk": per_chunk,
             "matches": reader.matches(), "events": reader.events, "status": reader.status()}
    (out_dir / f"piece_{int(start):06d}.json").write_text(json.dumps(piece, indent=1, default=str))
    shutil.rmtree(work, ignore_errors=True)
    tot = reader.totals
    print(f"piece {start:.0f}-{next_start:.0f} s ({stopped}) in {time.time() - wall0:.0f} s wall; totals so far "
          f"{tot['media_s']:.0f} s media, cpu {tot['cpu_s']:.0f} s ({tot['media_s'] / max(tot['cpu_s'], 1e-6):.1f}x per "
          f"core), reader wall {tot['wall_s']:.0f} s", flush=True)


if __name__ == "__main__":
    main()
