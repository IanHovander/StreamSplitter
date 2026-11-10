"""Phase 0: play a saved stream into a live folder at real-time speed, as if the event went live at `--from-s`, so
`streamsplitter live --no-record` can be timed on an event it has never seen without downloading the whole day.

    .venv\\Scripts\\python.exe phase0\\vod_live.py GNJ6Isz0ncg --from-s 4992 --tag mttd24_live1 --event 2024mttd

ffmpeg reads the video and audio straight from YouTube at native speed (`-re`) into 20 s TS chunks, like the recorder.
Each chunk is listed in index.json `--list-delay-s` after it closes (the recorder lists live chunks 13-16 s after the
platform). `wall0` maps stream time to this run's wall clock, so the follower's "s after the match end" is the
latency a live event would see.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yt_dlp  # noqa: E402

from streamsplitter import config, ffmpeg  # noqa: E402
from streamsplitter.record import js_runtimes  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--from-s", type=float, required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--event", required=True)
    ap.add_argument("--chunk-s", type=float, default=20.0)
    ap.add_argument("--list-delay-s", type=float, default=15.0)
    ap.add_argument("--limit-s", type=float, default=3600.0)
    a = ap.parse_args(argv)
    out = config.work_root() / "live" / a.tag
    out.mkdir(parents=True, exist_ok=True)
    # a fresh recording: drop old chunks and the index, but not the follower's own files (its lock, status, state),
    # since the follower may already be waiting here
    for old in [*out.glob("chunk_*.ts"), out / "segments.csv", out / "index.json"]:
        old.unlink(missing_ok=True)
    t_start = time.time()
    opts = {"format": "bv*[vcodec^=avc1][height<=1080]+ba[ext=m4a]", "quiet": True, "skip_download": True,
            "js_runtimes": js_runtimes()}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={a.video}", download=False)
    fmts = info.get("requested_formats") or [info]
    ua = (fmts[0].get("http_headers") or {}).get("User-Agent", "Mozilla/5.0")
    print(f"formats {[f.get('format_id') for f in fmts]} {fmts[0].get('height')}p in {time.time() - t_start:.1f}s",
          flush=True)
    ins = []
    for f in fmts:
        ins += ["-user_agent", ua, "-re", "-ss", f"{a.from_s:.3f}", "-t", f"{a.limit_s:.0f}", "-i", f["url"]]
    seg_csv = out / "segments.csv"
    amap = ["-map", "1:a:0"] if len(fmts) > 1 else ["-map", "0:a:0"]
    cmd = [ffmpeg.find(), "-hide_banner", "-loglevel", "error", "-y", *ins, "-map", "0:v:0", *amap, "-c", "copy",
           "-f", "segment", "-segment_time", str(a.chunk_s), "-segment_format", "mpegts",
           "-segment_list", str(seg_csv), "-segment_list_type", "csv", str(out / "chunk_%05d.ts")]
    proc = subprocess.Popen(cmd)
    wall0 = time.time()                 # stream time `from_s` is "live" now
    print(f"PLAYING from {a.from_s:.0f}s at {time.strftime('%H:%M:%S')} (setup {wall0 - t_start:.1f}s)", flush=True)
    live = {"tag": a.tag, "event": a.event, "video": a.video, "live": True, "status": "recording",
            "release_timestamp": None, "chunks": []}
    pending: list[dict] = []
    seen = 0
    while True:
        rows = []
        if seg_csv.is_file():
            with seg_csv.open(newline="") as f:
                rows = [r for r in csv.reader(f) if len(r) == 3]
        for name, t0, t1 in rows[seen:]:
            t0, t1 = a.from_s + float(t0), a.from_s + float(t1)
            pending.append({"seq": len(live["chunks"]) + len(pending), "path": name, "t0": t0, "t1": t1,
                            "wall0": wall0 + t0 - a.from_s, "closed_at": time.time()})
        seen = len(rows)
        for c in [c for c in pending if time.time() >= c["closed_at"] + a.list_delay_s]:
            pending.remove(c)
            live["chunks"].append({**{k: v for k, v in c.items() if k != "closed_at"}, "wall_closed": time.time(),
                                   "gap_before_s": 0.0 if live["chunks"] else None, "run": 0,
                                   "source_video": a.video})
            tmp = out / "index.json.tmp"
            tmp.write_text(json.dumps(live, indent=1), encoding="utf-8")
            os.replace(tmp, out / "index.json")
            print(f"LISTED seq {c['seq']} t={c['t0']:.1f}-{c['t1']:.1f} at {time.strftime('%H:%M:%S')}", flush=True)
        if proc.poll() is not None and not pending:
            break
        time.sleep(0.5)
    print(f"DONE ffmpeg exit {proc.returncode}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
