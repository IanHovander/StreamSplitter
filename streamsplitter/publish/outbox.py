"""Outbox: every clip waiting to go to Slack or YouTube, kept on disk so spotty internet and restarts lose nothing.

One sqlite file per recording (<data_root>/outbox/<tag>.sqlite, WAL), one row per job: its Job as JSON (with the
destination's resume state), status queued -> running -> done | failed, attempts, next try, last error, result.
The follower only inserts rows (add() is a few milliseconds and never touches the network); one worker thread per
destination sends them.

Files. add() puts the job's file into the outbox's own folder (<data_root>/outbox/<tag>/files/<job id>.mp4) and points
the job there: a hard link (instant, no extra space) where the disk allows one, else a copy. The pipeline renames a
clip when the results screen corrects its name; the job's file does not move with it, so a job queued during an
outage still finds its file, and nothing the outbox holds open blocks that rename. The outbox's file is deleted once
the job is done.

Order and dependencies. A destination's jobs go in creation order, so Slack's channel and YouTube's playlist stay
chronological. A job may say `state["after"] = <job id>`: it waits until that job is done, and when that job's result
has a "ts" (a Slack message) it gets it as `state["thread_ts"]` (or, when Slack had not shown the message's ts yet,
the result's "file_id" as `state["thread_file_id"]`, which SlackDestination resolves), which is how the score version
lands in the clip's thread. If that job failed for good the reply goes without a thread rather than not at all.

Failures. TransientError (no network, a timeout, 429/5xx): the job stays first in line and is tried again after 15 s,
30 s, 60 s, ... up to 10 min, forever (the service's retry_after wins). The job's state is saved after every attempt,
failed ones included, and during one whenever the destination calls save_state (YouTubeDestination's on_state, after
each step of a resumable upload), so an upload resumes where it stopped, even after a crash. When a
destination has been failing for 10 min the contact is told once, and again when it recovers. PermanentError (and a
clip file that is gone): the job is marked failed and the contact is told; failed jobs are tried once more when the
outbox is opened again (a restart after logging in again sends them). Jobs a crash left "running" are queued again.

A job that keeps failing while the service works (one bad file) must not hold up the rest: after STUCK_AFTER
transient failures in a row of the first job, the next job is tried once as a probe. If the probe goes through, the
service is fine and the first job is "held": it keeps retrying on its own backoff without blocking the queue. If the
probe fails too, it is an outage and the order is kept. A background job (add(background=True), e.g. a YouTube
description update waiting for TBA's score) is held from the start: it never blocks the queue or counts as an outage.

Hooks. `hooks[dest](job)` runs in the worker before each attempt (TBA's score into the match); it may raise
TransientError for "not yet" (the job waits without being sent). `after_send[dest](job, result)` runs after a job is
done (queue a follow-up).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from .base import Destination, Job, PermanentError, TransientError

BACKOFF_FIRST_S = 15.0
BACKOFF_MAX_S = 600.0
OUTAGE_WARN_S = 600.0      # tell the contact once a destination has failed this long
STUCK_AFTER = 4            # transient failures in a row of the first job before a later job is tried as a probe
IDLE_S = 60.0              # a worker with nothing due looks again at least this often (add() wakes it at once)
LABELS = {"slack": "Slack", "youtube": "YouTube", "wiki": "Wiki", "drive": "Google Drive", "tba": "The Blue Alliance"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    destination TEXT NOT NULL,
    kind TEXT NOT NULL,
    json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_try REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    result TEXT,
    held INTEGER NOT NULL DEFAULT 0,
    background INTEGER NOT NULL DEFAULT 0,
    source TEXT,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    done_at REAL
);
CREATE INDEX IF NOT EXISTS jobs_dest_status ON jobs (destination, status);
"""


def label(dest: str) -> str:
    return LABELS.get(dest, dest)


def backoff(attempts: int, retry_after: float | None = None) -> float:
    """Seconds before the next try after `attempts` failures in a row: 15 s doubling to 10 min; the service's own
    retry_after wins (kept between 1 s and an hour, in case it says something odd)."""
    if retry_after is not None:
        return min(max(float(retry_after), 1.0), 3600.0)
    return min(BACKOFF_MAX_S, BACKOFF_FIRST_S * 2 ** max(0, attempts - 1))


@dataclass
class _Health:
    """One destination's run of failures, for the outage warning and the recovery notice."""
    failing_since: float | None = None
    last_error: str | None = None
    warned: bool = False            # the outage warning was given for this run of failures
    dm_warned: bool = False         # ... and went to the contact (so the recovery notice should too)
    recovering: int | None = None   # jobs sent since the destination came back after a warned outage
    probing: str | None = None      # id of the first job while a later one is tried as a probe


class Outbox:
    def __init__(self, path: Path, destinations: dict[str, Destination], notifier=None, *, hooks=None,
                 clock=time.time, log=None, stage: bool = True):
        """`hooks`: {destination: fn(job)} run in the worker just before every attempt (e.g. fill in TBA's score
        before a YouTube upload); TransientError / PermanentError from a hook count as the attempt's outcome (the job
        is not sent), any other error is logged and the job is sent anyway. `stage`: put each job's file in the
        outbox's own folder (see the module docstring)."""
        if notifier is None:
            from .notify import Notifier
            notifier = Notifier()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.files_dir = self.path.parent / self.path.stem / "files"
        self.stage = stage
        self.destinations = dict(destinations)
        self.notifier = notifier
        self.hooks = dict(hooks or {})
        self.after_send: dict = {}               # destination -> fn(job, result), after a job is done
        self.clock = clock
        self.log = log or getattr(notifier, "log", None) or print
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), timeout=30.0, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)
        self._stop = threading.Event()
        self._wake = {d: threading.Event() for d in self.destinations}
        self._threads: list[threading.Thread] = []
        self._health: dict[str, _Health] = {d: _Health() for d in self.destinations}
        self._probe_next: set[str] = set()
        now = self.clock()
        with self._lock:
            # a crash mid-send leaves "running"; a person may have fixed what made a job fail (logged in again)
            n_run = self._db.execute("UPDATE jobs SET status='queued', next_try=0, updated=? WHERE status='running'",
                                     (now,)).rowcount
            n_fail = self._db.execute("UPDATE jobs SET status='queued', next_try=0, updated=? "
                                      "WHERE status='failed'", (now,)).rowcount
        self.recovered = {"running": n_run, "failed": n_fail}
        if n_run or n_fail:
            self.log(f"outbox {self.path.name}: {n_run} interrupted and {n_fail} failed job(s) queued again")

    # the follower's side ------------------------------------------------------------------------------------------
    def add(self, job: Job, *, background: bool = False, not_before: float | None = None) -> bool:
        """Queue `job` (its file staged into the outbox's folder; job.path is changed to point there). False when its
        id is already queued, running or done (so adding twice is harmless); a job that failed before is queued again
        with the new details. `background`: never blocks the jobs after it (and its file is not staged);
        `not_before`: not tried before this time."""
        now = self.clock()
        with self._lock:
            row = self._db.execute("SELECT status FROM jobs WHERE id=?", (job.id,)).fetchone()
        if row is not None and row["status"] != "failed":
            return False
        source = job.path
        if self.stage and not background:
            job.path = self._stage(job)
        data = json.dumps(job.to_dict())
        held, next_try = int(background), float(not_before or 0.0)
        with self._lock:
            if row is None:
                self._db.execute("INSERT OR IGNORE INTO jobs (id, destination, kind, json, status, held, background, "
                                 "next_try, source, created, updated) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)",
                                 (job.id, job.destination, job.kind, data, held, held, next_try, source, now, now))
            else:
                self._db.execute("UPDATE jobs SET json=?, kind=?, destination=?, status='queued', next_try=?, held=?, "
                                 "background=?, source=?, updated=? WHERE id=?",
                                 (data, job.kind, job.destination, next_try, held, held, source, now, job.id))
        ev = self._wake.get(job.destination)
        if ev is not None:
            ev.set()
        return True

    def _stage(self, job: Job) -> str:
        """The job's file in files_dir: a hard link, else a copy (written under a temporary name, then renamed). The
        original path when there is nothing to stage or staging fails (logged; the job still has a chance)."""
        if not job.path:
            return job.path
        src = Path(job.path)
        dst = self.files_dir / (re.sub(r"[^A-Za-z0-9._-]", "_", job.id) + (src.suffix or ".mp4"))
        if not src.is_file():                    # renamed or deleted since; a failed job queued again keeps its copy
            return str(dst) if dst.is_file() else job.path
        try:
            if src.resolve().parent == self.files_dir.resolve():
                return job.path
            self.files_dir.mkdir(parents=True, exist_ok=True)
            dst.unlink(missing_ok=True)
            try:
                os.link(src, dst)
            except OSError:                      # another disk, or one without hard links (FAT/exFAT): copy
                tmp = dst.with_name(dst.name + ".part")
                shutil.copyfile(src, tmp)
                os.replace(tmp, dst)
            return str(dst)
        except OSError as e:
            self.log(f"outbox: could not stage {src.name} ({e}); the job uses the clip where it is")
            return job.path

    def _unstage(self, job: Job) -> None:
        try:
            p = Path(job.path)
            if p.parent.resolve() == self.files_dir.resolve():
                p.unlink(missing_ok=True)
        except OSError:
            pass                                 # e.g. still open somewhere on Windows: left for the folder's owner

    def save_state(self, job: Job) -> None:
        """Save `job.state` now (destinations call it mid-send, e.g. YouTubeDestination's on_state after each step of
        a resumable upload, so a crash resumes the same session)."""
        try:
            data = json.dumps(job.to_dict())
        except (RuntimeError, TypeError, ValueError):
            return
        with self._lock:
            self._db.execute("UPDATE jobs SET json=?, updated=? WHERE id=?", (data, self.clock(), job.id))

    def retarget(self, job_id: str, path: str | None = None, match=None) -> bool:
        """Give a job that has not started a corrected match (the results screen renamed the clip after it was
        queued) or another file. False when the job is not queued any more (or does not exist)."""
        with self._lock:
            row = self._db.execute("SELECT json, status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["status"] != "queued":
                return False
            job = Job.from_dict(json.loads(row["json"]))
            if path is not None:
                job.path = str(path)
            if match is not None:
                job.match = match
            self._db.execute("UPDATE jobs SET json=?, updated=? WHERE id=?",
                             (json.dumps(job.to_dict()), self.clock(), job_id))
        return True

    def get(self, job_id: str) -> dict | None:
        """One job as stored: {"id", "status", "attempts", "next_try", "last_error", "held", "job": Job, "result"}."""
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return None if row is None else self._row_dict(row)

    def jobs(self, destination: str | None = None, status: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM jobs WHERE 1=1", []
        if destination:
            q, args = q + " AND destination=?", args + [destination]
        if status:
            q, args = q + " AND status=?", args + [status]
        with self._lock:
            rows = self._db.execute(q + " ORDER BY rowid", args).fetchall()
        return [self._row_dict(r) for r in rows]

    def status(self) -> dict:
        """Per destination: queued (waiting clips; held ones included), running, done, failed, held, background
        (queued follow-ups such as description updates, not counted in queued), failing_since, last_error, active (a
        worker sends them; False for a destination that is not set up, whose jobs wait for the next start)."""
        with self._lock:
            rows = self._db.execute("SELECT destination, status, background, COUNT(*) AS n, SUM(held) AS h FROM jobs "
                                    "GROUP BY destination, status, background").fetchall()
        out = {}
        for d in [*self.destinations, *sorted({r["destination"] for r in rows} - set(self.destinations))]:
            h = self._health.get(d) or _Health()
            out[d] = {"queued": 0, "running": 0, "done": 0, "failed": 0, "held": 0, "background": 0,
                      "failing_since": h.failing_since, "last_error": h.last_error, "active": d in self.destinations}
        for r in rows:
            s = out[r["destination"]]
            if r["status"] == "queued" and r["background"]:
                s["background"] += r["n"]
                continue
            s[r["status"]] += r["n"]
            if r["status"] == "queued":
                s["held"] += r["h"] or 0
        return out

    # workers ------------------------------------------------------------------------------------------------------
    def start(self) -> None:
        """One worker thread per destination; returns at once."""
        self._stop.clear()
        for d in self.destinations:
            t = threading.Thread(target=self._worker, args=(d,), name=f"outbox-{d}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self, timeout: float = 10.0) -> None:
        """Stop the workers. A send in progress cannot be interrupted: after `timeout` it is left to the process exit,
        and its job, still "running" on disk, is queued again (with its resume state) at the next start."""
        self._stop.set()
        for ev in self._wake.values():
            ev.set()
        deadline = time.monotonic() + timeout
        for t in self._threads:
            t.join(max(0.0, deadline - time.monotonic()))
        self._threads = [t for t in self._threads if t.is_alive()]

    def busy(self) -> bool:
        """A worker is still sending (stop() timed out on an upload in progress): the run must not look stopped."""
        return any(t.is_alive() for t in self._threads)

    def close(self) -> None:
        self.stop(0.0)
        if not self._threads:
            with self._lock:
                self._db.close()

    def _worker(self, dest: str) -> None:
        ev = self._wake[dest]
        while not self._stop.is_set():
            ev.clear()
            try:
                wait = self.step(dest)
            except Exception as e:  # noqa: BLE001 - a worker must never die (a disk error, a bug): log and go on
                self.log(f"outbox {dest}: {type(e).__name__}: {e}\n{traceback.format_exc()}")
                wait = 30.0
            if wait > 0:
                ev.wait(min(wait, IDLE_S))

    def step(self, dest: str) -> float:
        """At most one attempt for `dest` (the workers' loop body; tests call it with a fake clock). Returns the
        seconds until there may be something to do (0: try again now)."""
        now = self.clock()
        with self._lock:
            picked, wait = self._pick(dest, now)
            if picked is not None:
                row, job = picked
                self._db.execute("UPDATE jobs SET status='running', attempts=attempts+1, updated=? WHERE id=?",
                                 (now, job.id))
        wait = min(wait, self._check_outage(dest, now))
        if picked is None:
            return max(0.0, wait)
        self._attempt(dest, row, job)
        return 0.0

    def _pick(self, dest: str, now: float):
        rows = self._db.execute("SELECT * FROM jobs WHERE destination=? AND status='queued' ORDER BY rowid",
                                (dest,)).fetchall()
        normal, held = [], []
        for r in rows:
            job = Job.from_dict(json.loads(r["json"]))
            after = job.state.get("after")
            if after:
                dep = self._db.execute("SELECT status FROM jobs WHERE id=?", (after,)).fetchone()
                if dep is not None and dep["status"] in ("queued", "running"):
                    continue                    # e.g. the score reply before its clip is posted
            (held if r["held"] else normal).append((r, job))
        h = self._health[dest]
        if dest in self._probe_next:
            self._probe_next.discard(dest)
            if len(normal) > 1:
                h.probing = normal[0][1].id
                return normal[1], 0.0
        if normal and normal[0][0]["next_try"] <= now:
            return normal[0], 0.0
        due = [x for x in held if x[0]["next_try"] <= now]
        if due:
            return due[0], 0.0
        times = [x[0]["next_try"] for x in normal[:1] + held]
        return None, (min(times) - now) if times else IDLE_S

    def _attempt(self, dest: str, row, job: Job) -> None:
        after = job.state.get("after")
        if after and not job.state.get("thread_ts"):
            dep = self.get(after)
            if dep is not None and dep["status"] == "done":
                res = dep["result"] or {}
                if res.get("ts"):
                    job.state["thread_ts"] = res["ts"]
                elif res.get("file_id"):         # Slack had not shown the clip's message yet: it finds it later
                    job.state["thread_file_id"] = res["file_id"]
            elif dep is None or dep["status"] == "failed":
                self.log(f"outbox: {job.id}: {after} was not sent; sending this without it")
        error, retry_after, permanent, result = None, None, False, None
        hook = self.hooks.get(dest)
        if hook is not None:
            try:
                hook(job)
            except TransientError as e:          # "not yet" (e.g. TBA has no score for a describe job): retry later
                error, retry_after = e, e.retry_after
            except PermanentError as e:
                error, permanent = e, True
            except Exception as e:  # noqa: BLE001 - a hook only improves the job (e.g. TBA's score)
                self.log(f"outbox: {job.id}: before-send step failed ({type(e).__name__}: {e}); sending anyway")
        self._save_job(job)
        if error is None:
            # the destination saves its resume state from inside send() through save_state (its on_state hook);
            # it is saved again below whatever send() does
            try:
                result = self.destinations[dest].send(job)
            except TransientError as e:
                error, retry_after = e, e.retry_after
            except PermanentError as e:
                error, permanent = e, True
            except FileNotFoundError as e:       # the clip is gone: no retry brings it back
                error, permanent = e, True
            except Exception as e:  # noqa: BLE001 - an unmapped error (or a bug): retried; the probe stops it blocking
                error = e
        now = self.clock()
        if error is None:
            self._done(dest, job, result, now)
        elif permanent:
            self._failed(dest, row, job, error, now)
        else:
            self._transient(dest, row, job, error, retry_after, now)

    def _done(self, dest: str, job: Job, result, now: float) -> None:
        with self._lock:
            self._db.execute("UPDATE jobs SET status='done', json=?, result=?, last_error=NULL, held=0, updated=?, "
                             "done_at=? WHERE id=?",
                             (json.dumps(job.to_dict()), json.dumps(result if result is not None else {}), now, now,
                              job.id))
            left = self._db.execute("SELECT COUNT(*) FROM jobs WHERE destination=? AND status='queued' AND held=0",
                                    (dest,)).fetchone()[0]
        self._unstage(job)
        self.log(f"sent {job.id}" + (f" -> {result.get('url') or result.get('permalink')}"
                                     if isinstance(result, dict) and (result.get("url") or result.get("permalink"))
                                     else ""))
        after = self.after_send.get(dest)
        if after is not None:
            try:
                after(job, result)
            except Exception as e:  # noqa: BLE001 - a follow-up is a nicety; the job itself is done
                self.log(f"outbox: after {job.id}: {type(e).__name__}: {e}")
        h = self._health[dest]
        if h.probing is not None and h.probing != job.id:
            stuck = self.get(h.probing)
            if stuck is not None and stuck["status"] == "queued":
                with self._lock:
                    self._db.execute("UPDATE jobs SET held=1 WHERE id=?", (h.probing,))
                self.notifier.warn(f"{label(dest)}: {stuck['job'].match.name} ({stuck['job'].kind}) keeps failing "
                                   f"({stuck['last_error']}); the clips queued after it go first, and it keeps being "
                                   f"retried.", key=f"held:{dest}")
        h.probing = None
        h.failing_since, h.last_error = None, None
        if h.warned:
            h.warned, h.recovering = False, 0
        if h.recovering is not None:
            h.recovering += 1
            if left == 0:
                self._resumed(dest, h, 0)

    def _resumed(self, dest: str, h: _Health, waiting: int) -> None:
        more = f"; {waiting} still waiting" if waiting else ""
        self.notifier.info(f"{label(dest)} uploads resumed; {h.recovering} queued clip(s) sent{more}.",
                           dm=h.dm_warned)
        h.recovering, h.dm_warned = None, False

    def _failed(self, dest: str, row, job: Job, error: Exception, now: float) -> None:
        msg = _short(error)
        with self._lock:
            self._db.execute("UPDATE jobs SET status='failed', json=?, last_error=?, updated=? WHERE id=?",
                             (json.dumps(job.to_dict()), msg, now, job.id))
        self._health[dest].probing = None
        self.notifier.error(f"{label(dest)}: could not send {job.match.name} ({job.kind}): {msg}. It is not retried "
                            f"until StreamSplitter is restarted.", key=f"failed:{dest}:{msg[:80]}")

    def _transient(self, dest: str, row, job: Job, error: Exception, retry_after: float | None, now: float) -> None:
        msg = _short(error)
        attempts = row["attempts"] + 1
        delay = backoff(attempts, retry_after)
        with self._lock:
            self._db.execute("UPDATE jobs SET status='queued', json=?, last_error=?, next_try=?, updated=? WHERE id=?",
                             (json.dumps(job.to_dict()), msg, now + delay, now, job.id))
        self.log(f"{label(dest)}: {job.id} not sent ({msg}); try {attempts + 1} in {delay:.0f} s")
        h = self._health[dest]
        probe = h.probing
        h.probing = None
        if row["held"]:
            return                               # a held job failing says nothing about the service
        if h.recovering is not None:
            with self._lock:
                waiting = self._db.execute("SELECT COUNT(*) FROM jobs WHERE destination=? AND status='queued' "
                                           "AND background=0",
                                           (dest,)).fetchone()[0]
            if h.recovering:
                self._resumed(dest, h, waiting)
            h.recovering = None
        if h.failing_since is None:
            h.failing_since = now
        h.last_error = msg
        if probe is None and attempts >= STUCK_AFTER:
            self._probe_next.add(dest)           # is it this job, or the service? try the next job once

    def _check_outage(self, dest: str, now: float) -> float:
        """Warn once when `dest` has been failing for OUTAGE_WARN_S; returns the seconds until that is due."""
        h = self._health[dest]
        if h.failing_since is None or h.warned:
            return IDLE_S
        left = h.failing_since + OUTAGE_WARN_S - now
        if left > 0:
            return left
        with self._lock:
            waiting = self._db.execute("SELECT COUNT(*) FROM jobs WHERE destination=? AND status IN "
                                       "('queued', 'running') AND background=0", (dest,)).fetchone()[0]
        h.warned = True
        h.dm_warned = self.notifier.warn(
            f"{label(dest)} uploads have been failing for {int((now - h.failing_since) // 60)} min "
            f"({h.last_error}). {waiting} clip(s) waiting; they are kept and retried every few minutes.",
            key=f"outage:{dest}")
        return IDLE_S

    # storage ------------------------------------------------------------------------------------------------------
    def _save_job(self, job: Job) -> None:
        with self._lock:
            self._db.execute("UPDATE jobs SET json=?, updated=? WHERE id=?",
                             (json.dumps(job.to_dict()), self.clock(), job.id))

    @staticmethod
    def _row_dict(row) -> dict:
        return {"id": row["id"], "destination": row["destination"], "kind": row["kind"], "status": row["status"],
                "attempts": row["attempts"], "next_try": row["next_try"], "last_error": row["last_error"],
                "held": bool(row["held"]), "created": row["created"], "updated": row["updated"],
                "done_at": row["done_at"], "job": Job.from_dict(json.loads(row["json"])),
                "result": json.loads(row["result"]) if row["result"] else None}


def _short(e: Exception) -> str:
    from .notify import redact
    text = str(e).strip() or type(e).__name__
    if isinstance(e, (FileNotFoundError,)):
        text = f"file not found: {getattr(e, 'filename', None) or text}"
    text = " ".join(text.split())
    return redact(text[:300])
