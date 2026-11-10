"""Cutter: writes a clip from the recorded chunks by stream copy (no re-encode).

A stream-copied clip must start on a keyframe, so the clip starts at the last keyframe at or before (start - pad):
padding is `pad` up to pad + one keyframe interval (3-5 s on YouTube). The end is cut at (end + pad). Output is MP4
with the index at the front (faststart) so a browser can play it while it downloads.

Chunks are {"path", "t0", "t1"} in stream seconds (t0 = the chunk's first video frame), as in the recorder's index.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from . import ffmpeg, media


def keyframes(path, t0: float) -> list[float]:
    """Stream times of a chunk's video keyframes."""
    v0 = media.stream_starts(path).get("video", 0.0)
    out = subprocess.run([ffmpeg.find("ffprobe"), "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
                          "-show_entries", "frame=pts_time", "-of", "csv=p=0", str(path)],
                         capture_output=True, check=True, text=True).stdout
    # one line per keyframe; some streams (Chezy 2026, 60 fps) add side-data fields after a comma: keep the first
    vals = [x.split(",")[0] for x in out.split()]
    return [t0 + float(x) - v0 for x in vals if x not in ("", "N/A")]


def _span(chunks: list[dict], a: float, b: float) -> list[dict]:
    return [c for c in chunks if c["t1"] > a and c["t0"] < b]


def _cut_span(chunks: list[dict], want_a: float, want_b: float, out: Path) -> float:
    """Copy [want_a snapped back to a keyframe, want_b] into `out` (container from its suffix); returns the start."""
    span = _span(chunks, want_a - 10.0, want_b)
    if not span:
        raise ValueError("no recorded chunks cover this window")
    kfs = [k for c in span for k in keyframes(c["path"], c["t0"]) if k <= want_a + 1e-3]
    a = max(kfs) if kfs else span[0]["t0"]
    span = [c for c in span if c["t1"] > a]
    # The chunks are joined as one byte stream (MPEG-TS allows it) with their own timestamps kept (-copyts), and
    # the cut is by timestamp: no seeking, so nothing is guessed. The cut starts 0.1 s before the keyframe: with
    # B-frames the keyframe's decode time is a few frames before its display time, and a cut at the display time
    # dropped it, so the picture started a whole GOP (5 s) after the sound. Earlier video packets are not keyframes
    # and ffmpeg drops them; the clip may carry up to 0.1 s of extra sound at the start.
    ts0 = media.stream_starts(span[0]["path"]).get("video", 0.0) - span[0]["t0"]   # stream time -> TS time
    src = "concat:" + "|".join(str(c["path"]) for c in span)
    out.parent.mkdir(parents=True, exist_ok=True)
    mp4 = ["-movflags", "+faststart"] if out.suffix == ".mp4" else []
    def run(audio: bool):
        subprocess.run([ffmpeg.find(), "-v", "error", "-y", "-copyts", "-i", src,
                        "-ss", f"{ts0 + a - 0.1:.3f}", "-to", f"{ts0 + want_b:.3f}", "-map", "0:v:0",
                        *(["-map", "0:a:0"] if audio else []),
                        "-c", "copy", "-avoid_negative_ts", "make_zero", *mp4, str(out)], check=True)
    try:
        run(True)
    except subprocess.CalledProcessError:
        # a sound track MP4 cannot hold (1671's MadTown 2026 stream: sample rate 0): the picture alone beats no clip
        run(False)
    return a


def cut(chunks: list[dict], start: float, end: float, out: Path, pad: float = 3.0) -> dict:
    """Match clip: [start - pad, end + pad] to `out`; returns the actual clip times. The copy starts at the keyframe
    before start - pad and an edit list starts playback at start - pad exactly (`trim_start`)."""
    a = _cut_span(chunks, start - pad, end + pad, out)
    lead = trim_start(out, start - pad - a)
    return {"path": str(out), "clip_start": round(start - pad if lead else a, 3), "clip_end": round(end + pad, 3),
            "pad_before": round(pad if lead else start - a, 3), "pad_after": round(pad, 3),
            "keyframe_lead": round(start - pad - a, 3)}


def trim_start(path: Path, skip: float) -> bool:
    """Start an MP4 `skip` seconds later without re-encoding. The frames from its first keyframe to the new start stay
    in the file and an edit list hides them; browsers, phones, VLC and ffmpeg honour it, and a player that ignored it
    would only show that lead too. Needed because these streams put keyframes 5-7 s apart (Chezy 2026: median 5.7 s,
    max 7.0 s), so a keyframe cut alone gave 3-10 s of padding instead of 3. True when trimmed."""
    if skip < 0.05:
        return False
    tmp = path.with_name(path.stem + ".trim" + path.suffix)
    subprocess.run([ffmpeg.find(), "-v", "error", "-y", "-ss", f"{skip:.3f}", "-i", str(path), "-map", "0", "-c", "copy",
                    "-movflags", "+faststart", str(tmp)], check=True)
    tmp.replace(path)
    return True


def cut_score(chunks: list[dict], start: float, end: float, piece_a: float, piece_b: float, out: Path,
              pad: float = 3.0) -> dict:
    """Score clip: the match clip followed directly by the results piece [piece_a, piece_b] (dead time skipped)."""
    with tempfile.TemporaryDirectory() as tmp:
        p1, p2 = Path(tmp) / "match.ts", Path(tmp) / "piece.ts"
        a = _cut_span(chunks, start - pad, end + pad, p1)
        pa = _cut_span(chunks, piece_a, piece_b, p2)
        lst = Path(tmp) / "list.txt"
        lst.write_text(f"file '{p1.as_posix()}'\nfile '{p2.as_posix()}'\n", encoding="utf-8")
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([ffmpeg.find(), "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-c", "copy", "-movflags", "+faststart", str(out)], check=True)
    lead = trim_start(out, start - pad - a)
    return {"path": str(out), "clip_start": round(start - pad if lead else a, 3), "clip_end": round(end + pad, 3),
            "piece_start": round(pa, 3), "piece_end": round(piece_b, 3)}
