"""Outbox: durable retries with backoff, restarts, idempotent add, dependencies (the Slack thread), permanent
failures, the outage warning and recovery notice, a stuck job not blocking the rest, staged files. Fake destinations
and a fake clock; no network.

    .venv\\Scripts\\python.exe -m pytest -q tests\\test_outbox.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from streamsplitter.publish import outbox as ob  # noqa: E402
from streamsplitter.publish.base import Job, Match, PermanentError, TransientError  # noqa: E402
from streamsplitter.publish.outbox import Outbox  # noqa: E402


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeNotifier:
    def __init__(self):
        self.msgs: list[tuple[str, str, str | None]] = []
        self.lines: list[str] = []

    def log(self, s, **_):
        self.lines.append(s)

    def info(self, text, key=None, dm=True):
        self.msgs.append(("info", text, key))
        return True

    def warn(self, text, key=None, dm=True):
        self.msgs.append(("warn", text, key))
        return True

    def error(self, text, key=None, dm=True):
        self.msgs.append(("error", text, key))
        return True

    def of(self, level):
        return [m for m in self.msgs if m[0] == level]


class Crash(BaseException):
    """Stands for the process dying in the middle of a send (nothing after it runs)."""


class FakeDest:
    """send() follows `script` (per job id, else the default): an exception to raise, a dict to return, or a callable
    run on the job. Records every attempt."""

    def __init__(self, name="slack", default=None):
        self.name = name
        self.scripts: dict[str, list] = {}
        self.default = default if default is not None else (lambda job: {"ts": f"ts-{job.id}"})
        self.sent: list[tuple[str, dict, str]] = []   # (job id, state copy, path)

    def send(self, job: Job) -> dict:
        self.sent.append((job.id, dict(job.state), job.path))
        script = self.scripts.get(job.id)
        step = script.pop(0) if script else self.default
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            return step(job)
        return step


def match(name="2026mttd_qm1") -> Match:
    return Match(key=name, name=name, event_key="2026mttd")


def job(name="2026mttd_qm1", kind="clip", dest="slack", path="", state=None) -> Job:
    return Job(id=f"{name}:{kind}:{dest}", destination=dest, kind=kind, path=path, match=match(name),
               state=dict(state or {}))


def make(tmp_path, dests=None, **kw):
    clock = kw.pop("clock", None) or Clock()
    note = kw.pop("notifier", None) or FakeNotifier()
    dests = dests if dests is not None else {"slack": FakeDest("slack")}
    box = Outbox(tmp_path / "box.sqlite", dests, note, clock=clock, stage=kw.pop("stage", False), **kw)
    return box, clock, note, dests


def drain(box, dest, clock, n=50):
    """Run step() until nothing is due, jumping the clock to each next try (at most n attempts)."""
    for _ in range(n):
        wait = box.step(dest)
        if wait >= ob.IDLE_S and not box.jobs(dest, "queued"):
            return
        if wait > 0:
            clock.t += wait


# ---------------------------------------------------------------------------------------------------------------
# basics

def test_add_is_idempotent_by_id(tmp_path):
    box, clock, note, dests = make(tmp_path)
    assert box.add(job()) is True
    assert box.add(job()) is False                      # queued
    box.step("slack")
    assert box.get("2026mttd_qm1:clip:slack")["status"] == "done"
    assert box.add(job()) is False                      # done
    assert len(dests["slack"].sent) == 1


def test_creation_order_per_destination(tmp_path):
    box, clock, note, dests = make(tmp_path)
    for n in (3, 1, 2):
        box.add(job(f"2026mttd_qm{n}"))
    drain(box, "slack", clock)
    assert [s[0].split(":")[0] for s in dests["slack"].sent] == ["2026mttd_qm3", "2026mttd_qm1", "2026mttd_qm2"]


def test_transient_backoff_doubles_and_retry_after_wins(tmp_path):
    box, clock, note, dests = make(tmp_path)
    d = dests["slack"]
    j = job()

    def remember(n):
        def f(jb):
            jb.state["tries"] = n                       # destination resume state, must survive the failure
            raise TransientError("offline")
        return f
    d.scripts[j.id] = [remember(1), remember(2), remember(3), TransientError("429", retry_after=90), {"ts": "1.2"}]
    box.add(j)
    t0 = clock.t
    waits = []
    for _ in range(4):
        assert box.step("slack") == 0.0
        row = box.get(j.id)
        assert row["status"] == "queued"
        waits.append(round(row["next_try"] - clock.t))
        assert box.step("slack") > 0                    # nothing due before next_try
        clock.t = row["next_try"]
    assert waits == [15, 30, 60, 90]
    assert box.get(j.id)["job"].state["tries"] == 3       # saved after the failed attempt
    assert d.sent[1][1]["tries"] == 1                     # and handed back on the retry
    box.step("slack")
    row = box.get(j.id)
    assert row["status"] == "done" and row["result"] == {"ts": "1.2"} and row["attempts"] == 5
    assert clock.t - t0 == 15 + 30 + 60 + 90


def test_backoff_caps_at_ten_minutes():
    assert [ob.backoff(n) for n in (1, 2, 3, 6, 7, 20)] == [15, 30, 60, 480, 600, 600]
    assert ob.backoff(3, retry_after=5) == 5


def test_restart_resumes_queued_and_running_jobs_with_their_state(tmp_path):
    box, clock, note, dests = make(tmp_path)
    a, b = job("2026mttd_qm1", dest="youtube"), job("2026mttd_qm2", dest="youtube")
    first = FakeDest("youtube")

    def crash_mid_upload(jb):
        jb.state["upload_uri"] = "https://upload/session/1"
        box.save_state(jb)                              # what YouTubeDestination's on_state does mid-send
        raise Crash()
    first.scripts[a.id] = [crash_mid_upload]
    box.destinations = {"youtube": first}
    box._health = {"youtube": ob._Health()}
    box.add(a)
    box.add(b)
    with pytest.raises(Crash):
        box.step("youtube")
    assert box.get(a.id)["status"] == "running"           # the crash left it so
    box._db.close()

    again = FakeDest("youtube", default=lambda jb: {"video_id": "v-" + jb.id})
    box2 = Outbox(tmp_path / "box.sqlite", {"youtube": again}, note, clock=clock, stage=False)
    assert box2.recovered == {"running": 1, "failed": 0}
    drain(box2, "youtube", clock)
    assert [s[0] for s in again.sent] == [a.id, b.id]
    assert again.sent[0][1]["upload_uri"] == "https://upload/session/1"
    assert all(r["status"] == "done" for r in box2.jobs("youtube"))


def test_dependency_waits_and_gets_thread_ts(tmp_path):
    box, clock, note, dests = make(tmp_path)
    d = dests["slack"]
    clip = job(kind="clip")
    score = job(kind="score", state={"after": clip.id})
    d.scripts[clip.id] = [TransientError("offline"), {"ts": "1730.0001", "file_id": "F1"}]
    box.add(clip)
    box.add(score)
    box.step("slack")                                   # clip fails
    clock.t += 1000
    box.step("slack")                                   # clip goes
    box.step("slack")                                   # then the reply
    assert [s[0] for s in d.sent] == [clip.id, clip.id, score.id]
    assert d.sent[-1][1]["thread_ts"] == "1730.0001"


def test_dependency_without_ts_passes_the_file_id(tmp_path):
    box, clock, note, dests = make(tmp_path)
    d = dests["slack"]
    clip = job(kind="clip")
    score = job(kind="score", state={"after": clip.id})
    d.scripts[clip.id] = [{"ts": None, "file_id": "F9"}]
    box.add(clip)
    box.add(score)
    drain(box, "slack", clock)
    st = d.sent[-1][1]
    assert st.get("thread_file_id") == "F9" and "thread_ts" not in st


def test_score_reply_never_goes_before_its_clip(tmp_path):
    box, clock, note, dests = make(tmp_path)
    d = dests["slack"]
    clip = job(kind="clip")
    d.scripts[clip.id] = [TransientError("offline")] * 3 + [{"ts": "9.9"}]
    box.add(clip)
    box.add(job(kind="score", state={"after": clip.id}))
    drain(box, "slack", clock)
    kinds = [s[0].split(":")[1] for s in d.sent]
    assert kinds == ["clip"] * 4 + ["score"]


def test_permanent_failure_notifies_once_and_is_retried_after_restart(tmp_path):
    box, clock, note, dests = make(tmp_path)
    d = dests["slack"]
    j = job()
    d.scripts[j.id] = [PermanentError("invite the app to #errata")]
    box.add(j)
    box.add(job("2026mttd_qm2"))
    drain(box, "slack", clock)
    assert box.get(j.id)["status"] == "failed"
    assert box.get("2026mttd_qm2:clip:slack")["status"] == "done"      # the queue went on
    errors = note.of("error")
    assert len(errors) == 1 and "invite the app" in errors[0][1]
    assert len([s for s in d.sent if s[0] == j.id]) == 1                # not retried
    box._db.close()
    box2 = Outbox(tmp_path / "box.sqlite", {"slack": d}, note, clock=clock, stage=False)
    assert box2.recovered["failed"] == 1
    drain(box2, "slack", clock)
    assert box2.get(j.id)["status"] == "done"


def test_missing_file_is_permanent(tmp_path):
    box, clock, note, dests = make(tmp_path)
    dests["slack"].scripts["2026mttd_qm1:clip:slack"] = [FileNotFoundError(2, "gone", "x.mp4")]
    box.add(job())
    box.step("slack")
    assert box.get("2026mttd_qm1:clip:slack")["status"] == "failed"


# ---------------------------------------------------------------------------------------------------------------
# outages

def test_outage_warns_once_after_ten_minutes_and_reports_recovery(tmp_path):
    box, clock, note, dests = make(tmp_path)
    d = dests["slack"]
    online = {"up": False}

    def send(jb):
        if not online["up"]:
            raise TransientError("no network")
        return {"ts": "1"}
    d.default = send
    for n in (1, 2, 3):
        box.add(job(f"2026mttd_qm{n}"))
    start = clock.t
    while clock.t - start < 30 * 60:
        wait = box.step("slack")
        clock.t += max(wait, 1.0)
    warns = [m for m in note.of("warn") if m[2] == "outage:slack"]
    assert len(warns) == 1 and "failing for 10 min" in warns[0][1] and "3 clip(s) waiting" in warns[0][1]
    assert box.status()["slack"]["failing_since"] == start
    online["up"] = True
    drain(box, "slack", clock)
    infos = [m[1] for m in note.of("info")]
    assert infos == ["Slack uploads resumed; 3 queued clip(s) sent."]
    assert box.status()["slack"]["failing_since"] is None


def test_warn_comes_on_time_even_with_long_backoff(tmp_path):
    box, clock, note, dests = make(tmp_path)
    dests["slack"].default = TransientError("down", retry_after=3000)
    box.add(job())
    start = clock.t
    box.step("slack")
    wait = box.step("slack")                            # the next try is 3000 s away, the warning 600 s
    assert wait == pytest.approx(600, abs=1)
    clock.t = start + 600
    box.step("slack")
    assert [m[2] for m in note.of("warn")] == ["outage:slack"]


def test_job_stuck_alone_is_held_and_does_not_block(tmp_path):
    box, clock, note, dests = make(tmp_path)
    d = dests["slack"]
    bad = job("2026mttd_qm1")
    d.scripts[bad.id] = [TransientError("HTTP 500 for this file")] * 100
    box.add(bad)
    box.add(job("2026mttd_qm2"))
    box.add(job("2026mttd_qm3"))
    for _ in range(4 * ob.STUCK_AFTER):                 # a step to try, a step to find the wait, per attempt
        wait = box.step("slack")
        clock.t += wait
    st = {r["id"].split(":")[0]: r for r in box.jobs("slack")}
    assert st["2026mttd_qm2"]["status"] == "done" and st["2026mttd_qm3"]["status"] == "done"
    assert st["2026mttd_qm1"]["status"] == "queued" and st["2026mttd_qm1"]["held"]
    assert [m[2] for m in note.of("warn")] == ["held:slack"]
    assert box.status()["slack"]["held"] == 1


def test_outage_keeps_the_order(tmp_path):
    box, clock, note, dests = make(tmp_path)
    d = dests["slack"]
    online = {"up": False}

    def send(jb):
        if not online["up"]:
            raise TransientError("no network")
        return {"ts": "1"}
    d.default = send
    for n in (1, 2, 3):
        box.add(job(f"2026mttd_qm{n}"))
    for _ in range(ob.STUCK_AFTER * 3):
        clock.t += box.step("slack")
    assert not any(r["held"] for r in box.jobs("slack"))  # the probe failed too: an outage, not one bad job
    online["up"] = True
    n0 = len(d.sent)
    drain(box, "slack", clock)
    assert [s[0].split(":")[0] for s in d.sent[n0:]] == ["2026mttd_qm1", "2026mttd_qm2", "2026mttd_qm3"]


# ---------------------------------------------------------------------------------------------------------------
# hooks, background jobs, files, threads

def test_hook_fills_the_job_and_transient_hook_waits_in_background(tmp_path):
    box, clock, note, dests = make(tmp_path, dests={"youtube": FakeDest("youtube", default={"video_id": "v"})})
    d = dests["youtube"]
    ready = {"score": False}

    def hook(jb):
        if jb.kind == "describe" and not ready["score"]:
            raise TransientError("TBA has not posted the score yet", retry_after=600)
        jb.match.red_score = 5
    box.hooks["youtube"] = hook
    box.add(job("2026mttd_qm1", kind="describe", dest="youtube"), background=True)
    box.add(job("2026mttd_qm2", dest="youtube"))
    clock.t += box.step("youtube")
    clock.t += box.step("youtube")
    assert [s[0] for s in d.sent] == ["2026mttd_qm2:clip:youtube"]      # the waiting describe did not block it
    s = box.status()["youtube"]
    assert s["queued"] == 0 and s["background"] == 1 and s["failing_since"] is None
    assert box.get("2026mttd_qm2:clip:youtube")["job"].match.red_score == 5
    ready["score"] = True
    clock.t += 600
    box.step("youtube")
    assert box.get("2026mttd_qm1:describe:youtube")["status"] == "done"


def test_after_send_hook_runs_on_done(tmp_path):
    box, clock, note, dests = make(tmp_path)
    seen = []
    box.after_send["slack"] = lambda jb, res: seen.append((jb.id, res))
    box.add(job())
    box.step("slack")
    assert seen == [("2026mttd_qm1:clip:slack", {"ts": "ts-2026mttd_qm1:clip:slack"})]


def test_not_before_delays_a_job(tmp_path):
    box, clock, note, dests = make(tmp_path)
    box.add(job(), not_before=clock.t + 100)
    assert box.step("slack") == pytest.approx(60)       # nothing due; idle wait capped by IDLE_S at most
    assert not dests["slack"].sent
    clock.t += 100
    box.step("slack")
    assert dests["slack"].sent


def test_files_are_staged_and_survive_a_rename(tmp_path):
    clips = tmp_path / "clips"
    clips.mkdir()
    src = clips / "2026mttd_qm1.mp4"
    src.write_bytes(b"video bytes")
    box, clock, note, dests = make(tmp_path, stage=True)
    d = dests["slack"]
    got = {}

    def send(jb):
        got["data"] = Path(jb.path).read_bytes()
        return {"ts": "1"}
    d.default = send
    j = job(path=str(src))
    box.add(j)
    staged = Path(box.get(j.id)["job"].path)
    assert staged.parent == box.files_dir and staged.name == "2026mttd_qm1_clip_slack.mp4"
    src.replace(clips / "2026mttd_qm2.mp4")             # the results screen corrected the name
    box.step("slack")
    assert got["data"] == b"video bytes"
    assert not staged.exists()                          # removed once sent
    assert (clips / "2026mttd_qm2.mp4").read_bytes() == b"video bytes"


def test_threads_send_and_stop_promptly(tmp_path):
    note = FakeNotifier()
    d = FakeDest("slack")
    box = Outbox(tmp_path / "box.sqlite", {"slack": d}, note, stage=False)
    box.start()
    box.add(job())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and box.get("2026mttd_qm1:clip:slack")["status"] != "done":
        time.sleep(0.02)
    assert box.get("2026mttd_qm1:clip:slack")["status"] == "done"
    t0 = time.monotonic()
    box.stop(timeout=5)
    assert time.monotonic() - t0 < 1.0                  # an idle worker (60 s wait) stops at once
    assert not box._threads


def test_status_counts_and_inactive_destination(tmp_path):
    box, clock, note, dests = make(tmp_path)
    box.add(job())
    box.add(job(dest="youtube"))                        # YouTube not set up: waits for a later start
    s = box.status()
    assert s["slack"]["queued"] == 1 and s["slack"]["active"]
    assert s["youtube"]["queued"] == 1 and not s["youtube"]["active"]
