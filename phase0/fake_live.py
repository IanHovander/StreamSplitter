"""Phase 0 fake-live harness, step 1: cut a saved day stream into the chunks a live recorder would write.

    .venv\\Scripts\\python.exe phase0\\fake_live.py D1        -> <data>/phase0/chunks/D1/chunk_00000.ts ... + index.json

Chunks are ~60 s MPEG-TS pieces copied from the stream (no re-encode), each starting at a keyframe, exactly like the
live recorder's output. index.json lists every chunk's stream start/end (seconds from the start of the video) and the
wall-clock time the live recorder would have stamped on it: the stream's actual start (yt-dlp `release_timestamp`)
plus the stream time. The real arrival time also carries the platform delay; phase 0 fits that offset per day.

`iter_chunks(tag)` replays the index in order; the detectors only ever see one chunk at a time.
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from streamsplitter import config, ffmpeg  # noqa: E402
from phase0.get_day import DAYS, SEALED, day_inputs, day_path  # noqa: E402

CHUNK_S = 60


def chunk_dir(tag: str) -> Path:
    return config.work_root() / "phase0" / "chunks" / tag


def make_chunks(tag: str) -> Path:
    if tag in SEALED:
        raise SystemExit(f"{tag} is sealed")
    srcs = day_inputs(tag)
    if not srcs:
        raise SystemExit(f"{tag}: not downloaded")
    out = chunk_dir(tag)
    index_path = out / "index.json"
    if index_path.is_file():
        return index_path
    out.mkdir(parents=True, exist_ok=True)
    seg_csv = out / "segments.csv"
    ins = [x for p in srcs for x in ("-i", str(p))]
    cmd = [ffmpeg.find(), "-hide_banner", "-loglevel", "error", "-y", *ins, "-map", "0:v:0",
           "-map", f"{len(srcs) - 1}:a:0", "-c", "copy", "-f", "segment", "-segment_time", str(CHUNK_S), "-segment_format", "mpegts",
           "-segment_list", str(seg_csv), "-segment_list_type", "csv", str(out / "chunk_%05d.ts")]
    subprocess.run(cmd, check=True)
    dp = day_path(tag)
    infos = [dp.with_suffix(".info.json")] + sorted(dp.parent.glob(dp.stem + ".f*.info.json"))
    info_path = next((p for p in infos if p.is_file()), None)
    info = json.loads(info_path.read_text(encoding="utf-8")) if info_path else {}
    wall0 = info.get("release_timestamp") or info.get("timestamp")
    chunks = []
    with seg_csv.open(newline="") as f:
        for i, (name, t0, t1) in enumerate(csv.reader(f)):
            chunks.append({"seq": i, "path": name, "t0": float(t0), "t1": float(t1),
                           "wall0": None if wall0 is None else wall0 + float(t0)})
    event, date, vid = DAYS[tag]
    index = {"tag": tag, "event": event, "date": date, "video": vid, "release_timestamp": wall0, "chunks": chunks}
    index_path.write_text(json.dumps(index, indent=1), encoding="utf-8")
    return index_path


def iter_chunks(tag: str):
    """Yield (path, chunk dict) in stream order."""
    out = chunk_dir(tag)
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))
    for c in index["chunks"]:
        yield out / c["path"], c


def main(argv: list[str]) -> int:
    for tag in argv:
        p = make_chunks(tag)
        n = len(json.loads(p.read_text(encoding="utf-8"))["chunks"])
        print(f"{tag}: {n} chunks -> {p.parent}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
