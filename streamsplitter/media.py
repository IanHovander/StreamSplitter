"""Small ffmpeg/ffprobe helpers shared by the detectors and the cutter."""

from __future__ import annotations

import json
import subprocess

import numpy as np

from . import ffmpeg


def stream_starts(path) -> dict[str, float]:
    """Start time (seconds, container timeline) of the first video and audio stream of a file."""
    out = subprocess.run([ffmpeg.find("ffprobe"), "-v", "error", "-show_entries", "stream=codec_type,start_time",
                          "-of", "json", str(path)], capture_output=True, check=True, text=True).stdout
    starts: dict[str, float] = {}
    for s in json.loads(out).get("streams", []):
        kind = s.get("codec_type")
        if kind in ("video", "audio") and kind not in starts and s.get("start_time") not in (None, "N/A"):
            starts[kind] = float(s["start_time"])
    return starts


def exact_seek(a: float, back: float = 12.0) -> tuple[list[str], list[str]]:
    """ffmpeg (input args, output args) that start reading exactly at file-relative second `a` of an MPEG-TS chunk.

    An input seek alone (`-ss a -i chunk.ts`) can land on the keyframe after `a`, up to one keyframe interval late
    (measured: 5 s late on the saved 2026 day streams). So the input seek goes to `back` seconds earlier, and an output
    seek, which decodes and drops frames by timestamp, covers the rest exactly."""
    pre = max(0.0, a - back)
    return ["-ss", f"{pre:.3f}"], ["-ss", f"{a - pre:.3f}"]


def decode_audio(path, sr: int) -> np.ndarray:
    """Mono float32 samples of a file's first audio stream at `sr` Hz."""
    raw = subprocess.run([ffmpeg.find(), "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr),
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
