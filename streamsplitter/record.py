"""Live recorder: a YouTube or Twitch live stream -> ~20 s MPEG-TS chunks + a chunk index the pipeline polls.

    streamsplitter record URL [--also URL2 ...] [--tag TAG] [--event KEY] [--out DIR] [--keep-hours 28]
                              [--max-height 1080] [--chunk-s 20] [--poll-s 45] [--stall-s 60] [--rewind-s 0]
                              [--recheck-s 300] [--duration S] [--idle-exit MIN]

URL is a video (youtube.com/watch?v=..., youtu.be/...), a channel's live page (youtube.com/@Name/live,
youtube.com/channel/<id>/live) or a Twitch channel (twitch.tv/<name>). While nothing is live the recorder polls every
--poll-s seconds. It follows the channel across stream drops, restarts and new videos, and keeps writing the same
index. --also: the event's other links (live passes the TBA webcasts that show what is live today, e.g. its Twitch
channel), tried in order whenever URL is not live. Twitch
(checked 2026-11-06): HLS with PDT on every 2 s segment, muxed 1080p60; an affiliate or partner channel opens
every connection with a ~30 s stitched ad (recorded like the stream, 30 fps, timestamps continuous), a plain channel
(MadTown's) with none. Chunks go to <work_root>/live/<tag>/ (config.work_root()); stop it with Ctrl+C.

Several links: the best one that works (user, 2026-11-10)
--------------------------------------------------------
MadTown 2026 was recorded from Twitch for ten hours on Saturday (lost video every few seconds, wasted uploads) while
a good YouTube stream of the same field ran, and on Sunday morning from an official YouTube video that said it was
live and sent nothing, one minute at a time, 18 times. So the links are a preference list, best first: URL and its
--also ones, or links.json in the chunk folder when `live` wrote one for this URL ({"for": URL, "links": [...]},
YouTube before Twitch; read again at every look, so `live` can add a stream it finds during the day without
restarting the recorder).

- **Looking for a better one.** While a link other than the first is being recorded, every --recheck-s (300; 0 =
  never) the links before it are resolved in a side thread. One that is live must also be *delivering*: its playlist
  must gain a segment within FLOW_WAIT_S (flowing()). Then the run is stopped ("a better stream is live") and the next
  run records that link from the wall-clock time the recording ended at, so nothing is missing (YouTube keeps an hour).
  A look waits (up to HOLD_MAX_S) while `live` says a match timer is counting (match_on.json): the two streams differ
  by a few seconds, which would put a jump into that match's clip.
- **A link that fails is tried last for a while.** A run that ends by itself within HEALTHY_S (the stream stopped
  or stalled), a fetch that will not start, or WAIT_GIVE_UP_S with no new segment puts its link behind the others
  and out of the looks for COOL_S, doubling up to COOL_MAX_S each time it fails again; a run of HEALTHY_S clears it.
  A run started by a look stalls after PROBATION_STALL_S without data instead of --stall-s, so a wrong choice costs
  about 20 s of the other stream (Twitch keeps only ~10 s to refill from).

How it records
--------------
yt-dlp (Python API, in-process) resolves the URL to HLS playlists: on YouTube (2026-11) a video-only H.264 rendition
(the best at or below --max-height) plus the AAC "high" audio rendition; on Twitch one muxed playlist. Each "run"
(one connection to the stream) uses two ffmpeg processes and this process in between:

    fetch    ffmpeg -i video.m3u8 [-i audio.m3u8] -c copy -copyts -f mpegts pipe:1
    relay    this process copies fetch's stdout into segment's stdin
    segment  ffmpeg -f mpegts -i pipe:0 -c copy -f segment -segment_time 20 -segment_format mpegts
                    -initial_offset <t_base> -segment_list rNNN.csv rNNN_%05d.ts

Stream copy only (no re-encode); the segment muxer cuts only at video keyframes, so every chunk starts on one. The
relay is there for robustness on every OS: whatever kills this process, `segment` sees the end of its input, closes
its last chunk cleanly and writes it to its list, and `fetch` gets a broken pipe and exits, so no orphaned ffmpeg
keeps recording. The next start lists ("salvages") that last chunk. `-copyts` in fetch keeps the separate YouTube
audio and video playlists in sync; `segment` re-bases the stream to t_base and smooths backward timestamp jumps,
while forward jumps of up to 10 min (segments the platform failed to serve) are kept as real holes, not hidden.

Stream time, wall time and gaps
-------------------------------
Stream time t is "wall seconds since the index's origin", continuous inside a run. The top-level
`release_timestamp` is the wall time of t = 0, so wall0 = release_timestamp + t0 for every chunk (up to the gap
estimate below); the namer's `release_ts + t` keeps working across restarts. YouTube and Twitch playlists carry
EXT-X-PROGRAM-DATE-TIME (PDT): the time the platform took in each segment. The recorder reads which segment fetch
opened first (fetch's log) and pins that run's wall clock to its PDT (`wall_source: "pdt"`). Without PDT it uses the
arrival time minus the playlist backlog (`"arrival"`, a few seconds rougher). Either way wall0 is the platform's
ingest time, not the venue time: the encoder's own delay (a few s) remains and is still the namer's to fit.

A new run (after a drop, a stall, a URL refresh, a recorder restart or a new video) starts at
t_base = max(end of the index, its own first frame's wall time - release_timestamp), so the jump in t equals the
real time missed. When the previous run is PDT-pinned, the new run asks ffmpeg to start at the first playlist
segment that begins at or after the end of what was recorded, so a short drop is refilled from the platform's DVR
window (YouTube keeps about an hour; Twitch about 10 s) and the gap is then 0 to one segment (5 s on YouTube).

Index (index.json, rewritten atomically: temp file + os.replace)
---------------------------------------------------------------
Same keys as phase0/fake_live.py, plus live-only ones. A chunk is listed only when its file is complete.

    tag, event, date (local date the index was made), video (first video id), release_timestamp (wall time of t=0),
    chunks: [{seq, path, t0, t1, wall0,                       # as in fake_live; path relative to the index folder
              wall_closed,      # unix time the chunk was finished and listed (salvaged: the file's mtime)
              run,              # run number ("segment of stream"); a new run = a new connection
              source_video,     # video id the chunk came from (a new id = the stream restarted as a new video)
              gap_before_s,     # t0 minus the previous listed chunk's t1: 0.0 when continuous (under 20 ms),
                                # > 0 = real time missing before this chunk (wall-clock estimate across runs),
                                # null for the index's first chunk
              salvaged}]        # only present (true) on chunks listed after a crash of the recorder
    live: true, source_url, chunk_s, keep_hours, status (recording | waiting | stopped), updated (heartbeat),
    next_seq, t_end / wall_end (end of everything ever listed), pruned_through_seq,
    runs: [{run, video, title, webpage_url, link (the link of the list this run records), video_release_timestamp,
            format, started, t_base, gap_before_s,
            wall_source, wall_offset (wall = t + wall_offset), start_index, first_segment, edge_lag_s, csv, log,
            ended, end_reason, n_chunks, holes}]

seq is never reused and stays contiguous; old chunks are deleted (and dropped from `chunks`) once their wall_closed
is more than keep_hours old, never sooner. Readers should key on seq, not on list position.

Within a run, t1 of chunk n equals t0 of chunk n+1 exactly, except when the platform failed to serve segments: a hole
that lands on a chunk boundary shows as gap_before_s > 0; one inside a chunk makes that chunk's t1 - t0 longer than
its pictures (the run's `holes` counts them). The first chunk of a run starts at its first packet, which can be a few
frames (B-frames, audio first) before its first video frame, as in fake_live. In the files, the MPEG-TS timestamps are
t plus a constant (ffmpeg's 1.4 s mux delay) in every run, so the cutter can join chunks across runs; they wrap every
26.5 h, which ffmpeg unwraps when the chunks are read in order.

JavaScript runtime (yt-dlp's Deno warning), checked 2026-11-04 with yt-dlp 2026.10.26: live HLS resolves without
any JS runtime (yt-dlp warns that JS-less YouTube extraction is deprecated); with Node enabled the formats are the
same. The recorder enables any of deno/node/bun it finds on PATH, which silences the warning; none is required.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import io
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, config, ffmpeg

CHUNK_S = 20                 # a clip waits for the chunk holding its end padding (60 s: up to a minute more)
KEEP_HOURS = 28.0
POLL_S = 45.0
STALL_S = 60.0
MAX_RESUME_S = 7200.0
INDEX_NAME = "index.json"
LINKS_NAME = "links.json"    # written by live: {"for": URL, "links": [every link of the event, best first]}
MATCH_ON_NAME = "match_on.json"   # written by live after each chunk: {"counting": bool, "wall": that chunk's end}
RECHECK_S = 300.0            # while a lesser link is recorded, look this often for a better one (0: never)
FLOW_WAIT_S = 12.0           # a better link's playlist must gain a segment within this (or 3 target durations)
FOUND_FRESH_S = 120.0        # a better link found this long ago is resolved again rather than trusted
HOLD_MAX_S = 240.0           # a look waits at most this long for the match being played to end
MATCH_ON_FRESH_S = 90.0
COOL_S = 300.0               # a link that just failed: tried after the others and left out of the looks for this
COOL_MAX_S = 3600.0          # long, doubling up to this each time it fails again
HEALTHY_S = 600.0            # a run this long clears its link's cool-down; a shorter one that ends by itself is a failure
WAIT_GIVE_UP_S = 90.0        # "live" with no segment newer than the recording for this long: a failure
PROBATION_STALL_S = 20.0     # a run started by a look is dropped after this long without data (for HEALTHY_S)
_NOT_THE_LINKS_FAULT = ("stopped", "a better stream", "stream URL about to expire", "segment ffmpeg")

log = logging.getLogger("streamsplitter.record")


# ----------------------------------------------------------------------------------------------------------------
# Index bookkeeping (no network, no ffmpeg: unit-tested in tests/test_record.py)
# ----------------------------------------------------------------------------------------------------------------

def write_json_atomic(path: Path, obj, retries: int = 100) -> None:
    """Write `obj` as JSON to `path` so a reader sees either the old or the new file, never half of one.

    On Windows os.replace fails while a reader has the file open; the readers hold it for milliseconds, so retry."""
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    for i in range(retries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == retries - 1:
                raise
            time.sleep(0.02)


def read_segment_list(path: Path) -> list[tuple[str, float, float]]:
    """Entries (file name, start s, end s) of an ffmpeg segment list in csv form; a half-written last line is ignored."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    if not text.endswith("\n"):
        text = text[: text.rfind("\n") + 1]
    out = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) == 3:
            try:
                out.append((row[0], float(row[1]), float(row[2])))
            except ValueError:
                continue
    return out


GAP_EPS = 0.02


def _snap(gap: float) -> float:
    """ffmpeg's segment list ends a chunk up to a frame (or 0.3 ms of audio) before the next one starts; call any gap
    under 20 ms continuous (0.0). Larger values are real time missing."""
    return 0.0 if abs(gap) < GAP_EPS else round(gap, 6)


def chunk_name(run: int, k: int) -> str:
    return f"r{run:03d}_{k:05d}.ts"


def ready_entries(entries: list, done: int, folder: Path, run: int, finished: bool) -> list:
    """The entries past the first `done` whose files are complete. ffmpeg writes a list line just before it closes
    that file and opens the next one, so a file is complete once the next file exists or ffmpeg has exited."""
    out = []
    for k in range(done, len(entries)):
        if not finished and not (Path(folder) / chunk_name(run, k + 1)).exists():
            break
        out.append(entries[k])
    return out


class LiveIndex:
    """index.json of one live recording: chunks, runs, and pruning. Pure bookkeeping; the caller saves."""

    def __init__(self, folder: Path, data: dict):
        self.folder = Path(folder)
        self.data = data

    @classmethod
    def open(cls, folder: Path, tag: str, *, event: str | None = None, source_url: str | None = None,
             keep_hours: float = KEEP_HOURS, chunk_s: float = CHUNK_S) -> "LiveIndex":
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / INDEX_NAME
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if event:
                data["event"] = event
            if source_url:
                data["source_url"] = source_url
            data["keep_hours"] = keep_hours
        else:
            data = {"tag": tag, "event": event, "date": _dt.date.today().isoformat(), "video": None,
                    "release_timestamp": None, "chunks": [], "live": True, "source_url": source_url,
                    "chunk_s": chunk_s, "keep_hours": keep_hours, "status": "starting", "updated": None,
                    "next_seq": 0, "t_end": None, "wall_end": None, "wall_end_source": None,
                    "pruned_through_seq": -1, "recorder": {"version": __version__, "host": socket.gethostname()},
                    "runs": []}
        return cls(folder, data)

    @property
    def path(self) -> Path:
        return self.folder / INDEX_NAME

    @property
    def chunks(self) -> list[dict]:
        return self.data["chunks"]

    @property
    def runs(self) -> list[dict]:
        return self.data["runs"]

    def save(self, now: float | None = None) -> None:
        self.data["updated"] = round(time.time() if now is None else now, 3)
        write_json_atomic(self.path, self.data)

    def resume_wall(self) -> float | None:
        """Wall time (PDT) of the end of everything recorded, when it is PDT-pinned; else None."""
        return self.data["wall_end"] if self.data.get("wall_end_source") == "pdt" else None

    def begin_run(self, *, anchor_wall: float, wall_source: str, video: str | None, now: float | None = None,
                  **info) -> dict:
        """Open run k whose first frame has wall time `anchor_wall`; sets its t_base and the gap before it."""
        now = time.time() if now is None else now
        d = self.data
        t_end = d.get("t_end")
        if t_end is None:                       # nothing listed yet: this run defines t = 0
            d["release_timestamp"] = round(anchor_wall, 3)
            t_base, gap, overlap = 0.0, None, 0.0
        else:
            want = anchor_wall - d["release_timestamp"]
            overlap = max(0.0, t_end - want)
            t_base = max(want, t_end)
            gap = t_base - t_end
        run = {"run": len(self.runs), "video": video, **info, "started": round(now, 3),
               "t_base": round(t_base, 6), "gap_before_s": None if gap is None else round(_snap(gap), 3),
               "wall_source": wall_source, "wall_offset": round(anchor_wall - t_base, 3),
               "ended": None, "end_reason": None, "n_chunks": 0, "holes": 0, "skipped_segments": 0}
        if overlap > 0.0:
            run["overlap_s"] = round(overlap, 3)
        self.runs.append(run)
        if d.get("video") is None:
            d["video"] = video
        return run

    def add_chunk(self, run: dict, name: str, csv_t0: float, csv_t1: float, wall_closed: float,
                  salvaged: bool = False) -> dict:
        """List one finished chunk of `run` (times from the run's segment list, which start at 0 for the run)."""
        d = self.data
        t0, t1 = round(run["t_base"] + csv_t0, 6), round(run["t_base"] + csv_t1, 6)
        prev = d.get("t_end")
        gap = None if prev is None else _snap(t0 - prev)
        if run["n_chunks"] > 0 and gap is not None and gap > 0.0:
            run["holes"] += 1
        c = {"seq": d["next_seq"], "path": name, "t0": t0, "t1": t1,
             "wall0": round(t0 + run["wall_offset"], 3), "wall_closed": round(wall_closed, 3),
             "run": run["run"], "source_video": run.get("video"), "gap_before_s": gap}
        if salvaged:
            c["salvaged"] = True
        self.chunks.append(c)
        d["next_seq"] += 1
        d["t_end"] = max(t1, prev if prev is not None else t1)
        d["wall_end"] = round(t1 + run["wall_offset"], 3)
        d["wall_end_source"] = run["wall_source"]
        run["n_chunks"] += 1
        return c

    def end_run(self, run: dict, reason: str, now: float | None = None) -> None:
        run["ended"] = round(time.time() if now is None else now, 3)
        run["end_reason"] = reason

    def open_runs(self) -> list[dict]:
        return [r for r in self.runs if r.get("ended") is None]

    def salvage(self, run: dict, now: float | None = None) -> list[dict]:
        """List the chunks a stopped recorder left in `run`'s segment list but never listed, then close the run.

        Their segment ffmpeg finished them after the recorder died (end of input), so they are complete; their
        wall_closed is the file's modification time."""
        entries = read_segment_list(self.folder / run["csv"])
        out = []
        for name, a, b in entries[run["n_chunks"]:]:
            f = self.folder / name
            if not f.is_file():
                continue
            out.append(self.add_chunk(run, name, a, b, f.stat().st_mtime, salvaged=True))
        self.end_run(run, f"recorder stopped; {len(out)} chunk(s) salvaged", now)
        return out

    def prune(self, now: float | None = None) -> list[str]:
        """Delete chunks finished more than keep_hours ago (and stray unlisted chunk files older than that)."""
        now = time.time() if now is None else now
        cutoff = now - float(self.data["keep_hours"]) * 3600.0
        removed, keep = [], []
        for c in self.chunks:
            if c["wall_closed"] < cutoff:
                try:
                    (self.folder / c["path"]).unlink(missing_ok=True)
                except OSError:                    # open by a reader (Windows): try again next time
                    keep.append(c)
                    continue
                removed.append(c["path"])
                self.data["pruned_through_seq"] = max(self.data.get("pruned_through_seq", -1), c["seq"])
            else:
                keep.append(c)
        self.data["chunks"] = keep
        listed = {c["path"] for c in keep}
        for f in self.folder.glob("r[0-9][0-9][0-9]_*.ts"):
            try:
                if f.name not in listed and f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed.append(f.name)
            except OSError:
                pass
        live_runs = {c["run"] for c in keep}
        for r in self.runs:
            if r.get("ended") is not None and r["ended"] < cutoff and r["run"] not in live_runs:
                for key in ("csv", "log"):
                    if r.get(key):
                        try:
                            (self.folder / r[key]).unlink(missing_ok=True)
                        except OSError:
                            pass
        return removed


# ----------------------------------------------------------------------------------------------------------------
# HLS playlists
# ----------------------------------------------------------------------------------------------------------------

def _iso(s: str) -> float | None:
    try:
        return _dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_playlist(text: str, base_url: str = "") -> dict:
    """A media playlist: {media_seq, target, ended, segments: [{uri, seq, dur, pdt}]}. PDT (unix s) is given per
    segment when the playlist tags it, or carried forward from the last tagged segment (YouTube tags only the first)."""
    lines = [x.strip() for x in text.splitlines()]
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise ValueError("not an m3u8 playlist")
    media_seq, target, ended, master = 0, None, False, False
    segs: list[dict] = []
    pdt = dur = None
    for line in lines[1:]:
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            media_seq = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            target = float(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            pdt = _iso(line.split(":", 1)[1])
        elif line.startswith("#EXTINF:"):
            dur = float(line[8:].split(",")[0])
        elif line.startswith("#EXT-X-ENDLIST"):
            ended = True
        elif line.startswith("#EXT-X-STREAM-INF"):
            master = True
        elif not line.startswith("#"):
            if pdt is None and segs and segs[-1]["pdt"] is not None:
                pdt = segs[-1]["pdt"] + segs[-1]["dur"]
            segs.append({"uri": urllib.parse.urljoin(base_url, line), "seq": media_seq + len(segs),
                         "dur": dur if dur is not None else (target or 0.0), "pdt": pdt})
            pdt = dur = None
    return {"media_seq": media_seq, "target": target or 5.0, "ended": ended, "master": master, "segments": segs}


def has_pdt(pl: dict | None) -> bool:
    return bool(pl and pl["segments"] and all(s["pdt"] is not None for s in pl["segments"]))


def pick_start(pl: dict, resume_wall: float | None, rewind_s: float = 0.0, max_resume_s: float = MAX_RESUME_S,
               default: int = -3) -> int | None:
    """ffmpeg -live_start_index for this playlist. Resume: the first segment that starts at or after `resume_wall`
    (no older than max_resume_s before the live edge); None when that segment does not exist yet (wait for it).
    Fresh start: `rewind_s` back from the live edge, or `default` (ffmpeg's own: three segments from the end)."""
    segs = pl["segments"]
    if not segs or not has_pdt(pl):
        return default
    edge = segs[-1]["pdt"] + segs[-1]["dur"]
    if resume_wall is not None:
        floor = max(resume_wall, edge - max_resume_s) - 0.25
        for i, s in enumerate(segs):
            if s["pdt"] >= floor:
                return i
        return None
    if rewind_s > 0:
        for i, s in enumerate(segs):
            if s["pdt"] >= edge - rewind_s:
                return i
    return default


_SQ = re.compile(r"/sq/(\d+)")


def match_segment(url: str, pl: dict | None) -> dict | None:
    """The playlist segment ffmpeg opened at `url` (exact URL, or YouTube's /sq/<n>/ sequence number)."""
    if not pl:
        return None
    for s in pl["segments"]:
        if s["uri"] == url:
            return s
    m = _SQ.search(url)
    if m and pl["segments"] and _SQ.search(pl["segments"][0]["uri"]):
        n = int(m.group(1))
        for s in pl["segments"]:
            m2 = _SQ.search(s["uri"])
            if m2 and int(m2.group(1)) == n and _same_rendition(url, s["uri"]):
                return s
    return None


def _same_rendition(a: str, b: str) -> bool:
    ia, ib = re.search(r"/itag/(\d+)", a), re.search(r"/itag/(\d+)", b)
    return (ia is None or ib is None) or ia.group(1) == ib.group(1)


def fetch_text(url: str, headers: dict | None = None, timeout: float = 15.0) -> str:
    req = urllib.request.Request(url, headers=dict(headers or {}))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_playlist(url: str | None, headers: dict | None = None) -> dict | None:
    if not url:
        return None
    try:
        pl = parse_playlist(fetch_text(url, headers), url)
    except Exception as e:  # noqa: BLE001 - any failure just means "no playlist info"
        log.info("  could not read the playlist (%s)", _short(e))
        return None
    return None if pl["master"] else pl


# ----------------------------------------------------------------------------------------------------------------
# Resolving the URL with yt-dlp
# ----------------------------------------------------------------------------------------------------------------

class NotLive(Exception):
    """The URL has nothing live right now (offline channel, upcoming, ended, or yt-dlp could not resolve it)."""


@dataclass
class Source:
    video: str
    title: str
    webpage_url: str
    extractor: str
    video_release_ts: float | None
    video_url: str                  # HLS media playlist with the video (muxed on Twitch)
    audio_url: str | None           # separate audio playlist (YouTube), else None
    headers: dict = field(default_factory=dict)
    fmt: dict = field(default_factory=dict)
    expire: float | None = None     # unix time the URLs stop working, when the URL says


class _YdlLog:
    """Route yt-dlp's messages to our log; each distinct warning once."""

    def __init__(self):
        self.seen: set[str] = set()

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        if msg not in self.seen:
            self.seen.add(msg)
            log.info("  yt-dlp: %s", msg)

    def error(self, msg):
        pass


_YDL_LOG = _YdlLog()


def js_runtimes() -> dict:
    """JavaScript runtimes yt-dlp may use: whichever of deno/node/bun is on PATH (none is needed for live HLS)."""
    return {name: {} for name in ("deno", "node", "bun") if shutil.which(name)}


def format_selector(max_height: int) -> str:
    h = int(max_height)
    hls = "[protocol^=m3u8]"
    return "/".join([f"bv[height<={h}][vcodec^=avc1]{hls}+ba{hls}", f"b[height<={h}][vcodec^=avc1]{hls}",
                     f"bv[height<={h}]{hls}+ba{hls}", f"b[height<={h}]{hls}", f"b{hls}"])


def _desc(f: dict) -> str:
    if f.get("vcodec") not in (None, "none"):
        parts = [f"{f.get('width')}x{f.get('height')}", f"{f.get('fps') or '?'}fps", str(f.get("vcodec"))]
        if f.get("acodec") not in (None, "none"):
            parts.append(str(f.get("acodec")))
    else:
        parts = [str(f.get("acodec") or "audio"), str(f.get("format_note") or "")]
    return " ".join(p for p in parts if p) + f" ({f.get('format_id')})"


def _expire(url: str) -> float | None:
    m = re.search(r"/expire/(\d+)", url) or re.search(r"[?&]expire=(\d+)", url)
    return float(m.group(1)) if m else None


def resolve(url: str, max_height: int = 1080) -> Source:
    """Resolve a video / channel-live / Twitch URL to the live HLS playlists, or raise NotLive."""
    import yt_dlp

    if "://" not in url:
        url = "https://" + url
    params = {"quiet": True, "no_warnings": False, "noplaylist": True, "skip_download": True,
              "format": format_selector(max_height), "logger": _YDL_LOG, "socket_timeout": 20}
    rt = js_runtimes()
    if rt:
        params["js_runtimes"] = rt
    try:
        with yt_dlp.YoutubeDL(params) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise NotLive(_short(e)) from None
    if info.get("_type") == "playlist" or not info.get("formats"):
        raise NotLive("no live video at this URL")
    status = info.get("live_status") or ("is_live" if info.get("is_live") else None)
    if status != "is_live":
        raise NotLive(f"{info.get('id')}: {status or 'not live'}")
    req = info.get("requested_formats") or [info]
    vid = next((f for f in req if f.get("vcodec") not in (None, "none")), req[0])
    aud = next((f for f in req if f is not vid and f.get("vcodec") in (None, "none")), None)
    if not str(vid.get("protocol", "")).startswith("m3u8"):
        raise NotLive(f"{info.get('id')}: no HLS format offered ({vid.get('protocol')})")
    return Source(video=str(info.get("id")), title=str(info.get("title") or ""),
                  webpage_url=str(info.get("webpage_url") or url), extractor=str(info.get("extractor_key") or ""),
                  video_release_ts=info.get("release_timestamp") or info.get("timestamp"),
                  video_url=vid["url"], audio_url=aud["url"] if aud else None,
                  headers=dict(vid.get("http_headers") or info.get("http_headers") or {}),
                  fmt={"video": _desc(vid), "audio": _desc(aud) if aud else "muxed"},
                  expire=_expire(vid["url"]))


def _short(e) -> str:
    s = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    s = re.sub(r"https?://\S+", lambda m: urllib.parse.urlsplit(m.group(0)).netloc + "/...", s)
    return s.replace("ERROR: ", "")[:300]


# ----------------------------------------------------------------------------------------------------------------
# ffmpeg command lines
# ----------------------------------------------------------------------------------------------------------------

def _http_args(headers: dict) -> list[str]:
    ua = next((v for k, v in headers.items() if k.lower() == "user-agent"), None)
    rest = "".join(f"{k}: {v}\r\n" for k, v in headers.items() if k.lower() != "user-agent")
    out = ["-user_agent", ua] if ua else []
    return out + (["-headers", rest] if rest else [])


_HELP_CACHE: dict[tuple[str, str], str] = {}


def ffmpeg_has(ff: str, topic: str, option: str) -> bool:
    """Whether this ffmpeg build knows `option` (e.g. topic 'demuxer=hls'); older builds lack some."""
    key = (ff, topic)
    if key not in _HELP_CACHE:
        try:
            _HELP_CACHE[key] = subprocess.run([ff, "-hide_banner", "-h", topic], capture_output=True, text=True,
                                              timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired):
            _HELP_CACHE[key] = ""
    return f"-{option} " in _HELP_CACHE[key]


def fetch_cmd(ff: str, src: Source, start_video: int | None, start_audio: int | None,
              seg_retry: bool = True) -> list[str]:
    """fetch: the live HLS playlist(s) -> one MPEG-TS byte stream on stdout, source timestamps kept (-copyts)."""
    cmd = [ff, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info"]

    def inp(url, start):
        # YouTube redirects every segment to another host, so keep-alive only fails and retries: switch it off.
        a = ["-rw_timeout", "20000000", "-http_persistent", "0", *(["-seg_max_retry", "3"] if seg_retry else []),
             *_http_args(src.headers)]
        if start is not None:
            a += ["-live_start_index", str(start)]
        return a + ["-i", url]

    cmd += inp(src.video_url, start_video)
    if src.audio_url:
        cmd += inp(src.audio_url, start_audio)
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
    return cmd + ["-c", "copy", "-copyts", "-f", "mpegts", "pipe:1"]


def segment_cmd(ff: str, folder: Path, run: int, t_base: float | None, chunk_s: float) -> list[str]:
    """segment: stdin -> rNNN_%05d.ts chunks at keyframes every ~chunk_s, listed in rNNN.csv as each one closes.

    Timestamps start at 0 for the run (forward jumps up to 600 s kept as holes, backward jumps smoothed);
    -initial_offset shifts the timestamps written into the files by t_base, but not the cut points or the list
    (-output_ts_offset would move the cut points too). t_base None: no shift (ffmpeg builds without the option)."""
    folder = Path(folder)
    off = [] if t_base is None else ["-initial_offset", f"{t_base:.6f}"]
    return [ff, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "warning", "-dts_delta_threshold", "600",
            "-f", "mpegts", "-i", "pipe:0", "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
            "-f", "segment", "-segment_time", f"{chunk_s:g}", "-segment_format", "mpegts", "-reset_timestamps", "0",
            *off, "-segment_list", str(folder / f"r{run:03d}.csv"),
            "-segment_list_type", "csv", str(folder / f"r{run:03d}_%05d.ts")]


def _popen_group() -> dict:
    """Keep Ctrl+C away from the ffmpegs: the recorder stops them itself, in order (fetch, then segment)."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


_OPENING = re.compile(r"Opening '([^']+)' for reading")


def redact(line: str) -> str:
    """Shorten the long signed URLs in ffmpeg's log to host + sequence number."""
    def short(m):
        u = m.group(0)
        sq = _SQ.search(u)
        return urllib.parse.urlsplit(u).netloc + "/..." + (f"/sq/{sq.group(1)}" if sq else "")
    return re.sub(r"https?://[^\s']+", short, line)


# ----------------------------------------------------------------------------------------------------------------
# One run: fetch -> relay -> segment
# ----------------------------------------------------------------------------------------------------------------

class LiveRun:
    def __init__(self, rec: "Recorder", src: Source, attempt: int = 0):
        self.rec, self.src, self.attempt = rec, src, attempt
        self.idx = rec.index
        self.run: dict | None = None
        self.p1 = self.p2 = None
        self.opened: list[str] = []
        self.opened_event = threading.Event()
        self.bytes = 0
        self.last_data = time.monotonic()
        self.started_mono = time.monotonic()
        self.probation = False               # started by a look for a better link: a short stall time at first
        self.stop_reason: str | None = None
        self.relay_error: str | None = None
        self.threads: list[threading.Thread] = []
        self.relay_thread: threading.Thread | None = None
        self.skipped = 0
        self.log_path = self.idx.folder / f"r{len(self.idx.runs):03d}.log"

    # -- start --------------------------------------------------------------------------------------------------
    def start(self) -> str:
        """'ok' (recording), 'wait' (nothing new to fetch yet), 'retry' (bad start, try again) or 'failed'."""
        rec, src, idx = self.rec, self.src, self.idx
        run_no = len(idx.runs)
        tf = time.time()
        vpl = fetch_playlist(src.video_url, src.headers)
        apl = fetch_playlist(src.audio_url, src.headers) if src.audio_url else None
        pdt_ok = has_pdt(vpl)
        edge_lag = None
        if pdt_ok:
            last = vpl["segments"][-1]
            edge_lag = tf - (last["pdt"] + last["dur"])
            if abs(edge_lag) > 600:
                log.info("  playlist times are %.0f s off this computer's clock; using arrival times", edge_lag)
                pdt_ok = False
        resume = idx.resume_wall() if pdt_ok else None
        start_v = start_a = None
        if vpl:
            start_v = pick_start(vpl, resume, rec.rewind_s if idx.data.get("t_end") is None else 0.0)
            if start_v is None:
                return "wait"
            start_a = start_v
            if apl and start_v >= 0:
                seq = vpl["segments"][start_v]["seq"]
                ka = seq - apl["media_seq"]
                start_a = ka if 0 <= ka < len(apl["segments"]) else pick_start(apl, resume)
                if start_a is None:
                    return "wait"
        log_name = self.log_path.name
        cmd = fetch_cmd(rec.ff, src, start_v, start_a, seg_retry=ffmpeg_has(rec.ff, "demuxer=hls", "seg_max_retry"))
        self.p1 = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   **_popen_group())
        self._thread(self._read_fetch_log)
        # The fetch log names the first segment it opened before any data comes out.
        seg = None
        deadline = time.monotonic() + 30.0
        refetched = False
        while time.monotonic() < deadline and not rec.stop_event.is_set():
            self.opened_event.wait(0.5)
            self.opened_event.clear()
            opened = list(self.opened)
            seg = next((s for s in (match_segment(u, vpl) for u in opened) if s is not None), None)
            if seg is not None:
                break
            unknown = [u for u in opened if match_segment(u, apl) is None]
            if unknown and not refetched and vpl is not None:
                vpl2 = fetch_playlist(src.video_url, src.headers)    # it opened a segment newer than our copy
                if vpl2 is not None and (not pdt_ok or has_pdt(vpl2)):
                    vpl = vpl2
                refetched = True
                continue
            if self.p1.poll() is not None:
                break
        if self.p1.poll() is not None or rec.stop_event.is_set():
            self._kill(self.p1)
            return "failed" if not rec.stop_event.is_set() else "retry"
        if seg is not None and pdt_ok:
            anchor, source = seg["pdt"], "pdt"
            if resume is not None and seg["pdt"] < resume - 0.25 and self.attempt < 2:
                log.info("  fetch started %.1f s before the end of the recording; restarting it", resume - seg["pdt"])
                self._kill(self.p1)
                return "retry"
        elif seg is not None:
            i = next(i for i, s in enumerate(vpl["segments"]) if s is seg)
            anchor, source = tf - sum(s["dur"] for s in vpl["segments"][i:]), "arrival"
        else:
            back = 3 * (vpl["target"] if vpl else 5.0)
            anchor, source = tf - back, "arrival"
        self.run = idx.begin_run(
            anchor_wall=anchor, wall_source=source, video=src.video, title=src.title,
            webpage_url=src.webpage_url, link=getattr(self.rec, "cur_url", None),
            video_release_timestamp=src.video_release_ts, format=src.fmt, start_index=start_v, edge_lag_s=None if edge_lag is None else round(edge_lag, 2),
            first_segment=None if seg is None else {"seq": seg["seq"], "pdt": seg["pdt"]},
            url_expires=src.expire, csv=f"r{run_no:03d}.csv", log=log_name)
        idx.data["status"] = "recording"
        idx.save()
        r = self.run
        gap = "" if r["gap_before_s"] is None else f", gap before {r['gap_before_s']:.1f} s"
        log.info("run %d: %s %s | %s + %s | t_base %.1f%s | wall from %s%s", r["run"], src.video,
                 _ascii(src.title)[:60], src.fmt["video"], src.fmt["audio"], r["t_base"], gap, source,
                 "" if edge_lag is None else f" | platform {edge_lag:.1f} s behind its own clock")
        self.p2_log = open(idx.folder / log_name, "a", encoding="utf-8")
        r["ts_offset"] = r["t_base"] if rec.ts_offset_ok else 0.0
        self.p2 = subprocess.Popen(segment_cmd(rec.ff, idx.folder, r["run"], r["t_base"] if rec.ts_offset_ok else None,
                                               rec.chunk_s),
                                   stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.p2_log,
                                   cwd=str(idx.folder), **_popen_group())
        self.last_data = time.monotonic()
        self.relay_thread = self._thread(self._relay)
        return "ok"

    def _thread(self, fn) -> threading.Thread:
        t = threading.Thread(target=fn, daemon=True)
        t.start()
        self.threads.append(t)
        return t

    def _read_fetch_log(self) -> None:
        with open(self.log_path, "a", encoding="utf-8") as f:
            for raw in iter(self.p1.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                m = _OPENING.search(line)
                if m and ".m3u8" not in m.group(1).split("?")[0][-8:]:
                    self.opened.append(m.group(1))
                    self.opened_event.set()
                if "skipping" in line.lower():
                    self.skipped += 1           # copied into the run by poll(), on the main thread
                f.write("[fetch] " + redact(line) + "\n")
                f.flush()

    def _relay(self) -> None:
        out = self.p2.stdin
        try:
            while True:
                b = self.p1.stdout.read1(1 << 16)
                if not b:
                    break
                out.write(b)
                out.flush()
                self.bytes += len(b)
                self.last_data = time.monotonic()
        except (OSError, ValueError) as e:
            self.relay_error = _short(e)
        finally:
            try:
                out.close()
            except OSError:
                pass
            if self.p1.poll() is None and self.relay_error:
                self._kill(self.p1)

    # -- while running ------------------------------------------------------------------------------------------
    def poll(self) -> int:
        """List the chunks segment has finished; returns how many were added."""
        r, idx = self.run, self.idx
        r["skipped_segments"] = self.skipped
        entries = read_segment_list(idx.folder / r["csv"])
        finished = self.p2.poll() is not None
        new = ready_entries(entries, r["n_chunks"], idx.folder, r["run"], finished)
        for name, a, b in new:
            now = time.time()
            c = idx.add_chunk(r, name, a, b, now)
            lag = now - (c["wall0"] + c["t1"] - c["t0"])
            gap = "" if not c["gap_before_s"] else f" gap {c['gap_before_s']:+.2f} s"
            log.info("chunk %d %s t %.2f-%.2f (%.2f s) lag %.1f s%s", c["seq"], name, c["t0"], c["t1"],
                     c["t1"] - c["t0"], lag, gap)
        if new:
            idx.save()
        return len(new)

    def finished(self) -> bool:
        return self.p2 is None or (self.p2.poll() is not None and not self.relay_thread.is_alive())

    def stall_s(self) -> float:
        if self.probation and time.monotonic() - self.started_mono < HEALTHY_S:
            return min(self.rec.stall_s, PROBATION_STALL_S)
        return self.rec.stall_s

    def stalled(self) -> bool:
        return time.monotonic() - self.last_data > self.stall_s()

    def stop(self, reason: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = reason
            log.info("run %d: stopping (%s)", self.run["run"], reason)
        self._kill(self.p1, graceful=True)

    def close(self) -> None:
        """Wait for segment to finish the last chunk, list everything, and close the run."""
        if self.p1 is not None and self.p1.poll() is None and self.stop_reason is None:
            self.stop_reason = "stopped"
        self._kill(self.p1, graceful=True)
        for t in self.threads:
            t.join(timeout=30)
        if self.p2 is not None:
            try:
                self.p2.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self._kill(self.p2)
            self.poll()
            self.p2_log.close()
        reason = self.stop_reason or f"input ended (fetch exit {self.p1.returncode})"
        if self.relay_error:
            reason += f"; relay: {self.relay_error}"
        self.idx.end_run(self.run, reason)
        self.idx.save()
        log.info("run %d ended: %s; %d chunk(s), %.0f MB", self.run["run"], reason, self.run["n_chunks"],
                 self.bytes / 1e6)

    @staticmethod
    def _kill(p, graceful: bool = False) -> None:
        if p is None or p.poll() is not None:
            return
        try:
            p.terminate()
            p.wait(timeout=10 if graceful else 3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                p.kill()
                p.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass


# ----------------------------------------------------------------------------------------------------------------
# The recorder: resolve, run, reconnect, prune
# ----------------------------------------------------------------------------------------------------------------

def flowing(src: Source, wait_s: float = FLOW_WAIT_S, sleep=time.sleep) -> bool:
    """True when the stream is delivering video: its playlist gains a segment within `wait_s` (or three target
    durations). A YouTube video can say it is live and send nothing (MadTown 2026: the event's own videos, while its
    Twitch channel and another team's YouTube stream ran)."""
    pl = fetch_playlist(src.video_url, src.headers)
    if not pl or not pl["segments"]:
        return False
    last = pl["segments"][-1]["seq"]
    deadline = time.monotonic() + max(wait_s, 3.0 * pl["target"])
    while time.monotonic() < deadline:
        sleep(2.0)
        pl = fetch_playlist(src.video_url, src.headers)
        if pl and pl["segments"] and pl["segments"][-1]["seq"] > last:
            return True
    return False


class _FolderLock:
    """One recorder per folder (an OS lock, released if the process dies)."""

    def __init__(self, path: Path):
        self.f = open(path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.f.close()
            raise SystemExit(f"another recorder is already writing {path.parent}")

    def release(self) -> None:
        try:
            self.f.close()
        except OSError:
            pass


class Recorder:
    def __init__(self, url: str, folder: Path, tag: str, *, event: str | None = None, keep_hours: float = KEEP_HOURS,
                 max_height: int = 1080, chunk_s: float = CHUNK_S, poll_s: float = POLL_S, stall_s: float = STALL_S,
                 rewind_s: float = 0.0, duration: float | None = None, idle_exit_min: float | None = None,
                 resolver=resolve, also: list[str] | None = None, recheck_s: float = RECHECK_S, flow=flowing):
        """`also`: the event's other streams (e.g. its Twitch channel), tried in order whenever `url` is not live.
        While one of them is recorded, `url` is looked at every `recheck_s` and taken back once `flow` says it is
        delivering (the module docstring: "Several links")."""
        self.url, self.folder, self.tag = url, Path(folder), tag
        self.also = [u for u in also or [] if u and u != url]
        self.event, self.keep_hours, self.max_height = event, keep_hours, max_height
        self.chunk_s, self.poll_s, self.stall_s, self.rewind_s = chunk_s, poll_s, stall_s, rewind_s
        self.duration, self.idle_exit_min, self.resolver = duration, idle_exit_min, resolver
        self.recheck_s, self.flow = recheck_s, flow
        self.stop_event = threading.Event()
        self.ff = ffmpeg.find()
        self.ts_offset_ok = ffmpeg_has(self.ff, "muxer=segment", "initial_offset")
        self.index: LiveIndex | None = None
        self._last_prune = 0.0
        self.cur_url: str | None = None                       # the link being recorded (or last tried)
        self.cur_video: str | None = None                     # ... and its video
        self._cool: dict[str, tuple[float, float]] = {}       # link -> (tried last until, this cool-down's length)
        self._waiting: dict[str, float] = {}                  # link -> since when it has had no new segment
        self._found: tuple[str, Source, float] | None = None  # a better link a look found: (link, source, when)
        self._chosen: str | None = None                       # the link a look chose, until its first run starts
        self._looker: threading.Thread | None = None
        self._next_look = 0.0

    def request_stop(self, *_):
        self.stop_event.set()

    def _sleep(self, s: float) -> None:
        self.stop_event.wait(max(0.0, s))

    def _time_up(self) -> bool:
        return self.duration is not None and time.monotonic() - self.t_start >= self.duration

    def _prune(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._last_prune < 300:
            return
        self._last_prune = time.monotonic()
        removed = self.index.prune()
        if removed:
            log.info("pruned %d file(s) older than %g h", len(removed), self.keep_hours)
            self.index.save()

    # -- several links: the best one that works (the module docstring) -----------------------------------------------
    def _links(self) -> list[str]:
        """Every link of the event, best first: links.json when `live` wrote one for this link, else the link and
        its --also ones."""
        links = [self.url, *self.also]
        try:
            d = json.loads((self.folder / LINKS_NAME).read_text(encoding="utf-8"))
            if d.get("for") == self.url and isinstance(d.get("links"), list):
                listed = [u for u in d["links"] if isinstance(u, str) and u]
                links = listed + [u for u in links if u not in listed] if listed else links
        except (OSError, ValueError, AttributeError):
            pass
        return list(dict.fromkeys(links))

    def _cooling(self, url: str) -> bool:
        c = self._cool.get(url)
        return c is not None and time.monotonic() < c[0]

    def _bad(self, url: str, why: str) -> None:
        """`url` just failed: the other links are tried before it, and the looks leave it alone, for COOL_S (doubling
        up to COOL_MAX_S each time it fails again)."""
        step = min(COOL_MAX_S, 2 * self._cool[url][1]) if url in self._cool else COOL_S
        self._cool[url] = (time.monotonic() + step, step)
        if len(self._links()) > 1:
            log.info("%s %s; the event's other links come first for the next %.0f min", url, why, step / 60)

    def _good(self, url: str) -> None:
        self._cool.pop(url, None)

    def _match_on(self) -> bool:
        """True while `live` says a match timer is counting down (match_on.json, written after each chunk it reads)."""
        try:
            d = json.loads((self.folder / MATCH_ON_NAME).read_text(encoding="utf-8"))
            return bool(d.get("counting")) and time.time() - float(d.get("wall") or 0.0) < MATCH_ON_FRESH_S
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    def _look(self, better: list[str]) -> None:
        """A look (its own thread: resolving and the flow check take up to half a minute): the first of the better
        links that is live and delivering becomes self._found, which ends the run being recorded."""
        for url in better:
            if self.stop_event.is_set():
                return
            try:
                src = self.resolver(url, self.max_height)
            except Exception:  # noqa: BLE001 - not live, or whatever yt-dlp does: look again next time
                continue
            video = getattr(src, "video", None)
            if video and video == self.cur_video:             # a channel's live page and its video, both listed
                continue
            try:
                ok = self.flow(src)
            except Exception as e:  # noqa: BLE001
                log.info("could not check %s (%s)", url, _short(e))
                continue
            if ok:
                self._found = (url, src, time.monotonic())
                return
            self._bad(url, "says it is live but sends nothing")

    def _look_due(self) -> None:
        """Called every second while recording: starts a look when one is due and a better link exists."""
        now = time.monotonic()
        if (not self.recheck_s or now < self._next_look or self._found is not None
                or (self._looker is not None and self._looker.is_alive())):
            return
        if now < self._next_look + HOLD_MAX_S and self._match_on():
            return
        self._next_look = now + self.recheck_s
        links = self._links()
        better = [u for u in (links[:links.index(self.cur_url)] if self.cur_url in links else links)
                  if not self._cooling(u)]
        if better:
            self._looker = threading.Thread(target=self._look, args=(better,), daemon=True)
            self._looker.start()

    def _after_run(self, url: str | None, lasted: float, stop_reason: str | None) -> None:
        if url is None:
            return
        if lasted >= HEALTHY_S:
            self._good(url)
        elif stop_reason is None or not stop_reason.startswith(_NOT_THE_LINKS_FAULT):
            self._bad(url, f"ended after {lasted:.0f} s ({stop_reason or 'the stream stopped'})")

    def _resolve_live(self):
        """(Source, None) for the best link that is live, else (None, why for each). A link a look just found is
        taken as it is; links that failed lately come after the others."""
        found, self._found = self._found, None
        if found is not None and time.monotonic() - found[2] < FOUND_FRESH_S:
            self.cur_url, self.cur_video = found[0], getattr(found[1], "video", None)
            self._chosen = found[0]
            return found[1], None
        links = self._links()
        whys = []
        for url in [u for u in links if not self._cooling(u)] + [u for u in links if self._cooling(u)]:
            try:
                src = self.resolver(url, self.max_height)
            except NotLive as e:
                whys.append(str(e))
                continue
            except Exception as e:  # noqa: BLE001 - keep recording whatever yt-dlp does
                whys.append(f"resolve failed: {_short(e)}")
                continue
            if url != links[0]:
                log.info("the stream link is not live (%s); recording the event's other stream %s instead",
                         whys[0] if whys else "it failed a moment ago", url)
            self.cur_url, self.cur_video = url, getattr(src, "video", None)
            return src, None
        return None, "; ".join(whys)

    def _salvage(self) -> None:
        for r in self.index.open_runs():
            # A recorder that just died leaves its segment ffmpeg finishing the last chunk: wait until quiet.
            files = [self.folder / r["csv"], *self.folder.glob(f"r{r['run']:03d}_*.ts")]
            for _ in range(30):
                newest = max((f.stat().st_mtime for f in files if f.exists()), default=0.0)
                if time.time() - newest > 3.0:
                    break
                time.sleep(1.0)
            got = self.index.salvage(r)
            log.info("run %d was left open by a stopped recorder: %d chunk(s) salvaged", r["run"], len(got))
        self.index.save()

    def record(self) -> int:
        self.folder.mkdir(parents=True, exist_ok=True)
        lock = _FolderLock(self.folder / "record.lock")
        self.t_start = time.monotonic()
        try:
            self.index = LiveIndex.open(self.folder, self.tag, event=self.event, source_url=self.url,
                                        keep_hours=self.keep_hours, chunk_s=self.chunk_s)
            free_gb = shutil.disk_usage(self.folder).free / 1e9
            log.info("streamsplitter %s recording %s -> %s (%.0f GB free; keep %g h)", __version__, self.url,
                     self.folder, free_gb, self.keep_hours)
            if len(self._links()) > 1:
                log.info("the event's links, best first: %s%s", ", ".join(self._links()),
                         f" (a better one is looked for every {self.recheck_s:g} s while a lesser one is recorded)"
                         if self.recheck_s else "")
            if free_gb < 30:
                log.info("WARNING: under 30 GB free; a 1080p stream needs about 3 GB per hour kept")
            if not self.ts_offset_ok:
                log.info("WARNING: this ffmpeg has no segment -initial_offset; chunk timestamps restart in every "
                         "run, so a clip must not span two runs")
            self._salvage()
            self._prune(force=True)
            last_live = time.monotonic()
            quick = 0                                         # fast retries right after a run ends
            attempt = 0
            while not self.stop_event.is_set() and not self._time_up():
                src, msg = self._resolve_live()
                if src is None:
                    if self.idle_exit_min and time.monotonic() - last_live > 60 * self.idle_exit_min:
                        log.info("nothing live for %g min; exiting", self.idle_exit_min)
                        break
                    self.index.data["status"] = "waiting"
                    self.index.save()
                    wait = 5.0 * 2 ** quick if quick < 3 else self.poll_s
                    quick += 1
                    log.info("not live (%s); next try in %.0f s", msg, wait)
                    self._sleep(wait)
                    self._prune()
                    continue
                last_live = time.monotonic()
                url = self.cur_url
                run = LiveRun(self, src, attempt)
                run.probation = url is not None and url == self._chosen
                status = run.start()
                if status != "ok":
                    attempt = attempt + 1 if status == "retry" else 0
                    if status == "wait":
                        log.info("no new segment after the end of the recording yet; waiting")
                        if time.monotonic() - self._waiting.setdefault(url, time.monotonic()) > WAIT_GIVE_UP_S:
                            del self._waiting[url]
                            self._bad(url, f"has had no new video for {WAIT_GIVE_UP_S:g} s")
                    elif status == "failed":
                        log.info("fetch failed to start (see %s)", run.log_path.name)
                        self._bad(url, "could not be opened")
                    self._sleep(2.0 if status == "retry" else min(self.poll_s, 5.0 * 2 ** min(quick, 3)))
                    quick += 1
                    continue
                attempt = 0
                self._waiting.pop(url, None)
                self._chosen = None
                self._next_look = time.monotonic() + self.recheck_s
                next_save = time.monotonic() + 30
                while not run.finished():
                    self._sleep(1.0)
                    try:
                        run.poll()
                        if self.stop_event.is_set() or self._time_up():
                            run.stop("stopped")
                        elif run.p2.poll() is not None and run.p1.poll() is None:
                            run.stop(f"segment ffmpeg exited ({run.p2.returncode})")
                        elif run.stalled():
                            run.stop(f"no data for {run.stall_s():g} s")
                        elif (src.expire and time.time() > src.expire - 600
                              and time.monotonic() - run.started_mono > 600):
                            run.stop("stream URL about to expire; refreshing")
                        elif self._found is not None:
                            run.stop(f"a better stream is live: {self._found[0]}")
                        else:
                            self._look_due()
                        if time.monotonic() > next_save:
                            self.index.save()
                            next_save = time.monotonic() + 30
                        self._prune()
                    except Exception as e:  # noqa: BLE001 - e.g. a disk or index write error: keep recording
                        log.info("error while recording (will retry): %s", _short(e))
                        self._sleep(5.0)
                try:
                    run.close()
                except Exception as e:  # noqa: BLE001
                    log.info("error closing run: %s", _short(e))
                self._after_run(url, time.monotonic() - run.started_mono, run.stop_reason)
                if time.monotonic() - run.started_mono < 30:          # died at once: don't hammer the platform
                    self._sleep(min(self.poll_s, 5.0 * 2 ** min(quick, 3)))
                    quick += 1
                else:
                    quick = 0
            self.index.data["status"] = "stopped"
            self.index.save()
            log.info("stopped; %d chunk(s) listed in %s", len(self.index.chunks), self.index.path)
            return 0
        finally:
            lock.release()


# ----------------------------------------------------------------------------------------------------------------
# Command line
# ----------------------------------------------------------------------------------------------------------------

def _ascii(s: str) -> str:
    return s.encode("ascii", "replace").decode("ascii")


def default_tag(url: str) -> str:
    """A folder name from the URL: @handle, channel id, video id or twitch_<name>."""
    u = urllib.parse.urlsplit(url if "://" in url else "https://" + url)
    host, parts = u.netloc.lower(), [p for p in u.path.split("/") if p]
    q = urllib.parse.parse_qs(u.query)
    if "twitch.tv" in host and parts:
        tag = "twitch_" + parts[0]
    elif "v" in q:
        tag = q["v"][0]
    elif parts and parts[0].startswith("@"):
        tag = parts[0][1:]
    elif len(parts) >= 2 and parts[0] in ("channel", "c", "user", "live"):
        tag = parts[1]
    elif "youtu.be" in host and parts:
        tag = parts[0]
    else:
        tag = parts[-1] if parts else host
    return re.sub(r"[^A-Za-z0-9_.-]", "_", tag) or "stream"


def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("url", help="live video URL, channel live URL (youtube.com/@Name/live) or twitch.tv/<name>")
    ap.add_argument("--also", action="append", default=[], metavar="URL",
                    help="another link of the same event, tried when URL is not live (e.g. its Twitch channel); "
                         "may be given more than once")
    ap.add_argument("--tag", help="folder name under <work_root>/live (default: from the URL)")
    ap.add_argument("--event", help="event key for the index (e.g. 2026mttd)")
    ap.add_argument("--out", type=Path, help="folder for chunks and index.json (default <work_root>/live/<tag>)")
    ap.add_argument("--keep-hours", type=float, default=KEEP_HOURS, help="delete chunks this old (default 28)")
    ap.add_argument("--max-height", type=int, default=1080, help="highest video height to record (default 1080)")
    ap.add_argument("--chunk-s", type=float, default=CHUNK_S, help="chunk length in seconds (default 20)")
    ap.add_argument("--poll-s", type=float, default=POLL_S, help="seconds between checks while not live")
    ap.add_argument("--stall-s", type=float, default=STALL_S, help="reconnect after this long without data")
    ap.add_argument("--rewind-s", type=float, default=0.0,
                    help="new index only: start this far back in the platform's DVR window (default: live edge)")
    ap.add_argument("--recheck-s", type=float, default=RECHECK_S,
                    help="while one of the --also links is recorded, look this often whether a better link is "
                         "live again (default 300; 0 = never)")
    ap.add_argument("--duration", type=float, help="stop after this many seconds (tests)")
    ap.add_argument("--idle-exit", type=float, help="exit after this many minutes with nothing live")


def run_from_args(a: argparse.Namespace) -> int:
    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except (AttributeError, ValueError):
        pass
    tag = a.tag or default_tag(a.url)
    folder = a.out or (config.work_root() / "live" / tag)
    folder.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S")
    h1 = logging.StreamHandler(sys.stdout)
    h1.setFormatter(fmt)
    h2 = logging.FileHandler(folder / "record.log", encoding="utf-8")
    h2.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.handlers[:] = [h1, h2]
    log.setLevel(logging.INFO)
    log.propagate = False
    rec = Recorder(a.url, folder, tag, event=a.event, keep_hours=a.keep_hours, max_height=a.max_height,
                   chunk_s=a.chunk_s, poll_s=a.poll_s, stall_s=a.stall_s, rewind_s=a.rewind_s,
                   duration=a.duration, idle_exit_min=a.idle_exit, also=getattr(a, "also", None),
                   recheck_s=getattr(a, "recheck_s", RECHECK_S))
    signal.signal(signal.SIGINT, rec.request_stop)
    signal.signal(signal.SIGTERM, rec.request_stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, rec.request_stop)
    return rec.record()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="streamsplitter record", description="Record a live stream into chunks.")
    add_arguments(ap)
    return run_from_args(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
