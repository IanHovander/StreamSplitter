"""Phase 0: the 2026 results-screen references, cut from dev day D1 (2026cancmp, 2026-04-11).

Frames chosen by eye on 2026-11-03 (F:/StreamSplitterData/phase0/scratch_results/): qm40 blue win at video 2215 s,
qm39 red win at video 1686 s.

    .venv\\Scripts\\python.exe phase0\\build_results_refs.py   -> streamsplitter/seasons/2026/results_refs.npy
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streamsplitter import ffmpeg  # noqa: E402
from streamsplitter.detect.results import H, W  # noqa: E402
from phase0.get_day import day_path  # noqa: E402

FRAMES = [("D1", 2215.0), ("D1", 1686.0)]


def thumb(tag: str, t: float) -> np.ndarray:
    raw = subprocess.run([ffmpeg.find(), "-v", "error", "-ss", str(t), "-i", str(day_path(tag)), "-frames:v", "1",
                          "-vf", f"scale={W}:{H}:flags=area", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(H, W, 3)


if __name__ == "__main__":
    refs = np.stack([thumb(tag, t) for tag, t in FRAMES])
    out = ROOT / "streamsplitter" / "seasons" / "2026" / "results_refs.npy"
    np.save(out, refs)
    print(out, refs.shape)
