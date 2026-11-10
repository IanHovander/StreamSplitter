"""Live: record a stream and cut each match as soon as it ends.

    streamsplitter live [URL] --event 2026mttd [--tag mttd] [--deliver FOLDER] [--no-publish]

This runs two things:
- **The recorder** (record.py), as a child process. It lists each finished ~20 s chunk in
  <work_root>/live/<tag>/index.json and is restarted if it ever exits.
- **The follower**, in this process. It polls that index and feeds each new chunk to the event-agnostic pipeline
  (pipeline.py).

Each match clip, and its score version a few minutes later, goes to <data_root>/clips/<tag>/. A copy goes to the
delivery folder, if one is given (--deliver, or `deliver_dir` in config.toml; e.g. a OneDrive or Google Drive folder
shared with drive team). The TBA schedule is fetched at the start and every `schedule_every_s`, since events publish
it during the event. Without one, clips are still cut and named from the overlay's title.

**Restarts.** follower.json in the chunk folder keeps the last chunk read, the end of the last match cut, and the
results-screen searches still open (the clip waits for its score version; YouTube waits for that). A restarted
follower re-reads the last few chunks (to pick up a match in progress), and from an open search's match start when
there is one, so the search is taken up again; it does not cut a match again, and counts the clips already in the
clip folder for replay names. A search whose chunks are gone, and every open search at a clean stop, is settled
without a score version, so YouTube gets that match clip rather than nothing. follower.json is written durably with
the copy before kept (follower.json.bak); with neither readable, the follower carries on after the last clip in the
clip folder instead of cutting the whole recording again (and says so).

**One run per recording.** A `live` run holds an OS lock on live.lock in the chunk folder until its process ends
(after its last upload attempt); a second run refuses to start, and read_status() reports "running" while the lock
is held, so `start` and the setup wizard wait for a stopping run instead of starting a second one that would send
every queued clip again.

**Publishing.** With [slack], [youtube], [drive] or [wiki] turned on in config.toml (the setup wizard writes them),
each clip is handed to publish.Publisher right after its CLIP / SCORE line: the team's own matches go to Slack, the
others to YouTube, every match to the team's Google Drive folder and the team wiki, through an outbox on disk
(publish/outbox.py) that retries across network drops and restarts. The follower only queues; uploads run on their own
threads. A full Google Drive pauses the Drive queue ([drive] paused) and DMs the contact once. --no-publish turns
publishing and Slack DMs off.

**Watching.** Besides the console/log, a Slack contact ([notify] slack_user) gets a DM when the recorder exited (it is
restarted), no new chunk came for 3 min (stream down?), a disk holding data_root or work_root has under 10 GB free,
a chunk could not be read (it is skipped; the follower keeps going), or more than 5 clips wait for one destination.
A heartbeat keeps a scheduled "may have stopped" DM pushed back every minute, 3 min ahead (publish/notify.py), so the
contact hears 2-3 min after the PC dies; a clean stop cancels it and says why.

**The stream link** (user, 2026-11-04: leave it running for the whole event). URL on the command line, else [event]
stream_url in config.toml, else the stream The Blue Alliance lists for the event (streams.py: today's dated webcast,
else the channel's live page, else the first listed), looked up at the start. With none at all the run still starts
and DMs the contact how to set one; it asks TBA again every TBA_RECHECK_S. While following TBA's link (no link set)
and nothing is live, TBA is asked again every TBA_RECHECK_S; each event morning a set-up link to a single video
that is not live (yesterday's) is replaced by a webcast TBA dates today (a channel's live page is kept, it follows
the channel; a link the contact chose from Slack is kept and the morning DM names TBA's). A new link restarts the
recorder with the same tag, so the chunk index continues.

**What the contact hears about the stream** (EventWatch, on its own thread; console/log too, and only there without
Slack): "Stream started: <title> <url>. Recording; your team's matches will be posted to #<channel>." when the
recorder starts recording a new video, or the first one this run; "Stream back after <n> min" when the same video
comes back after STREAM_BACK_S or more; and at 08:00 in the event's time zone (TBA's timezone) on each event day, if
nothing is live: "Waiting for the stream to start. Checking the suspected stream link every minute: <url>. To use a
different link, reply here: stream <link>". A run started later in the day says so WAIT_GRACE_S after it started.

**The event's other streams** (user, 2026-11-06: MadTown 2026 streamed on Twitch only, while the YouTube day videos
TBA lists never went live): the recorder is started with --also for each of the event's other TBA webcasts that
show what is live today (streams.alternates: today's dated one, a channel's live page, the Twitch channel), asked at
every (re)start. Whenever the link is not live, it records the first of them that is; the start message lists them.
The "no new video for 3 min" warning waits until the stream was live once in this run (before that the morning DM
covers it).

**A better stream** (user, 2026-11-10: that Twitch stream glitched all Saturday and wasted the day's uploads, while
a team streamed the whole event on YouTube under a link nobody listed). The recorder's links are a list, best first
(EventWatch.links): the link chosen for this run when one was (`stream <link>` from Slack, a URL on the command
line), then YouTube links, then Twitch; TBA's before found ones. The recorder records the first that delivers and
looks every record.RECHECK_S (5 min) whether a better one does (record.py: it changes between matches, see
MATCH_ON_AFTER_S and _match_on, and backs off from a link that keeps failing). The list is also written to
links.json in the chunk folder, which the recorder reads at every look, so a link that turns up needs no restart.
Links that turn up: TBA's webcasts as they change, and what a search of YouTube's live videos finds (scout.py, every
SCOUT_S on the event's days; SCOUT_IDLE_S while a YouTube stream is being recorded): a video whose title names the
event and whose picture shows three or more of the event's team numbers joins the list (found_streams.json keeps it
over a restart; config.toml is never changed); one that could not be confirmed is only suggested to the contact,
once, with the reply that records it. With no link at all, a confirmed find is recorded (SRC_SEARCH). The contact
hears "Found a YouTube stream ... on no list" and, when the recorder took it, "Changed to a better stream: ... To go
back, reply here: stream <old link>". `[event] find_streams = false` turns the search off.

**Slack commands** (publish/commands.py): status, set stream <link>, set stream auto, set teams <numbers>, pause /
resume youtube, drive, wiki (LiveControls does them). While a Slack listener of this version runs, it reads the DMs
and passes these to this run through its mailbox (commands.Mailbox, on its own thread, holding the Watcher's lock so
EventWatch and a command never run at once); with no listener, this run reads its owner's DM itself
(commands.CommandPoller) and answers everything there. `set stream` saves [event] stream_url, `set teams` [slack]
teams, pause [youtube] / [drive] / [wiki] paused (a paused run stays paused after a restart). Nothing stops the
recorder from its own reader (the listener's stop asks first). Every other setting changed from Slack (`settings`,
`set ...`, slackadmin.py) is read here only at the next start (build_publishing).

**Background runs** (for the setup wizard and `streamsplitter start/stop/status`): start_detached() starts `live` as a
background process with no window that someone could close (tools/launch_detached.ps1 explains why), logging to
<data_root>/logs/live_<tag>.log; request_stop() asks it to stop with a file it checks every poll (never a console
signal); read_status() reads the status.json it rewrites every poll.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from . import config, record, scout, streams, tba
from .pipeline import Pipeline
from .publish.notify import HEARTBEAT_S
from .publish.outbox import IDLE_S, Outbox

REREAD_S = 300.0           # a restarted follower re-reads the chunks of this many seconds before the last one it read
NO_CHUNK_WARN_S = 180.0    # no new chunk for this long: the stream (or the recorder) is probably down
DISK_MIN_GB = 10.0         # warn under this much free space (a 1080p stream takes about 3 GB per hour)
DISK_CHECK_S = 60.0
DISK_REWARN_S = 1800.0     # a disk still low is mentioned again (log and DM) this often
BACKLOG_WARN = 5           # warn when more clips than this wait for one destination ...
BACKLOG_REARM = 2          # ... and again only after it went down to this
STATUS_STALE_S = 120.0     # a status.json not rewritten for this long belongs to a run that is not running
FOLLOWER_HUNG_S = 300.0    # the heartbeat stops pushing the alarm back when the follower loop is stuck this long
STOP_FILE = "stop"         # in <work_root>/live/<tag>/: request_stop() writes it, the follower stops at its next poll
STATUS_FILE = "status.json"
STATE_FILE = "follower.json"
LOCK_FILE = "live.lock"
LOCK_WAIT_S = 3.0          # a starting run tries the lock this long (a status check holds it for an instant)
HEARTBEAT_RETRY_S = 15.0   # after a heartbeat could not reach Slack (the alarm is due 3 min ahead: retry soon)
WATCH_TICK_S = 5.0         # EventWatch looks at the recorder's index this often
MORNING_HOUR = 8           # the "waiting for the stream" DM, in the event's own time zone, on each event day
WAIT_GRACE_S = 120.0       # ... not before the recorder had this long to find the stream
STREAM_BACK_S = 600.0      # the same video back after a gap this long: "Stream back after n min"
TBA_RECHECK_S = 600.0      # no link, or following TBA's while nothing is live: ask TBA again this often
SCOUT_S = 300.0            # search YouTube this often for a live stream of the event that is on no list (scout.py) ...
SCOUT_FIRST_S = 60.0       # the first search, this long after the start
SCOUT_IDLE_S = 1800.0      # ... and this often while a YouTube stream is being recorded (the search finds spares)
FOUND_KEEP_S = 36 * 3600.0  # a stream the search found stays on the recording's list this long (a day's video)
FOUND_FILE = "found_streams.json"
MATCH_ON_AFTER_S = 20.0    # a match timer read this shortly before a chunk's end: a match is being played
EVENT_RETRY_S = 600.0      # TBA's event record (time zone, days) is asked again this often until it is had
RESTORE_LEAD_S = 30.0      # a restored score search re-reads from this long before its match start
RESTORE_NEED_S = 15.0      # ... and is restored only while chunks from this long before the start are still kept
# what follower.json keeps of a row whose results-screen search is open: enough to search and cut its score version
# (pipeline._scores) and to queue it (publish.Publisher, whose clip job ids are in "jobs")
PENDING_KEYS = ("name", "naming", "start", "end", "start_src", "end_src", "place", "clip", "numbers_shown", "partial",
                "gap_inside", "jobs")


def fetch_schedule(event: str) -> list[dict] | None:
    try:
        return tba.get(f"/event/{event}/matches") or None
    except Exception as e:                       # offline, no key, or the event has no matches yet
        print(f"TBA schedule for {event}: not available ({e})", flush=True)
        return None


def _load_state(path: Path) -> tuple[dict, str]:
    """follower.json as (state, where from): "file"; "backup" (follower.json was empty or damaged, e.g. a power cut
    while it was written: the copy kept before the last write); "none" (neither exists: a first run, or deleted);
    "lost" (neither can be read)."""
    damaged = False
    for p, where in ((path, "file"), (path.with_name(path.name + ".bak"), "backup")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError):            # JSONDecodeError and UnicodeDecodeError are ValueErrors
            damaged = True
            continue
        if isinstance(data, dict):
            return data, where
        damaged = True
    return {}, ("lost" if damaged else "none")


def _save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _json_default(x):
    item = getattr(x, "item", None)              # a numpy number from the detectors
    return item() if callable(item) else str(x)


def _replace(src: Path, dst: Path, tries: int = 50) -> None:
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:                  # Windows: a virus scanner or backup tool has it open for a moment
            if i == tries - 1:
                raise
            time.sleep(0.02)


def _save_follower(path: Path, state: dict, keep_backup: bool = True) -> None:
    """follower.json, durably: written to a temporary file and fsynced (as record.py writes its index), the current
    copy moved to follower.json.bak, then the new one moved in, so a power cut at any point leaves one of the two
    whole. (Without the fsync a file can come back empty after a power cut, and the follower then re-cut every chunk
    of the recording.) `keep_backup` is False while the current follower.json is the damaged one: the good .bak
    stays."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(state, indent=1, default=_json_default))
        f.flush()
        os.fsync(f.fileno())
    if keep_backup and path.is_file():
        _replace(path, path.with_name(path.name + ".bak"))
    _replace(tmp, path)


def _pending_record(row: dict) -> dict:
    return {k: row[k] for k in PENDING_KEYS if k in row}


def _restorable(rec: dict, chunks: list[dict], folder: Path) -> bool:
    """Whether the chunks a restored score search needs (from just before its match start: the cutter copies the
    match again) are still kept."""
    need = float(rec["start"]) - RESTORE_NEED_S
    first = min((c for c in chunks if c["t1"] > need), key=lambda c: c["t0"], default=None)
    return first is not None and first["t0"] <= need + 1.0 and (folder / first["path"]).is_file()


def _recover_state(index: dict, out_dir: Path) -> tuple[dict, str] | None:
    """A starting point when follower.json and its copy are both unreadable but clips were cut already: just after
    the newest match clip in the clip folder (the newest chunk closed before that clip was written; matches ending
    by then were cut), instead of re-reading, and re-cutting as replays, the whole recording. When the clips are
    older than every chunk kept (none was cut from them), the newest chunk minus REREAD_S. None for a first run
    (no chunks or no clips)."""
    chunks = [c for c in index.get("chunks") or [] if c.get("seq") is not None and c.get("t1") is not None]
    clips = [p for p in Path(out_dir).glob("*.mp4") if not p.stem.endswith("_score")]
    if not chunks or not clips:
        return None
    newest = max(clips, key=lambda p: p.stat().st_mtime)
    written = newest.stat().st_mtime
    before = [c for c in chunks if c.get("wall_closed") and c["wall_closed"] <= written]
    if before:
        c = max(before, key=lambda c: c["seq"])
        return ({"last_seq": c["seq"], "last_t1": c["t1"], "last_end": c["t1"]},
                f"after the newest clip in the clip folder ({newest.stem})")
    c = max(chunks, key=lambda c: c["seq"])
    return {"last_seq": c["seq"], "last_t1": c["t1"]}, f"{_duration(REREAD_S)} before the newest recorded video"


def live_folder(tag: str) -> Path:
    """<work_root>/live/<tag>: the recorder's chunks and index, the follower's state, status.json and the stop file."""
    return config.work_root() / "live" / tag


def _duration(s: float) -> str:
    m = int(s // 60)
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


class Monitor:
    """What the follower watches besides the chunks (stream silence, disks, the outbox's backlog, chunk errors, the
    recorder), the stop file, and status.json for read_status(). Its warnings go through the notifier: always the
    console/log, a Slack DM when one is set up (rate-limited by key)."""

    def __init__(self, folder: Path, notifier=None, publisher=None, *, tag: str | None = None,
                 disk_paths: list[tuple[str, Path]] | None = None, log=print, clock=time.time):
        if notifier is None:
            from .publish.notify import Notifier
            notifier = Notifier(log=lambda s: log(s, flush=True))
        self.folder = Path(folder)
        self.tag = tag or self.folder.name
        self.notifier, self.publisher = notifier, publisher
        self.clock = clock
        self.disk_paths = list(disk_paths or [])
        self.started = clock()
        self._started_real = time.time()          # the stop file's mtime is real time even with a test clock
        self.stop_by: str | None = None           # who asked for the stop (stop_asker), read before the file goes
        self.tick = self.started                  # the follower loop's last sign of life (Heartbeat checks it)
        self.last_new_chunk = self.started
        self.last_chunk: dict | None = None
        self.chunks_read = self.chunk_errors = self.clips = self.scores = 0
        self.last_clip: str | None = None
        self.recorder: str | None = None          # "running", "restarted", None (--no-record)
        self.recorder_restarts = 0
        self.stream_status: str | None = None     # the recorder's index: recording | waiting | stopped
        self.outbox_status: dict | None = None
        self.youtube_quota: dict | None = None
        self.drive_storage: dict | None = None    # the Drive destination's last free-space reading
        self._quota_warned = False
        self._no_chunk_dm: bool | None = None     # None: not warned; else whether the warning went by DM
        self._backlog_warned: set[str] = set()
        self._disk_warned: dict[str, float] = {}
        self._next_disk = 0.0
        self._status_error = False
        self.stopping = False                     # a stop was asked for: status says so until the run has ended
        self.seen_live = False                    # the stream was live once in this run (the no-video warning waits)
        self._arrival_said = False                # EventWatch announced the stream's return: no "arriving again"
        self.stream: dict | None = None           # EventWatch: link, where it came from, live or not (status.json)

    def arrival_announced(self) -> None:
        """EventWatch said "Stream started" / "Stream back": the "arriving again" note would repeat it."""
        self._arrival_said = self._no_chunk_dm is not None

    @property
    def status_path(self) -> Path:
        return self.folder / STATUS_FILE

    @property
    def stop_path(self) -> Path:
        return self.folder / STOP_FILE

    def stop_requested(self) -> bool:
        """A stop file written since this run started (request_stop). It is removed, so the next run starts."""
        try:
            st = self.stop_path.stat()
        except OSError:
            return False
        by = stop_asker(self.stop_path)
        try:
            self.stop_path.unlink()
        except OSError:
            pass
        if st.st_mtime < self._started_real - 5.0:
            return False
        self.stop_by = by
        return True

    def poll(self, index: dict | None = None) -> None:
        """Every follower poll: the checks that do not depend on a chunk, then status.json."""
        now = self.clock()
        self.tick = now
        if index is not None:
            self.stream_status = index.get("status")
            if not self.seen_live and self.stream_status == "recording":
                self.seen_live, self.last_new_chunk = True, now   # the 3 min count from the stream going live
        if self._no_chunk_dm is None and self.seen_live and now - self.last_new_chunk >= NO_CHUNK_WARN_S:
            self._no_chunk_dm = self.notifier.warn(
                f"No new video from the stream for {_duration(now - self.last_new_chunk)} (recorder: "
                f"{self.stream_status or 'no index yet'}). Is the stream down? StreamSplitter keeps trying and "
                f"carries on when it is back.", key="no_chunks")
        if now >= self._next_disk:
            self._next_disk = now + DISK_CHECK_S
            self._check_disks()
            self._check_quota()
            self._check_drive()
        self._check_backlog()
        self.write_status()

    def chunk_read(self, c: dict) -> None:
        now = self.clock()
        if self._no_chunk_dm is not None:
            if not self._arrival_said:
                self.notifier.info(f"Video from the stream is arriving again (none for "
                                   f"{_duration(now - self.last_new_chunk)}).", dm=self._no_chunk_dm)
            self._no_chunk_dm = None
        self._arrival_said = False
        self.seen_live = True
        self.tick = self.last_new_chunk = now
        self.chunks_read += 1
        self.last_chunk = {"seq": c.get("seq"), "t0": c.get("t0"), "t1": c.get("t1"),
                           "wall_closed": c.get("wall_closed"), "read_at": now}

    def chunk_failed(self, c: dict, e: Exception) -> None:
        self.chunk_errors += 1
        self.notifier.error(f"Could not read chunk {c.get('seq')} ({type(e).__name__}: {e}); skipped it and kept "
                            f"going. A clip around then may be missing.", key="chunk_error")

    def clip(self, row: dict) -> None:
        self.clips += 1
        self.last_clip = row.get("name") or Path(row["clip"]["path"]).stem

    def score(self, row: dict) -> None:
        self.scores += 1

    def recorder_exited(self, code) -> None:
        self.recorder, self.recorder_restarts = "restarted", self.recorder_restarts + 1
        self.notifier.warn(f"The recorder exited (code {code}) and has been restarted; a few seconds of the stream "
                           f"may be missing.", key="recorder")

    def _check_disks(self) -> None:
        seen = set()
        for role, path in self.disk_paths:
            try:
                path = Path(path)
                usage = shutil.disk_usage(path)
            except OSError:
                continue
            anchor = path.resolve().anchor.lower() or str(path)
            if anchor in seen:
                continue
            seen.add(anchor)
            free_gb = usage.free / 1e9
            last = self._disk_warned.get(anchor)
            if free_gb < DISK_MIN_GB and (last is None or self.clock() - last >= DISK_REWARN_S):
                self._disk_warned[anchor] = self.clock()
                self.notifier.warn(f"Only {free_gb:.1f} GB free on the disk holding the {role} ({path}). A 1080p "
                                   f"stream needs about 3 GB per hour: free some space soon.", key=f"disk:{anchor}")

    def _check_quota(self) -> None:
        """YouTube's daily limits (the destination's own local estimate, youtube.QuotaTracker): warn once when no
        upload is possible until midnight US Pacific, and again only after uploads were possible in between."""
        if self.publisher is None:
            return
        dest = getattr(self.publisher.outbox, "destinations", {}).get("youtube")
        tracker = getattr(getattr(dest, "client", None), "quota", None)
        if tracker is None:
            return
        try:
            st = tracker.status()
        except Exception:  # noqa: BLE001 - informational
            return
        self.youtube_quota = {k: st.get(k) for k in ("day", "uploads", "uploads_left", "remaining", "blocked",
                                                     "resets_in_s")}
        if st.get("blocked") and not self._quota_warned:
            self._quota_warned = True
            self.notifier.warn(f"YouTube's daily limit is used up ({st.get('uploads')} uploads today): uploads wait "
                               f"until midnight US Pacific, in {_duration(float(st.get('resets_in_s') or 0))}. They "
                               f"are kept and go out then.", key="youtube_quota")
        elif not st.get("blocked"):
            self._quota_warned = False

    def _check_drive(self) -> None:
        """Google Drive's free space as its destination last read it (no network here: the Drive worker asks Google
        before uploads and warns about low space itself), for status.json and the status reply."""
        if self.publisher is None:
            return
        dest = getattr(self.publisher.outbox, "destinations", {}).get("drive")
        fn = getattr(dest, "storage_status", None)
        if fn is None:
            return
        try:
            st = fn()
        except Exception:  # noqa: BLE001 - informational
            return
        if st:
            self.drive_storage = dict(st)

    def _check_backlog(self) -> None:
        if self.publisher is None:
            return
        try:
            st = self.publisher.outbox.status()
        except Exception:  # noqa: BLE001 - status is informational
            return
        self.outbox_status = st
        from .publish.outbox import label
        for dest, s in st.items():
            waiting = s["queued"] + s["running"]
            if s.get("paused"):                   # held from Slack (pause youtube): the contact knows
                continue
            if waiting > BACKLOG_WARN and dest not in self._backlog_warned:
                self._backlog_warned.add(dest)
                why = (f"last error: {s['last_error']}" if s.get("last_error")
                       else "it is not set up: run `streamsplitter setup`" if not s.get("active")
                       else "the upload is slower than the matches")
                self.notifier.warn(f"{label(dest)}: {waiting} clips are waiting to be sent ({why}).",
                                   key=f"backlog:{dest}")
            elif waiting <= BACKLOG_REARM:
                self._backlog_warned.discard(dest)

    def write_status(self, final: bool = False, reason: str | None = None) -> None:
        now = self.clock()
        data = {"tag": self.tag, "pid": os.getpid(), "host": socket.gethostname(), "running": not final,
                "stopping": self.stopping and not final, "started": self.started, "updated": now,
                "last_chunk": self.last_chunk,
                "last_chunk_time": (self.last_chunk or {}).get("read_at"), "chunks_read": self.chunks_read,
                "chunk_errors": self.chunk_errors, "clips_cut": self.clips, "scores_cut": self.scores,
                "last_clip": self.last_clip, "recorder": self.recorder, "recorder_restarts": self.recorder_restarts,
                "stream_status": self.stream_status, "stream": self.stream,
                "outbox": self.outbox_status, "youtube_quota": self.youtube_quota,
                "drive_storage": self.drive_storage, "last_warning": getattr(self.notifier, "last_warning", None), "stopped_reason": reason}
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            _save_state(self.status_path, data)
            self._status_error = False
        except OSError as e:                      # e.g. read_status had it open at that instant (Windows)
            if not self._status_error:
                print(f"could not write {self.status_path} ({e}); trying again", flush=True)
            self._status_error = True


def _hand(fn, row: dict, mon: Monitor, log) -> None:
    """Give a row to the publisher. Its failure (a bug) must not stop the follower; the clip is on disk anyway."""
    try:
        fn(row)
    except Exception as e:  # noqa: BLE001
        log(f"publish {row.get('name')}: {type(e).__name__}: {e}\n{traceback.format_exc()}", flush=True)
        mon.notifier.error(f"Could not queue {row.get('name') or 'a clip'} for upload ({type(e).__name__}: {e}); the "
                           f"clip is saved in the clips folder.", key="publish")


def follow(index_path: Path, pipe: Pipeline, event: str | None = None, poll_s: float = 2.0,
           schedule_every_s: float = 300.0, stop=None, log=print, publisher=None, notifier=None,
           monitor: Monitor | None = None) -> str:
    """Process chunks as they are listed in `index_path` until `stop()` is true or a stop file appears
    (request_stop). Each clip goes to `publisher` (publish.Publisher) right after its CLIP / SCORE line. A chunk that
    raises is logged, reported and skipped. A clean stop settles the results-screen searches still open (YouTube gets
    their match clip now); a crash leaves them in follower.json for the next run. Returns why it stopped."""
    index_path = Path(index_path)
    state_path = index_path.parent / STATE_FILE
    state, state_from = _load_state(state_path)
    for p in pipe.out_dir.glob("*.mp4"):          # replay numbering continues from the clips already cut
        if not p.stem.endswith("_score"):
            key = re.sub(r"_r\d+$", "", p.stem)
            pipe.namer.used[key] = pipe.namer.used.get(key, 0) + 1
    mon = monitor or Monitor(index_path.parent, notifier, publisher, log=log)
    if state_from == "backup":
        log(f"{STATE_FILE} was damaged (a power cut while it was written?); carrying on from {STATE_FILE}.bak",
            flush=True)
    done: set[int] = set()
    last_sched = -1e9
    index = None
    started = False                               # the first index read sets where to start (it needs the chunks)
    last_seq, last_t1, reread_from = None, float("inf"), float("inf")
    keep_backup = state_from == "file"
    save_failed = False

    def save() -> None:
        nonlocal keep_backup, save_failed
        state["score_pending"] = [_pending_record(r) for r in getattr(pipe, "score_pending", None) or []]
        try:
            _save_follower(state_path, state, keep_backup=keep_backup)
            keep_backup, save_failed = True, False
        except OSError as e:                      # e.g. a full disk: keep cutting; the disk check warns too
            log(f"could not save {state_path} ({e})", flush=True)
            if not save_failed:
                mon.notifier.error(f"Could not save StreamSplitter's progress ({STATE_FILE}: {e}). It keeps going; "
                                   f"after a restart it may cut a recent match again.", key="follower_state")
            save_failed = True

    def hand_score(r: dict) -> None:
        if r.get("score_clip"):
            log(f"SCORE {r['name']} -> {r['score_clip'].get('delivered') or r['score_clip']['path']}", flush=True)
            mon.score(r)
        elif r.get("score_error"):
            log(f"SCORE {r.get('name')}: no score version ({r['score_error']}); its match clip goes to YouTube",
                flush=True)
        if publisher is not None:
            _hand(publisher.on_score, r, mon, log)

    def no_score(r: dict, why: str) -> None:
        r["score_piece"] = None
        log(f"SCORE {r.get('name')}: {why}; settled without a score version", flush=True)
        hand_score(r)

    def finish(why: str) -> str:
        """A clean stop: the searches still open settle without a score version, so their match clips are queued
        for YouTube now rather than whenever StreamSplitter runs again."""
        mon.stopping = True
        mon.write_status()                        # "stopping" from now on: a new run waits for this one
        pending = getattr(pipe, "score_pending", None)
        rows = list(pending or [])
        if not started:                           # never taken up: settle them as saved
            rows += [dict(r) for r in state.get("score_pending") or [] if isinstance(r, dict)]
        if rows:
            if pending:
                pending.clear()
            for r in rows:
                no_score(r, "StreamSplitter is stopping")
            if started or state_from in ("file", "backup"):
                save()
        return why

    def start() -> None:
        """Where to start, from follower.json (or, without one, the clip folder), and the score searches to take
        up again."""
        nonlocal last_seq, last_t1, reread_from
        if not state and state_from in ("none", "lost"):
            got = _recover_state(index, pipe.out_dir)
            if got is not None:
                state.update(got[0])
                mon.notifier.warn(f"StreamSplitter's progress file ({state_path}) was "
                                  f"{'damaged' if state_from == 'lost' else 'missing'}, so it carries on {got[1]} "
                                  f"instead of cutting the whole recording again. The match it was on, and the "
                                  f"YouTube upload of the last match or two, may be missing: check the clip folder "
                                  f"and the playlist.", key="follower_state")
        last_seq = state.get("last_seq")
        last_t1 = state.get("last_t1", float("inf"))
        reread_from = last_t1 - REREAD_S
        pipe.skip_until = state.get("last_end")
        records = [dict(r) for r in state.get("score_pending") or []
                   if isinstance(r, dict) and r.get("start") is not None and r.get("end") is not None]
        if not records:
            return
        chunks = index.get("chunks") or []
        keep = [r for r in records if hasattr(pipe, "restore_pending") and _restorable(r, chunks, index_path.parent)]
        for r in records:
            if not any(r is k for k in keep):
                no_score(r, "its recorded video is gone, so its results screen cannot be searched")
        if keep:
            pipe.restore_pending(keep)
            reread_from = min(reread_from, min(float(r["start"]) for r in keep) - RESTORE_LEAD_S)
            log(f"taking up the results-screen search of {', '.join(str(r.get('name')) for r in keep)} again "
                f"(re-reading from its match)", flush=True)
        save()

    def stopping() -> str | None:
        if stop and stop():
            return "stopped"
        if mon.stop_requested():
            log("stop requested (stop file)", flush=True)
            return "stop requested"
        return None

    while True:
        why = stopping()
        if why:
            return finish(why)
        mon.poll(index)
        if event and time.time() - last_sched >= schedule_every_s:
            last_sched = time.time()
            s = fetch_schedule(event)
            if s:
                pipe.set_schedule(s)
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            time.sleep(poll_s)
            continue
        if not started:
            started = True
            start()
        new = [c for c in index.get("chunks", []) if c["seq"] not in done
               and (last_seq is None or c["seq"] > last_seq or c["t1"] > reread_from)]
        if not new:
            if index.get("finished"):              # a saved video copied to its end (vod.py): nothing more comes
                return finish("the whole recording is read")
            time.sleep(poll_s)
            continue
        for c in new:
            why = stopping()
            if why:
                return finish(why)
            done.add(c["seq"])
            path = index_path.parent / c["path"]
            if not path.is_file():                 # pruned meanwhile
                continue
            t0 = time.perf_counter()
            wall = c.get("wall_closed") or ((c.get("wall0") or 0.0) + c["t1"] - c["t0"])
            n_rows = len(getattr(pipe, "rows", None) or [])
            waiting = list(getattr(pipe, "score_pending", None) or [])
            try:
                rows = pipe.feed(path, c, now_wall=wall)
                settled = list(pipe.score_done)
                log(f"chunk {c['seq']} t={c['t0']:.0f}-{c['t1']:.0f} read in {time.perf_counter() - t0:.1f}s",
                    flush=True)
            except Exception as e:  # noqa: BLE001 - one bad chunk must not end the event's recording
                log(f"chunk {c['seq']}: {type(e).__name__}: {e} (skipped)\n{traceback.format_exc()}", flush=True)
                mon.chunk_failed(c, e)
                # what it finished before failing: clips cut, and score searches settled (left score_pending)
                rows = list((getattr(pipe, "rows", None) or [])[n_rows:])
                still = getattr(pipe, "score_pending", None) or []
                settled = [r for r in waiting if not any(r is x for x in still)]
            mon.chunk_read(c)
            if not index.get("vod"):
                _match_on(index_path.parent, pipe, c, wall)
            for r in rows:
                if index.get("vod") and c.get("wall0"):
                    _date_clip(r, c["wall0"] + r["end"] - c["t0"])
                late = (time.time() - (c["wall0"] + r["end"] - c["t0"]) if c.get("wall0") and not index.get("vod")
                        else None)
                log(f"CLIP {r['name']} -> {r['clip'].get('delivered') or r['clip']['path']}"
                    + (f" ({late:.0f} s after the match end)" if late is not None else "")
                    + (" [stream gap inside]" if r.get("gap_inside") else ""), flush=True)
                mon.clip(r)
                if publisher is not None:
                    _hand(publisher.on_clip, r, mon, log)
            for r in settled:
                hand_score(r)
            state["last_seq"], state["last_t1"] = c["seq"], c["t1"]
            if getattr(pipe, "rows", None):
                state["last_end"] = max([r["end"] for r in pipe.rows]
                                        + ([state["last_end"]] if state.get("last_end") is not None else []))
            save()
            mon.poll(index)


def _match_on(folder: Path, pipe, c: dict, wall: float) -> None:
    """Tell the recorder whether a match was being played at the end of this chunk (record.MATCH_ON_NAME): it does
    not change to a better stream in the middle of a match, which would put a jump into that clip. A timer that
    was read counting within MATCH_ON_AFTER_S says so (it also covers the pause between auto and teleop)."""
    try:
        counting = any(c["t1"] - e["t_last"] < MATCH_ON_AFTER_S for e in pipe.reader.episodes())
    except Exception:  # noqa: BLE001 - a pipeline without an overlay reader: the recorder changes when it likes
        return
    try:
        record.write_json_atomic(folder / record.MATCH_ON_NAME, {"counting": counting, "wall": wall})
    except OSError:
        pass


def _date_clip(row: dict, ended: float) -> None:
    """A saved video's clip (vod.py) cut hours later: its file time is when the match ended, which Google Drive
    shows as its modified time (publisher: the clip file's time is when it was played)."""
    try:
        os.utime(row["clip"]["path"], (ended, ended))
    except (KeyError, TypeError, OSError):
        pass


def keep_awake() -> subprocess.Popen | None:
    """Stop the computer sleeping while a recording runs (a sleeping laptop misses matches). Windows: the thread
    execution state, which lasts while this process runs. macOS: `caffeinate -i` tied to this process. Linux:
    `systemd-inhibit`, when there is one."""
    if os.name == "nt":
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        return None
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        return subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    if shutil.which("systemd-inhibit"):
        return subprocess.Popen(["systemd-inhibit", "--what=sleep", "--why=StreamSplitter recording", "sleep", "infinity"])
    return None


def _start_recorder(a, tag: str, url: str, also: list[str] = ()) -> subprocess.Popen:
    cmd = [sys.executable, "-m", "streamsplitter.cli", "record", url, "--tag", tag]
    if a.event:
        cmd += ["--event", a.event]
    for u in also:
        cmd += ["--also", u]
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    return subprocess.Popen(cmd, cwd=str(config.ROOT), **kw)


def _stop_recorder(p: subprocess.Popen) -> None:
    if p.poll() is not None:
        return
    try:
        p.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
        p.wait(timeout=20)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        p.kill()


class RecorderProc:
    """The recorder child process on the current stream link: restarted when it exits (check(), from the follower)
    and when the link changes (restart(), from a Slack command or EventWatch), always with the same tag, so its chunk
    index continues. enabled=False (--no-record): only the link is kept. `also_for(url)`: the event's other links,
    best first (EventWatch.others), asked at every (re)start; the recorder records the best that delivers."""

    def __init__(self, a, tag: str, *, enabled: bool = True, start=None, stop=None, also_for=None):
        self.a, self.tag, self.enabled = a, tag, enabled
        self._start, self._stop = start or _start_recorder, stop or _stop_recorder
        self.also_for = also_for
        self.proc = None
        self.url: str | None = None
        self.also: list[str] = []
        self._lock = threading.Lock()

    def _launch(self, url: str | None) -> None:
        self.url = url
        self.also = []
        if self.also_for is not None and url:
            try:
                self.also = list(self.also_for(url) or [])
            except Exception as e:  # noqa: BLE001 - the link alone still records
                print(f"could not list the event's other streams ({type(e).__name__}: {e})", flush=True)
        self.proc = self._start(self.a, self.tag, url, self.also) if (self.enabled and url) else None

    def start(self, url: str | None) -> None:
        with self._lock:
            self._launch(url)

    def restart(self, url: str | None) -> None:
        """Stop the recorder (it lists its last chunk) and start it on `url`."""
        with self._lock:
            if self.proc is not None:
                self._stop(self.proc)
            self._launch(url)

    def check(self):
        """Restart a recorder that exited; returns its exit code, else None (also while a restart is under way)."""
        if not self._lock.acquire(blocking=False):
            return None
        try:
            p = self.proc
            if p is None or p.poll() is None:
                return None
            code = p.returncode
            self._launch(self.url)
            return code
        finally:
            self._lock.release()

    def stop(self) -> None:
        with self._lock:
            if self.proc is not None:
                self._stop(self.proc)


def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("url", nargs="?", help="the event's stream: a video URL, youtube.com/@Channel/live, or "
                    "twitch.tv/<name> (default: [event] stream_url in config.toml, else the stream The Blue Alliance "
                    "lists for --event)")
    ap.add_argument("--event", help="TBA event key (e.g. 2026mttd), for match names from the schedule")
    ap.add_argument("--tag", help="name of this recording (default: the event key, else from the URL)")
    ap.add_argument("--deliver", type=Path, help="also copy every clip here (default: deliver_dir in config.toml)")
    ap.add_argument("--deliver-height", type=int, help="make the delivered copies this tall (e.g. 720: 3x smaller,"
                    " quicker to upload and to open on a phone); default: the full recording")
    ap.add_argument("--no-record", action="store_true", help="only follow an index another recorder writes")
    ap.add_argument("--no-publish", action="store_true", help="do not send clips to Slack or YouTube, and no Slack "
                    "DMs (console and log only), whatever config.toml says")


def _tag(a: argparse.Namespace) -> str:
    """The recording's name. ValueError without a URL, a --tag or an --event to name it after."""
    from .record import default_tag
    if not (a.tag or a.event or a.url):
        raise ValueError("give the stream link or --event (the TBA event key), e.g. streamsplitter live --event "
                         "2026mttd")
    return a.tag or a.event or default_tag(a.url)


# ---------------------------------------------------------------------------------------------------------------
# Publishing: notifier, destinations, outbox, publisher, heartbeat
# ---------------------------------------------------------------------------------------------------------------

def build_publishing(tag: str, log=print, only: set[str] | None = None, event: str | None = None):
    """(notifier, outbox, publisher, heartbeat_s, summary) for a live run, from config.toml's [event] [slack]
    [youtube] [drive] [wiki] [notify] and the stored secrets. outbox and publisher are None when none of Slack,
    YouTube, Google Drive and the wiki is turned on. A destination turned on but not set up (no token, no channel, no
    playlist, no Drive folder, no wiki bot password) is reported to the contact and the log with the fix, and left
    out: its clips still queue, and go after a restart once it is set up. Never raises for a missing or bad setting.
    `only` (vod.py --to): the destinations outside it are off for this run. `event` (vod.py --event): another
    event's key than [event]'s, whose clips then never go to [event]'s YouTube playlist or Drive folder."""
    from .publish.notify import Notifier, alarm_timing, redact
    from .publish.publisher import Publisher

    def say(s):
        log(s, flush=True)

    ev, sl, yt, wk, nt = (config.section(n) for n in ("event", "slack", "youtube", "wiki", "notify"))
    dr = config.section("drive")
    if event and event != ev.get("key"):
        ev = {"key": event}
        dr = {k: v for k, v in dr.items() if not k.startswith("event_folder")}
    if only is not None:
        sl, yt, wk, dr = ({**c, "enabled": bool(c.get("enabled")) and n in only}
                          for n, c in (("slack", sl), ("youtube", yt), ("wiki", wk), ("drive", dr)))
    problems: list[str] = []
    client = slack_mod = None
    if sl.get("enabled") or nt.get("slack_user"):
        token = config.get_secret("slack_bot_token")
        if not token:
            problems.append("Slack is turned on in config.toml but no Slack bot token is stored. Run `streamsplitter "
                            "setup` and do the Slack step again (or set enabled = false under [slack]).")
        else:
            try:
                from .publish import slack as slack_mod
                client = slack_mod.SlackClient(token)
            except Exception as e:  # noqa: BLE001 - not set up right: say how to fix it, keep recording
                problems.append(f"Slack could not be started ({type(e).__name__}: {redact(e)}). Run `streamsplitter "
                                f"setup` and check the Slack step.")
    heartbeat_s, alarm_s, note = alarm_timing(nt.get("heartbeat_s"), nt.get("alarm_after_s"))
    if note:
        say(note)
    notifier = Notifier(client if nt.get("slack_user") else None, nt.get("slack_user"), tag=tag, alarm_after_s=alarm_s,
                        state_path=config.data_root() / "outbox" / f"{tag}.alarm.json", log=say)
    for p in problems:
        notifier.error(p)
    if client is not None and not nt.get("slack_user"):
        notifier.info("No Slack contact is set ([notify] slack_user): warnings go to this window and the log only, "
                      "and there is no stopped-running alarm.")
    dests = {}
    summary = []
    if sl.get("enabled") and client is not None:
        if not sl.get("channel_id"):
            notifier.error("Slack: no channel is chosen ([slack] channel_id). Run `streamsplitter setup` and pick "
                           "the team's channel.")
        else:
            try:
                dests["slack"] = slack_mod.SlackDestination(client, sl["channel_id"],
                                                            clip_height=int(sl.get("clip_height") or 0),
                                                            channel_name=sl.get("channel_name"),
                                                            teams=[str(t) for t in sl.get("teams") or []])
                summary.append(f"Slack #{sl.get('channel_name') or sl['channel_id']} gets the matches of "
                               f"{', '.join(map(str, sl.get('teams') or [])) or 'no team (none set)'}")
            except Exception as e:  # noqa: BLE001
                notifier.error(f"Slack could not be started ({type(e).__name__}: {redact(e)}). Run `streamsplitter "
                               f"setup` and check the Slack step.")
    if yt.get("enabled") and yt.get("playlist_event") and ev.get("key") and yt["playlist_event"] != ev["key"]:
        # the event changed and the YouTube step was not run again: never fill the old event's playlist
        notifier.error(f"YouTube: the playlist set up ('{yt.get('playlist_title') or yt.get('playlist_id')}') is for "
                       f"{yt['playlist_event']}, but the event is now {ev['key']}, so nothing is uploaded. Run "
                       f"`streamsplitter setup --section youtube` to make this event's playlist; the clips wait and "
                       f"go up after the next start.")
    elif yt.get("enabled"):
        d = _youtube_destination(yt, notifier)
        if d is not None:
            dests["youtube"] = d
            summary.append(f"YouTube playlist \"{yt.get('playlist_title') or yt['playlist_id']}\" gets "
                           + ("every match" if yt.get("route", "all") == "all" else "the other matches"))
            if yt.get("tba_links"):
                d = _tba_link_destination(notifier)
                if d is not None:
                    dests["tba"] = d
                    summary.append("each YouTube video is added to its match on The Blue Alliance")
    if dr.get("enabled"):
        d = _drive_destination(dr, ev, yt, sl, notifier)
        if d is not None:
            dests["drive"] = d
            summary.append(f"the Google Drive folder \"{dr.get('folder_title') or dr['folder_id']}\" gets every match")
    if wk.get("enabled"):
        d = _wiki_destination(wk, ev, notifier)
        if d is not None:
            dests["wiki"] = d
            summary.append(f"the wiki page \"{d.page_title or d.page_format}\" gets every match")
    if not (sl.get("enabled") or yt.get("enabled") or wk.get("enabled") or dr.get("enabled")):
        return notifier, None, None, heartbeat_s, "publishing is off"
    outbox = PausableOutbox(config.data_root() / "outbox" / f"{tag}.sqlite", dests, notifier, log=say)
    for d in dests.values():
        if hasattr(d, "on_state") and d.on_state is None:
            # YouTube saves its upload session after every step, Slack the file id of an upload being shared
            d.on_state = outbox.save_state
    if "drive" in dests:
        dests["drive"].on_full = lambda text: _drive_full(outbox, notifier, text)
    if yt.get("paused") and "youtube" in dests:   # `pause youtube` from Slack holds across restarts
        outbox.pause("youtube")
        summary.append("YouTube uploads are paused (reply resume youtube to send them)")
    if dr.get("paused") and "drive" in dests:     # `pause drive`, or a full Drive
        outbox.pause("drive")
        summary.append("Google Drive uploads are paused (reply resume drive to send them)")
    if wk.get("paused") and "wiki" in dests:
        outbox.pause("wiki")
        summary.append("wiki uploads are paused (reply resume wiki to send them)")
    publisher = Publisher(outbox, cfg_event=ev, cfg_slack=sl, cfg_youtube=yt, cfg_wiki=wk, cfg_drive=dr, log=say)
    outbox.start()
    return notifier, outbox, publisher, heartbeat_s, "; ".join(summary) or "no destination is ready"


class PausableOutbox(Outbox):
    """The outbox, plus holding one destination's uploads (`pause youtube`, `pause drive`, `pause wiki` from Slack, or
    a full Google Drive): its worker sends nothing while it is held, the jobs stay queued on disk in order, and
    resume() sends them. An upload already under way finishes first. status() says "paused" per destination. (A
    subclass because outbox.py has another owner.)"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.paused: set[str] = set()

    def pause(self, dest: str) -> None:
        self.paused.add(dest)

    def resume(self, dest: str) -> None:
        self.paused.discard(dest)
        if dest == "drive":
            # the job that found the Drive full waits an hour before its next try: `resume drive` means now
            with self._lock:
                self._db.execute("UPDATE jobs SET next_try=0 WHERE destination=? AND status='queued' AND "
                                 "background=0", (dest,))
        ev = self._wake.get(dest)
        if ev is not None:
            ev.set()

    def step(self, dest: str) -> float:
        if dest in self.paused:
            return IDLE_S
        return super().step(dest)

    def status(self) -> dict:
        st = super().status()
        for d, s in st.items():
            s["paused"] = d in self.paused
        return st


def _youtube_destination(yt: dict, notifier):
    from .publish.notify import redact
    token, client_json = config.get_secret("youtube_token"), config.get_secret("youtube_client")
    if not token or not client_json:
        notifier.error("YouTube is turned on in config.toml but StreamSplitter is not logged in to YouTube. Run "
                       "`streamsplitter setup` and do the YouTube step (it logs in in the browser).")
        return None
    if not yt.get("playlist_id"):
        notifier.error("YouTube: no playlist is set ([youtube] playlist_id). Run `streamsplitter setup` to create "
                       "the event's playlist.")
        return None
    try:
        from .publish import templates, youtube
        client = youtube.YouTubeClient(token, json.loads(client_json),
                                       on_token=lambda j: config.set_secret("youtube_token", j))
        return youtube.YouTubeDestination(client, yt["playlist_id"], yt.get("title_format") or templates.TITLE_FORMAT,
                                          yt.get("description_format") or templates.DESCRIPTION_FORMAT,
                                          privacy=yt.get("privacy") or "unlisted")
    except Exception as e:  # noqa: BLE001 - not set up right: say how to fix it, keep recording
        notifier.error(f"YouTube could not be started ({type(e).__name__}: {redact(e)}). Run `streamsplitter setup` "
                       f"and do the YouTube step again.")
        return None


def _drive_destination(dr: dict, ev: dict, yt: dict, sl: dict, notifier):
    """The Google Drive destination from [drive], the Drive login and the team's Google sign-in app (stored for
    YouTube); None (the contact told how to fix it) when it is not set up. The description is [drive]
    description_format, else YouTube's, else the default. An event folder found or made for this event is saved in
    [drive], so the next start does not look for it again."""
    from .publish.notify import redact
    token, client_json = config.get_secret("drive_token"), config.get_secret("youtube_client")
    if not token or not client_json:
        notifier.error("Google Drive is turned on in config.toml ([drive] enabled) but StreamSplitter is not logged "
                       "in to Google Drive. Run `streamsplitter setup --section drive` (it logs in in the browser); "
                       "the clips wait and go up after the next start.")
        return None
    if not dr.get("folder_id"):
        notifier.error("Google Drive: no folder is set ([drive] folder_id). Run `streamsplitter setup --section "
                       "drive` to make it; the clips wait and go up after the next start.")
        return None
    try:
        from .publish import drive, templates
        client = drive.DriveClient(token, json.loads(client_json),
                                   on_token=lambda j: config.set_secret("drive_token", j))
        team = str((sl.get("teams") or [""])[0] or "") or None
        desc = dr.get("description_format") or yt.get("description_format") or templates.default_description(team)

        def saved(key, folder):
            if key == ev.get("key") == config.section("event").get("key"):   # not another event's (vod --event)
                config.save_sections({"drive": {"event_folder_id": folder["id"], "event_folder_event": key,
                                                "event_folder_title": folder.get("name"),
                                                "event_folder_url": folder.get("url")}})
        height = dr.get("height")
        return drive.DriveDestination(
            client, dr["folder_id"], folder_title=dr.get("folder_title"), event=ev,
            event_folder_id=dr.get("event_folder_id"), event_folder_event=dr.get("event_folder_event"),
            height=drive.DEFAULT_HEIGHT if height is None else int(height),
            name_format=dr.get("name_format") or drive.NAME_FORMAT, description_format=desc, on_folder=saved,
            warn=lambda text, key: notifier.warn(text, key=key), log=lambda s: notifier.log(s))
    except Exception as e:  # noqa: BLE001 - not set up right: say how to fix it, keep recording
        notifier.error(f"Google Drive could not be started ({type(e).__name__}: {redact(e)}). Run `streamsplitter "
                       f"setup --section drive`.")
        return None


def _tba_link_destination(notifier):
    """The Blue Alliance video links ([youtube] tba_links, for event organizers) from the stored TBA write key; None
    (the contact told how to fix it) without one. The links wait in the outbox meanwhile."""
    from .publish import tbalink
    from .publish.notify import redact
    auth_id, secret = config.get_secret("tba_write_id"), config.get_secret("tba_write_secret")
    if not auth_id or not secret:
        notifier.error("The Blue Alliance video links are on ([youtube] tba_links) but no TBA write key is stored. "
                       f"Run `{tbalink.SETUP_AGAIN}` to add it, or turn the links off there; the links wait and go "
                       "after the next start.")
        return None
    try:
        return tbalink.TBALinkDestination(tbalink.TrustedClient(auth_id, secret), log=lambda s: notifier.log(s))
    except Exception as e:  # noqa: BLE001 - not set up right: say how to fix it, keep recording
        notifier.error(f"The Blue Alliance video links could not be started ({type(e).__name__}: {redact(e)}). Run "
                       f"`{tbalink.SETUP_AGAIN}`.")
        return None


def _drive_full(outbox, notifier, text: str) -> None:
    """The Drive is full: hold its queue (also across a restart, as `pause drive` does) and tell the contact. Nothing
    is tried while it is held, so this comes once per pause; `resume drive` sends the held videos (and, if the Drive
    is still full, the next attempt pauses and says so again)."""
    outbox.pause("drive")
    try:
        config.save_sections({"drive": {"paused": True}})
    except Exception:  # noqa: BLE001 - held for this run anyway
        pass
    notifier.warn(text)


def _wiki_destination(wk: dict, ev: dict, notifier):
    """The team wiki's destination from [wiki] and the stored bot password; None (the contact told how to fix it)
    when it is not set up. A page title chosen for another event is not used: this event's page is named from the
    format (the setup clears it when the event changes; the contact is told)."""
    from .publish.notify import redact
    password = config.get_secret("wiki_bot_password")
    if not (wk.get("api_url") and wk.get("username") and password):
        notifier.error("The team wiki is turned on in config.toml ([wiki] enabled) but its bot login is not set up "
                       "(the api.php address, the bot user name or the bot password is missing). Run `streamsplitter "
                       "setup --section wiki`; the clips wait and go up after the next start.")
        return None
    try:
        from .publish import wiki
        page_title = wk.get("page_title") or None
        if page_title and wk.get("page_event") and ev.get("key") and wk["page_event"] != ev["key"]:
            now = wiki.render_page_title(wk.get("page_format") or wiki.PAGE_FORMAT, ev.get("name"), ev.get("year"),
                                         ev["key"])
            notifier.warn(f"The wiki page set up ('{page_title}') is for {wk['page_event']}, but the event is now "
                          f"{ev['key']}: its matches go to '{now}' instead. To pick the page, run `streamsplitter "
                          "setup --section wiki`.", key="wiki_page")
            page_title = None
        client = wiki.WikiClient(wk["api_url"], wk["username"], password)
        return wiki.WikiDestination(client, page_title=page_title, page_event=wk.get("page_event") if page_title
                                    else None, page_format=wk.get("page_format") or wiki.PAGE_FORMAT,
                                    embed_format=wk.get("embed_format") or wiki.EMBED_FORMAT,
                                    private=bool(wk.get("private", True)),
                                    private_markup=wk.get("private_markup") or wiki.PRIVATE_MARKUP,
                                    warn=lambda text: notifier.warn(text, key=f"wiki:{text[:60]}"))
    except Exception as e:  # noqa: BLE001 - not set up right: say how to fix it, keep recording
        notifier.error(f"The team wiki could not be started ({type(e).__name__}: {redact(e)}). Run `streamsplitter "
                       f"setup --section wiki`.")
        return None


class Heartbeat:
    """Keeps the stopped-running alarm pushed back (publish/notify.py) while the follower is alive. It pushes only
    while the follower loop has shown life within `hung_s`, so a follower stuck on one chunk lets the alarm go off
    too, not only a dead process. When Slack cannot be reached it tries again after HEARTBEAT_RETRY_S."""

    def __init__(self, notifier, monitor: Monitor, every_s: float = HEARTBEAT_S, hung_s: float = FOLLOWER_HUNG_S,
                 clock=time.time):
        self.notifier, self.monitor = notifier, monitor
        self.every_s, self.hung_s, self.clock = every_s, hung_s, clock
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="heartbeat", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(5.0)

    def beat(self) -> bool:
        if self.clock() - self.monitor.tick >= self.hung_s:
            return True                          # stuck follower: let the alarm come due
        try:
            return self.notifier.heartbeat()
        except Exception as e:  # noqa: BLE001 - never let the heartbeat thread die
            print(f"heartbeat: {type(e).__name__}: {e}", flush=True)
            return False

    def next_wait(self, ok: bool, took_s: float) -> float:
        """Seconds until the next beat, counted from the start of the one that took `took_s`: each alarm is due
        alarm_after_s after its beat started, so a slow Slack call must not push the next beat later too (that
        alarm could then not be deleted either)."""
        every = self.every_s if ok else min(HEARTBEAT_RETRY_S, self.every_s)
        return max(1.0, every - took_s)

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            ok = self.beat()
            self._stop.wait(self.next_wait(ok, time.monotonic() - t0))


# ---------------------------------------------------------------------------------------------------------------
# The stream link, the stream notices, and the Slack commands' actions
# ---------------------------------------------------------------------------------------------------------------

SRC_CLI, SRC_CONFIG, SRC_SLACK, SRC_TBA = "the command line", "config.toml", "Slack", "The Blue Alliance"
SRC_SEARCH = "a YouTube search"            # no link anywhere, and the search (scout.py) found the event's stream


def _clock_text(t: float | None) -> str:
    return time.strftime("%a %H:%M", time.localtime(t)) if t else "?"


class EventWatch:
    """Which stream link is recorded, and what the contact hears about the stream (module docstring): the start
    and back notices, the morning "waiting for the stream" notice, TBA's webcasts. tick() runs every WATCH_TICK_S on
    the watcher thread, never the follower's (it may wait on TBA, YouTube or a recorder restart)."""

    def __init__(self, folder: Path, recorder: RecorderProc, notifier, monitor: Monitor, *, event_key: str | None,
                 cli_url: str | None = None, channel_name: str | None = None, clips_dir: Path | None = None,
                 clock=time.time, tba_get=None, channel_live=streams.channel_live_page, tz=None, log=print):
        """`channel_name`: the Slack channel the team's matches go to (None: Slack posting is off). `tba_get`,
        `channel_live` and `tz` (the event's time zone) are for tests."""
        self.folder = Path(folder)
        self.recorder, self.notifier, self.monitor = recorder, notifier, monitor
        self.event_key, self.cli_url = event_key, cli_url
        self.channel_name, self.clips_dir = channel_name, clips_dir
        self.clock, self.tba_get, self.tz, self.log = clock, tba_get, tz, log
        self.channel_live = None
        if channel_live is not None:              # a video's channel does not change: ask YouTube once per video
            found: dict[str, str | None] = {}
            self.channel_live = lambda u: found[u] if u in found else found.setdefault(u, channel_live(u))
        self.started = clock()
        self.can_reply = False                  # the contact's replies are read (publish/commands.py)
        self.event: dict | None = None          # TBA's event record (name, dates, timezone, webcasts)
        self.event_error: str | None = None
        self._event_next = 0.0
        self.url: str | None = None
        self.source: str | None = None
        self.label: str | None = None           # TBA's words for the link it suggested
        self.mode = "fixed"                     # "auto": no link set, TBA's is followed
        self.live = False
        self.live_since = self.live_title = self.live_url = None
        self._known_runs: set | None = None
        self._last_video = None
        self._announced = False
        self._morning_day = None
        self._tba_next = 0.0
        self._tz_said = False
        self.scout = None                       # scout.Scout, the search for streams on no list (None: off)
        self.found: dict[str, dict] = {}        # link -> {"video", "title", "channel", "seen", "at"}: found and vetted
        self._suggested: set[str] = set()       # videos the contact was told about that could not be vetted
        self._scout_next = self.started + SCOUT_FIRST_S
        self._links_written: dict | None = None

    # TBA ---------------------------------------------------------------------------------------------------------
    def _cache(self) -> Path:
        return self.folder / "tba_event.json"

    def fetch_event(self) -> dict | None:
        """TBA's event record now (kept in tba_event.json for a restart without internet), else the last one had."""
        if not self.event_key:
            return self.event
        try:
            ev = streams.fetch_event(self.event_key, self.tba_get)
        except Exception as e:  # noqa: BLE001 - no key, offline: the cached copy, and ask again later
            self.event_error = f"{type(e).__name__}: {e}"
            self._event_next = self.clock() + EVENT_RETRY_S
            if self.event is None:
                try:
                    self.event = json.loads(self._cache().read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pass
            return self.event
        self.event, self.event_error = ev, None
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            _save_state(self._cache(), ev)
        except OSError:
            pass
        return ev

    def _local(self, t: float):
        tz, exact = (self.tz, True) if self.tz is not None else streams.event_zone(self.event)
        if not exact and not self._tz_said and (self.event or {}).get("timezone"):
            self._tz_said = True
            self.log(f"no time-zone data for {self.event['timezone']} (install the tzdata package); event times use "
                     f"this computer's time zone", flush=True)
        return streams.event_now(self.event, t, tz)

    def tba_pick(self) -> dict | None:
        """TBA's recommended link for today (streams.pick), asked now."""
        ev = self.fetch_event()
        if not ev:
            return None
        return streams.pick(ev, self._local(self.clock()).date(), self.channel_live)

    def alternates(self, url: str) -> list[str]:
        """The event's other streams the recorder tries when `url` is not live (streams.alternates, from TBA's
        webcasts; asked now when not had yet)."""
        ev = self.event if self.event is not None else self.fetch_event()
        return streams.alternates(ev, self._local(self.clock()).date(), url) if ev else []

    def pinned(self) -> bool:
        """The link was chosen for this run (`stream <link>` from Slack, a URL on the command line): it stays the
        first choice whatever else is listed or found."""
        return self.mode == "fixed" and self.source in (SRC_SLACK, SRC_CLI)

    def links(self, url: str | None = None) -> list[str]:
        """Every link a recorder started on `url` may record, best first: it records the first that delivers and
        looks every few minutes for a better one (record.py). `url` comes first when it was chosen for this run
        (pinned()) or is not a Twitch link. After it, YouTube before Twitch (user, 2026-11-10: MadTown 2026's Twitch
        stream glitched all Saturday while a team streamed the event on YouTube): TBA's other webcasts
        (alternates()), then the streams the search found (self.found)."""
        url = url or self.url
        if not url:
            return []
        first = self.pinned() or streams.kind(url) != "twitch"
        kinds = {"youtube": 1, "twitch": 2}
        return sorted(dict.fromkeys([url, *self.alternates(url), *self.found]),
                      key=lambda u: 0 if (first and u == url) else kinds.get(streams.kind(u), 3))

    def others(self, url: str) -> list[str]:
        """RecorderProc.also_for: the other links of a recorder started on `url`, best first. The whole list also
        goes into links.json, which the recorder reads at every look, so a stream found later needs no restart."""
        links = self.links(url)
        self.write_links(url, links)
        return [u for u in links if u != url]

    def write_links(self, url: str | None = None, links: list[str] | None = None) -> None:
        """links.json for the recorder (record.LINKS_NAME), rewritten only when the list changed."""
        url = url or getattr(self.recorder, "url", None) or self.url
        if not url:
            return
        data = {"for": url, "links": links if links is not None else self.links(url)}
        if data == self._links_written:
            return
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            record.write_json_atomic(self.folder / record.LINKS_NAME, data)
            self._links_written = data
        except OSError as e:
            self.log(f"could not write {record.LINKS_NAME} ({type(e).__name__}: {e})", flush=True)

    def links_text(self, url: str | None, also: list[str]) -> str:
        """The start notice's words for the link(s) the recorder was started with."""
        if not url:
            return "no stream link yet"
        links = [u for u in self.links(url) if u == url or u in also]
        if links[0] == url:
            return (f"stream link {url} (from {self.source})"
                    + (f"; when it is not live: {', '.join(links[1:])}" if links[1:] else ""))
        return (f"stream links, best first: {', '.join(links)} (YouTube before Twitch; {url} is the one from "
                f"{self.source}). The first that is live is recorded, and a better one is looked for every "
                f"{record.RECHECK_S / 60:.0f} minutes")

    def event_name(self) -> str:
        # [event]'s name until TBA's record is fetched: the start DM said "for 2026mttd (2026mttd)" (2026-11-05)
        return ((self.event or {}).get("name") or self._config_event().get("name") or self.event_key
                or "the event")

    # the link ------------------------------------------------------------------------------------------------------
    def _config_event(self) -> dict:
        """[event] of config.toml when it is this event's (another event's link must not be used or overwritten)."""
        ev = config.section("event")
        return ev if not (self.event_key and ev.get("key") and ev["key"] != self.event_key) else {}

    def save_link(self, url: str | None) -> bool:
        """[event] stream_url = url (None: removed, so TBA's is followed). False when config.toml is another event's
        or cannot be written."""
        if not self._config_event() and config.section("event").get("key"):
            return False
        try:
            config.save_sections({"event": {"stream_url": url}})
            return True
        except Exception as e:  # noqa: BLE001 - say so; the link still changes for this run
            self.log(f"could not save the stream link in config.toml ({type(e).__name__}: {e})", flush=True)
            return False

    def _set(self, url: str | None, source: str | None, label: str | None = None) -> None:
        self.url, self.source, self.label = url, source, label
        self.log(f"stream link: {url or 'none'}" + (f" (from {source})" if source else ""), flush=True)
        self._status()

    def choose(self) -> str | None:
        """The link to start with: the command line's, else config.toml's, else TBA's (asked now). None: no link."""
        cfg = (self._config_event().get("stream_url") or "").strip() or None
        if self.cli_url:
            norm = streams.normalize_stream_url(cfg or "")
            same = cfg is not None and (cfg == self.cli_url or (norm is not None and norm
                                                                == streams.normalize_stream_url(self.cli_url)))
            self._set(self.cli_url, SRC_CONFIG if same else SRC_CLI)
        elif cfg:
            self._set(cfg, SRC_CONFIG)
        else:
            self.mode = "auto"
            p = self.tba_pick()
            self._tba_next = self.clock() + TBA_RECHECK_S
            if p:
                self._set(p["url"], SRC_TBA, p["label"].replace(" (recommended)", ""))
            else:
                self._set(None, None)
        return self.url

    def switch(self, url: str, source: str, label: str | None = None) -> None:
        """Record `url` from now on: the recorder restarts on it (same tag: the chunk index continues)."""
        self.url, self.source, self.label = url, source, label        # before the restart: links() ranks by them
        self.recorder.restart(url)
        self.monitor.recorder = None if not self.recorder.enabled else "running"
        self._set(url, source, label)

    def no_link_text(self) -> str:
        why = ("lists no stream for it yet" if self.event is not None or not self.event_key
               else "could not be asked (no TBA key, or no internet)")
        how = (f"To set one, reply here: stream <link> (the event's YouTube or Twitch link, for example stream "
               f"https://www.youtube.com/@ChannelName/live)." if self.can_reply else
               "To set one, run `streamsplitter setup --section stream` and start StreamSplitter again.")
        return (f"No stream link for {self.event_name()}: none is set, and The Blue Alliance {why}. {how} "
                f"StreamSplitter asks The Blue Alliance again every {TBA_RECHECK_S / 60:.0f} minutes.")

    def _how_to_change(self) -> str:
        return ("To use a different link, reply here: stream <link>" if self.can_reply else
                "To use a different link, run `streamsplitter setup --section stream` and start StreamSplitter again")

    # every WATCH_TICK_S -------------------------------------------------------------------------------------------
    def tick(self) -> None:
        now = self.clock()
        if self.event is None and self.event_key and now >= self._event_next:
            self.fetch_event()
        index = self._read_index()
        self.live = bool(index) and index.get("status") == "recording"
        self._runs(index)
        if (self.url is None or (self.mode == "auto" and not self.live)) and now >= self._tba_next:
            self._recheck_tba()
        self._scout_tick(now, index)
        self._morning(now)
        self._status()

    def _read_index(self) -> dict | None:
        try:
            data = json.loads((self.folder / "index.json").read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def _status(self) -> None:
        self.monitor.stream = {"url": self.url, "source": self.source, "following_tba": self.mode == "auto",
                               "live": self.live, "live_since": self.live_since, "title": self.live_title,
                               "video_url": self.live_url}

    def _recheck_tba(self) -> None:
        self._tba_next = self.clock() + TBA_RECHECK_S
        p = self.tba_pick()
        if not p or p["url"] == self.url:
            return
        old, label = self.url, p["label"].replace(" (recommended)", "")
        self.switch(p["url"], SRC_TBA, label)
        if old is None:
            self.notifier.info(f"The Blue Alliance now lists a stream for {self.event_name()}: {p['url']} ({label}). "
                               f"Recording it; you get a message here when it is live.")
        else:
            self.notifier.info(f"Switched the stream link to {p['url']} ({label}, from The Blue Alliance); it was "
                               f"{old}. {self._how_to_change()}.")

    # a stream on no list (scout.py) ---------------------------------------------------------------------------------
    def find_streams(self, sc) -> None:
        """Turn the search on (`sc`: a scout.Scout). What an earlier run of StreamSplitter found is on the list
        again (found_streams.json in the chunk folder)."""
        self.scout = sc
        try:
            d = json.loads((self.folder / FOUND_FILE).read_text(encoding="utf-8"))
            now = self.clock()
            self.found = {u: i for u, i in (d.get("found") or {}).items()
                          if isinstance(i, dict) and now - float(i.get("at") or 0.0) <= FOUND_KEEP_S}
            self._suggested = {str(v) for v in d.get("suggested") or []}
        except (OSError, ValueError, AttributeError, TypeError):
            pass

    def _save_found(self) -> None:
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            record.write_json_atomic(self.folder / FOUND_FILE,
                                     {"found": self.found, "suggested": sorted(self._suggested)})
        except OSError as e:
            self.log(f"could not write {FOUND_FILE} ({type(e).__name__}: {e})", flush=True)

    def _recording(self, index: dict | None) -> str | None:
        """The link of the recorder's run in progress (index.json), else None."""
        runs = [r for r in (index or {}).get("runs") or [] if isinstance(r, dict)]
        if not (self.live and runs and runs[-1].get("ended") is None):
            return None
        return runs[-1].get("link") or runs[-1].get("webpage_url")

    def _scout_tick(self, now: float, index: dict | None) -> None:
        """Search YouTube for a live stream of the event that is on no list: every SCOUT_S on the event's days
        (SCOUT_IDLE_S while a YouTube stream is being recorded). The search runs on the scout's own thread; a later
        tick takes what it found."""
        if self.scout is None:
            return
        res = self.scout.take()
        if res is not None:
            self._scouted(res, index)
        if now < self._scout_next or self.scout.busy():
            return
        on_youtube = streams.kind(self._recording(index)) == "youtube"
        self._scout_next = now + (SCOUT_IDLE_S if on_youtube else SCOUT_S)
        days = streams.event_days(self.event)
        if days and not days[0] <= self._local(now).date() <= days[1]:
            return
        known = {streams.video_id(u) for u in self.links()} | {i.get("video") for i in self.found.values()}
        known |= {r.get("video") for r in (index or {}).get("runs") or [] if isinstance(r, dict)}
        self.scout.start(self.event, known=known - {None}, refresh=self.fetch_event)

    def _scouted(self, res: dict, index: dict | None) -> None:
        """A finished search. A stream whose picture shows the event's teams joins the recording's list (the
        recorder takes it by itself when it is the best that delivers); one that could not be confirmed is only
        suggested to the contact, once, and looked at again by every search."""
        now = self.clock()
        if res.get("error"):
            self.log(f"stream search: {res['error']}", flush=True)
        rec = self._recording(index)
        changed = False
        for s in res.get("streams") or []:
            what = f"{' '.join(str(s.get('title') or '').split())} ({s.get('channel') or 'YouTube'}) {s['url']}"
            if s.get("ok"):
                if s["url"] in self.found:
                    continue
                self.found[s["url"]] = {"video": s.get("video"), "title": s.get("title"), "channel": s.get("channel"),
                                        "seen": s.get("seen"), "at": now}
                changed = True
                self._found_notice(s, what, rec)
            elif s.get("live"):
                self.log(f"stream search: {what} may be the event; not confirmed ({s.get('why')})", flush=True)
                if s.get("video") not in self._suggested and streams.kind(rec) != "youtube":
                    self._suggested.add(s.get("video"))
                    changed = True
                    how = (f"To record it now, reply here: stream {s['url']}" if self.can_reply else
                           "To record it now, run `streamsplitter setup --section stream` and start StreamSplitter "
                           "again")
                    self.notifier.info(f"A live YouTube stream may be {self.event_name()}: {what}. It is not "
                                       f"recorded: StreamSplitter could not confirm it is this event "
                                       f"({s.get('why')}); it looks again every {SCOUT_S / 60:.0f} minutes. {how}")
        for u in [u for u, i in self.found.items() if now - float(i.get("at") or 0.0) > FOUND_KEEP_S]:
            del self.found[u]
            changed = True
        if changed:
            self._save_found()
        self.write_links()                       # also TBA's webcasts as the search just read them

    def _found_notice(self, s: dict, what: str, rec: str | None) -> None:
        head = (f"Found a YouTube stream of {self.event_name()} that is on no list: {what}. Its picture shows this "
                f"event's teams ({', '.join(s.get('seen') or [])}). ")
        if self.url is None:                     # no link anywhere: this is the one
            self.switch(s["url"], SRC_SEARCH)
            self.notifier.info(f"{head}No other link is set, so it is recorded from now. {self._how_to_change()}")
            return
        links = self.links()
        at = links.index(s["url"]) if s["url"] in links else len(links)
        cur = links.index(rec) if rec in links else len(links)
        if rec is None:
            text = (f"{head}It is on the recording's list now; you get a message here when the recorder is on it. "
                    f"{self._how_to_change()}")
        elif at < cur:
            text = (f"{head}It is on the recording's list now, ahead of {rec}: the recorder changes to it within "
                    f"about {record.RECHECK_S / 60:.0f} minutes, between matches, and says so here.")
        elif self.pinned() and streams.kind(rec) != "youtube":
            text = (f"{head}It is not recorded: you chose {self.url}, which stays the first choice. "
                    + (f"To record the found one, reply here: stream {s['url']}" if self.can_reply else
                       "To record the found one, start StreamSplitter on it"))
        else:
            self.log(f"stream search: found {what}; kept as a spare (teams {', '.join(s.get('seen') or [])} on its "
                     f"picture)", flush=True)
            return
        self.notifier.info(text)

    # what the contact hears about the stream -----------------------------------------------------------------------
    def _runs(self, index: dict | None) -> None:
        """Announce each recorder run that starts a stream (a new video, or the first one this run) or brings one
        back after STREAM_BACK_S; reconnects inside one stream stay quiet."""
        runs = sorted((r for r in (index or {}).get("runs") or [] if isinstance(r, dict) and r.get("run") is not None),
                      key=lambda r: r["run"])
        if self._known_runs is None:              # what an earlier run of StreamSplitter recorded is not news
            self._known_runs = {r["run"] for r in runs if float(r.get("started") or 0.0) < self.started}
            prior = [r for r in runs if r["run"] in self._known_runs]
            self._last_video = prior[-1].get("video") if prior else None
        for r in runs:
            if r["run"] in self._known_runs:
                continue
            self._known_runs.add(r["run"])
            self._announce(r, index, runs)

    def _gap(self, r: dict, index: dict, runs: list[dict]) -> float:
        prev = next((x for x in reversed(runs) if x["run"] < r["run"]), None)
        if prev is None:
            return 0.0
        ends = [c.get("wall_closed") or 0.0 for c in index.get("chunks") or [] if c.get("run") == prev["run"]]
        end = max(ends) if ends else (prev.get("ended") or prev.get("started") or 0.0)
        return max(0.0, float(r.get("started") or 0.0) - float(end))

    def _where(self) -> str:
        if self.channel_name:
            return f"your team's matches will be posted to #{self.channel_name.lstrip('#')}"
        return f"the clips are saved in {self.clips_dir}" if self.clips_dir else "the clips are being cut"

    def _announce(self, r: dict, index: dict, runs: list[dict]) -> None:
        video, title = r.get("video"), " ".join(str(r.get("title") or "").split())
        url = r.get("webpage_url") or index.get("source_url") or self.url
        first, self._announced = not self._announced, True
        new_video = video != self._last_video
        gap = 0.0 if (first or new_video) else self._gap(r, index, runs)
        self._last_video = video
        self.live_since, self.live_title, self.live_url = r.get("started"), title or None, url
        what = " ".join(x for x in (title, url) if x)
        prev = next((x for x in reversed(runs) if x["run"] < r["run"]), None) or {}
        old = prev.get("link") or prev.get("webpage_url")
        links = self.links() if r.get("link") else []
        if new_video and not first and old and str(prev.get("end_reason") or "").startswith("a better stream is live"):
            text = (f"Changed to a better stream: {what} (it was {old}). Recording; {self._where()}."
                    + (f" To go back, reply here: stream {old}" if self.can_reply else ""))
        elif first or new_video:
            text = f"Stream started: {what}. Recording; {self._where()}."
            if links and r["link"] in links[1:]:
                text += (f" The first choice, {links[0]}, is not live; it is looked at every "
                         f"{record.RECHECK_S / 60:.0f} minutes and taken as soon as it delivers.")
        elif gap >= STREAM_BACK_S:
            text = f"Stream back after {int(gap // 60)} min: {what}. Recording again."
        else:
            return
        self.notifier.info(text)
        self.monitor.arrival_announced()

    def _morning(self, now: float) -> None:
        """Once on each event day from MORNING_HOUR (event time): if nothing is live, the "waiting" notice."""
        local = self._local(now)
        day = local.date()
        if day == self._morning_day or local.hour < MORNING_HOUR or now - self.started < WAIT_GRACE_S:
            return
        days = streams.event_days(self.event)
        if days and not days[0] <= day <= days[1]:
            return
        self._morning_day = day
        if self.live:
            return
        note = self._morning_link(day)
        if self.url is None:
            self.notifier.info(self.no_link_text())
            return
        self.notifier.info(f"{note}Waiting for the stream to start. Checking the suspected stream link every minute: "
                           f"{self.url}. {self._how_to_change()}")

    def _morning_link(self, day) -> str:
        """Each event morning while nothing is live: a webcast TBA dates today replaces a set-up link to a single
        video (yesterday's, say; a channel's live page is kept: it follows the channel, and a dated video would go
        stale if the stream is restarted as a new one). Following TBA: its pick for today. A link the contact chose
        from Slack stays; the notice names TBA's."""
        ev = self.fetch_event()
        if not ev:
            return ""
        if self.mode == "auto":
            p = streams.pick(ev, day, self.channel_live)
            url, label = (p["url"], p["label"].replace(" (recommended)", "")) if p else (None, None)
        elif self.url and streams.is_live_page(self.url):
            return ""                            # follows the channel from day to day: not live yet, not stale
        else:
            url, label = streams.todays_webcast(ev, day), "today's stream"
        if not url or url == self.url:
            return ""
        if self.source == SRC_SLACK:
            return f"(The Blue Alliance lists {url} for today; reply stream auto to use it.) "
        old = self.url
        self._tba_next = self.clock() + TBA_RECHECK_S
        self.switch(url, SRC_TBA, label)
        return f"Switched to {label} from The Blue Alliance" + (f" (it was {old})" if old else "") + ". "


def _count_today(folder: Path | None) -> int:
    """Match clips (not score versions) written today, by this computer's clock."""
    if folder is None:
        return 0
    today = time.localtime().tm_yday, time.localtime().tm_year
    n = 0
    for p in Path(folder).glob("*.mp4"):
        if p.stem.endswith("_score"):
            continue
        try:
            t = time.localtime(p.stat().st_mtime)
        except OSError:
            continue
        n += (t.tm_yday, t.tm_year) == today
    return n


class LiveControls:
    """What the Slack commands do (publish/commands.py dispatch); every method returns the reply (plain text)."""

    def __init__(self, watch: EventWatch, monitor: Monitor, *, publisher=None, clips_dir: Path | None = None,
                 channel_name: str | None = None, clock=time.time):
        self.watch, self.monitor, self.publisher = watch, monitor, publisher
        self.clips_dir, self.channel_name, self.clock = clips_dir, channel_name, clock

    @property
    def outbox(self):
        return getattr(self.publisher, "outbox", None)

    def _where(self) -> str:
        return f"#{self.channel_name.lstrip('#')}" if self.channel_name else "the team's Slack channel"

    # status --------------------------------------------------------------------------------------------------------
    def stream_text(self) -> str:
        w = self.watch
        if w.url is None:
            return "Stream: no link yet. " + ("Reply set stream <link> to set one." if w.can_reply else "")
        src = (" (following The Blue Alliance)" if w.mode == "auto" else f" (from {w.source})") if w.source else ""
        if w.live:
            what = " ".join(x for x in (w.live_title, w.live_url) if x)
            return f"Stream: live, recording since {_clock_text(w.live_since)}: {what}. Link: {w.url}{src}."
        return f"Stream: waiting for it to start; checking {w.url} every minute{src}."

    def _event_label(self) -> str:
        """"MadTown Throwdown (2026mttd)": the event this run records."""
        tag = getattr(self.monitor, "tag", None)
        try:
            name = self.watch.event_name()
        except Exception:  # noqa: BLE001 - only words
            name = None
        return f"{name} ({tag})" if name and tag and name != tag else str(name or tag or "the event")

    def help_facts(self) -> dict:
        """What the Slack help's first line says about this run (publish/commands.HelpContext)."""
        return {"recording": self._event_label(), "since": getattr(self.monitor, "started", None),
                "drive": self.drive_on, "wiki": self.wiki_on}

    def status_text(self) -> str:
        m, now = self.monitor, self.clock()
        lines = [f"Recording: {self._event_label()} on {socket.gethostname()}, running since "
                 f"{_clock_text(m.started)} ({_duration(now - m.started)}).", self.stream_text()]
        last = (m.last_chunk or {}).get("read_at")
        lines.append(f"Last video from the stream: {_duration(now - last)} ago." if last else
                     "No video from the stream yet in this run.")
        clips = f"Clips cut today: {_count_today(self.clips_dir)}"
        lines.append(clips + (f" (last: {m.last_clip})." if m.last_clip else "."))
        box = self.outbox
        if box is not None:
            from .publish.outbox import label
            try:
                st = box.status()
            except Exception:  # noqa: BLE001 - informational
                st = {}
            for dest, s in st.items():
                waiting = s.get("queued", 0) + s.get("running", 0)
                line = f"{label(dest)}: {s.get('done', 0)} sent, {waiting} waiting, {s.get('failed', 0)} failed"
                if s.get("paused"):
                    line += f" (paused: reply resume {dest} to send them)"
                elif not s.get("active", True):
                    line += " (not set up: run streamsplitter setup)"
                elif s.get("failing_since") and s.get("last_error"):
                    line += f"; failing since {_clock_text(s['failing_since'])}: {s['last_error']}"
                lines.append(line + ".")
        q = m.youtube_quota or {}
        if q.get("uploads_left") is not None:
            lines.append(f"YouTube uploads left today: {q['uploads_left']}"
                         + (" (none until midnight US Pacific)." if q.get("blocked") else "."))
        d = m.drive_storage or {}
        if d.get("free_gb") is not None:
            lines.append(f"Google Drive space: {d['free_gb']} GB free"
                         + (f" of {d['limit_gb']} GB" if d.get("limit_gb") is not None else "")
                         + (f", about {d['matches_left']} more matches" if d.get("matches_left") is not None else "")
                         + (f" ({d['account']})" if d.get("account") else "") + ".")
        return "\n".join(lines)

    # stream ------------------------------------------------------------------------------------------------------
    def set_stream(self, url: str) -> str:
        w = self.watch
        if url == w.url:
            return f"That is the link StreamSplitter checks already. {self.stream_text()}"
        saved = w.save_link(url)
        w.mode = "fixed"
        w.switch(url, SRC_SLACK)
        where = "saved in config.toml" if saved else "for this run only: config.toml could not be changed"
        if not w.recorder.enabled:
            return f"OK: the stream link is now {url} ({where}). This run does not record (started with --no-record)."
        return (f"OK: the stream link is now {url} ({where}). The recorder restarted on it; you get a message here "
                f"when the stream is live.")

    def stream_auto(self) -> str:
        w = self.watch
        w.save_link(None)
        w.mode = "auto"
        p = w.tba_pick()
        w._tba_next = self.clock() + TBA_RECHECK_S
        if p is None:
            now = f"; it keeps checking {w.url}" if w.url else ""
            return (f"OK: following The Blue Alliance's link from now on, but it lists no stream for "
                    f"{w.event_name()} right now{' (or could not be asked)' if w.event_error else ''}{now}. It asks "
                    f"again every {TBA_RECHECK_S / 60:.0f} minutes.")
        label = p["label"].replace(" (recommended)", "")
        if p["url"] == w.url:
            w._set(w.url, SRC_TBA, label)
            return f"OK: following The Blue Alliance's link, {p['url']} ({label}), which is the one it checks already."
        w.switch(p["url"], SRC_TBA, label)
        return f"OK: switched to The Blue Alliance's link: {p['url']} ({label}). The recorder restarted on it."

    # teams -------------------------------------------------------------------------------------------------------
    def _slack_dest(self):
        return (getattr(self.outbox, "destinations", None) or {}).get("slack")

    def teams_text(self) -> str:
        cfg = getattr(self.publisher, "cfg_slack", None)
        teams = list((cfg if cfg is not None else config.section("slack")).get("teams") or [])
        off = "" if self._slack_dest() is not None else " (Slack posting is off or not set up: streamsplitter setup)"
        if not teams:
            return f"No team's matches go to {self._where()}{off}."
        return f"Matches of {', '.join(map(str, teams))} go to {self._where()}{off}."

    def set_teams(self, teams: list[str]) -> str:
        try:
            config.save_sections({"slack": {"teams": list(teams)}})
            saved = ""
        except Exception as e:  # noqa: BLE001
            saved = f" (for this run only: config.toml could not be changed: {type(e).__name__})"
        if self.publisher is not None and getattr(self.publisher, "cfg_slack", None) is not None:
            self.publisher.cfg_slack["teams"] = list(teams)
        dest = self._slack_dest()
        if dest is not None and hasattr(dest, "teams"):
            dest.teams = {str(t) for t in teams}
        if not teams:
            return f"OK: no team's matches go to {self._where()} now{saved}. Reply teams 5026 to post them again."
        off = "" if dest is not None else " Slack posting is off or not set up, so nothing is posted yet."
        return f"OK: matches of {', '.join(teams)} go to {self._where()} from the next match on{saved}.{off}"

    # pause / resume ----------------------------------------------------------------------------------------------
    @property
    def wiki_on(self) -> bool:
        """The team wiki gets the matches in this run (help then lists pause wiki / resume wiki)."""
        return "wiki" in (getattr(self.outbox, "destinations", None) or {})

    @property
    def drive_on(self) -> bool:
        """Google Drive gets the matches in this run (help then lists pause drive / resume drive)."""
        return "drive" in (getattr(self.outbox, "destinations", None) or {})

    def _pausable(self, dest: str):
        box = self.outbox
        if box is None or dest not in (getattr(box, "destinations", None) or {}) or not hasattr(box, "pause"):
            return None
        return box

    def pause(self, dest: str) -> str:
        """Hold `dest`'s uploads ("youtube", "drive" or "wiki"), also across a restart ([<dest>] paused)."""
        from .publish.outbox import label
        dest = dest or "youtube"
        box = self._pausable(dest)
        if box is None:
            return f"{label(dest)} uploads are not set up in this run, so there is nothing to pause."
        box.pause(dest)
        try:
            config.save_sections({dest: {"paused": True}})
        except Exception:  # noqa: BLE001 - held for this run anyway
            pass
        s = (box.status() or {}).get(dest) or {}
        busy = " One upload already under way finishes first." if s.get("running") else ""
        return (f"OK: {label(dest)} uploads are paused; {s.get('queued', 0)} waiting. New matches wait too and "
                f"nothing is lost, also across a restart.{busy} Reply resume {dest} to send them.")

    def resume(self, dest: str) -> str:
        from .publish.outbox import label
        dest = dest or "youtube"
        box = self._pausable(dest)
        if box is None:
            return f"{label(dest)} uploads are not set up in this run."
        was = dest in getattr(box, "paused", set())
        box.resume(dest)
        try:
            config.save_sections({dest: {"paused": None}})
        except Exception:  # noqa: BLE001
            pass
        s = (box.status() or {}).get(dest) or {}
        return (f"OK: {label(dest)} uploads resumed; {s.get('queued', 0)} waiting go out now, in order."
                if was else f"{label(dest)} uploads were not paused; {s.get('queued', 0)} waiting.")


class Watcher:
    """The thread that runs EventWatch.tick() every WATCH_TICK_S and the Slack command poller when it is due. `lock`
    is held for each of them: the Slack listener's commands, which the recording's mailbox takes on its own thread
    (publish/commands.Mailbox), hold it too, so EventWatch and the Slack commands never run at the same time."""

    def __init__(self, watch: EventWatch, poller=None, tick_s: float = WATCH_TICK_S, log=print, lock=None):
        self.watch, self.poller, self.tick_s, self.log = watch, poller, tick_s, log
        self.lock = lock if lock is not None else threading.RLock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="event-watch", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(5.0)

    def _run(self) -> None:
        next_poll = 0.0
        while not self._stop.is_set():
            try:
                with self.lock:
                    self.watch.tick()
            except Exception as e:  # noqa: BLE001 - never let the watcher die
                self.log(f"event watch: {type(e).__name__}: {e}\n{traceback.format_exc()}", flush=True)
            if self.poller is not None and time.monotonic() >= next_poll and not self._stop.is_set():
                try:
                    with self.lock:
                        wait = self.poller.poll()
                except Exception as e:  # noqa: BLE001
                    self.log(f"Slack commands: {type(e).__name__}: {e}", flush=True)
                    wait = 60.0
                next_poll = time.monotonic() + max(1.0, float(wait))
            self._stop.wait(self.tick_s)


def run_from_args(a: argparse.Namespace) -> int:
    """`streamsplitter live`: refuses (exit 1) while another run of the same recording holds its lock; exit 2
    without a stream link or an event to find one for."""
    try:
        tag = _tag(a)
    except ValueError as e:
        print(f"streamsplitter live: {e}", flush=True)
        return 2
    folder = live_folder(tag)
    folder.mkdir(parents=True, exist_ok=True)
    lock = RunLock(folder / LOCK_FILE)
    if not lock.acquire(wait_s=LOCK_WAIT_S):
        print(f"StreamSplitter is already running for {tag} (another `live` run holds {lock.path}), so this one does "
              f"not start: two runs would send every clip twice. `streamsplitter status` shows how it is doing; "
              f"`streamsplitter stop` stops it.", flush=True)
        return 1
    held = {"outbox": None}
    try:
        return _run(a, tag, folder, held)
    finally:
        # an upload still sending (outbox.stop timed out) ends with the process, which also releases the lock: until
        # then a new run must not start and send that clip again
        box = held["outbox"]
        if box is None or not getattr(box, "busy", lambda: False)():
            lock.release()


def _run(a: argparse.Namespace, tag: str, folder: Path, held: dict) -> int:
    deliver = a.deliver or config.deliver_dir()
    if deliver is not None:
        deliver = Path(deliver) / tag
    out = config.data_root() / "clips" / tag
    (folder / STOP_FILE).unlink(missing_ok=True)          # a stop asked of an earlier run
    stopping = {"now": False}

    def _stop(*_):
        stopping["now"] = True
    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _stop)
    awake = keep_awake()
    if getattr(a, "no_publish", False):
        from .publish.notify import Notifier
        notifier, outbox, publisher, heartbeat_s, summary = Notifier(), None, None, HEARTBEAT_S, "publishing is off"
    else:
        notifier, outbox, publisher, heartbeat_s, summary = build_publishing(tag)
    held["outbox"] = outbox
    monitor = Monitor(folder, notifier, publisher, tag=tag,
                      disk_paths=[("clips (data_root)", config.data_root()),
                                  ("recorded chunks (work_root)", config.work_root())])
    monitor.write_status()                                # "running" from the start, for read_status
    slack_dests = getattr(outbox, "destinations", None) or {}
    channel = (config.section("slack").get("channel_name") or None) if "slack" in slack_dests else None
    recorder = RecorderProc(a, tag, enabled=not a.no_record)
    watch = EventWatch(folder, recorder, notifier, monitor, event_key=a.event, cli_url=a.url, channel_name=channel,
                       clips_dir=out)
    recorder.also_for = watch.others                      # the event's other links, best first (EventWatch.links)
    if a.event and not a.no_record and config.section("event").get("find_streams", True):
        watch.find_streams(scout.Scout(a.event, log=print))   # a live YouTube stream of the event that is on no list
    url = watch.choose()                                  # may ask TBA (only when no link is set)
    print(f"recording {url or '(no stream link yet)'} -> {folder}\nclips -> {out}"
          + (f"\ncopies -> {deliver}" if deliver else ""), flush=True)
    recorder.start(url)
    monitor.recorder = None if a.no_record else ("running" if url else "waiting for a stream link")
    poller = mailbox = None
    slack_lock = threading.RLock()                        # EventWatch and the Slack commands, one at a time
    if getattr(notifier, "dm_enabled", False):
        # the Slack commands (publish/commands.py): this recording reads its owner's DM while no Slack listener of
        # this version runs, and takes that listener's commands through its mailbox while one does
        from .publish import commands
        controls = LiveControls(watch, monitor, publisher=publisher, clips_dir=out, channel_name=channel)
        poller = commands.CommandPoller(notifier.client, notifier.user, controls, notifier=notifier,
                                        channel_name=channel,
                                        state_path=config.data_root() / "outbox" / f"{tag}.commands.json",
                                        shared_path=commands.shared_position(notifier.user),
                                        log=lambda s: print(s, flush=True))
        mailbox = commands.Mailbox(folder, controls, channel_name=channel, log=lambda s: print(s, flush=True),
                                   lock=slack_lock)
        watch.can_reply = True
    watcher = Watcher(watch, poller, lock=slack_lock)
    heartbeat = Heartbeat(notifier, monitor, every_s=heartbeat_s)
    reason = "stopped"
    try:
        pipe = Pipeline(out, event=a.event, deliver_dir=deliver, deliver_height=a.deliver_height)
        if publisher is not None:
            publisher.schedule = lambda: pipe.namer.schedule
            publisher.score_search = pipe.score
        heartbeat.start()
        link = watch.links_text(url, recorder.also)
        notifier.info(f"StreamSplitter started on {socket.gethostname()} for {watch.event_name()} ({tag}): {link}. "
                      f"{summary}." + (" Reply help for what you can do; replies come within about 20 s."
                                       if poller is not None else ""))
        if url is None:
            notifier.warn(watch.no_link_text(), key="no_stream_link")
        if mailbox is not None:
            mailbox.start()
        watcher.start()
        last_check = time.time()

        def stop() -> bool:
            nonlocal last_check
            if not stopping["now"] and time.time() - last_check > 10:
                last_check = time.time()
                code = recorder.check()
                if code is not None:
                    print(f"recorder exited ({code}); restarted it", flush=True)
                    monitor.recorder_exited(code)
            return stopping["now"]
        reason = follow(folder / "index.json", pipe, event=a.event, stop=stop, publisher=publisher,
                        notifier=notifier, monitor=monitor)
        if reason == "stopped" and stopping["now"]:
            reason = "stopped from the console (Ctrl+C or its window was closed)"
        elif reason == "stop requested":
            reason = f"stop requested ({monitor.stop_by or 'streamsplitter stop'})"
    except BaseException as e:
        reason = ("stopped from the console (Ctrl+C)" if isinstance(e, KeyboardInterrupt)
                  else f"crashed: {type(e).__name__}: {e}")
        raise
    finally:
        watcher.stop()
        if mailbox is not None:
            mailbox.stop()
        heartbeat.stop()
        if outbox is not None:
            outbox.stop(timeout=5.0)
        monitor.write_status(final=True, reason=reason)
        notifier.stopped(reason)
        recorder.stop()
        if awake is not None:
            awake.terminate()
    return 0


# ---------------------------------------------------------------------------------------------------------------
# Background runs: start, stop, status (the setup wizard's and the start/stop/status commands' building blocks)
# ---------------------------------------------------------------------------------------------------------------

class AlreadyRunning(RuntimeError):
    """A `live` run of this recording holds its lock (it is running, or still stopping)."""


class RunLock:
    """One `live` run per recording: an OS lock on <live folder>/live.lock (as record.py's _FolderLock does for the
    recorder), held for the whole run. The OS releases it when the process ends, however it ends, so a crash never
    leaves it stuck; two runs at once would both send every queued clip."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._f = None

    def acquire(self, wait_s: float = 0.0) -> bool:
        deadline = time.monotonic() + wait_s
        while not self._try():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)
        return True

    def _try(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self._f = f
        return True

    def release(self) -> None:
        f, self._f = self._f, None
        if f is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            f.close()


def lock_held(tag: str) -> bool | None:
    """Whether a `live` run of `tag` holds its lock. None when there is no lock file (no run of this version used the
    folder) or it cannot be tried. Trying takes the lock for an instant, which a starting run waits out
    (LOCK_WAIT_S)."""
    path = live_folder(tag) / LOCK_FILE
    if not path.is_file():
        return None
    probe = RunLock(path)
    try:
        if probe._try():
            probe.release()
            return False
    except OSError:
        return None
    return True


def wait_stopped(tag: str, timeout_s: float = 180.0, poll_s: float = 1.0, sleep=time.sleep) -> bool:
    """Wait until the run of `tag` has stopped (read_status no longer says running: its lock is released and its
    last status written). True when it stopped within `timeout_s`."""
    deadline = time.monotonic() + timeout_s
    while read_status(tag).get("running"):
        if time.monotonic() >= deadline:
            return False
        sleep(poll_s)
    return True


def start_detached(argv: list[str]) -> int:
    """Start `streamsplitter live <argv>` (argv as for the command, e.g. [URL, "--event", "2026mttd"]; a leading
    "live" is allowed) as a background process that outlives the terminal or wizard that started it, logging to
    <data_root>/logs/live_<tag>.log (appended). Returns its pid. Raises ValueError for arguments `live` rejects.

    Windows: no console window (CREATE_NO_WINDOW gives it a hidden console, which the recorder and ffmpeg share, so
    nobody can close it by mistake; tools/launch_detached.ps1 says why that matters), its own process group, and out
    of the caller's job object when that is allowed, so closing the caller does not end it. mac/Linux: a new session
    (no controlling terminal, so no SIGHUP when the terminal closes) with stdin from /dev/null, like nohup.
    Raises AlreadyRunning while a run of the same recording holds its lock (running, or still stopping)."""
    argv = [str(x) for x in argv]
    if argv and argv[0] == "live":
        argv = argv[1:]
    ap = argparse.ArgumentParser(prog="streamsplitter live", add_help=False)
    add_arguments(ap)
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        raise ValueError(f"not valid arguments for streamsplitter live: {' '.join(argv)}") from None
    tag = _tag(a)
    if lock_held(tag):
        raise AlreadyRunning(f"StreamSplitter is already running for {tag}, or still stopping (it finishes an upload "
                             f"in progress first). Wait until `streamsplitter status` says it is not running.")
    log_dir = config.data_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"live_{tag}.log"
    cmd = [sys.executable, "-u", "-m", "streamsplitter.cli", "live", *argv]
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    with open(log_path, "ab") as out:
        out.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} start: streamsplitter live {' '.join(argv)}\n"
                  .encode("utf-8"))
        out.flush()
        kw = dict(cwd=str(config.ROOT), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=env,
                  close_fds=True)
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            try:
                p = subprocess.Popen(cmd, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kw)
            except OSError:                       # the caller's job object forbids breaking away
                p = subprocess.Popen(cmd, creationflags=flags, **kw)
        else:
            p = subprocess.Popen(cmd, start_new_session=True, **kw)
    return p.pid


def request_stop(tag: str, by: str = "streamsplitter stop") -> Path:
    """Ask the `live` run recording `tag` to stop: a stop file it checks every poll (within a few seconds, or after
    the chunk it is reading), then it stops cleanly (uploads in progress resume next time, the contact is told).
    Never a console signal: a detached run has no console to send one to, and a signal to a process group can reach
    processes it was not meant for. `by` says who asked, for the stop DM (stop_asker). Returns the file's path."""
    path = live_folder(tag) / STOP_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"stop requested {time.strftime('%Y-%m-%d %H:%M:%S')}\nby: {by}\n", encoding="utf-8")
    return path


def stop_asker(path: Path) -> str | None:
    """Who asked for the stop in stop file `path` (request_stop's `by`: "streamsplitter stop", "from Slack by Ian
    H"), or None (a file from before 2026-11-05, or unreadable)."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("by: "):
                return line[4:].strip() or None
    except OSError:
        pass
    return None


def _pid_alive(pid) -> bool:
    """Whether process `pid` exists. Windows: OpenProcess + GetExitCodeProcess (never os.kill(pid, 0), which on
    Windows terminates the process). Elsewhere: signal 0."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        h = k32.OpenProcess(0x1000, False, pid)               # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5                # access denied: it exists
        try:
            code = wintypes.DWORD()
            return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_status(tag: str) -> dict:
    """What the `live` run of `tag` last wrote to status.json (running, pid, last chunk and its time, clips and
    score clips cut, outbox status per destination, last warning, updated), plus "age_s". While a run holds the
    recording's lock it is "running" whatever the file says (with "stopping" once it wrote its last status: it is
    finishing an upload). Otherwise "running" is corrected to False when the file is older than STATUS_STALE_S or its
    process is gone, since a crash or a power cut cannot say so itself; "reason" then says why. No status file:
    {"running": False, "reason": ...}."""
    path = live_folder(tag) / STATUS_FILE
    held = lock_held(tag)
    data = None
    for _ in range(5):                            # the writer replaces it atomically; Windows can refuse an instant
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            break
        except FileNotFoundError:
            if held:
                return {"tag": tag, "running": True, "reason": "starting (no status written yet)"}
            return {"tag": tag, "running": False, "reason": "no status file: it has not run for this tag"}
        except (OSError, json.JSONDecodeError):
            time.sleep(0.1)
    if not isinstance(data, dict):
        if held:
            return {"tag": tag, "running": True, "reason": "running (status file unreadable)"}
        return {"tag": tag, "running": False, "reason": "status file unreadable"}
    age = time.time() - float(data.get("updated") or 0.0)
    data["age_s"] = round(age, 1)
    if held:
        if not data.get("running"):
            data["running"], data["stopping"] = True, True
            data["reason"] = "stopping: it is finishing what it was doing (an upload can take a minute)"
        elif age > STATUS_STALE_S:
            data["reason"] = (f"no update for {_duration(age)}, but its process is still there: it may be stuck "
                              f"(try `streamsplitter stop`)")
    elif data.get("running"):
        if age > STATUS_STALE_S:
            data["running"] = False
            data["reason"] = (f"no update for {_duration(age)}: it stopped without saying so (a crash, the process "
                              f"was ended, or the PC slept or lost power)")
        elif data.get("host") == socket.gethostname() and not _pid_alive(data.get("pid")):
            data["running"] = False
            data["reason"] = "its process has ended"
    elif data.get("stopped_reason"):
        data["reason"] = data["stopped_reason"]
    if data.get("running") and not data.get("stopping") and (live_folder(tag) / STOP_FILE).is_file():
        data["stopping"] = True                   # asked to stop (request_stop); it sees that at its next poll
    return data
