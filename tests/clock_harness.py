"""Fake-live helpers for the clock reader tests: split a video into MPEG-TS chunks and feed them in order.

    python tests/clock_harness.py <video> [--seg 60] [--out DIR] [--start S] [--duration S] [--json OUT]

Chunks are cut with `ffmpeg -c copy -f segment` (stream copy, keyframe-aligned, like the live recorder).  Each chunk's
t0 is its first video pts minus the first chunk's (or, with --start, the cut's own start), so t is seconds of the
source video from the first video frame of the first chunk.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from streamsplitter import ffmpeg as _ffmpeg  # noqa: E402
from streamsplitter.detect.clock import ClockReader, probe  # noqa: E402

SCRATCH = Path(r"F:\StreamSplitterData\phase0\scratch_clock")


def split_chunks(src: str | Path, seg: float = 60.0, out_dir: Path | None = None, start: float | None = None,
                 duration: float | None = None, video_only: bool = False) -> list[tuple[str, float]]:
    """Cut `src` into ~seg-second TS chunks (cached by name) and return [(path, t0)] in order."""
    src = Path(src)
    key = hashlib.md5(f"{src.resolve()}|{seg}|{start}|{duration}|{video_only}".encode()).hexdigest()[:8]
    out_dir = Path(out_dir or SCRATCH / "chunks" / f"{src.stem}_{key}")
    done = out_dir / "chunks.json"
    if done.is_file():
        return [(str(out_dir / p), t) for p, t in json.loads(done.read_text())]
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [_ffmpeg.find("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(src)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-map", "0:v:0"] + ([] if video_only else ["-map", "0:a?"])
    cmd += ["-c", "copy", "-f", "segment", "-segment_time", str(seg), "-segment_format", "mpegts",
            str(out_dir / "c%05d.ts")]
    subprocess.run(cmd, check=True)
    paths = sorted(out_dir.glob("c*.ts"))
    starts = [probe(p)["start"] for p in paths]
    base = starts[0]
    chunks = [(p.name, round(s - base, 6)) for p, s in zip(paths, starts)]
    done.write_text(json.dumps(chunks))
    return [(str(out_dir / p), t) for p, t in chunks]


def run(chunks, reader: ClockReader | None = None, verbose: bool = True) -> dict:
    reader = reader or ClockReader()
    readings, per_chunk = [], []
    for path, t0 in chunks:
        r = reader.feed(path, t0)
        readings += r
        st = dict(reader.last_stats)
        per_chunk.append(st)
        if verbose:
            vals = sorted({x["value_s"] for x in r})
            print(f"  chunk t0={t0:8.2f} mode={st['mode']:6s} wall={st['wall_s']:5.2f}s "
                  f"cpu(py+ff)={st['python_cpu_s'] + st['ffmpeg_cpu_s']:5.2f}s readings={len(r):4d} "
                  f"values {vals[:1]}..{vals[-1:]}", flush=True)
    return {"readings": readings, "matches": reader.matches(), "events": reader.events, "status": reader.status(),
            "per_chunk": per_chunk, "reader": reader}


def expected_display(t: float, m: dict, auto_s: int = 20, tele_s: int = 140) -> int | None:
    """Value the fitted model says the overlay shows at stream time t (display convention: v while the remaining
    time is in (v-1, v]).  None outside the match or when the model lacks the anchors."""
    a, ae, ts, me, k = m.get("auto_start"), m.get("auto_end"), m.get("teleop_start"), m.get("match_end"), m.get("rate")
    if None in (ts, me, k):
        return None
    if a is not None and not m.get("auto_inferred") and a <= t < ae:
        return int(math.ceil(auto_s - (t - a) / k - 1e-9))
    if ae is not None and ae <= t < ts:
        return tele_s
    if ts <= t < me:
        return int(math.ceil(tele_s - (t - ts) / k - 1e-9))
    return None


def check_against_model(readings: list[dict], matches: list[dict], margin_s: float = 0.15) -> dict:
    """Readings inside each fitted match versus the model's displayed value.  Readings within `margin_s` of a model
    tick are skipped (which of the two values a sample shows there is a coin toss)."""
    agree = disagree = skipped = 0
    bad = []
    for m in matches:
        if not m.get("ok"):
            continue
        k = m["rate"]
        for r in readings:
            v = expected_display(r["t"], m)
            if v is None:
                continue
            # distance to the nearest model tick
            if m["teleop_start"] <= r["t"] < m["match_end"]:
                e = (r["t"] - m["teleop_start"]) / k
            elif m.get("auto_start") is not None and m["auto_start"] <= r["t"] < m["auto_end"]:
                e = (r["t"] - m["auto_start"]) / k
            else:
                e = 0.5
            if abs(e - round(e)) * k < margin_s:
                skipped += 1
                continue
            if r["value_s"] == v:
                agree += 1
            else:
                disagree += 1
                bad.append({**r, "expected": v})
    n = agree + disagree
    return {"n": n, "agree": agree, "disagree": disagree, "skipped_near_tick": skipped,
            "agreement": round(agree / n, 5) if n else None, "bad": bad[:50]}


def check_against_research(readings: list[dict], timing_json: str | Path) -> dict:
    """Readings versus the research reader's per-frame clock strings (`per_frame.clock` of a *.timing.json), at the
    nearest source frame (t * fps_nominal; t = 0 is the first video frame).  Frames the research left unread or within
    one frame of a change in its own reads are skipped."""
    d = json.loads(Path(timing_json).read_text(encoding="utf-8"))
    fps = float(d["video_info"]["fps_nominal"])
    clk = d["per_frame"]["clock"]
    agree = disagree = skipped = 0
    bad = []
    for r in readings:
        i = int(round(r["t"] * fps))
        if not (1 <= i < len(clk) - 1):
            skipped += 1
            continue
        ref = clk[i]
        if ref in ("", "absent") or not re.match(r"^\d{1,2}:\d\d$", ref) or clk[i - 1] != ref or clk[i + 1] != ref:
            skipped += 1
            continue
        if r["text"] == ref:
            agree += 1
        else:
            disagree += 1
            bad.append({**r, "research": ref, "frame": i})
    n = agree + disagree
    return {"n": n, "agree": agree, "disagree": disagree, "skipped": skipped,
            "agreement": round(agree / n, 5) if n else None, "bad": bad[:50]}


def compare_anchors(m: dict, ref: dict) -> dict:
    """Signed differences (s) of the fitted anchors from a reference {'auto_start', 'teleop_start', 'match_end'}."""
    return {k: round(m[k] - ref[k], 3) for k in ("auto_start", "teleop_start", "match_end")
            if m.get(k) is not None and ref.get(k) is not None}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--seg", type=float, default=60.0)
    ap.add_argument("--start", type=float, default=None)
    ap.add_argument("--duration", type=float, default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--threads", type=int, default=1)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    t = time.time()
    chunks = split_chunks(args.video, args.seg, start=args.start, duration=args.duration)
    print(f"{len(chunks)} chunks ({time.time() - t:.1f} s to split)")
    res = run(chunks, ClockReader(threads=args.threads))
    reader = res.pop("reader")
    for m in res["matches"]:
        print({k: m.get(k) for k in ("auto_start", "teleop_start", "match_end", "rate", "pause_s", "rms_s", "n_auto",
                                      "n_teleop", "ok", "failures", "teams", "close_reason")})
    tot = reader.totals
    print(f"totals: {tot['media_s']:.0f} s media, wall {tot['wall_s']:.1f} s ({tot['media_s'] / tot['wall_s']:.1f}x), "
          f"cpu {tot['cpu_s']:.1f} s ({tot['media_s'] / max(tot['cpu_s'], 1e-6):.1f}x real time per core)")
    if args.json:
        Path(args.json).write_text(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
