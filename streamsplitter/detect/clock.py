"""Incremental reader of FIRST's 2026 scoreboard clock on a live stream delivered as consecutive MPEG-TS chunks.

Ported (copied and adapted, not imported) from the research reader `overlay_reader.py`: section 3 (ink masks,
glyphs, line grouping, normalised glyph templates, NCC classification), 3a (clock location: the white box whose dark
ink forms an m:ss line that changes every second; trimmed-box and enlarged-overlay retries), 4 (clock runs, countdown
labelling that learns the 0-9 digit templates from the video itself, no OCR engine), 5 (clock transitions, match-time
model, checks, discontinuities, TELEOP-only fallback) and the team-number part of section 6.

API
---
    from streamsplitter.detect.clock import ClockReader

    reader = ClockReader()                  # keyword options: see ClockReader.__init__
    for path, t0 in chunks:                 # chunks in stream order; t0 = stream seconds of the chunk's first video frame
        for r in reader.feed(path, t0):     # list of readings, sorted by t
            ...                             # {"t": 1234.567, "text": "2:13", "value_s": 133, "conf": 0.47}
    reader.matches()      # one dict per countdown episode (normally one match), see ClockReader.matches
    reader.teams(t)       # {"red": ["254", ...], "blue": [...], ...} read off the overlay for the match around t, or None
    reader.status()       # mode, clock ROI, template state, timing of the last call (for logs / a status page)

Readings
    `t` is stream seconds: t0 + (frame pts - the chunk's first video pts). `conf` is the weakest digit's template score
    (NCC, capped at 0.5 + the margin to the next-best digit); the research reader's per-frame reads agreed with the
    countdown labels at these scores.  Readings normally belong to the chunk just fed.  Two exceptions: when the clock
    is first located (or found again) the call also reads the previous one or two chunks, and when the digit templates
    are first learned the call returns every buffered sample that can now be read (up to `buffer_s` back).  Consumers
    must therefore accept readings older than the current chunk; each sample is reported at most once.

How it works
    SEARCH (no clock yet, or lost):  decode only the chunk's keyframes (`-skip_frame nokey`, colour, full frame), keep
        the white boxes (top or bottom 35 % of the picture) of the last `search_window_s` seconds, and look for a box
        whose dark ink is an m:ss / mm:ss line (3-4 tall glyphs and a colon) in most of >= 5 frames, with >= 4
        different line shapes and changes between samples, and digits 2.2-10 % of the picture height.  Once digit
        templates exist, a new candidate must also read as a countdown (value drops by the elapsed time, +-2 s); one
        the templates cannot read needs 10 frames and 6 shapes.  A clock place seen before is resumed as soon as 3
        frames of the last minute read as m:ss there (a static 0:20 between matches is enough).  ~0.7 s CPU per chunk.
    TRACK (clock known):  decode the chunk's reference frames only (`-skip_frame noref`; 7-17 samples/s on the streams
        measured, capped at `sample_fps`), cropped to the clock, grey.  Each sample keeps the normalised shapes of its
        tall glyphs.  Runs of an unchanged ones digit are labelled by the countdown rules (a chain of ~1 s runs that ends
        in the 0:00 hold or the 2:20 pause hold is counted back from 0:01; one that starts after the 2:20 or 0:20 hold is
        counted forward; the tens/minutes changes must fall where the labels say), and the labelled glyphs train the
        0-9 templates.  Once all ten digits have a template every sample is read by NCC.
    Episodes:  clock transitions (a new value held >= 0.4 s) are grouped into countdown episodes; when an episode ends
        (TELEOP reaches 0:00, the clock resets, or it is gone too long) the research model is fitted:
        t = a_phase + rate * elapsed_displayed_seconds, giving AUTO start/end, TELEOP start and match end in stream
        seconds, checked against the research bands (rate 0.97-1.06, pause 3.0-5.5 s, rms <= 0.1 s, >= 5 AUTO and >= 30
        TELEOP transitions, no jump of > 2 s).  When AUTO was not on screen, AUTO is inferred 20 s + 4 s before TELEOP.
    Relocation:  if the clock is missing for `rescan_after_s` (10 s inside a countdown episode), each chunk is also
        searched (keyframes) for a clock elsewhere: a known place (the other overlay layout) or a new one (moved or
        zoomed overlay); after `relocate_after_s` the reader drops back to SEARCH (cheap).  Each clock place keeps its
        own digit templates; a new place borrows the previous place's until it has learned its own (normalised glyphs
        do not depend on size, so a zoomed overlay reads at once).
    Team numbers (second priority):  once per episode, while TELEOP is on screen, the chunk's keyframes are decoded in
        colour around the clock box; white digit groups on the dark red / dark blue panels are read with the clock's
        templates (2x upsampled, two ink thresholds) and voted across frames.  See `teams()` for how far to trust them.

Dependencies: Python 3.11+, numpy, opencv-python-headless, and ffmpeg/ffprobe as subprocesses (streamsplitter.ffmpeg).
"""

from __future__ import annotations

import json
import logging
import math
import re
import subprocess
import threading
import time
from collections import deque

import cv2
import numpy as np

from streamsplitter import ffmpeg as _ffmpeg

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------------------------------------
# Constants (research values unless noted)
# --------------------------------------------------------------------------------------------------------------------

GW, GH = 18, 26            # normalised glyph size (width, height)
RUN_NCC = 0.90             # NCC below which a clock glyph has changed
SOLID_MIN_BOX = 400        # a component this large (w*h, px) and >= 80 % filled is a solid blob, not a glyph
SOLID_MIN_BOX_LARGE = 900  # the same rule for an enlarged (~1.33x zoomed) overlay, whose colon dots are larger
TRIM_FILL = 0.5            # trimmed white boxes: edge rows/columns less than half white are peeled off
WHITE_THR = 200            # min(B, G, R) of the clock's white box
DARK_THR = 110             # grey level below which a pixel is clock ink

SEASON_2026 = {"auto_s": 20, "teleop_s": 140, "auto_to_teleop_pause_s": 4.0}

# model checks (research bands, overlay_reader.py "STAGED 2026-09-24"); the rate is video seconds per displayed second
CHECK_RATE = (0.97, 1.06)
CHECK_PAUSE_S = (3.0, 5.5)
CHECK_RMS_S = 0.25         # research 0.10 s; D2 2026cagle qm63, a full match, ticks with rms 0.14 s (max 0.35 s) at
                           # 7.3 samples/s.  Replays and aborted attempts fail on structure (AUTO only, rate, reset)
CHECK_MIN_AUTO = 5
CHECK_MIN_TELEOP = 30
DISC_S = 2.0
DISC_SIDE = 3
INFER_AUTO_AGREE_S = 0.5

_RE_SHOWINFO = re.compile(r"\bn:\s*(\d+)\s+pts:\s*(-?\d+)\s+pts_time:([-+0-9.eE]+)")
_RE_BENCH = re.compile(r"bench:\s+utime=([0-9.]+)s\s+stime=([0-9.]+)s")


# --------------------------------------------------------------------------------------------------------------------
# ffmpeg helpers
# --------------------------------------------------------------------------------------------------------------------

def _rate(s) -> float | None:
    try:
        a, b = str(s).split("/")
        return float(a) / float(b) if float(b) else None
    except (ValueError, ZeroDivisionError):
        return None


def probe(path, ffprobe_bin: str | None = None) -> dict | None:
    """Video start time (container pts of the first video packet), size, frame rate and duration of a chunk."""
    cmd = [ffprobe_bin or _ffmpeg.find("ffprobe"), "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=start_time,width,height,avg_frame_rate,r_frame_rate:format=duration,start_time",
           "-of", "json", str(path)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        j = json.loads(r.stdout or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as e:
        log.warning("ffprobe failed on %s: %s", path, e)
        return None
    st = j.get("streams") or []
    if not st or not st[0].get("width"):
        return None
    s, f = st[0], j.get("format") or {}
    start = s.get("start_time", f.get("start_time"))
    return {"start": float(start) if start not in (None, "N/A") else 0.0,
            "w": int(s["width"]), "h": int(s["height"]),
            "fps": _rate(s.get("avg_frame_rate")) or _rate(s.get("r_frame_rate")),
            "duration": float(f.get("duration") or 0.0)}


def decode(path, *, skip: str | None, crop=None, pix_fmt: str = "gray", min_spacing: float | None = None,
           threads: int = 1, ffmpeg_bin: str | None = None, size=None, on_frame=None):
    """Decode a chunk with ffmpeg into raw frames plus their pts (seconds, the container's own clock).

    skip: None (every frame), "noref" (reference frames only) or "nokey" (keyframes only).
    crop: [x0, y0, x1, y1] (even x0/y0 recommended); size: (w, h) of the uncropped video (needed when crop is None).
    min_spacing: keep a frame only if it is at least this many seconds after the last kept one.
    on_frame(k, frame): called for each frame as it arrives (then frames are not kept); otherwise they are returned.
    Returns (pts list, frames array or None, {"ffmpeg_cpu_s", "n"}).
    """
    vf = []
    if min_spacing:
        vf.append(f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{min_spacing:.4f})'")
    if crop is not None:
        x0, y0, x1, y1 = (int(v) for v in crop)
        vf.append(f"crop={x1 - x0}:{y1 - y0}:{x0}:{y0}")
        w, h = x1 - x0, y1 - y0
    else:
        w, h = size
    vf.append(f"format={pix_fmt}")
    vf.append("showinfo=checksum=0")
    ch = 1 if pix_fmt == "gray" else 3
    cmd = [ffmpeg_bin or _ffmpeg.find("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "info", "-benchmark",
           "-threads", str(int(threads))]
    if skip:
        cmd += ["-skip_frame", skip]
    cmd += ["-copyts", "-i", str(path), "-map", "0:v:0", "-an", "-sn", "-dn", "-filter_threads", "1",
            "-vf", ",".join(vf), "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", pix_fmt, "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    err_lines: list[str] = []

    def _drain():
        for line in iter(proc.stderr.readline, b""):
            err_lines.append(line.decode("utf-8", "replace"))

    th = threading.Thread(target=_drain, daemon=True)
    th.start()
    fb = w * h * ch
    frames = []
    k = 0
    while True:
        buf = proc.stdout.read(fb)
        if not buf or len(buf) < fb:
            break
        fr = np.frombuffer(buf, np.uint8).reshape((h, w) if ch == 1 else (h, w, 3))
        if on_frame is not None:
            on_frame(k, fr)
        else:
            frames.append(fr)
        k += 1
    proc.stdout.close()
    proc.wait()
    th.join(timeout=10)
    pts = []
    cpu = 0.0
    for line in err_lines:
        m = _RE_SHOWINFO.search(line)
        if m:
            pts.append(float(m.group(3)))
            continue
        m = _RE_BENCH.search(line)
        if m:
            cpu = float(m.group(1)) + float(m.group(2))
    if proc.returncode != 0 and k == 0:
        tail = "".join(err_lines[-5:]).strip()
        log.warning("ffmpeg failed on %s (exit %s): %s", path, proc.returncode, tail[-400:])
    if len(pts) != k:
        log.warning("ffmpeg on %s: %d frames but %d showinfo lines; truncating", path, k, len(pts))
        n = min(len(pts), k)
        pts, k = pts[:n], n
        if on_frame is None:
            frames = frames[:n]
    arr = None
    if on_frame is None:
        arr = np.stack(frames) if frames else np.zeros((0, h, w) if ch == 1 else (0, h, w, 3), np.uint8)
    return pts, arr, {"ffmpeg_cpu_s": cpu, "n": k}


# --------------------------------------------------------------------------------------------------------------------
# 3. Glyphs, lines, templates (research section 3)
# --------------------------------------------------------------------------------------------------------------------

def ink_dark(grey: np.ndarray, thr: int = DARK_THR) -> np.ndarray:
    return (grey < thr).astype(np.uint8)


def ink_bright(bgr: np.ndarray, thr: int = 170) -> np.ndarray:
    return (bgr.min(axis=2) >= thr).astype(np.uint8)


def extract_glyphs(ink: np.ndarray, min_h: int = 5, solid_min_box: int = SOLID_MIN_BOX) -> list[dict]:
    """Connected components of the ink; solid blobs (icons, boxes) are dropped, dots are kept."""
    num, lab, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    gl = []
    for i in range(1, num):
        x, y, w, h, a = (int(v) for v in stats[i])
        if a < 6 or (h < min_h and w < min_h):
            continue
        fill = a / float(w * h)
        if fill >= 0.8 and w * h >= solid_min_box:
            continue
        gl.append({"x0": x, "y0": y, "x1": x + w, "y1": y + h, "w": w, "h": h, "area": a, "fill": fill,
                   "mask": (lab[y:y + h, x:x + w] == i).astype(np.uint8)})
    gl.sort(key=lambda g: g["x0"])
    return gl


def group_lines(glyphs: list[dict], gap_factor: float = 1.6) -> list[list[dict]]:
    """Group glyphs into text lines by vertical overlap, sort by x, split at wide horizontal gaps."""
    lines: list[list[dict]] = []
    for g in sorted(glyphs, key=lambda g: -g["h"]):
        best = None
        for ln in lines:
            y0 = min(x["y0"] for x in ln)
            y1 = max(x["y1"] for x in ln)
            ov = min(y1, g["y1"]) - max(y0, g["y0"])
            if ov > 0.5 * min(g["h"], y1 - y0):
                best = ln
                break
        if best is None:
            lines.append([g])
        else:
            best.append(g)
    out = []
    for ln in lines:
        ln.sort(key=lambda g: g["x0"])
        lh = max(g["h"] for g in ln)
        cur = [ln[0]]
        for g in ln[1:]:
            if g["x0"] - max(x["x1"] for x in cur) > max(gap_factor * lh, 12):
                out.append(cur)
                cur = [g]
            else:
                cur.append(g)
        out.append(cur)
    return out


def _line_bbox(ln):
    return (min(g["x0"] for g in ln), min(g["y0"] for g in ln), max(g["x1"] for g in ln), max(g["y1"] for g in ln))


def norm_glyph(mask: np.ndarray):
    v = cv2.resize(mask.astype(np.float32), (GW, GH), interpolation=cv2.INTER_AREA).ravel()
    v -= v.mean()
    return v / (np.linalg.norm(v) + 1e-6), mask.shape[1] / max(mask.shape[0], 1)


def glyph_tilt(mask: np.ndarray) -> float:
    """Slope (columns per row) of the ink column-centroid against the row index; a slash is about -0.35."""
    ys, xs = np.nonzero(mask)
    if len(ys) < 10:
        return 0.0
    rows = np.unique(ys)
    if len(rows) < 4:
        return 0.0
    cent = np.array([xs[ys == r].mean() for r in rows])
    return float(np.polyfit(rows, cent, 1)[0])


def _ncc(a, b) -> float:
    return float(np.dot(np.asarray(a, np.float32), np.asarray(b, np.float32)))


class DigitTemplates:
    """0-9 glyph templates accumulated from countdown-labelled clock runs (weighted mean of normalised glyphs)."""

    def __init__(self):
        self.sum = np.zeros((10, GW * GH), np.float64)
        self.asp = np.zeros(10, np.float64)
        self.weight = np.zeros(10, np.float64)
        self.runs = np.zeros(10, np.int64)
        self.glyph_h = None             # clock digit height the templates were learned at
        self.V = np.zeros((0, GW * GH), np.float32)
        self.A = np.zeros(0, np.float32)
        self.labels: list[int] = []

    @property
    def ready(self) -> bool:
        return bool((self.weight > 0).all())

    def add(self, digit: int, vec, aspect: float, n: int):
        self.sum[digit] += np.asarray(vec, np.float64) * n
        self.asp[digit] += float(aspect) * n
        self.weight[digit] += n
        self.runs[digit] += 1

    def finalize(self):
        have = np.nonzero(self.weight > 0)[0]
        self.labels = [int(d) for d in have]
        if len(have) == 0:
            self.V = np.zeros((0, GW * GH), np.float32)
            self.A = np.zeros(0, np.float32)
            return
        V = self.sum[have] / self.weight[have, None]
        V /= np.linalg.norm(V, axis=1, keepdims=True) + 1e-9
        self.V = V.astype(np.float32)
        self.A = (self.asp[have] / self.weight[have]).astype(np.float32)

    def match(self, vec, aspect: float):
        """(label, ncc, margin): best template by NCC minus an aspect penalty; margin to the best other label."""
        if not self.labels:
            return None, 0.0, 0.0
        s = self.V @ np.asarray(vec, np.float32) - 0.8 * np.abs(np.log(max(aspect, 1e-3) / self.A))
        i = int(np.argmax(s))
        if len(s) > 1:
            s2 = s.copy()
            s2[i] = -1e9
            margin = float(s[i] - s2.max())
        else:
            margin = 1.0
        return self.labels[i], float(self.V[i] @ np.asarray(vec, np.float32)), margin

    def reset(self):
        self.__init__()

    def to_json(self) -> dict:
        return {"ready": self.ready, "runs_per_digit": [int(x) for x in self.runs],
                "samples_per_digit": [int(x) for x in self.weight], "glyph_h": self.glyph_h}


def classify_glyph(g: dict, line_h: float, templates: DigitTemplates | None):
    """'dot', 'slash', a digit label (str) or 'junk', with a score (research classify_glyph)."""
    w, h = g["w"], g["h"]
    if h < 0.45 * line_h and w < 0.6 * line_h:
        return "dot", 1.0
    if w <= 0.7 * h and h >= 0.6 * line_h and g.get("fill", 1.0) <= 0.45 and abs(glyph_tilt(g["mask"])) >= 0.22:
        top = g["mask"][:max(1, int(0.2 * h))]
        if top.any(axis=0).mean() < 0.6:      # a "7" covers its full width at the top, a slash does not
            return "slash", 1.0
    if templates is None or not templates.labels:
        return "digit?", 0.0
    vec, ar = norm_glyph(g["mask"])
    label, score, margin = templates.match(vec, ar)
    if label is None or score < 0.55:
        return "junk", score
    return str(label), min(score, 0.5 + margin)


# --------------------------------------------------------------------------------------------------------------------
# 3a. Clock location (research section 3a, adapted to per-frame white boxes over a sliding window of keyframes)
# --------------------------------------------------------------------------------------------------------------------

def _trim_box(comp: np.ndarray, fill: float = TRIM_FILL):
    """Peel the edge rows/columns of a white component's box that are less than `fill` white (its own pixels; `comp`
    is the component's mask inside its bounding box).  A clock box whose white component leaks into a bright picture
    (necmp1 qm18/qm52) gets its own edge back.  Returns the trimmed box in `comp` coordinates."""
    x0, y0, y1, x1 = 0, 0, comp.shape[0], comp.shape[1]
    for _ in range(2):
        sub = comp[y0:y1, x0:x1]
        if sub.size == 0:
            break
        rows = sub.mean(axis=1)
        while y1 - y0 > 1 and rows[0] < fill:
            y0 += 1
            rows = rows[1:]
        while y1 - y0 > 1 and rows[-1] < fill:
            y1 -= 1
            rows = rows[:-1]
        cols = comp[y0:y1, x0:x1].mean(axis=0)
        while x1 - x0 > 1 and cols[0] < fill:
            x0 += 1
            cols = cols[1:]
        while x1 - x0 > 1 and cols[-1] < fill:
            x1 -= 1
            cols = cols[:-1]
    return int(x0), int(y0), int(x1), int(y1)


def white_box_candidates(bgr: np.ndarray) -> list[dict]:
    """White boxes of one frame in the top or bottom 35 % of the picture: [{'box', 'tbox', 'crop'}] (crop is grey)."""
    H, W = bgr.shape[:2]
    white = (bgr.min(axis=2) >= WHITE_THR).astype(np.uint8)
    white = cv2.morphologyEx(white, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    num, lab, stats, _ = cv2.connectedComponentsWithStats(white, connectivity=8)
    out = []
    grey = None
    for i in range(1, num):
        x, y, w, h, a = (int(v) for v in stats[i])
        if a < 1200 or h < 24 or w < 50 or w > 0.6 * W or h > 0.45 * H:
            continue
        cy = y + 0.5 * h
        if not (cy < 0.35 * H or cy > 0.65 * H):
            continue
        if grey is None:
            grey = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        a0, b0, a1, b1 = _trim_box((lab[y:y + h, x:x + w] == i).astype(np.float32))
        out.append({"box": (x, y, x + w, y + h), "tbox": (x + a0, y + b0, x + a1, y + b1),
                    "crop": grey[y:y + h, x:x + w].copy()})
    return out


def _clock_line(ink: np.ndarray, solid_min_box: int = SOLID_MIN_BOX):
    """The tall glyphs of a m:ss / mm:ss line in a clock crop, or None."""
    glyphs = extract_glyphs(ink, solid_min_box=solid_min_box)
    if not glyphs:
        return None
    for ln in sorted(group_lines(glyphs), key=lambda ln: -max(g["h"] for g in ln)):
        lh = max(g["h"] for g in ln)
        tall = [g for g in ln if g["h"] >= 0.6 * lh]
        small = [g for g in ln if g["h"] < 0.45 * lh and g["w"] < 0.6 * lh]
        if len(tall) in (3, 4) and small:
            return tall
    return None


def rois_agree(roi, other, max_centre_px: float = 40.0) -> bool:
    """Same overlay element: centres within `max_centre_px` and each side within a factor of 2 (research)."""
    cx, cy = (roi[0] + roi[2]) / 2.0, (roi[1] + roi[3]) / 2.0
    hx, hy = (other[0] + other[2]) / 2.0, (other[1] + other[3]) / 2.0
    if math.hypot(cx - hx, cy - hy) > max_centre_px:
        return False
    for a, b in ((roi[2] - roi[0], other[2] - other[0]), (roi[3] - roi[1], other[3] - other[1])):
        if b <= 0 or not 0.5 <= a / b <= 2.0:
            return False
    return True


def _iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def _line_shape(tall: list[dict]) -> np.ndarray:
    """Concatenated normalised shapes of the last two tall glyphs (tens and ones of the seconds)."""
    return np.concatenate([norm_glyph(g["mask"])[0] for g in tall[-2:]]) / math.sqrt(2.0)


def _evaluate_box(items: list[tuple[float, np.ndarray, tuple]], solid_min_box: int, min_n: int, min_distinct: int,
                  min_frac: float = 0.5):
    """items: (t, grey crop, crop origin+box) for one candidate box over the window.  Returns the clock candidate
    (research locate_clock rules: a m:ss line in most frames, many different shapes, changing between frames)."""
    groups: list[dict] = []
    for t, crop, (ox, oy, bx0, by0, bx1, by1) in items:
        tall = _clock_line(ink_dark(crop), solid_min_box)
        if tall is None:
            continue
        lh = max(g["h"] for g in tall)
        lx0, ly0, lx1, ly1 = _line_bbox(tall)
        yc = oy + 0.5 * (ly0 + ly1)
        shape = _line_shape(tall)
        for gr in groups:
            if abs(gr["yc"] - yc) < 0.5 * lh:
                break
        else:
            gr = {"yc": yc, "n": 0, "x0": 1e9, "y0": 1e9, "x1": -1, "y1": -1, "hs": [], "shapes": [], "changes": 0,
                  "last": None, "ng": [], "boxes": [], "t": []}
            groups.append(gr)
        gr["boxes"].append((bx0, by0, bx1, by1))
        gr["n"] += 1
        gr["t"].append(t)
        gr["x0"] = min(gr["x0"], ox + lx0)
        gr["y0"] = min(gr["y0"], oy + ly0)
        gr["x1"] = max(gr["x1"], ox + lx1)
        gr["y1"] = max(gr["y1"], oy + ly1)
        gr["hs"].append(lh)
        gr["ng"].append(len(tall))
        if gr["last"] is not None and _ncc(gr["last"], shape) < RUN_NCC:
            gr["changes"] += 1
        gr["last"] = shape
        if all(_ncc(shape, u) < RUN_NCC for u in gr["shapes"]):
            gr["shapes"].append(shape)
    best = None
    n_items = len(items)
    for gr in groups:
        if gr["n"] < min_n or gr["n"] < min_frac * n_items or len(gr["shapes"]) < min_distinct:
            continue
        if gr["changes"] < 0.5 * (gr["n"] - 1):
            continue
        gh = float(np.median(gr["hs"]))
        pad = int(round(0.25 * gh))
        bx0, by0, bx1, by1 = (int(round(v)) for v in np.median(np.array(gr["boxes"], float), axis=0))
        roi = [max(bx0, int(gr["x0"]) - pad), max(by0, int(gr["y0"]) - pad),
               min(bx1, int(gr["x1"]) + pad), min(by1, int(gr["y1"]) + pad)]
        cand = {"roi": roi, "box": [bx0, by0, bx1, by1], "glyph_h": gh, "n": gr["n"], "frames": n_items,
                "distinct": len(gr["shapes"]), "changes": gr["changes"], "solid_min_box": solid_min_box,
                "first_t": min(gr["t"]), "last_t": max(gr["t"])}
        if best is None or (cand["n"], cand["glyph_h"]) > (best["n"], best["glyph_h"]):
            best = cand
    return best


GLYPH_H_FRAC = (0.022, 0.10)   # clock digit height / picture height: compact 0.030, wide 0.060, wide zoomed 1.33x 0.080
                               # (D1 2026cancmp: a 138 px 'clock' in the lower camera view, a 16 px one on an 'Up next'
                               # card; both changing white boxes with digit-like ink, both outside this band)


def _read_items(items, solid_min_box: int, templates: DigitTemplates):
    """[(t, value)] for the window crops whose clock line reads as m:ss with every digit scoring >= 0.55."""
    out = []
    for t, crop, _ in items:
        tall = _clock_line(ink_dark(crop), solid_min_box)
        if tall is None:
            continue
        digits = []
        for g in tall:
            vec, ar = norm_glyph(g["mask"])
            lab, score, _ = templates.match(vec, ar)
            if lab is None or score < 0.55:
                digits = None
                break
            digits.append(lab)
        if not digits or digits[-2] > 5:
            continue
        m = digits[0] if len(digits) == 3 else 10 * digits[0] + digits[1]
        out.append((t, 60 * m + 10 * digits[-2] + digits[-1]))
    return out


def countdown_consistency(reads: list[tuple[float, int]]) -> tuple[int, int]:
    """(consistent, pairs): consecutive reads (<= 15 s apart, different values) whose value drop equals the time
    between them within 2 s, as a running match clock's does."""
    reads = sorted(reads)
    good = pairs = 0
    for (ta, va), (tb, vb) in zip(reads, reads[1:]):
        if tb - ta > 15.0 or va == vb:
            continue
        pairs += 1
        good += abs((va - vb) - (tb - ta)) <= 2.0
    return good, pairs


def locate_clock(window: list[dict], exclude: list | None = None, templates: DigitTemplates | None = None,
                 frame_h: int | None = None) -> dict | None:
    """Clock location over a window of search frames ({'t', 'cands'}).  Candidate boxes are clustered across frames;
    each cluster is tried untrimmed then trimmed, with the normal glyph rule then the enlarged-overlay rule.  The
    digit height must be plausible for a scoreboard clock (GLYPH_H_FRAC).  With ready templates (from a clock seen
    earlier) a candidate's reads must also count down at about one displayed second per second; a candidate the
    templates cannot read at all (a new overlay font) needs twice the usual evidence."""
    clusters: list[dict] = []
    for fr in window:
        for c in fr["cands"]:
            for cl in clusters:
                if _iou(cl["box"], c["box"]) >= 0.5:
                    cl["items"].append((fr["t"], c))
                    break
            else:
                clusters.append({"box": c["box"], "items": [(fr["t"], c)]})
    best = None
    for rule in (SOLID_MIN_BOX, SOLID_MIN_BOX_LARGE):
        for trim in (False, True):
            for cl in clusters:
                if len(cl["items"]) < 5:
                    continue
                items = []
                for t, c in cl["items"]:
                    x0, y0, x1, y1 = c["box"]
                    if trim:
                        tx0, ty0, tx1, ty1 = c["tbox"]
                        crop = c["crop"][ty0 - y0:ty1 - y0, tx0 - x0:tx1 - x0]
                        items.append((t, crop, (tx0, ty0, tx0, ty0, tx1, ty1)))
                    else:
                        items.append((t, c["crop"], (x0, y0, x0, y0, x1, y1)))
                cand = _evaluate_box(items, rule, min_n=5, min_distinct=4)
                if cand is None:
                    continue
                if exclude and any(rois_agree(cand["roi"], r) for r in exclude):
                    continue
                if frame_h and not GLYPH_H_FRAC[0] * frame_h <= cand["glyph_h"] <= GLYPH_H_FRAC[1] * frame_h:
                    continue
                if templates is not None and templates.ready:
                    reads = _read_items(items, rule, templates)
                    good, pairs = countdown_consistency(reads)
                    cand["verify"] = {"reads": len(reads), "consistent_pairs": good, "pairs": pairs}
                    if len(reads) >= 3:
                        if pairs < 2 or good < 0.6 * pairs:
                            continue            # readable, but not a running match clock
                    elif cand["n"] < 10 or cand["distinct"] < 6:
                        continue                # unreadable with the known digits: needs more evidence
                cand["trimmed"] = trim
                if best is None or (cand["n"], cand["glyph_h"]) > (best["n"], best["glyph_h"]):
                    best = cand
            if best is not None:
                return best
    return None


# --------------------------------------------------------------------------------------------------------------------
# 4. Runs, countdown labelling, templates (research section 4, in seconds instead of frames)
# --------------------------------------------------------------------------------------------------------------------

def build_runs(t: np.ndarray, ng: np.ndarray, vecs: np.ndarray, asp: np.ndarray, dt: float,
               max_gap: float = 0.35) -> list[dict]:
    """Maximal runs with the same number of glyphs and an unchanged ones digit (NCC >= RUN_NCC).  Unlike the research
    reader (every frame), samples without a clock line do not end a run unless the gap exceeds `max_gap` seconds."""
    runs: list[dict] = []
    cur = None
    for k in range(len(t)):
        g = int(ng[k])
        if g < 3:
            if cur is not None and t[k] - cur["tl"] > max_gap:
                cur = None
            continue
        v1 = vecs[k, g - 1].astype(np.float32)
        if (cur is not None and cur["ng"] == g and t[k] - cur["tl"] <= max_gap
                and _ncc(cur["cent"][g - 1] / (np.linalg.norm(cur["cent"][g - 1]) + 1e-6), v1) >= RUN_NCC):
            cur["tl"] = float(t[k])
            cur["n"] += 1
            cur["cent"][:g] += vecs[k, :g]
            cur["asp"][:g] += asp[k, :g]
        else:
            cur = {"t0": float(t[k]), "tl": float(t[k]), "n": 1, "ng": g,
                   "cent": vecs[k].astype(np.float32).copy(), "asp": asp[k].astype(np.float32).copy()}
            runs.append(cur)
    for r in runs:
        g = r["ng"]
        c = r["cent"][:g] / r["n"]
        r["cent"] = c / (np.linalg.norm(c, axis=1, keepdims=True) + 1e-6)
        r["asp"] = r["asp"][:g] / r["n"]
        r["t1"] = r["tl"] + dt
    return runs


RHYTHM_MIN = 5      # consecutive ~1 s runs of the seconds digit that prove a running clock (challenger test)


def once_a_second(t: np.ndarray, ng: np.ndarray, vecs: np.ndarray, asp: np.ndarray) -> int:
    """Longest chain of back-to-back runs of an unchanged seconds digit that each last about a second (0.75-1.3 s):
    the signature of a running match clock. Clutter that merely changes (people walking past a row of white chairs,
    banner letters) passes the location rules but not this."""
    if len(t) < 5:
        return 0
    dt = float(np.median(np.diff(t)))
    best = cur = 0
    last_tl = None
    for r in build_runs(t, ng, vecs, asp, dt):
        ok = 0.75 <= r["t1"] - r["t0"] <= 1.3
        joined = last_tl is not None and r["t0"] - last_tl <= 0.5
        cur = (cur + 1 if joined else 1) if ok else 0
        best = max(best, cur)
        last_tl = r["tl"]
    return best


def _digits_of(value: int, ng: int) -> list[int]:
    m, s = divmod(int(value), 60)
    if ng == 3:
        return [m, s // 10, s % 10]
    return [m // 10, m % 10, s // 10, s % 10]


def label_countdown(runs: list[dict], auto_s: int, tele_s: int) -> list[dict]:
    """Label the clock runs with seconds remaining (research label_countdown).  Countdown runs last about a second; a
    chain of them that ends in a hold (0:00, or the 2:20 pause hold after AUTO) is counted back from 0:01; a chain that
    starts after a hold whose minutes glyph equals its tens glyph (2:20) or its ones glyph (0:20) is counted forward.
    The tens and minutes glyph changes must then fall exactly where the labels say, else the chain is not trusted.
    Sets r['value'], r['kind'], r['chain_len']; returns the chain report."""
    for r in runs:
        r["dur"] = r["t1"] - r["t0"]
        r["value"] = None
        r["chain_len"] = 0
        r["kind"] = "countdown" if 0.6 <= r["dur"] <= 1.4 else ("hold" if r["dur"] > 1.4 else "glitch")
    chains: list[list[int]] = []
    cur: list[int] = []
    for i, r in enumerate(runs):
        if r["kind"] != "countdown":
            if r["kind"] == "hold" and cur:
                chains.append(cur)
                cur = []
            continue
        if cur:
            prev = runs[cur[-1]]
            step = r["t0"] - prev["t0"]
            if r["t0"] - prev["t1"] <= 0.35 and 0.75 <= step <= 1.45:
                cur.append(i)
                continue
            chains.append(cur)
        cur = [i]
    if cur:
        chains.append(cur)

    def same(a, b) -> bool:
        return _ncc(a, b) >= RUN_NCC

    report = []
    for ch in chains:
        first, last = runs[ch[0]], runs[ch[-1]]
        nxt = next((runs[j] for j in range(ch[-1] + 1, len(runs)) if runs[j]["kind"] == "hold"
                    and runs[j]["t0"] - last["t1"] <= 0.35), None)
        prv = next((runs[j] for j in range(ch[0] - 1, -1, -1) if runs[j]["kind"] == "hold"
                    and first["t0"] - runs[j]["t1"] <= 0.35), None)
        mode = None
        holds = []                              # hold runs labelled by this chain (reset with it)
        if nxt is not None and nxt["ng"] >= 3:
            if same(nxt["cent"][-1], nxt["cent"][-2]) and same(nxt["cent"][-1], nxt["cent"][-3]):
                mode, nxt["value"] = "backward_from_0:00", 0
            elif same(nxt["cent"][-3], nxt["cent"][-2]):
                mode, nxt["value"] = "backward_from_pause_hold", tele_s
            if mode:
                holds.append(nxt)
                for k, i in enumerate(reversed(ch)):
                    runs[i]["value"] = k + 1
                if prv is not None and prv["ng"] >= 3 and prv["value"] is None:
                    if same(prv["cent"][-3], prv["cent"][-2]):
                        prv["value"] = tele_s
                        holds.append(prv)
                    elif same(prv["cent"][-3], prv["cent"][-1]):
                        prv["value"] = auto_s
                        holds.append(prv)
        if mode is None and prv is not None and prv["ng"] >= 3:
            if same(prv["cent"][-3], prv["cent"][-2]):
                start, mode = tele_s, "forward_from_teleop_start"
            elif same(prv["cent"][-3], prv["cent"][-1]):
                start, mode = auto_s, "forward_from_auto_start"
            if mode:
                prv["value"] = start
                holds.append(prv)
                for k, i in enumerate(ch):
                    runs[i]["value"] = start - 1 - k
        mism = 0
        if mode:
            for a, b in zip(ch, ch[1:]):
                ra, rb = runs[a], runs[b]
                if ra["ng"] != rb["ng"] or rb["value"] is None or rb["value"] < 0:
                    continue
                g = ra["ng"]
                tens_changed = not same(ra["cent"][g - 2], rb["cent"][g - 2])
                if tens_changed != (rb["value"] % 10 == 9):
                    mism += 1
                if g == 3:
                    min_changed = not same(ra["cent"][0], rb["cent"][0])
                    if min_changed != (rb["value"] % 60 == 59):
                        mism += 1
            if any(runs[i]["value"] is not None and runs[i]["value"] < 0 for i in ch):
                mism += 1                       # counted forward past 0:00: not this chain's anchor
            if mism:
                for i in ch:
                    runs[i]["value"] = None
                for hr in holds:
                    if hr["chain_len"] == 0:     # not also vouched for by another chain
                        hr["value"] = None
            else:
                for i in ch:
                    runs[i]["chain_len"] = len(ch)
                for hr in holds:
                    hr["chain_len"] = max(hr["chain_len"], len(ch))
        report.append({"runs": len(ch), "t0": first["t0"], "t1": last["t1"], "mode": mode,
                       "first_value": runs[ch[0]]["value"], "last_value": runs[ch[-1]]["value"], "mismatches": mism})
    return report


# --------------------------------------------------------------------------------------------------------------------
# 5. Clock transitions and the match-time model (research section 5, in stream seconds)
# --------------------------------------------------------------------------------------------------------------------

def _model_rows(trans: list[dict], auto_s: int, tele_s: int) -> list[tuple[float, float, int]]:
    """(t, elapsed seconds in the period, phase 0 = AUTO / 1 = TELEOP), by the research rule."""
    rows = []
    phase = "auto"
    for tr in trans:
        v, p = tr["value"], tr["prev"]
        if phase == "auto" and v > p and v >= tele_s - 1 and 0 <= p <= 1:
            phase = "teleop"
            continue
        if p != v + 1 or tr.get("gap", 0.0) > 0.5:
            continue                        # not a countdown step, or its time is unknown (no samples before it)
        if phase == "auto" and v < auto_s:
            rows.append((tr["t"], float(auto_s - v), 0))
        elif phase == "teleop" and v < tele_s:
            rows.append((tr["t"], float(tele_s - v), 1))
    return rows


def find_discontinuities(rows: list[tuple]) -> list[dict]:
    """Steps of more than DISC_S in (t - elapsed) between neighbouring rows of one period that hold for DISC_SIDE rows
    on both sides: a jump in the video (necmp1 qm11 skips ~10 s of match; a cut to another field)."""
    out = []
    for ph in (0, 1):
        r = sorted((t, e) for t, e, p in rows if p == ph)
        off = np.array([t - e for t, e in r], float)
        for k in range(DISC_SIDE, len(r) - DISC_SIDE + 1):
            if abs(off[k] - off[k - 1]) <= DISC_S:
                continue
            before = float(np.median(off[k - DISC_SIDE:k]))
            after = float(np.median(off[k:k + DISC_SIDE]))
            if abs(after - before) > DISC_S:
                out.append({"phase": "auto" if ph == 0 else "teleop", "t_before": round(r[k - 1][0], 3),
                            "t_after": round(r[k][0], 3), "display_skip_s": round(before - after, 2)})
    return out


def _robust_lstsq(A: np.ndarray, y: np.ndarray, floor_s: float):
    used = np.ones(len(y), bool)
    sol = res_all = None
    for _ in range(8):
        sol, *_ = np.linalg.lstsq(A[used], y[used], rcond=None)
        res_all = y - A @ sol
        sig = 1.4826 * np.median(np.abs(res_all[used] - np.median(res_all[used]))) + 1e-9
        new_used = np.abs(res_all) <= max(floor_s, 4 * sig)
        if new_used.sum() < A.shape[1] + 2 or (new_used == used).all():
            break
        used = new_used
    return sol, res_all, used


def fit_clock_model(trans: list[dict], season: dict, floor_s: float = 0.15) -> dict:
    """Least-squares t = a_(phase, piece) + rate * elapsed with outlier rejection (research fit_clock_model and
    fit_clock_model_pieces), the research checks, and the TELEOP-only fallback (AUTO inferred) when AUTO was not seen.
    Times are stream seconds; `rate` is video seconds per displayed second (1.0117 = the overlay ticks 1.17 % slow)."""
    auto_s, tele_s = season["auto_s"], season["teleop_s"]
    pause_season = float(season.get("auto_to_teleop_pause_s", 4.0))
    rows = _model_rows(trans, auto_s, tele_s)
    out = {"ok": False, "n_transitions": len(trans), "auto_start": None, "auto_end": None, "teleop_start": None,
           "match_end": None, "auto_inferred": False}
    has_jump = any(0 <= tr["prev"] <= 1 and tr["value"] >= tele_s - 1 for tr in trans)
    steps = [tr for tr in trans if tr["prev"] == tr["value"] + 1 and tr.get("gap", 0.0) <= 0.5]
    if not has_jump:
        if any(tr["value"] > auto_s for tr in steps):
            # a TELEOP countdown without the AUTO -> TELEOP jump (AUTO off screen, or joined late)
            return _fit_teleop_only(trans, season, floor_s, out)
        # every countdown value <= 0:20: AUTO alone (aborted, or the overlay left) or the last 20 s of TELEOP
        tail = any(tr["value"] == 0 for tr in steps)
        span = float(tele_s if tail else auto_s)
        pts = [(tr["t"], span - tr["value"]) for tr in steps]
        out["mode"] = "teleop_tail_only" if tail else "auto_only"
        if len(pts) >= 5:
            R = np.array(pts, float)
            A = np.stack([R[:, 1], np.ones(len(R))], 1)
            sol, res_all, used = _robust_lstsq(A, R[:, 0], floor_s)
            rate = float(np.clip(sol[0], CHECK_RATE[0], CHECK_RATE[1]))
            a = float(np.median(R[used, 0] - rate * R[used, 1]))
            out["rate"] = round(rate, 5)
            out["rms_s"] = round(float(np.sqrt((res_all[used] ** 2).mean())), 4)
            if tail:
                out["match_end"] = a + rate * tele_s
                out["n_teleop"] = int(used.sum())
            else:
                out["auto_start"], out["auto_end"] = a, a + rate * auto_s
                out["n_auto"] = int(used.sum())
        out["reason"] = (f"only the last {auto_s} s of a countdown that reached 0:00 (TELEOP tail)" if tail
                         else f"a countdown from at most 0:{auto_s:02d} that did not reach TELEOP (AUTO only)")
        out["failures"] = [out["reason"]]
        return out
    if len(rows) < 10:
        out["reason"] = f"only {len(rows)} usable transitions"
        return out
    disc = find_discontinuities(rows)
    cuts = {0: sorted(d["t_after"] for d in disc if d["phase"] == "auto"),
            1: sorted(d["t_after"] for d in disc if d["phase"] == "teleop")}
    R = np.array(rows, float)
    piece = np.array([sum(1 for c in cuts[int(p)] if t >= c - 1e-6) for t, _, p in rows], int)
    keys = sorted({(int(p), int(k)) for (_, _, p), k in zip(rows, piece)})
    cols, names = [R[:, 1]], ["rate"]
    for ph, k in keys:
        cols.append(((R[:, 2] == ph) & (piece == k)).astype(float))
        names.append((ph, k))
    A = np.stack(cols, 1)
    sol, res_all, used = _robust_lstsq(A, R[:, 0], floor_s)
    coef = dict(zip(names, sol))
    rate = float(coef["rate"])
    ak = [k for ph, k in keys if ph == 0]
    tk = [k for ph, k in keys if ph == 1]
    a_auto0 = float(coef[(0, ak[0])]) if ak else None
    a_auto1 = float(coef[(0, ak[-1])]) if ak else None
    a_tele0 = float(coef[(1, tk[0])]) if tk else None
    a_tele1 = float(coef[(1, tk[-1])]) if tk else None
    res = res_all[used]
    n_auto = int(((R[:, 2] == 0) & used).sum())
    n_tele = int(((R[:, 2] == 1) & used).sum())
    out.update({"rate": round(rate, 5), "n_rows": len(rows), "n_used": int(used.sum()),
                "rms_s": round(float(np.sqrt((res ** 2).mean())), 4), "max_res_s": round(float(np.abs(res).max()), 4),
                "n_auto": n_auto, "n_teleop": n_tele, "discontinuities": disc,
                "auto_start": a_auto0, "auto_end": a_auto1 + rate * auto_s if a_auto1 is not None else None,
                "teleop_start": a_tele0, "match_end": a_tele1 + rate * tele_s if a_tele1 is not None else None})
    if a_auto1 is not None and a_tele0 is not None:
        out["pause_s"] = round((a_tele0 - a_auto1) / rate - auto_s, 3)
    if a_auto0 is None and a_tele0 is not None:
        _infer_auto(out, rate, auto_s, pause_season)
    _check(out, season)
    if not out["ok"] and n_auto < CHECK_MIN_AUTO and a_tele0 is not None:
        alt = _fit_teleop_only(trans, season, floor_s, dict(out))
        if alt.get("ok"):
            return alt
    return out


def _infer_auto(out: dict, rate: float, auto_s: int, pause: float):
    out["auto_start"] = out["teleop_start"] - rate * (auto_s + pause)
    out["auto_end"] = out["auto_start"] + rate * auto_s
    out["pause_s"] = pause
    out["auto_inferred"] = True


def _fit_teleop_only(trans: list[dict], season: dict, floor_s: float, out: dict) -> dict:
    """Research fit_clock_model_teleop_only: TELEOP start and match end from the TELEOP countdown, AUTO placed the
    season's AUTO length plus pause before TELEOP start."""
    auto_s, tele_s = season["auto_s"], season["teleop_s"]
    pause = float(season.get("auto_to_teleop_pause_s", 4.0))
    k0 = next((k for k, tr in enumerate(trans) if tr["value"] > auto_s), None)
    if k0 is None:
        # a countdown that never showed more than AUTO's 0:20: an AUTO-only fragment, or TELEOP's last 20 s
        rows = [(tr["t"], float(auto_s - tr["value"])) for tr in trans
                if tr["prev"] == tr["value"] + 1 and tr["value"] < auto_s]
        out["reason"] = f"no displayed value above {auto_s} s; {len(rows)} countdown transitions (ambiguous period)"
        out["ok"] = False
        return out
    auto_rows = [(tr["t"], float(auto_s - tr["value"])) for tr in trans[:k0]
                 if tr["prev"] == tr["value"] + 1 and tr["value"] < auto_s and tr.get("gap", 0.0) <= 0.5]
    rows = [(tr["t"], float(tele_s - tr["value"])) for tr in trans[k0:]
            if tr["prev"] == tr["value"] + 1 and tr["value"] < tele_s and tr.get("gap", 0.0) <= 0.5]
    if len(rows) < 10:
        out["reason"] = f"teleop-only: only {len(rows)} TELEOP rows"
        out["ok"] = False
        return out
    R = np.array(rows, float)
    disc = find_discontinuities([(t, e, 1) for t, e in rows])
    A = np.stack([R[:, 1], np.ones(len(R))], 1)
    sol, res_all, used = _robust_lstsq(A, R[:, 0], floor_s)
    rate, a_tele = float(sol[0]), float(sol[1])
    res = res_all[used]
    out.update({"rate": round(rate, 5), "n_rows": len(rows), "n_used": int(used.sum()),
                "rms_s": round(float(np.sqrt((res ** 2).mean())), 4), "max_res_s": round(float(np.abs(res).max()), 4),
                "n_auto": 0, "n_teleop": int(used.sum()), "discontinuities": disc,
                "teleop_start": a_tele, "match_end": a_tele + rate * tele_s})
    _infer_auto(out, rate, auto_s, pause)
    bad_auto = [t for t, e in auto_rows if abs(t - (out["auto_start"] + rate * e)) > INFER_AUTO_AGREE_S]
    out["observed_auto_rows"] = len(auto_rows)
    out["mode"] = "teleop_only_auto_inferred"
    fails = []
    if not CHECK_RATE[0] <= rate <= CHECK_RATE[1]:
        fails.append(f"clock rate {rate:.4f} outside {list(CHECK_RATE)}")
    if out["rms_s"] > CHECK_RMS_S:
        fails.append(f"fit residual rms {out['rms_s']:.3f} s > {CHECK_RMS_S}")
    if out["n_teleop"] < CHECK_MIN_TELEOP:
        fails.append(f"{out['n_teleop']} TELEOP transitions used < {CHECK_MIN_TELEOP}")
    n_above = sum(1 for (t, e), u in zip(rows, used) if u and tele_s - e > auto_s)
    if n_above < CHECK_MIN_TELEOP:
        fails.append(f"{n_above} used TELEOP transitions above {auto_s} s < {CHECK_MIN_TELEOP}")
    if bad_auto:
        fails.append(f"{len(bad_auto)} observed AUTO transitions disagree with the inferred AUTO")
    if disc:
        fails.append("video discontinuity: " + "; ".join(f"{d['display_skip_s']:+.1f} s at {d['t_after']}" for d in disc))
    out["failures"] = fails
    out["ok"] = not fails
    return out


def _check(model: dict, season: dict):
    """The research consistency checks (bands from 79 recorded casnv / necmp1 products)."""
    f = []
    names = ("auto_start", "auto_end", "teleop_start", "match_end")
    vals = [model.get(k) for k in names]
    missing = [k for k, v in zip(names, vals) if v is None]
    if missing:
        f.append("anchors missing: " + ", ".join(missing))
    elif not (vals[0] < vals[1] <= vals[2] < vals[3]):
        f.append("anchors out of order")
    rate = model.get("rate")
    if rate is None or not CHECK_RATE[0] <= rate <= CHECK_RATE[1]:
        f.append(f"clock rate {rate} outside {list(CHECK_RATE)}")
    pause = model.get("pause_s")
    if not model.get("auto_inferred") and (pause is None or not CHECK_PAUSE_S[0] - 0.034 <= pause <= CHECK_PAUSE_S[1]):
        f.append(f"AUTO -> TELEOP pause {pause} s outside {list(CHECK_PAUSE_S)}")
    if model.get("rms_s", 1.0) > CHECK_RMS_S:
        f.append(f"fit residual rms {model.get('rms_s')} s > {CHECK_RMS_S}")
    if not model.get("auto_inferred") and model.get("n_auto", 0) < CHECK_MIN_AUTO:
        f.append(f"{model.get('n_auto', 0)} AUTO transitions used < {CHECK_MIN_AUTO}")
    if model.get("n_teleop", 0) < CHECK_MIN_TELEOP:
        f.append(f"{model.get('n_teleop', 0)} TELEOP transitions used < {CHECK_MIN_TELEOP}")
    if model.get("discontinuities"):
        f.append("video discontinuity inside the match: " + "; ".join(
            f"{d['display_skip_s']:+.1f} s at {d['t_after']}" for d in model["discontinuities"]))
    model["failures"] = f
    model["ok"] = not f


# --------------------------------------------------------------------------------------------------------------------
# 6. Team numbers (research section 6, team-number part)
# --------------------------------------------------------------------------------------------------------------------

def _runs1d(flags: np.ndarray, min_len: int = 1) -> list[tuple[int, int]]:
    out, i, n = [], 0, len(flags)
    while i < n:
        if flags[i]:
            j = i
            while j < n and flags[j]:
                j += 1
            if j - i >= min_len:
                out.append((i, j))
            i = j
        else:
            i += 1
    return out


def _fill_small_holes(mask: np.ndarray, max_area: int) -> np.ndarray:
    """Fill holes (background components not touching the border) of at most max_area pixels."""
    m = mask.astype(np.uint8)
    inv = (1 - m).astype(np.uint8)
    num, lab, stats, _ = cv2.connectedComponentsWithStats(inv, connectivity=4)
    h, w = m.shape
    out = m.copy()
    for i in range(1, num):
        x, y, bw, bh, a = stats[i]
        if x == 0 or y == 0 or x + bw == w or y + bh == h or a > max_area:
            continue
        out[lab == i] = 1
    return out


def _core_rect(sub: np.ndarray):
    """The solid core of a component (rows/columns filled >= 50 % of the fullest), or None."""
    h, w = sub.shape
    x0, y0, x1, y1 = 0, 0, w, h
    for _ in range(3):
        colh = sub[y0:y1].sum(axis=0)
        if colh.max() == 0:
            return None
        x0, x1 = max(_runs1d(colh >= 0.5 * colh.max()), key=lambda r: r[1] - r[0])
        rowh = sub[:, x0:x1].sum(axis=1)
        y0, y1 = max(_runs1d(rowh >= 0.5 * rowh.max()), key=lambda r: r[1] - r[0])
    if sub[y0:y1, x0:x1].mean() < 0.5:
        return None
    return [int(x0), int(y0), int(x1), int(y1)]


def colour_classes(bgr: np.ndarray) -> dict[str, np.ndarray]:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    H, S, V = hsv[..., 0].astype(int), hsv[..., 1].astype(int), hsv[..., 2].astype(int)
    red_h = (H <= 12) | (H >= 165)
    blue_h = (H >= 95) & (H <= 130)
    sat = S >= 120
    return {"red": red_h & sat & (V >= 160), "dark_red": red_h & sat & (V >= 40) & (V < 160),
            "blue": blue_h & sat & (V >= 140), "dark_blue": blue_h & sat & (V >= 30) & (V < 140)}


def _colour_rects(cls_mask: np.ndarray, bw: int, bh: int) -> list[list[int]]:
    H, W = cls_mask.shape
    m = cv2.morphologyEx(cls_mask.astype(np.uint8), cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    m = _fill_small_holes(m, int(0.6 * bw * bh))
    num, lab, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    rects = []
    for i in range(1, num):
        x, y, w, h, a = (int(v) for v in stats[i])
        if a < 0.15 * bw * bh or a > 0.4 * H * W:
            continue
        r = _core_rect(lab[y:y + h, x:x + w] == i)
        if r is not None:
            rects.append([x + r[0], y + r[1], x + r[2], y + r[3]])
    return rects


def _digit_like(g, hc):
    return 0.35 * hc <= g["h"] <= 1.6 * hc and 0.15 <= g["w"] / max(g["h"], 1) <= 1.1


def _team_number_candidates(fr, panel, hc, templates: DigitTemplates, thr: int = 150):
    """Groups of 2-5 consecutive digit glyphs of similar height on a dark panel: [(bbox, text)] (research).
    `thr`: ink threshold.  150 breaks the thin anti-aliased stroke of a '2' on necmp1 keyframes ('2370' read as
    '370' in 6 of 15 frames); 110-150 merge '04' on the D1 2026cancmp overlay ('5104' read as '51'), which only 170
    (the research's ink default) separates."""
    x0, y0, x1, y1 = panel
    Z = 2
    big = cv2.resize(fr[y0:y1, x0:x1], None, fx=Z, fy=Z, interpolation=cv2.INTER_CUBIC)
    ink = ink_bright(big, thr=thr)
    glyphs = [g for g in extract_glyphs(ink, min_h=4) if 0.3 * hc * Z <= g["h"] <= 1.2 * hc * Z]
    out = []
    for ln in group_lines(glyphs, gap_factor=3.0):
        lh = float(np.median([g["h"] for g in ln]))
        group: list[tuple[dict, str]] = []

        def flush():
            if 2 <= len(group) <= 5:
                bb = _line_bbox([g for g, _ in group])
                out.append(([x0 + bb[0] // Z, y0 + bb[1] // Z, x0 + bb[2] // Z, y0 + bb[3] // Z],
                            "".join(lab for _, lab in group)))
            group.clear()

        for g in ln:
            kind, score = classify_glyph(g, lh, templates)
            if kind == "junk" and _digit_like(g, lh) and g["fill"] < 0.75:
                vec, ar = norm_glyph(g["mask"])
                lab, score, _ = templates.match(vec, ar)
                kind = str(lab) if lab is not None and score >= 0.45 else "junk"
            if kind in ("junk", "dot", "slash", "digit?"):
                flush()
                continue
            if group:
                prev = group[-1][0]
                if g["x0"] - prev["x1"] > 0.8 * prev["h"] or abs(g["h"] - prev["h"]) > 0.3 * prev["h"]:
                    flush()
            group.append((g, kind))
        flush()
    return out


def _contain(a, b) -> float:
    """Overlap area over the smaller box's area."""
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    sa = (a[2] - a[0]) * (a[3] - a[1])
    sb = (b[2] - b[0]) * (b[3] - b[1])
    return ix * iy / max(1, min(sa, sb))


def _cluster_boxes(items: list[tuple[list, object]], min_contain: float = 0.6) -> list[dict]:
    """Group boxes that overlap across frames (a truncated read's box lies inside the full one's)."""
    cl: list[dict] = []
    for bb, it in items:
        for c in cl:
            if _contain(c["bbox"], bb) >= min_contain:
                c["n"] += 1
                c["bbox"] = [min(c["bbox"][0], bb[0]), min(c["bbox"][1], bb[1]),
                             max(c["bbox"][2], bb[2]), max(c["bbox"][3], bb[3])]
                c["items"].append(it)
                break
        else:
            cl.append({"bbox": list(bb), "n": 1, "items": [it]})
    return cl


TEAM_INK_THRS = (130, 150, 170)   # team-number ink thresholds; each fails differently (see _team_number_candidates)


def read_team_numbers(frames: list[np.ndarray], box, hc: float, templates: DigitTemplates) -> dict:
    """Team numbers on the dark alliance panels near the clock box, voted over `frames` (BGR, same crop) and over two
    ink thresholds.  The usual failure is a truncated number ('370' for 2370 when a thin stroke breaks at 150; '51'
    for 5104 when '04' merges at 130 on the D1 2026cancmp overlay), so the most frequent read is replaced by a longer
    read that contains it when that one has at least a quarter of the frames.
    Returns {'red': [...], 'blue': [...], 'detail': {...}} with up to three numbers per alliance."""
    nk = len(frames)
    bx0, by0, bx1, by1 = box
    bw, bh = bx1 - bx0, by1 - by0
    median = np.median(np.stack(frames), axis=0).astype(np.uint8)
    cls = colour_classes(median)
    used = _colour_rects(cls["red"], bw, bh) + _colour_rects(cls["blue"], bw, bh)   # score and fuel boxes
    res: dict = {"red": [], "blue": [], "detail": {}}
    for colour in ("red", "blue"):
        panels = [bb for bb in _colour_rects(cls[f"dark_{colour}"], bw, bh)
                  if (bb[2] - bb[0]) * (bb[3] - bb[1]) >= 1.5 * bw * bh]
        found = []
        for pb in panels:
            items = []
            for k, fr in enumerate(frames):
                for thr in TEAM_INK_THRS:
                    for bb, text in _team_number_candidates(fr, pb, hc, templates, thr):
                        cx, cy = 0.5 * (bb[0] + bb[2]), 0.5 * (bb[1] + bb[3])
                        if any(u[0] <= cx <= u[2] and u[1] <= cy <= u[3] for u in used):
                            continue
                        items.append((bb, (k, text)))
            for c in _cluster_boxes(items):
                frames_seen = {k for k, _ in c["items"]}
                if len(frames_seen) < 0.6 * nk:
                    continue
                # support of a text = the frames where some threshold read it
                support: dict[str, set] = {}
                for k, text in c["items"]:
                    support.setdefault(text, set()).add(k)
                text = max(support, key=lambda s: (len(support[s]), len(s)))
                longer = [s for s in support if len(s) > len(text) and text in s and len(support[s]) >= 0.25 * nk]
                if longer:
                    text = max(longer, key=lambda s: (len(support[s]), len(s)))
                found.append({"roi": c["bbox"], "text": text.lstrip("0") or text,
                              "agreement": round(len(support[text]) / len(frames_seen), 3), "n": len(frames_seen)})
        found.sort(key=lambda f: (f["roi"][1] // max(1, int(hc)), f["roi"][0]))
        found = found[:3]
        res[colour] = [f["text"] for f in found]
        res["detail"][colour] = found
    res["frames"] = nk
    return res


# --------------------------------------------------------------------------------------------------------------------
# The streaming reader
# --------------------------------------------------------------------------------------------------------------------

class _Samples:
    """Rolling buffer of tracked clock samples."""

    def __init__(self):
        self.t = np.zeros(0, np.float64)
        self.ng = np.zeros(0, np.int8)
        self.vec = np.zeros((0, 4, GW * GH), np.float16)
        self.asp = np.zeros((0, 4), np.float32)
        self.lh = np.zeros(0, np.float32)
        self.val = np.zeros(0, np.int32)       # -2 not read yet, -1 no reading, else seconds
        self.conf = np.zeros(0, np.float32)
        self.text = []

    def __len__(self):
        return len(self.t)

    def append(self, t, ng, vec, asp, lh):
        n = len(t)
        self.t = np.concatenate([self.t, t])
        self.ng = np.concatenate([self.ng, ng])
        self.vec = np.concatenate([self.vec, vec])
        self.asp = np.concatenate([self.asp, asp])
        self.lh = np.concatenate([self.lh, lh])
        self.val = np.concatenate([self.val, np.full(n, -2, np.int32)])
        self.conf = np.concatenate([self.conf, np.zeros(n, np.float32)])
        self.text += [""] * n

    def keep(self, mask):
        self.t, self.ng, self.vec, self.asp = self.t[mask], self.ng[mask], self.vec[mask], self.asp[mask]
        self.lh, self.val, self.conf = self.lh[mask], self.val[mask], self.conf[mask]
        self.text = [s for s, k in zip(self.text, mask) if k]

    def trim_before(self, t_min):
        if len(self.t) and self.t[0] < t_min:
            self.keep(self.t >= t_min)

    def truncate_from(self, t_from):
        if len(self.t) and self.t[-1] >= t_from:
            self.keep(self.t < t_from)

    def clear(self):
        self.__init__()


class ClockReader:
    """Streaming reader of the 2026 FRC scoreboard clock.  See the module docstring for the API."""

    def __init__(self, *, sample_fps: float = 10.0, threads: int = 1, relocate_after_s: float = 300.0,
                 rescan_after_s: float = 30.0, search_window_s: float = 150.0, buffer_s: float = 420.0,
                 episode_gap_s: float = 45.0, read_teams: bool = True, season: dict | None = None,
                 ffmpeg_bin: str | None = None, ffprobe_bin: str | None = None, cv_threads: int | None = 1):
        """sample_fps: upper bound on clock samples per second (reference frames only are decoded, so a stream gives
        what its GOP structure allows below this, e.g. 7.5/s at necmp1).  threads: ffmpeg decoder threads.
        relocate_after_s: clock missing this long -> back to SEARCH.  rescan_after_s: clock missing this long -> also
        search each chunk's keyframes for a clock elsewhere.  search_window_s: keyframe history used for location.
        buffer_s: tracked samples kept for countdown labelling and late readings.  episode_gap_s: an episode with no
        transition for this long (and past its predicted end) is closed.  read_teams: read the six team numbers."""
        self.sample_fps = float(sample_fps)
        self.threads = int(threads)
        self.relocate_after_s = float(relocate_after_s)
        self.rescan_after_s = float(rescan_after_s)
        self.search_window_s = float(search_window_s)
        self.buffer_s = float(buffer_s)
        self.episode_gap_s = float(episode_gap_s)
        self.read_teams_enabled = bool(read_teams)
        self.season = dict(season or SEASON_2026)
        self.ffmpeg_bin = ffmpeg_bin or _ffmpeg.find("ffmpeg")
        self.ffprobe_bin = ffprobe_bin or _ffmpeg.find("ffprobe")
        if cv_threads is not None:
            cv2.setNumThreads(int(cv_threads))
        self.mode = "search"
        self.clock: dict | None = None
        self.known: list[dict] = []                 # clocks located before, each with its own digit templates
        self.excluded: list[tuple[list, float]] = []  # (roi, until stream time): located things that were not the clock
        self._own = DigitTemplates()                # templates being learned for the current clock
        self._borrowed: DigitTemplates | None = None
        self._clock_since = -math.inf               # samples before this belong to another clock position
        self._window: list[dict] = []               # search frames
        self._recent: deque = deque(maxlen=3)       # (path, t0, info, t_end)
        self._buf = _Samples()
        self._consumed: dict[float, float] = {}     # labelled run start -> t (runs already added to the templates)
        self._trans_last_t = -math.inf
        self._held_value = -1
        self._episodes: list[dict] = []
        self._ep: dict | None = None
        self._last_clock_t: float | None = None
        self._located_t: float | None = None
        self._use_noref = True
        self._chunk_cpu = 0.0
        self.last_stats: dict = {}
        self.totals = {"chunks": 0, "media_s": 0.0, "wall_s": 0.0, "cpu_s": 0.0, "ffmpeg_cpu_s": 0.0, "readings": 0}
        self.events: list[dict] = []                # log of located / lost / templates-ready events

    # ---------------------------------------------------------------------------------------------------------- API
    def feed(self, path, t0: float) -> list[dict]:
        """Process one chunk (`t0` = stream seconds of its first video frame); returns new readings sorted by t."""
        wall0, cpu0 = time.perf_counter(), time.process_time()
        self._chunk_cpu = 0.0
        info = probe(path, self.ffprobe_bin)
        if info is None:
            log.warning("chunk %s has no readable video; skipped", path)
            return []
        t_end = t0 + info["duration"]
        self._recent.append((str(path), float(t0), info, t_end))
        self.excluded = [(r, until) for r, until in self.excluded if until > t0]
        out: list[dict] = []
        if self.clock is not None and (info["w"], info["h"]) != tuple(self.clock["frame_wh"]):
            self._lose_clock(t0, f"picture size changed to {info['w']}x{info['h']}")
        if self.mode == "track":
            out += self._track_chunk(str(path), t0, info)
            searched = False
            if self.mode == "track":
                seen = self._last_clock_t if self._last_clock_t is not None else self._located_t
                gone = t_end - seen
                # the clock vanished: after rescan_after_s, or after 10 s in the middle of a countdown episode (the
                # overlay may have moved or been resized), also look for it elsewhere in this chunk's keyframes
                if gone > self.rescan_after_s or (self._ep is not None and gone > 10.0):
                    searched = True
                    found = self._search_chunk(str(path), t0, info,
                                               exclude=[self.clock["roi"]] + [r for r, _ in self.excluded])
                    if found is not None:
                        # what the old position read before it vanished stays; from then on (or from where the new
                        # clock was first seen in the search window) the recent chunks are read at the new place
                        keep = max(seen + 1e-3, (found.get("first_t") or t0) - 5.0)
                        self._buf.truncate_from(keep)
                        out = [r for r in out if r["t"] < keep]
                        out += self._start_tracking(found, t_end, backfill_from=keep, keep_until=keep)
                    elif gone > self.relocate_after_s:
                        self._lose_clock(t_end, f"clock not seen for {gone:.0f} s")
            if self.mode == "track" and not searched and not self._own.ready:
                out = self._challenge(str(path), t0, info, t_end, out)
            if self.mode == "track" and not self.templates.ready and self._located_t is not None \
                    and t_end - self._located_t > 400 and (self._last_clock_t or 0) > self._located_t:
                # a 'clock' that shows a line but never a labelled countdown: not the match clock
                self.excluded.append((list(self.clock["roi"]), t_end + 1800))
                self._lose_clock(t_end, "no labelled countdown 400 s after location (excluded for 30 min)")
        else:
            found = self._search_chunk(str(path), t0, info, exclude=[r for r, _ in self.excluded] or None)
            if found is not None:
                out += self._start_tracking(found, t_end, backfill_from=found.get("first_t", t0) - 5.0)
        self._tick(t_end)
        out.sort(key=lambda r: r["t"])
        wall = time.perf_counter() - wall0
        cpu = time.process_time() - cpu0
        self.last_stats = {"chunk": str(path), "t0": t0, "media_s": round(info["duration"], 3), "mode": self.mode,
                           "wall_s": round(wall, 3), "python_cpu_s": round(cpu, 3),
                           "ffmpeg_cpu_s": round(self._chunk_cpu, 3), "readings": len(out)}
        tot = self.totals
        tot["chunks"] += 1
        tot["media_s"] += info["duration"]
        tot["wall_s"] += wall
        tot["cpu_s"] += cpu + self._chunk_cpu
        tot["ffmpeg_cpu_s"] += self._chunk_cpu
        tot["readings"] += len(out)
        return out

    def matches(self) -> list[dict]:
        """Fitted countdown episodes, oldest first.  Each dict: auto_start, auto_end, teleop_start, match_end (stream
        seconds, None if not measurable), rate (video s per displayed s), pause_s, rms_s, n_auto / n_teleop (used
        transitions), auto_inferred (AUTO not on screen: placed 24 s before TELEOP start), ok + failures (research
        checks), discontinuities, first_t / last_t (first and last transition), close_reason, teams, final (False for
        the episode still running, whose fit is provisional)."""
        out = [dict(e) for e in self._episodes]
        if self._ep is not None and len(self._ep["trans"]) >= 3:
            out.append(self._episode_result(self._ep, final=False))
        return out

    def teams(self, t: float) -> dict | None:
        """Team numbers read for the episode around stream time t (from 60 s before its first transition to 5 min
        after its last), or None.  {'red': [3 str], 'blue': [3 str], 'agreement': lowest share of frames agreeing on
        a number, 'complete': three per alliance with agreement >= 0.6, 'attempts', 't'}.  Use only complete reads:
        an incomplete one can hold misread fragments (mawor qm10's soft 13-14 px digits give '11', '114')."""
        best, best_d = None, math.inf
        eps = list(self._episodes) + ([self._ep] if self._ep is not None else [])
        for e in eps:
            if e.get("teams") is None:
                continue
            a, b = e["first_t"] - 60.0, e["last_t"] + 300.0
            if a <= t <= b:
                d = abs(t - 0.5 * (e["first_t"] + e["last_t"]))
                if d < best_d:
                    best, best_d = e["teams"], d
        return best

    def status(self) -> dict:
        return {"mode": self.mode, "clock": {k: v for k, v in (self.clock or {}).items()} or None,
                "templates": self.templates.to_json(), "templates_borrowed": self.templates is not self._own,
                "known_clocks": [c["roi"] for c in self.known],
                "excluded": [r for r, _ in self.excluded], "decode_noref": self._use_noref,
                "episodes": len(self._episodes), "episode_open": self._ep is not None,
                "last_clock_t": self._last_clock_t, "last": self.last_stats, "totals": dict(self.totals)}

    # ------------------------------------------------------------------------------------------------------ search
    def _search_chunk(self, path, t0, info, exclude=None, allow_known=True, recent_s: float | None = None) -> dict | None:
        """recent_s: locate from the last recent_s seconds of the search window only (see _challenge)."""
        W, H = info["w"], info["h"]
        frames: list[dict] = []
        known_rois = [c["roi"] for c in self.known] if allow_known else []

        def on_frame(k, fr):
            item = {"k": k, "cands": white_box_candidates(fr), "known": []}
            grey = None
            for roi in known_rois:
                if grey is None:
                    grey = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
                item["known"].append(grey[roi[1]:roi[3], roi[0]:roi[2]].copy())
            frames.append(item)

        pts, _, st = decode(path, skip="nokey", pix_fmt="bgr24", size=(W, H), threads=self.threads,
                            ffmpeg_bin=self.ffmpeg_bin, on_frame=on_frame)
        self._chunk_cpu += st["ffmpeg_cpu_s"]
        if len(pts) < 4 and info["duration"] > 20:
            # keyframes too sparse: sample reference frames every 2 s instead (costs a reference-frame decode)
            frames.clear()
            pts, _, st = decode(path, skip="noref", pix_fmt="bgr24", size=(W, H), min_spacing=2.0,
                                threads=self.threads, ffmpeg_bin=self.ffmpeg_bin, on_frame=on_frame)
            self._chunk_cpu += st["ffmpeg_cpu_s"]
        for item in frames[:len(pts)]:
            item["t"] = t0 + (pts[item["k"]] - info["start"])
        frames = [f for f in frames if "t" in f]
        t_end = t0 + info["duration"]
        self._window = [f for f in self._window if f["t"] >= t_end - self.search_window_s
                        and f.get("wh") == (W, H)] + [dict(f, wh=(W, H)) for f in frames]
        # 1) a clock seen before, at its old place, if that place proved itself (learned its own digit templates from a
        #    labelled countdown): 3 frames of the last minute that read as m:ss (a static 0:20 or 0:00 between matches
        #    is enough: the place is already known to be the clock).  Places that never proved themselves are not
        #    resumed: on D2 2026cagle rows of white chairs and banner letters were located before the first match.
        if allow_known:
            for i, c in enumerate(self.known):
                if not c["templates"].ready or tuple(c["frame_wh"]) != (W, H) \
                        or (exclude and any(rois_agree(c["roi"], r) for r in exclude)):
                    continue
                items = []
                for f in self._window:
                    if i < len(f.get("known", [])) and f["t"] >= t_end - 60.0:
                        crop = f["known"][i]
                        if (crop >= WHITE_THR).mean() >= 0.3:
                            x0, y0, x1, y1 = c["roi"]
                            items.append((f["t"], crop, (x0, y0, x0, y0, x1, y1)))
                if len(items) < 3:
                    continue
                reads = _read_items(items, c["solid_min_box"], c["templates"])
                if len(reads) >= 3:
                    found = {k: v for k, v in c.items() if k != "templates"}
                    found.update({"first_t": min(t for t, _ in reads), "how": "resumed known clock"})
                    return found
        # 2) full location
        window = self._window if recent_s is None else [f for f in self._window if f["t"] >= t_end - recent_s]
        cand = locate_clock(window, exclude=exclude, templates=self.templates, frame_h=H)
        if cand is not None:
            cand["frame_wh"] = (W, H)
            cand["how"] = "located" + (" (trimmed box)" if cand.get("trimmed") else "") + \
                          (" (enlarged-overlay glyph rule)" if cand["solid_min_box"] != SOLID_MIN_BOX else "")
        return cand

    @property
    def templates(self) -> DigitTemplates:
        """Templates used for reading: the current clock's own once learned, else the previous clock's (a moved or
        zoomed overlay has the same digits; normalised glyphs are size-free), else the (unready) own ones."""
        if self._own.ready or self._borrowed is None:
            return self._own
        return self._borrowed

    def _start_tracking(self, clock: dict, t_end: float, backfill_from: float,
                        keep_until: float | None = None) -> list[dict]:
        clock = dict(clock)
        clock.setdefault("frame_wh", tuple(self._recent[-1][2][k] for k in ("w", "h")))
        old = self.clock
        prev_active = self.templates if self.templates.ready else None
        self.clock = clock
        self.mode = "track"
        entry = next((c for c in self.known if rois_agree(clock["roi"], c["roi"])
                      and tuple(c["frame_wh"]) == tuple(clock["frame_wh"])), None)
        if entry is None:
            entry = {k: clock[k] for k in ("roi", "box", "glyph_h", "solid_min_box", "frame_wh")}
            entry["templates"] = DigitTemplates()
            self.known.append(entry)
        self._own = entry["templates"]
        self._borrowed = prev_active if prev_active is not None and prev_active is not self._own else None
        self._window = []
        self._located_t = t_end
        self._last_clock_t = None
        self._log_event(t_end, "located", roi=clock["roi"], how=clock.get("how"), glyph_h=round(clock["glyph_h"], 1),
                        previous=old["roi"] if old else None, own_templates=self._own.ready,
                        borrowed_templates=self._borrowed is not None)
        out = []
        chunks = [c for c in self._recent if c[3] > backfill_from]
        if chunks:
            cut = chunks[0][1] if keep_until is None else max(chunks[0][1], keep_until)
            self._buf.truncate_from(cut)
            self._clock_since = cut
        for path, c0, info, _ in chunks:
            out += self._track_chunk(path, c0, info)
        return out

    def _challenge(self, path, t0, info, t_end, out: list[dict]) -> list[dict]:
        """The tracked place has not yet shown a labelled countdown (no digit templates of its own), so it may be
        clutter that looked like a changing m:ss line while the real clock sat frozen at 0:20 before a match (D2
        2026cagle: rows of white chairs, banner letters).  Keep searching this chunk's keyframes for another place.  A
        proven clock place (own templates) showing m:ss wins at once; a new candidate wins if its seconds digit changes
        once a second here (once_a_second >= RHYTHM_MIN) and the tracked place's does not as well.  The loser is
        excluded for 30 min and the recent chunks are read again at the winner, so the match start is not lost."""
        excl = ([self.clock["roi"]] + [r for r, _ in self.excluded]
                + [c["roi"] for c in self.known if not c["templates"].ready])
        # only the last 75 s: frames of the frozen 0:20 before the start would outvote the countdown's changing ones
        # in the full 150 s window (D2 f1m2: found 108 s into the match with it, the re-read nearly ran out)
        found = self._search_chunk(path, t0, info, exclude=excl, recent_s=75.0)
        if found is None:
            return out
        if found.get("how") != "resumed known clock":
            b = self._buf
            here = b.t >= t0
            cur = once_a_second(b.t[here], b.ng[here], b.vec[here], b.asp[here])
            t, ng, vec, asp, _ = self._place_samples(path, t0, info, found["roi"], found["solid_min_box"])
            new = once_a_second(t, ng, vec, asp)
            self._log_event(t_end, "challenger", roi=found["roi"], rhythm=new, current=self.clock["roi"],
                            current_rhythm=cur)
            if new < RHYTHM_MIN or new <= cur:
                return out
        old = list(self.clock["roi"])
        self.excluded.append((old, t_end + 1800))
        self.known = [c for c in self.known if c["templates"].ready or not rois_agree(c["roi"], old)]
        self._buf.clear()
        self._log_event(t_end, "switched", old=old, new=found["roi"], how=found.get("how"))
        return self._start_tracking(found, t_end, backfill_from=min((found.get("first_t") or t0) - 5.0, t0 - 125.0))

    def _lose_clock(self, t, why):
        self._log_event(t, "lost", roi=self.clock["roi"] if self.clock else None, why=why)
        self.mode = "search"
        self.clock = None
        self._window = []
        self._buf.clear()
        self._located_t = None

    def _log_event(self, t, what, **kw):
        ev = {"t": round(float(t), 3), "event": what, **kw}
        self.events.append(ev)
        log.info("clock %s at %.1f s: %s", what, t, kw)

    # ------------------------------------------------------------------------------------------------------- track
    def _place_samples(self, path, t0, info, roi, solid) -> tuple:
        """A chunk's samples at clock place `roi`: reference frames cropped to it, each with its tall glyphs' count,
        normalised shapes, aspect ratios and line height.  Returns (t, ng, vec, asp, lh), t in stream seconds."""
        W, H = info["w"], info["h"]
        x0, y0, x1, y1 = roi
        ex0, ey0 = x0 - (x0 % 2), y0 - (y0 % 2)
        ex1, ey1 = min(W, x1 + ((x1 - ex0) % 2)), min(H, y1 + ((y1 - ey0) % 2))
        pts, frames, st = decode(path, skip="noref" if self._use_noref else None, crop=(ex0, ey0, ex1, ey1),
                                 pix_fmt="gray", min_spacing=0.95 / self.sample_fps, threads=self.threads,
                                 ffmpeg_bin=self.ffmpeg_bin)
        self._chunk_cpu += st["ffmpeg_cpu_s"]
        n = len(pts)
        t = t0 + (np.array(pts, np.float64) - info["start"])
        ox, oy = x0 - ex0, y0 - ey0
        ng = np.zeros(n, np.int8)
        vec = np.zeros((n, 4, GW * GH), np.float16)
        asp = np.zeros((n, 4), np.float32)
        lh = np.zeros(n, np.float32)
        for k in range(n):
            crop = frames[k][oy:oy + (y1 - y0), ox:ox + (x1 - x0)]
            if (crop >= WHITE_THR).mean() < 0.3:
                continue
            tall = _clock_line(ink_dark(crop), solid)
            if tall is None:
                continue
            ng[k] = len(tall)
            lh[k] = max(g["h"] for g in tall)
            for p, g in enumerate(tall):
                v, a = norm_glyph(g["mask"])
                vec[k, p] = v
                asp[k, p] = a
        return t, ng, vec, asp, lh

    def _track_chunk(self, path, t0, info) -> list[dict]:
        t, ng, vec, asp, lh = self._place_samples(path, t0, info, self.clock["roi"], self.clock["solid_min_box"])
        n = len(t)
        if n == 0:
            return []
        if self._use_noref and n >= 10 and info["duration"] > 20:
            rate = (n - 1) / max(t[-1] - t[0], 1e-6)
            if rate < 5.0:
                self._use_noref = False
                log.info("reference frames give only %.1f samples/s: decoding every frame from now on", rate)
        if len(self._buf) and t[0] <= self._buf.t[-1]:
            keep = t > self._buf.t[-1]
            t, ng, vec, asp, lh = t[keep], ng[keep], vec[keep], asp[keep], lh[keep]
        if len(t) == 0:
            return []
        if (ng >= 3).any():
            self._last_clock_t = float(t[ng >= 3][-1])
        self._buf.append(t, ng, vec, asp, lh)
        self._buf.trim_before(self._buf.t[-1] - self.buffer_s)
        self._learn()
        out = self._read_pending()
        self._update_transitions()
        if self.read_teams_enabled:
            try:
                self._maybe_read_teams(path, t0, info)
            except Exception as e:          # team numbers are a bonus; never break the clock on them
                log.warning("team-number read failed on %s: %s", path, e)
        self._check_not_match_clock(out, t0)
        return out

    def _learn(self):
        b = self._buf
        if len(b) < 5:
            return
        dt = float(np.median(np.diff(b.t))) if len(b) > 1 else 0.1
        runs = build_runs(b.t, b.ng, b.vec, b.asp, dt)
        label_countdown(runs, self.season["auto_s"], self.season["teleop_s"])
        t_last = float(b.t[-1])
        own = self._own
        was_ready = own.ready
        added = 0
        for r in runs:
            if r["value"] is None or r["kind"] == "glitch" or r["value"] < 0:
                continue
            if r["chain_len"] < 10:
                continue                    # short chains have no tens change to check their labels against
            if r["tl"] >= t_last - 1e-6 or r["t0"] < self._clock_since:
                continue                    # still growing, or seen through another clock position
            key = round(r["t0"], 3)
            if key in self._consumed:
                continue
            self._consumed[key] = r["t0"]
            for p, d in enumerate(_digits_of(r["value"], r["ng"])):
                if 0 <= d <= 9:
                    own.add(d, r["cent"][p], r["asp"][p], r["n"])
            added += 1
        if added:
            if own.glyph_h is None:
                own.glyph_h = float(self.clock["glyph_h"])
            own.finalize()
        lo = float(b.t[0]) - 1.0
        self._consumed = {k: v for k, v in self._consumed.items() if v >= lo}
        if own.ready and not was_ready:
            self._log_event(t_last, "templates ready", roi=self.clock["roi"],
                            runs_per_digit=[int(x) for x in own.runs])

    def _read_pending(self) -> list[dict]:
        b = self._buf
        if not self.templates.ready:
            return []
        out = []
        T = self.templates
        for k in np.nonzero(b.val == -2)[0]:
            g = int(b.ng[k])
            b.val[k] = -1
            if g not in (3, 4):
                continue
            digits, conf = [], 1.0
            for p in range(g):
                lab, score, margin = T.match(b.vec[k, p].astype(np.float32), float(b.asp[k, p]))
                if lab is None or score < 0.55:
                    digits = None
                    break
                digits.append(lab)
                conf = min(conf, score, 0.5 + margin)
            if digits is None:
                continue
            if g == 3:
                m, s = digits[0], 10 * digits[1] + digits[2]
            else:
                m, s = 10 * digits[0] + digits[1], 10 * digits[2] + digits[3]
            if digits[-2] > 5:
                continue
            v = 60 * m + s
            b.val[k] = v
            b.conf[k] = conf
            text = f"{m}:{s:02d}"
            b.text[k] = text
            out.append({"t": round(float(b.t[k]), 3), "text": text, "value_s": int(v), "conf": round(float(conf), 3)})
        return out

    def _check_not_match_clock(self, out: list[dict], t0: float):
        """A tracked display that reads only values above 2:40 for a whole chunk is not the match clock (a break timer):
        exclude it for 30 min and search again."""
        if len(out) < 30 or self.clock is None:
            return
        vals = [r["value_s"] for r in out if r["t"] >= t0]
        if len(vals) >= 30 and min(vals) > self.season["teleop_s"] + 20:
            self.excluded.append((list(self.clock["roi"]), t0 + 1800))
            self._lose_clock(t0, f"tracked display shows {min(vals)}-{max(vals)} s: not the match clock")

    # ---------------------------------------------------------------------------------------- transitions/episodes
    def _update_transitions(self):
        b = self._buf
        n = len(b)
        if n < 3:
            return
        vals = np.where(b.val >= 0, b.val, -1)
        t = b.t
        t_last = float(t[-1])
        hold = 0.4
        # research clock_transitions: a new value that then holds for >= 0.4 s (80 % of the samples); `prev` is the
        # last value that held (a one-sample misread in between is ignored), and the transition time is the midpoint
        # between the first sample showing the new value and the sample before it; `gap` > 0.5 s means the change
        # happened somewhere in a stretch without samples (cut-away, overlay hidden), so its time is unknown
        i = int(np.searchsorted(t, self._trans_last_t, side="right"))
        i = max(i, 1)
        while i < n:
            v = vals[i]
            if v >= 0 and v != self._held_value:
                if t[i] + hold > t_last:
                    break                   # hold window not complete yet
                j_end = i
                while j_end < n and t[j_end] < t[i] + hold:
                    j_end += 1
                seg = vals[i:j_end]
                if len(seg) >= 2 and (seg == v).mean() >= 0.8:
                    j = i - 1
                    while j > 0 and vals[j] < 0:
                        j -= 1
                    tj = float(t[j])
                    gap = float(t[i] - tj)
                    tr = {"t": 0.5 * (tj + float(t[i])) if gap < 0.3 else float(t[i]), "value": int(v),
                          "prev": int(self._held_value), "t_first": float(t[i]), "gap": round(gap, 3)}
                    self._held_value = int(v)
                    self._trans_last_t = float(t[i])
                    self._episode_step(tr)
                    i = j_end
                    continue
            i += 1

    def _expected_value(self, ep, t) -> float | None:
        """Displayed value the open episode predicts at stream time t (its TELEOP line, else its last transition)."""
        tele_s = self.season["teleop_s"]
        fit = ep.get("fit")
        if fit and fit.get("teleop_start") is not None and fit.get("rate") and t >= fit["teleop_start"]:
            return tele_s - (t - fit["teleop_start"]) / fit["rate"]
        last = ep["trans"][-1]
        if 0 <= last["prev"] <= 1 and last["value"] >= tele_s - 1:      # in the 2:20 pause hold (about 4-5 s)
            return tele_s - max(0.0, t - last["t"] - 5.0)
        if last["prev"] == last["value"] + 1:
            return last["value"] - (t - last["t"])
        return None

    def _episode_step(self, tr):
        t, v, p = tr["t"], tr["value"], tr["prev"]
        tele_s, auto_s = self.season["teleop_s"], self.season["auto_s"]
        countdown = p >= 0 and p == v + 1
        to_teleop = p >= 0 and 0 <= p <= 1 and v >= tele_s - 1
        # the pre-match displays: 0:20 (only TELEOP's endgame counts into it, from 0:21) and 2:20 (only reached from
        # AUTO's 0:01): reaching either any other way is a reset.  D1 2026cancmp qm62 was aborted at 2:07, showed
        # 0:20 for 95 s and restarted; the restart's 0:19 happened to equal the aborted TELEOP's prediction.
        reset = p >= 0 and not countdown and ((v == auto_s and p != auto_s + 1) or (v >= tele_s - 1 and not to_teleop))
        ep = self._ep
        if ep is not None:
            exp = self._expected_value(ep, t)
            reason = None
            if v > p >= 0 and not to_teleop:
                reason = "clock went up (reset, replay or next match)"
            elif reset:
                reason = f"clock reset to {v // 60}:{v % 60:02d} (match aborted or restarted?)"
            elif countdown and exp is not None and t - ep["last_t"] > 8.0 and v > exp + 2.0:
                reason = "countdown inconsistent with the episode (shows more time left)"
            elif t - ep["last_t"] > 200.0:
                reason = "gap"
            if reason:
                self._close_episode(reason)
                ep = None
        if ep is None:
            if not countdown:
                return
            ep = self._ep = {"trans": [], "first_t": t, "last_t": t, "teams": None, "team_votes": [],
                             "team_attempts": 0, "fit": None}
        ep["trans"].append(tr)
        ep["last_t"] = t
        if len(ep["trans"]) % 10 == 0:
            ep["fit"] = fit_clock_model(ep["trans"], self.season)
        if countdown and v == 0:
            self._close_episode("reached 0:00")

    def _episode_result(self, ep, final: bool) -> dict:
        fit = fit_clock_model(ep["trans"], self.season)
        res = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in fit.items()}
        res.update({"first_t": round(ep["first_t"], 3), "last_t": round(ep["last_t"], 3),
                    "n_transitions": len(ep["trans"]), "teams": ep.get("teams"), "final": final,
                    "close_reason": ep.get("close_reason")})
        return res

    def _close_episode(self, reason):
        ep = self._ep
        if ep is None:
            return
        self._ep = None
        ep["close_reason"] = reason
        res = self._episode_result(ep, final=True)
        self._episodes.append(res)
        log.info("episode closed (%s): AUTO %s TELEOP-end %s ok=%s", reason, res.get("auto_start"),
                 res.get("match_end"), res.get("ok"))

    def _tick(self, now):
        ep = self._ep
        if ep is None:
            return
        fit = ep.get("fit") or {}
        end = fit.get("match_end")
        idle = now - ep["last_t"]
        if idle > 200.0 or (idle > self.episode_gap_s and (end is None or now > end + 10.0)):
            self._close_episode(f"no transition for {idle:.0f} s")

    # ------------------------------------------------------------------------------------------------------- teams
    def _maybe_read_teams(self, path, t0, info):
        ep = self._ep
        if ep is None or not self.templates.ready or ep.get("teams_done") or ep["team_attempts"] >= 3:
            return
        b = self._buf
        sel = (b.t >= t0) & (b.val > self.season["auto_s"]) & (b.val < self.season["teleop_s"])
        if sel.sum() < 20:
            return                          # read the teams while TELEOP is on screen
        ep["team_attempts"] += 1
        W, H = info["w"], info["h"]
        bx0, by0, bx1, by1 = self.clock["box"]
        bh = by1 - by0
        band = (0, max(0, (by0 - 3 * bh) // 2 * 2), W, min(H, (by1 + 3 * bh) // 2 * 2))
        pts, frames, st = decode(path, skip="nokey", crop=band, pix_fmt="bgr24", threads=self.threads,
                                 ffmpeg_bin=self.ffmpeg_bin)
        self._chunk_cpu += st["ffmpeg_cpu_s"]
        read_t = b.t[sel]
        keep = []
        for k, p in enumerate(pts):
            tk = t0 + (p - info["start"])
            if len(read_t) and np.min(np.abs(read_t - tk)) <= 0.25:
                keep.append(frames[k])
        if len(keep) < 3:
            return
        box_rel = (bx0 - band[0], by0 - band[1], bx1 - band[0], by1 - band[1])
        res = read_team_numbers(keep, box_rel, float(self.clock["glyph_h"]), self.templates)
        ep["team_votes"].append(res)
        # combine attempts: per alliance, the list read most often (ties: the latest)
        teams = {}
        agree = []
        for colour in ("red", "blue"):
            lists = [tuple(r[colour]) for r in ep["team_votes"] if r[colour]]
            if lists:
                best = max(set(lists), key=lambda L: (lists.count(L), len(L)))
                teams[colour] = list(best)
                last = next(r for r in reversed(ep["team_votes"]) if tuple(r[colour]) == best)
                agree += [d["agreement"] for d in last["detail"][colour]]
            else:
                teams[colour] = []
        teams["agreement"] = round(min(agree), 3) if agree else 0.0
        teams["attempts"] = len(ep["team_votes"])
        complete = len(teams["red"]) == 3 and len(teams["blue"]) == 3 and teams["agreement"] >= 0.6
        teams["complete"] = complete
        teams["t"] = round(t0, 3)
        ep["teams"] = teams
        if complete and (len(ep["team_votes"]) >= 2 and sum(
                1 for r in ep["team_votes"] if r["red"] == teams["red"] and r["blue"] == teams["blue"]) >= 2
                or teams["agreement"] >= 0.8):
            ep["teams_done"] = True
