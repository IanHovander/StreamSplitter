"""Overlay reader: the audience display's match timer, match title and team numbers, read by OCR. It works for any
event software (FMS, Cheesy Arena, ...) and any game.

Nothing here knows a layout, font, colour or sound. Every `full_every_s`, one whole frame is read: text detection plus
recognition (RapidOCR, ~0.45 s for a 1080p frame on a desktop CPU). Each text that reads as a time ("1:34", "0:11", or
a bare number up to PLAIN_MAX) becomes a candidate timer *place*. At `sample_fps`, every place is re-read by recognition
alone (~6 ms). A place whose value falls by one per second is a running timer, and its readings form countdown
*episodes*:

    an episode is a run of readings with (time of change + new value) = C; C is the stream time the display reads 0.

Displays round the remaining time up: they show 0:15 for the first second of a 15 s period and 0:00 at its end. So
the change to value v happens at C - v. C is the median over the episode's changes of (change time + new value). Each
change is placed midway between the last reading of the old value and the first of the new one (+-1/(2 sample_fps)).

An episode closes in three ways:
- its place stops changing for more than FROZEN_S: "stopped", or "zero" when it stopped at 0;
- its place cannot be read for LOST_S: "lost";
- two readings in a row break the countdown: "broken", and a new episode starts.

`from_value` is the value shown before the episode's first change: the period's length when the display sat at it
before the period began (FMS and Cheesy Arena both show the full auto time before a match).

Every whole-frame read is also kept as text (stream time, text, box) for naming: the match title ("Qualification 12")
and the team numbers.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from statistics import median

import cv2
import numpy as np

from .clock import decode, probe

log = logging.getLogger(__name__)

SAMPLE_FPS = 4.0
FULL_EVERY_S = 4.0
FROZEN_S = 1.6
LOST_S = 3.0
PLAIN_MAX = 30           # bare numbers above this are scores or team numbers, never a match timer
C_TOL_S = 1.0            # a change belongs to the episode when (change time + value) is within this of its C
PLACE_IDLE_S = 30.0      # a place that has not counted down for this long, and no whole-frame read still sees, is dropped
MAX_PLACES = 12
KEEP_TEXT_S = 1500.0
BREAK_READS = 6          # consecutive readings off the countdown, agreeing among themselves, that start a new episode
SLOW_EVERY = 4           # a place not yet seen counting is re-read on every 4th sample only (1/s at 4 fps)
PROMOTE_CHANGES = 5      # one-second changes that make a place a timer, re-read on every sample from then on
BACKFILL_S = 4.0         # a new place is also read on the samples since the last whole read (Eagan 2025: the
                         # overlay came on at 0:12 of auto; without this the first value seen was 0:10)
SKIP_MAX_S = 15.0        # BREAK_READS readings that agree, below the countdown by up to this much, are the same
                         # countdown after lost video: re-anchored, and `skipped` adds it up (MadTown 2026's
                         # practice-day Twitch VOD kept 61 of 94 min, losing 1-3 s at a time: a match's timer ran
                         # 0:53 -> 0:25 in 20 s of video, so every countdown broke into pieces and none was a match)

_MSS = re.compile(r"^\s*(\d{1,2})\s*[:;.]\s*(\d{2})\s*$")
_PLAIN = re.compile(r"^\s*(\d{1,2})\s*$")


def parse_time(txt: str | None) -> tuple[int, str] | None:
    """'1:34' -> (94, 'mss'); '15' -> (15, 'plain'); anything else None."""
    m = _MSS.match(txt or "")
    if m and int(m.group(2)) < 60:
        return int(m.group(1)) * 60 + int(m.group(2)), "mss"
    m = _PLAIN.match(txt or "")
    if m and int(m.group(1)) <= PLAIN_MAX:
        return int(m.group(1)), "plain"
    return None


def _engines():
    from rapidocr import RapidOCR
    # Two instances: a call with use_det=False changes the engine's later default calls (rapidocr 3.10).
    full = RapidOCR(params={"Global.use_cls": False, "Global.log_level": "warning"})
    rec = RapidOCR(params={"Global.use_cls": False, "Global.use_det": False, "Global.log_level": "warning"})
    return full, rec


class Place:
    """A screen box that showed a time in a whole-frame read, re-read every sample."""

    def __init__(self, pid: int, box, kind: str, t: float, w: int, h: int):
        x0, y0, x1, y1 = box
        bw, bh = x1 - x0, y1 - y0
        # wide enough for "0:11" and "10:00" alike, but kept off neighbouring text
        self.crop = (max(0, int(x0 - 0.35 * bw)), max(0, int(y0 - 0.2 * bh)),
                     min(w, int(x1 + 0.35 * bw)), min(h, int(y1 + 0.2 * bh)))
        self.box = box
        self.id, self.kind = pid, kind
        self.created = self.seen_full = self.last_count = t
        self.readings: list[tuple[float, int | None]] = []
        self.ep: dict | None = None          # the open episode
        self.closed: list[dict] = []
        self.promoted = False                # seen counting: re-read on every sample
        self.bad_run: list[tuple[float, int]] = []

    def centre(self):
        x0, y0, x1, y1 = self.box
        return 0.5 * (x0 + x1), 0.5 * (y0 + y1), y1 - y0

    # episodes ------------------------------------------------------------------------------------------------------
    def _new_ep(self, t: float, v: int) -> dict:
        return {"place": self.id, "box": list(self.box), "t_first": t, "t_last": t, "v_last": v, "t_v": t,
                "from_value": v, "changes": [], "C": None, "first_change": None, "skipped": 0.0, "early": []}

    def _close(self, reason: str) -> None:
        ep = self.ep
        self.ep = None
        if ep is None or not ep["changes"]:
            return
        ep["reason"] = "zero" if reason == "stopped" and ep["v_last"] == 0 else reason
        self.closed.append(_summary(ep, closed=True))

    def add(self, t: float, v: int | None) -> None:
        self.readings.append((t, v))
        ep = self.ep
        if v is None:
            if ep is not None and t - ep["t_last"] > LOST_S:
                self._close("lost")
            return
        if ep is None:
            self.ep = self._new_ep(t, v)
            return
        if v == ep["v_last"]:
            self.bad_run = []
            ep["t_last"] = t
            if t - ep["t_v"] > FROZEN_S and ep["changes"]:
                self._close("stopped")
                self.ep = self._new_ep(t, v)     # a frozen display may be the next period's start value
            elif not ep["changes"]:
                ep["from_value"] = v
            return
        mid = 0.5 * (ep["t_last"] + t)
        ok = v < ep["v_last"] and (ep["C"] is None or abs(mid + v - ep["C"]) <= C_TOL_S + (v < ep["v_last"] - 1))
        if ok and not ep["changes"] and ep["v_last"] - v > 1:
            ok = False                           # the first change must be a single step
        if ok:
            self.bad_run = []
            if ep["v_last"] - v == 1:
                if ep["first_change"] is None:
                    ep["first_change"] = mid
                if len(ep["early"]) < 5 and not ep["skipped"]:
                    ep["early"].append(mid + v)      # the zero it pointed to as it began (C0), never moved
                ep["changes"].append((mid, v))
                ep["C"] = median(m + x for m, x in ep["changes"][-40:])
            ep["v_last"], ep["t_v"], ep["t_last"] = v, t, t
            self.last_count = t
            return
        # inconsistent: a bad reading or two is an OCR slip (a misread can repeat for the second a value is shown);
        # BREAK_READS in a row that agree with each other start a new episode
        self.bad_run.append((t, v))
        if len(self.bad_run) >= BREAK_READS:
            run = self.bad_run[-BREAK_READS:]
            if self._lost_video(run):
                return
            if all(b[1] <= a[1] and abs((b[0] + b[1]) - (a[0] + a[1])) <= 1.3 + (a[1] == b[1]) * (b[0] - a[0])
                   for a, b in zip(run, run[1:])):
                self._close("broken")
                self.ep = self._new_ep(*run[0])
                self.bad_run = []
                for tb, vb in run[1:]:
                    self.add(tb, vb)

    def _lost_video(self, run: list[tuple[float, int]]) -> bool:
        """BREAK_READS readings below the open countdown that keep counting down and never fall back behind it (each
        one's t + v at most 1.3 s above the one before; lost video makes it drop): the display ran ahead because the
        stream lost video (SKIP_MAX_S). The countdown goes on from the last of them: its changes so far move with it,
        so C stays their median, and `skipped` adds the jump."""
        ep = self.ep
        if not ep["changes"] or ep["C"] is None or run[0][1] >= ep["v_last"]:
            return False
        if not all(b[1] <= a[1] and b[0] + b[1] <= a[0] + a[1] + 1.3 for a, b in zip(run, run[1:])):
            return False
        t, v = run[-1]
        jump = ep["C"] - (t + v - 0.5)          # v is shown for the second before C - v + 1: its middle
        if not 0.5 < jump <= SKIP_MAX_S:
            return False
        ep["changes"] = [(m - jump, x) for m, x in ep["changes"]]
        ep["C"] = median(m + x for m, x in ep["changes"][-40:])
        ep["skipped"] += jump
        ep["v_last"], ep["t_v"], ep["t_last"] = v, next(tb for tb, vb in run if vb == v), t
        self.last_count = t
        self.bad_run = []
        return True

    def episodes(self, now: float | None = None) -> list[dict]:
        """Closed episodes plus the open one (marked open)."""
        out = list(self.closed)
        if self.ep is not None and self.ep["changes"]:
            out.append(_summary(self.ep, closed=False))
        return out


def _summary(ep: dict, closed: bool) -> dict:
    """C: the stream time the display reads 0 (after any lost video). C0: the zero the countdown pointed to when it
    began (its first changes, as read), so its period began at C0 - its length even when video was lost since; C0 = C
    when none was."""
    ch = ep["changes"]
    skipped = ep.get("skipped", 0.0)
    first = ep.get("first_change")
    c0 = median(ep["early"]) if skipped and ep.get("early") else ep["C"]
    return {"place": ep["place"], "box": ep["box"], "t_first": round(ep["t_first"], 3), "t_last": round(ep["t_last"], 3),
            "C": round(ep["C"], 3), "C0": round(c0, 3), "skipped": round(skipped, 3),
            "from_value": ep["from_value"], "v_last": ep["v_last"], "n_changes": len(ch),
            "first_change": round(ch[0][0] if first is None else first, 3), "closed": closed,
            "reason": ep.get("reason")}


class OverlayReader:
    def __init__(self, sample_fps: float = SAMPLE_FPS, full_every_s: float = FULL_EVERY_S, threads: int = 0):
        self.sample_fps = sample_fps
        self.full_every_s = full_every_s
        self.threads = threads
        self.full, self.rec = _engines()
        self.places: list[Place] = []
        self.dropped: list[Place] = []
        self.texts: list[dict] = []          # whole-frame reads: {"t", "text", "box", "score"}
        self.thumbs: list[tuple[float, np.ndarray]] = []   # every sample, 32x18 grey: scene changes
        self._next_id = 0
        self._last_full = -1e9
        # the previous chunk's last samples (t, frame), so a place found near a chunk's start is backfilled too
        self._tail: deque = deque(maxlen=int(BACKFILL_S * sample_fps))
        self.stats = {"chunks": 0, "full_reads": 0, "rec_reads": 0}

    # ----------------------------------------------------------------------------------------------------------------
    def feed(self, path, t0: float) -> dict:
        """Read one chunk; returns per-chunk stats."""
        info = probe(path)
        if info is None:
            return {"frames": 0}
        w, h = info["w"], info["h"]
        pending: list[tuple[int, list | None, dict[int, int | None], np.ndarray]] = []
        step = 1.0 / self.sample_fps
        recent: deque = deque(maxlen=int(BACKFILL_S * self.sample_fps))   # (index into pending, frame)
        before: list[tuple[int, float, int | None]] = []     # (place id, t, value) on the previous chunk's last frames

        def on_frame(k: int, fr: np.ndarray) -> None:
            t_est = t0 + k * step               # true pts are known after the decode; this only paces whole reads
            full = None
            if t_est - self._last_full >= self.full_every_s - 1e-6:
                self._last_full = t_est
                full = self._read_full(fr)
                self.stats["full_reads"] += 1
                for p in self._update_places(full, t_est, w, h):
                    for i, old in recent:                # read the new place on the frames just before
                        pending[i][2][p.id] = self._read_place(p, old)
                    for t_old, old in self._tail:        # ... reaching back into the previous chunk
                        if t_old > t_est - BACKFILL_S:
                            before.append((p.id, t_old, self._read_place(p, old)))
            vals = {}
            for p in self.places:
                if not p.promoted:
                    if p.ep is not None and len(p.ep["changes"]) >= PROMOTE_CHANGES:
                        p.promoted = True
                    elif k % SLOW_EVERY:
                        continue
                vals[p.id] = self._read_place(p, fr)
            thumb = cv2.resize(cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY), (32, 18), interpolation=cv2.INTER_AREA)
            pending.append((k, full, vals, thumb))
            recent.append((len(pending) - 1, fr))

        pts, _, st = decode(path, skip=None, pix_fmt="bgr24", min_spacing=step - 1e-3, threads=self.threads,
                            size=(w, h), on_frame=on_frame)
        by_id = {p.id: p for p in self.places}
        for pid, t, v in sorted(before, key=lambda x: x[1]):     # earlier than every reading in this chunk
            if pid in by_id:
                by_id[pid].add(t, v)
        for i, fr in recent:
            if pending[i][0] < len(pts):
                self._tail.append((t0 + pts[pending[i][0]] - info["start"], fr))
        for k, full, vals, thumb in pending:
            if k >= len(pts):
                break
            t = t0 + pts[k] - info["start"]
            self.thumbs.append((t, thumb))
            if full is not None:
                for txt, box, sc in full:
                    self.texts.append({"t": round(t, 3), "text": txt, "box": box, "score": round(sc, 3)})
            for pid, v in vals.items():
                if pid in by_id:
                    by_id[pid].add(t, v)
        t_end = t0 + (pts[-1] - info["start"] if pts else 0.0)
        self._prune(t_end)
        self.stats["chunks"] += 1
        return {"frames": len(pts), "ffmpeg_cpu_s": st.get("ffmpeg_cpu_s"), "places": len(self.places)}

    def _read_full(self, fr: np.ndarray) -> list[tuple[str, list[int], float]]:
        r = self.full(fr)
        out = []
        if r.txts:
            for box, txt, sc in zip(r.boxes, r.txts, r.scores):
                b = [int(box[:, 0].min()), int(box[:, 1].min()), int(box[:, 0].max()), int(box[:, 1].max())]
                out.append((txt, b, float(sc)))
        return out

    def _read_place(self, p: Place, fr: np.ndarray) -> int | None:
        x0, y0, x1, y1 = p.crop
        r = self.rec(np.ascontiguousarray(fr[y0:y1, x0:x1]))
        self.stats["rec_reads"] += 1
        pt = parse_time(r.txts[0] if r.txts else None)
        return pt[0] if pt else None

    def _update_places(self, full, t: float, w: int, h: int) -> list[Place]:
        """Places for the times in a whole read; returns the new ones."""
        new = []
        for txt, b, sc in full:
            pt = parse_time(txt)
            if pt is None or sc < 0.6:
                continue
            cx, cy, bh = 0.5 * (b[0] + b[2]), 0.5 * (b[1] + b[3]), b[3] - b[1]
            hit = None
            for p in self.places:
                px, py, ph = p.centre()
                if abs(px - cx) < 0.8 * ph + 0.3 * (b[2] - b[0]) and abs(py - cy) < 0.5 * ph and 0.6 < bh / max(ph, 1) < 1.6:
                    hit = p
                    break
            if hit is not None:
                hit.seen_full = t
                continue
            if len(self.places) >= MAX_PLACES:
                continue
            self.places.append(Place(self._next_id, b, pt[1], t, w, h))
            new.append(self.places[-1])
            self._next_id += 1
        return new

    def _prune(self, now: float) -> None:
        keep = []
        for p in self.places:
            idle = now - max(p.last_count, p.seen_full)
            if idle > PLACE_IDLE_S and (p.ep is None or not p.ep["changes"]):
                self.dropped.append(p)
            else:
                keep.append(p)
        self.places = keep
        self.dropped = [p for p in self.dropped if p.closed and now - p.closed[-1]["t_last"] < KEEP_TEXT_S]
        self.texts = [x for x in self.texts if now - x["t"] < KEEP_TEXT_S]
        self.thumbs = [x for x in self.thumbs if now - x[0] < KEEP_TEXT_S]
        for p in self.places:
            if len(p.readings) > 20000:
                p.readings = p.readings[-5000:]

    # ----------------------------------------------------------------------------------------------------------------
    def episodes(self) -> list[dict]:
        out = [e for p in self.dropped for e in p.closed]
        for p in self.places:
            out += p.episodes()
        return sorted(out, key=lambda e: e["first_change"])

    def texts_between(self, a: float, b: float) -> list[dict]:
        return [x for x in self.texts if a <= x["t"] <= b]

    def full_reads(self, a: float, b: float) -> list[tuple[float, list[dict]]]:
        """Whole-frame reads in [a, b]: (time, its texts), in time order (a read with no text is not listed)."""
        by_t: dict[float, list[dict]] = {}
        for x in self.texts:
            if a <= x["t"] <= b:
                by_t.setdefault(x["t"], []).append(x)
        return sorted(by_t.items())

    def scene_change(self, a: float, b: float) -> float | None:
        """Time of the biggest picture change between samples in (a, b] (the first sample after it), or None."""
        th = [(t, im) for t, im in self.thumbs if a - 0.3 <= t <= b]
        if len(th) < 2:
            return None
        d = [float(np.abs(y.astype(np.int16) - x.astype(np.int16)).mean()) for (_, x), (_, y) in zip(th, th[1:])]
        i = int(np.argmax(d))
        return th[i + 1][0] if d[i] >= 8.0 else None

    def _thumb_at(self, t: float):
        return min(self.thumbs, key=lambda x: abs(x[0] - t))[1] if self.thumbs else None

    def arrived(self, ref_t: float, a: float, thr: float = 12.0) -> float | None:
        """When the picture at `ref_t` came on: the first sample of the unbroken run, back to `a`, that looks like it."""
        ref = self._thumb_at(ref_t)
        if ref is None:
            return None
        th = [(t, im) for t, im in self.thumbs if a <= t <= ref_t]
        on = None
        for t, im in reversed(th):
            if float(np.abs(im.astype(np.int16) - ref.astype(np.int16)).mean()) > thr:
                break
            on = t
        return on

    def left(self, ref_t: float, b: float, thr: float = 12.0) -> float | None:
        """When the picture at `ref_t` went away: the first sample after it, up to `b`, that does not look like it."""
        ref = self._thumb_at(ref_t)
        if ref is None:
            return None
        for t, im in self.thumbs:
            if ref_t < t <= b and float(np.abs(im.astype(np.int16) - ref.astype(np.int16)).mean()) > thr:
                return t
        return None

    def timer_read_near(self, place_id: int, t: float, tol: float = 1.0) -> bool:
        """Whether the timer place read any time within `tol` s of `t` (the in-match overlay is still up)."""
        for p in self.places + self.dropped:
            if p.id == place_id:
                return any(v is not None and abs(rt - t) <= tol for rt, v in p.readings[-4000:])
        return False
