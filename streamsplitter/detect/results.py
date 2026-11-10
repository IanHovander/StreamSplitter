"""Results-screen detector: when is FIRST's 2026 match-results screen on air?

The results screen is a full-frame graphic with a fixed layout: title bar ("Qualification 40 of 120"), BLUE and RED
score boxes, the centre table, three team rows per side, ranking points, and a white bottom bar. Only the yellow
WINNER box (on the winning side) and the losing side's dark header change with the result. A frame is compared with
reference screens (one blue win, one red win) at 64x36 pixels, ignoring the two WINNER corners; the score is the
fraction of compared pixels within `tol` of the reference in every channel.

Cheap scan: only keyframes are decoded (one every 2-5 s). Where presence flips between two keyframes, that interval
is decoded at 5 fps to place the flip to 0.2 s.
"""

from __future__ import annotations

import subprocess

import numpy as np

from .. import ffmpeg, media

W, H = 64, 36


def winner_mask() -> np.ndarray:
    m = np.ones((H, W), bool)
    y0, y1 = int(0.07 * H), int(0.32 * H)
    m[y0:y1, : int(0.31 * W)] = False
    m[y0:y1, int(0.69 * W):] = False
    return m


def keyframe_thumbs(path) -> np.ndarray:
    """Every keyframe of a file at WxH (BGR)."""
    raw = subprocess.run([ffmpeg.find(), "-v", "error", "-skip_frame", "nokey", "-i", str(path),
                          "-vf", f"scale={W}:{H}:flags=area", "-fps_mode", "passthrough",
                          "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, H, W, 3)


def thumbs_between(path, a: float, b: float, fps: float = 5.0) -> np.ndarray:
    """Frames of [a, b) seconds (file-relative) at `fps`, WxH (BGR)."""
    inp, outp = media.exact_seek(max(0.0, a))
    out = subprocess.run([ffmpeg.find(), "-v", "error", *inp, "-i", str(path), *outp, "-t", f"{b - a:.3f}",
                          "-vf", f"fps={fps},scale={W}:{H}:flags=area",
                          "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(out, np.uint8).reshape(-1, H, W, 3)


class ResultsDetector:
    def __init__(self, refs: np.ndarray, tol: int = 48, thr: float = 0.6):
        self.refs = refs.astype(np.int16)          # (n, H, W, 3)
        self.mask = winner_mask()
        self.tol = tol
        self.thr = thr

    def score(self, thumbs: np.ndarray) -> np.ndarray:
        """Best similarity to any reference, per frame, in [0, 1]."""
        if not len(thumbs):
            return np.zeros(0)
        d = np.abs(thumbs[:, None].astype(np.int16) - self.refs[None]).max(axis=-1) <= self.tol   # (n, r, H, W)
        return (d[:, :, self.mask].mean(axis=-1)).max(axis=1)

    def feed(self, path, t0: float) -> list[dict]:
        """Presence samples for one chunk: {"kind": "results", "t", "score", "on"}, refined around flips."""
        from ..cutter import keyframes
        kts = keyframes(path, t0)
        th = keyframe_thumbs(path)
        n = min(len(kts), len(th))
        kts, sc = kts[:n], self.score(th[:n])
        out = [{"kind": "results", "t": round(t, 3), "score": round(float(s), 3), "on": bool(s >= self.thr)}
               for t, s in zip(kts, sc)]
        refined = []
        for prev, nxt in zip(out, out[1:]):
            if prev["on"] != nxt["on"]:
                fr = thumbs_between(path, prev["t"] - t0, nxt["t"] - t0)
                fs = self.score(fr)
                refined += [{"kind": "results", "t": round(prev["t"] + i / 5.0, 3), "score": round(float(s), 3),
                             "on": bool(s >= self.thr)} for i, s in enumerate(fs)]
        return sorted(out + refined, key=lambda e: e["t"])


def _frame(path, t_rel: float) -> np.ndarray | None:
    inp, outp = media.exact_seek(t_rel)
    raw = subprocess.run([ffmpeg.find(), "-v", "error", *inp, "-i", str(path), *outp, "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], capture_output=True).stdout
    info = subprocess.run([ffmpeg.find("ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries",
                           "stream=width,height", "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
    try:    # MPEG-TS lists the stream twice (programs and streams): take the first line
        w, h = (int(v) for v in info.strip().splitlines()[0].split(",")[:2])
    except (ValueError, IndexError):
        return None
    return np.frombuffer(raw, np.uint8).reshape(h, w, 3) if len(raw) == w * h * 3 else None


def read_qr(chunks: list[dict], times: list[float]) -> str | None:
    """The results screen's QR code (a link to the official match page, e.g. frc.events/2026/CAGLE/qualification/63)
    from the first of `times` (stream seconds) where it decodes; the whole frame first, then the lower middle where
    the code sits, enlarged 2x."""
    import cv2
    det = cv2.QRCodeDetector()
    for t in times:
        c = next((c for c in chunks if c["t0"] <= t < c["t1"]), None)
        if c is None:
            continue
        v0 = media.stream_starts(c["path"]).get("video", 0.0)
        a0 = media.stream_starts(c["path"]).get("audio", v0)
        fr = _frame(c["path"], t - c["t0"] + max(0.0, v0 - min(v0, a0)))
        if fr is None:
            continue
        val, _, _ = det.detectAndDecode(fr)
        if not val:
            h, w = fr.shape[:2]
            crop = fr[int(0.6 * h):int(0.97 * h), int(0.4 * w):int(0.75 * w)]
            val, _, _ = det.detectAndDecode(cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC))
        if val:
            return val
    return None


def score_piece(samples: list[dict], match_end: float, lead_s: float = 3.0, cap_s: float = 30.0,
                gone_s: float = 30.0, search_s: float = 420.0) -> dict | None:
    """The results piece for a match, per the 2026-11-03 rule: from `lead_s` before the screen first appears after the
    match until whichever is first, `cap_s` after it appeared or just before it is removed for more than `gone_s`.
    Returns None when no results screen appears within `search_s` of the end. Shorter removals do not end it."""
    s = sorted((e for e in samples if match_end < e["t"] <= match_end + search_s), key=lambda e: e["t"])
    on_t = [e["t"] for e in s if e["on"]]
    if not on_t:
        return None
    appear = on_t[0]
    removal = None
    for e in s:
        if e["t"] <= appear or e["on"]:
            continue
        nxt_on = next((t for t in on_t if t > e["t"]), None)
        if nxt_on is None or nxt_on - e["t"] > gone_s:   # gone for more than gone_s from here: removed
            removal = e["t"]
            break
    end = appear + cap_s if removal is None else min(appear + cap_s, removal)
    return {"appear": appear, "start": appear - lead_s, "end": end, "removed": removal}
