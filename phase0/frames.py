"""Phase 0: frame sheets for looking at the stream at given stream times (visual checks, failure pictures).

    .venv\\Scripts\\python.exe phase0\\frames.py D1 OUT.png T [T ...] [--crop x0,y0,x1,y1] [--width 640]

Each tile is one frame, labelled with its stream time. --crop shows only that region (full resolution, e.g. the
scoreboard), otherwise the whole frame scaled to --width.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streamsplitter import ffmpeg, media  # noqa: E402
from phase0.fake_live import chunk_dir  # noqa: E402


def frame_at(day: str, t: float) -> np.ndarray | None:
    cdir = chunk_dir(day)
    index = json.loads((cdir / "index.json").read_text(encoding="utf-8"))
    c = next((c for c in index["chunks"] if c["t0"] <= t < c["t1"]), None)
    if c is None:
        return None
    path = cdir / c["path"]
    inp, outp = media.exact_seek(t - c["t0"])
    raw = subprocess.run([ffmpeg.find(), "-v", "error", *inp, "-i", str(path), *outp, "-frames:v", "1",
                          "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True).stdout
    if not raw:
        return None
    return cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)


def sheet(day: str, times: list[float], out: Path, crop=None, width: int = 640, cols: int = 3) -> Path:
    tiles = []
    for t in times:
        fr = frame_at(day, t)
        if fr is None:
            fr = np.zeros((360, 640, 3), np.uint8)
        if crop:
            x0, y0, x1, y1 = crop
            fr = fr[y0:y1, x0:x1]
        else:
            fr = cv2.resize(fr, (width, int(fr.shape[0] * width / fr.shape[1])), interpolation=cv2.INTER_AREA)
        lab = np.zeros((28, fr.shape[1], 3), np.uint8)
        cv2.putText(lab, f"{day} t={t:.2f}", (6, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
        tiles.append(np.vstack([lab, fr]))
    h = max(t.shape[0] for t in tiles)
    w = max(t.shape[1] for t in tiles)
    tiles = [cv2.copyMakeBorder(t, 0, h - t.shape[0], 0, w - t.shape[1], cv2.BORDER_CONSTANT) for t in tiles]
    while len(tiles) % cols:
        tiles.append(np.zeros_like(tiles[0]))
    grid = np.vstack([np.hstack(tiles[i:i + cols]) for i in range(0, len(tiles), cols)])
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), grid)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("day")
    ap.add_argument("out")
    ap.add_argument("times", nargs="+", type=float)
    ap.add_argument("--crop")
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--cols", type=int, default=3)
    a = ap.parse_args(argv)
    crop = tuple(int(v) for v in a.crop.split(",")) if a.crop else None
    print(sheet(a.day, a.times, Path(a.out), crop, a.width, a.cols))
    return 0


if __name__ == "__main__":
    sys.exit(main())
