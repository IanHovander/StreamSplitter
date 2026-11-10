"""live.py's publishing hooks: follow() hands clips to the publisher and survives a chunk that raises; the stop file;
status.json and read_status; the follower's warnings (Monitor); start_detached's process flags; build_publishing with
missing tokens. A stub pipeline and fakes; no recorder, no network.

    .venv\\Scripts\\python.exe -m pytest -q tests\\test_live_publish.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import namedtuple
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from streamsplitter import config, live  # noqa: E402


class FakeNotifier:
    def __init__(self):
        self.msgs = []
        self.last_warning = None
        self.beats = 0

    def log(self, s):
        pass

    def _add(self, level, text, key):
        self.msgs.append((level, text, key))
        if level != "info":
            self.last_warning = {"level": level, "text": text}
        return True

    def info(self, text, key=None, dm=True):
        return self._add("info", text, key)

    def warn(self, text, key=None, dm=True):
        return self._add("warn", text, key)

    def error(self, text, key=None, dm=True):
        return self._add("error", text, key)

    def heartbeat(self):
        self.beats += 1
        return True

    def keys(self, level=None):
        return [k for lv, _, k in self.msgs if level is None or lv == level]


class Namer:
    def __init__(self):
        self.used = {}


class StubPipe:
    """The parts of Pipeline that follow() uses. Chunk 2 raises after cutting one clip and settling one score search
    (as Pipeline.feed would if the results-screen rename failed); chunk 3 settles another."""

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.namer = Namer()
        self.skip_until = None
        self.rows, self.score_pending, self.score_done = [], [], []
        self.fed = []

    def _row(self, name, end):
        return {"name": name, "naming": {"name": name, "key": name}, "end": end, "start": end - 150,
                "clip": {"path": str(self.out_dir / f"{name}.mp4")}}

    def feed(self, path, c, now_wall=None):
        self.fed.append(c["seq"])
        self.score_done = []
        if c["seq"] == 1:
            r = self._row("2026mttd_qm1", 30.0)
            self.rows.append(r)
            self.score_pending.append(r)
            return [r]
        if c["seq"] == 2:
            r = self._row("2026mttd_qm2", 50.0)
            self.rows.append(r)
            self.score_pending.append(r)
            settled = self.score_pending.pop(0)            # qm1 settled, then the failure
            settled["score_clip"] = {"path": str(self.out_dir / "2026mttd_qm1_score.mp4")}
            raise PermissionError(32, "The process cannot access the file because it is being used")
        if c["seq"] == 3:
            done = self.score_pending.pop(0)
            self.score_done = [done]
            return []
        return []


class StubPublisher:
    def __init__(self):
        self.clips, self.scores = [], []
        self.outbox = namedtuple("O", "status destinations")(lambda: {}, {})

    def on_clip(self, row):
        self.clips.append(row["name"])

    def on_score(self, row):
        self.scores.append(row["name"])


def write_index(folder: Path, n: int, status="recording") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    chunks = []
    for i in range(1, n + 1):
        (folder / f"r001_{i:05d}.ts").write_bytes(b"x")
        chunks.append({"seq": i, "path": f"r001_{i:05d}.ts", "t0": 20.0 * (i - 1), "t1": 20.0 * i,
                       "wall0": 1e9 + 20 * (i - 1), "wall_closed": 1e9 + 20 * i})
    p = folder / "index.json"
    p.write_text(json.dumps({"status": status, "chunks": chunks}), encoding="utf-8")
    return p


@pytest.fixture
def roots(tmp_path, monkeypatch):
    monkeypatch.setenv("STREAMSPLITTER_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("STREAMSPLITTER_WORK", str(tmp_path / "work"))
    monkeypatch.setenv("STREAMSPLITTER_CONFIG", str(tmp_path / "config.toml"))
    return tmp_path


def test_follow_publishes_and_survives_a_chunk_exception(roots):
    folder = live.live_folder("2026mttd")
    index = write_index(folder, 3)
    out = roots / "clips"
    out.mkdir()
    pipe, pub, note = StubPipe(out), StubPublisher(), FakeNotifier()
    lines = []
    reason = live.follow(index, pipe, poll_s=0.01, stop=lambda: len(pipe.fed) >= 3,
                         log=lambda s, **_: lines.append(s), publisher=pub, notifier=note)
    assert reason == "stopped"
    assert pipe.fed == [1, 2, 3]                          # kept going after chunk 2 raised
    assert pub.clips == ["2026mttd_qm1", "2026mttd_qm2"]  # qm2 was cut before the exception
    assert pub.scores == ["2026mttd_qm1", "2026mttd_qm2"]  # qm1 settled inside the failed chunk, qm2 later
    assert note.keys("error") == ["chunk_error"]
    assert any(s.startswith("CLIP 2026mttd_qm2") for s in lines) and any("SCORE 2026mttd_qm1" in s for s in lines)
    state = json.loads((folder / "follower.json").read_text())
    assert state["last_seq"] == 3 and state["last_end"] == 50.0
    st = json.loads((folder / "status.json").read_text())
    assert st["running"] and st["chunks_read"] == 3 and st["chunk_errors"] == 1 and st["clips_cut"] == 2
    assert st["last_warning"]["text"].startswith("Could not read chunk 2")


def test_stop_file_stops_the_follower(roots):
    folder = live.live_folder("2026mttd")
    index = write_index(folder, 0)
    pipe = StubPipe(roots)
    mon = live.Monitor(folder, FakeNotifier())
    path = live.request_stop("2026mttd")
    assert path == folder / live.STOP_FILE and path.exists()
    t0 = time.monotonic()
    assert live.follow(index, pipe, poll_s=0.01, monitor=mon, log=lambda s, **_: None) == "stop requested"
    assert time.monotonic() - t0 < 2 and not path.exists()
    assert mon.stop_by == "streamsplitter stop"                       # read before the file was removed


def test_the_stop_says_who_asked(roots):
    """2026-11-05: a stop from Slack DMed "stopped: stop requested (streamsplitter stop)"."""
    folder = live.live_folder("2026mttd")
    mon = live.Monitor(folder, FakeNotifier())
    live.request_stop("2026mttd", by="from Slack by Ian H")
    assert mon.stop_requested() is True and mon.stop_by == "from Slack by Ian H"
    (folder / live.STOP_FILE).write_text("stop requested 2026-11-04 10:00:00\n", encoding="utf-8")   # older file
    assert live.stop_asker(folder / live.STOP_FILE) is None


def test_the_start_dm_names_the_event_before_tba_answers(roots):
    """2026-11-05: "StreamSplitter started on HOMEPC for 2026mttd (2026mttd)" until TBA's record arrived."""
    config.save_sections({"event": {"key": "2026mttd", "name": "MadTown Throwdown"}})
    w = event_watch(roots, Clock(), tba=lambda path: {})[0]
    assert w.event_name() == "MadTown Throwdown"
    config.save_sections({"event": {"key": "2026cc", "name": "Chezy Champs"}})      # another event's: not used
    assert w.event_name() == "2026mttd"


def test_stale_stop_file_is_ignored(roots):
    folder = live.live_folder("x")
    path = live.request_stop("x")
    old = time.time() - 3600
    os.utime(path, (old, old))
    mon = live.Monitor(folder, FakeNotifier())
    assert mon.stop_requested() is False and not path.exists() and mon.stop_by is None


def test_read_status(roots):
    assert live.read_status("none")["running"] is False
    folder = live.live_folder("2026mttd")
    mon = live.Monitor(folder, FakeNotifier(), tag="2026mttd")
    mon.write_status()
    st = live.read_status("2026mttd")
    assert st["running"] is True and st["pid"] == os.getpid() and st["age_s"] < 5 and not st["stopping"]
    live.request_stop("2026mttd")                         # asked to stop, not seen yet: a start must wait
    assert live.read_status("2026mttd")["stopping"] is True
    (folder / live.STOP_FILE).unlink()
    data = json.loads((folder / "status.json").read_text())
    data["updated"] = time.time() - live.STATUS_STALE_S - 10
    (folder / "status.json").write_text(json.dumps(data))
    st = live.read_status("2026mttd")
    assert st["running"] is False and "no update" in st["reason"]
    data["updated"], data["pid"] = time.time(), 2 ** 22 + 12345   # a pid that is not running
    (folder / "status.json").write_text(json.dumps(data))
    assert live.read_status("2026mttd")["running"] is False
    mon.write_status(final=True, reason="stop requested (streamsplitter stop)")
    st = live.read_status("2026mttd")
    assert st["running"] is False and st["reason"] == "stop requested (streamsplitter stop)"


def test_pid_alive():
    assert live._pid_alive(os.getpid()) is True
    assert live._pid_alive(0) is False and live._pid_alive(None) is False


class Clock:
    def __init__(self):
        self.t = 5000.0

    def __call__(self):
        return self.t


def test_monitor_warnings(roots, monkeypatch):
    clock, note = Clock(), FakeNotifier()
    status = {"youtube": {"queued": 6, "running": 0, "last_error": "no network", "active": True}}
    pub = StubPublisher()
    pub.outbox = namedtuple("O", "status destinations")(lambda: status, {})
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(live.shutil, "disk_usage", lambda p: Usage(1e12, 1e12 - 4e9, 4e9))
    mon = live.Monitor(roots / "f", note, pub, disk_paths=[("clips", roots), ("chunks", roots)], clock=clock)
    mon.poll({"status": "recording"})
    assert note.keys() == ["disk:" + roots.resolve().anchor.lower(), "backlog:youtube"]   # one disk, said once
    clock.t += live.NO_CHUNK_WARN_S
    mon.poll({"status": "waiting"})
    assert note.keys()[-1] == "no_chunks" and "recorder: waiting" in note.msgs[-1][1]
    mon.poll()
    assert note.keys().count("no_chunks") == 1 and note.keys().count("backlog:youtube") == 1
    mon.chunk_read({"seq": 9})
    assert note.msgs[-1][0] == "info" and "arriving again" in note.msgs[-1][1]
    status["youtube"]["queued"] = 1
    mon.poll()
    status["youtube"]["queued"] = 7
    mon.poll()
    assert note.keys().count("backlog:youtube") == 2     # re-armed after it went down
    mon.recorder_exited(1)
    assert note.keys()[-1] == "recorder" and "restarted" in note.msgs[-1][1]


def test_monitor_youtube_quota_warns_once(roots):
    clock, note = Clock(), FakeNotifier()
    quota = {"blocked": True, "uploads": 100, "resets_in_s": 7200}
    tracker = namedtuple("T", "status")(lambda: dict(quota))
    dest = namedtuple("D", "client")(namedtuple("C", "quota")(tracker))
    pub = StubPublisher()
    pub.outbox = namedtuple("O", "status destinations")(lambda: {}, {"youtube": dest})
    mon = live.Monitor(roots / "f", note, pub, clock=clock)
    mon.poll()
    clock.t += live.DISK_CHECK_S
    mon.poll()
    assert note.keys() == ["youtube_quota"] and "2 h 0 min" in note.msgs[0][1]
    quota["blocked"] = False
    clock.t += live.DISK_CHECK_S
    mon.poll()
    quota["blocked"] = True
    clock.t += live.DISK_CHECK_S
    mon.poll()
    assert [k for k in note.keys() if k != "no_chunks"] == ["youtube_quota", "youtube_quota"]
    assert mon.youtube_quota["blocked"] is True


def test_heartbeat_stops_pushing_when_the_follower_is_stuck(roots):
    clock, note = Clock(), FakeNotifier()
    mon = live.Monitor(roots / "f", note, clock=clock)
    hb = live.Heartbeat(note, mon, every_s=120, clock=clock)
    assert hb.beat() and note.beats == 1
    clock.t += live.FOLLOWER_HUNG_S + 1                   # no poll and no chunk for 5 min
    hb.beat()
    assert note.beats == 1
    mon.poll()
    hb.beat()
    assert note.beats == 2


def test_start_detached_runs_live_in_the_background(roots, monkeypatch):
    seen = {}

    class P:
        pid = 4242

    def popen(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return P()
    monkeypatch.setattr(live.subprocess, "Popen", popen)
    pid = live.start_detached(["live", "https://www.youtube.com/@x/live", "--event", "2026mttd"])
    assert pid == 4242
    assert seen["cmd"][1:] == ["-u", "-m", "streamsplitter.cli", "live", "https://www.youtube.com/@x/live", "--event",
                               "2026mttd"]
    kw = seen["kw"]
    assert kw["stdin"] is subprocess.DEVNULL and kw["stderr"] is subprocess.STDOUT
    assert Path(kw["stdout"].name) == config.data_root() / "logs" / "live_2026mttd.log"
    if os.name == "nt":
        assert kw["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert kw["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert kw["start_new_session"] is True
    assert "start: streamsplitter live" in (config.data_root() / "logs" / "live_2026mttd.log").read_text()
    with pytest.raises(ValueError):
        live.start_detached(["--bogus"])


def test_start_detached_falls_back_when_breakaway_is_refused(roots, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows job objects")
    flags = []

    class P:
        pid = 7

    def popen(cmd, **kw):
        flags.append(kw["creationflags"])
        if kw["creationflags"] & subprocess.CREATE_BREAKAWAY_FROM_JOB:
            raise PermissionError(5, "Access is denied")
        return P()
    monkeypatch.setattr(live.subprocess, "Popen", popen)
    assert live.start_detached(["https://twitch.tv/x"]) == 7 and len(flags) == 2


def test_build_publishing_reports_missing_setup_without_crashing(roots, monkeypatch):
    sections = {"event": {"key": "2026mttd"}, "slack": {"enabled": True, "teams": ["5026"], "channel_id": "C1"},
                "youtube": {"enabled": True, "playlist_id": "PL1"}, "notify": {"slack_user": "U1"}}
    monkeypatch.setattr(config, "section", lambda name: dict(sections.get(name, {})))
    monkeypatch.setattr(config, "get_secret", lambda name: None)
    lines = []
    notifier, outbox, publisher, hb_s, summary = live.build_publishing("2026mttd", log=lambda s, **_: lines.append(s))
    try:
        text = "\n".join(lines)
        assert "no Slack bot token is stored" in text and "not logged in to YouTube" in text
        assert "streamsplitter setup" in text
        assert outbox is not None and outbox.destinations == {} and publisher is not None
        assert hb_s == 60.0 and summary == "no destination is ready"           # the alarm pushed back every minute
        assert not notifier.dm_enabled
    finally:
        if outbox is not None:
            outbox.close()


def test_build_publishing_wires_the_real_destinations(roots, monkeypatch):
    """Constructors only (no call reaches Slack or Google): the real classes accept what build_publishing passes."""
    pytest.importorskip("slack_sdk")
    pytest.importorskip("google.oauth2.credentials")
    sections = {"event": {"key": "2026mttd"},
                "slack": {"enabled": True, "teams": ["5026"], "channel_id": "C0123456789", "channel_name": "errata"},
                "youtube": {"enabled": True, "playlist_id": "PL1", "playlist_title": "2026 Madtown Throwdown"},
                "notify": {"slack_user": "U0123456789"}}
    secrets = {"slack_bot_token": "xoxb-not-a-real-token",
               "youtube_token": json.dumps({"token": "t", "refresh_token": "r", "client_id": "c.apps",
                                            "client_secret": "s", "token_uri": "https://oauth2.googleapis.com/token"}),
               "youtube_client": json.dumps({"installed": {"client_id": "c.apps", "client_secret": "s"}})}
    monkeypatch.setattr(config, "section", lambda name: dict(sections.get(name, {})))
    monkeypatch.setattr(config, "get_secret", lambda name: secrets.get(name))
    lines = []
    notifier, outbox, publisher, hb_s, summary = live.build_publishing("2026mttd", log=lambda s, **_: lines.append(s))
    try:
        assert set(outbox.destinations) == {"slack", "youtube"}, lines
        assert outbox.destinations["youtube"].on_state == outbox.save_state
        assert outbox.destinations["slack"].on_state == outbox.save_state      # the file id of a share in progress
        assert outbox.destinations["slack"].teams == {"5026"}
        assert notifier.dm_enabled and notifier.tag == "2026mttd"
        assert "Slack #errata" in summary and "2026 Madtown Throwdown" in summary
        assert not any("xoxb-" in s for s in lines)
    finally:
        outbox.close()
        notifier.close()


def tba_offline(path, key=None):
    raise live.tba.TBAError("offline (test)")


class StubOutbox:
    def __init__(self):
        self.stopped = False
        self.destinations = {}

    def status(self):
        return {}

    def stop(self, timeout=10.0):
        self.stopped = True


class RecNotifier(FakeNotifier):
    def __init__(self):
        super().__init__()
        self.stop_reason = None

    def stopped(self, reason, timeout=15.0):
        self.stop_reason = reason


def test_run_from_args_wires_publishing_and_stops_cleanly(roots, monkeypatch):
    import argparse
    note, box, pub = RecNotifier(), StubOutbox(), StubPublisher()
    pub.outbox = box
    seen = {}

    class Pipe(StubPipe):
        score = True

        def __init__(self, out, **kw):
            super().__init__(Path(out))
            self.namer.schedule = ["sched"]

    def fake_follow(index, pipe, **kw):
        seen.update(kw)
        seen["schedule"] = pub.schedule()
        seen["locked"] = live.lock_held("2026mttd")
        return "stop requested"
    monkeypatch.setattr(live, "Pipeline", Pipe)
    monkeypatch.setattr(live, "follow", fake_follow)
    monkeypatch.setattr(live, "keep_awake", lambda: None)
    monkeypatch.setattr(live.signal, "signal", lambda *a: None)
    monkeypatch.setattr(live.tba, "get", tba_offline)          # the event watch asks TBA for the event
    monkeypatch.setattr(live, "build_publishing", lambda tag, log=print: (note, box, pub, 120.0, "Slack #errata"))
    ap = argparse.ArgumentParser()
    live.add_arguments(ap)
    a = ap.parse_args(["https://twitch.tv/x", "--event", "2026mttd", "--no-record"])
    assert live.run_from_args(a) == 0
    assert seen["publisher"] is pub and seen["notifier"] is note and seen["schedule"] == ["sched"]
    assert seen["locked"] is True and live.lock_held("2026mttd") is False      # held for the run, then released
    assert box.stopped and note.stop_reason == "stop requested (streamsplitter stop)"
    assert any(m[0] == "info" and "StreamSplitter started" in m[1] for m in note.msgs)
    st = live.read_status("2026mttd")
    assert st["running"] is False and st["reason"] == "stop requested (streamsplitter stop)"


def test_a_run_with_slack_dms_opens_its_mailbox_for_the_listener(roots, monkeypatch):
    """publish/commands.py: while it runs, a recording takes this version's Slack listener's commands through its
    mailbox (advertised in its folder with its process id), and withdraws it when it stops. Its start message says how
    long a reply takes."""
    import argparse
    from streamsplitter import listener
    from streamsplitter.publish import commands

    class DMNotifier(RecNotifier):
        dm_enabled = True
        user = "U0IAN"

        class client:                                       # never read here: a listener of this version is "alive"
            @staticmethod
            def history(*a, **k):
                raise AssertionError("the recording read Slack while the listener runs")
    note, box, pub = DMNotifier(), StubOutbox(), StubPublisher()
    pub.outbox = box
    seen = {}
    monkeypatch.setattr(commands, "listener_protocol", lambda: commands.PROTOCOL)

    def fake_follow(index, pipe, **kw):
        folder = live.live_folder("2026mttd")
        seen["advert"] = commands.advertised(folder)
        req = {"id": "1730000010.000100-U0IAN", "user": "U0IAN", "text": "pause youtube", "channel": "DU0IAN"}
        (folder / commands.INBOX_DIR / f"{req['id']}.json").write_text(json.dumps(req))
        reply = listener.listen_folder() / commands.REPLIES_DIR / f"{req['id']}.json"
        for _ in range(60):                                 # the mailbox looks twice a second
            if reply.is_file():
                break
            time.sleep(0.05)
        seen["reply"] = json.loads(reply.read_text())
        return "stop requested"

    class Pipe(StubPipe):
        score = True

        def __init__(self, out, **kw):
            super().__init__(Path(out))
    monkeypatch.setattr(live, "Pipeline", Pipe)
    monkeypatch.setattr(live, "follow", fake_follow)
    monkeypatch.setattr(live, "keep_awake", lambda: None)
    monkeypatch.setattr(live.signal, "signal", lambda *a: None)
    monkeypatch.setattr(live.tba, "get", tba_offline)
    monkeypatch.setattr(live, "build_publishing", lambda tag, log=print: (note, box, pub, 120.0, "Slack #errata"))
    ap = argparse.ArgumentParser()
    live.add_arguments(ap)
    assert live.run_from_args(ap.parse_args(["https://twitch.tv/x", "--event", "2026mttd", "--no-record"])) == 0
    assert seen["advert"]["protocol"] == commands.PROTOCOL and seen["advert"]["pid"] == os.getpid()
    assert seen["reply"]["channel"] == "DU0IAN" and "uploads are not set up in this run" in seen["reply"]["text"]
    assert commands.advertised(live.live_folder("2026mttd")) is None          # withdrawn at the stop
    started = next(m[1] for m in note.msgs if "StreamSplitter started" in m[1])
    assert started.endswith("Reply help for what you can do; replies come within about 20 s.")


def live_args(*extra):
    import argparse
    ap = argparse.ArgumentParser()
    live.add_arguments(ap)
    return ap.parse_args(["https://twitch.tv/x", "--event", "2026mttd", "--no-record", *extra])


def test_second_live_run_refuses_while_the_first_holds_the_lock(roots, monkeypatch, capsys):
    """Review 2026-11-04: two `live` runs of one recording would both send every queued clip. A second run refuses;
    status says running while the lock is held, also after the first wrote its final status (still uploading);
    start_detached refuses; the wizard's wait returns once the lock is released."""
    monkeypatch.setattr(live, "LOCK_WAIT_S", 0.2)
    folder = live.live_folder("2026mttd")
    first = live.RunLock(folder / live.LOCK_FILE)
    assert first.acquire()
    try:
        stop_file = live.request_stop("2026mttd")          # asked of the first run: the second must not eat it
        monkeypatch.setattr(live, "build_publishing", lambda *a, **k: pytest.fail("the second run started"))
        assert live.run_from_args(live_args()) == 1
        assert "already running for 2026mttd" in capsys.readouterr().out and stop_file.exists()
        stop_file.unlink()
        assert live.read_status("2026mttd")["running"] is True                 # no status file yet: starting
        live.Monitor(folder, FakeNotifier(), tag="2026mttd").write_status(final=True, reason="stop requested")
        st = live.read_status("2026mttd")
        assert st["running"] is True and st["stopping"] is True                 # final status, upload still going
        monkeypatch.setattr(live.subprocess, "Popen", lambda *a, **k: pytest.fail("a second run was started"))
        with pytest.raises(live.AlreadyRunning):
            live.start_detached(["https://twitch.tv/x", "--event", "2026mttd"])
        assert live.wait_stopped("2026mttd", timeout_s=0.3, poll_s=0.05) is False
    finally:
        first.release()
    assert live.lock_held("2026mttd") is False and live.read_status("2026mttd")["running"] is False
    assert live.wait_stopped("2026mttd", timeout_s=0.3, poll_s=0.05) is True


# ---------------------------------------------------------------------------------------------------------------
# follower.json: open score searches across a crash, a clean stop, a damaged file

class Dest:
    def __init__(self, name):
        self.name, self.sent = name, []

    def send(self, job):
        self.sent.append(job)
        return {"ts": "11.1"} if self.name == "slack" else {"video_id": "v1"}


def real_publisher(roots):
    """A real Publisher and Outbox (fake destinations, no network), as build_publishing makes them."""
    from streamsplitter.publish import notify
    from streamsplitter.publish.outbox import Outbox
    from streamsplitter.publish.publisher import Publisher
    box = Outbox(roots / "data" / "outbox" / "t.sqlite", {"slack": Dest("slack"), "youtube": Dest("youtube")},
                 notify.Notifier(log=lambda s: None), stage=False)
    pub = Publisher(box, cfg_event={"key": "2026mttd"}, cfg_slack={"enabled": True, "teams": ["5026"]},
                    cfg_youtube={"enabled": True}, schedule=lambda: [], tba_get=lambda p: {}, log=lambda s: None)
    return pub, box


class SearchPipe:
    """Pipeline's side of follow() for one match, qm1 (stream 25-50 s): cut when chunk 3 is read (unless it ended by
    skip_until, as after a restart), its results-screen search open until chunk `settle_at` (score clip cut then).
    restore_pending as Pipeline's."""

    def __init__(self, out_dir: Path, settle_at=None):
        self.out_dir, self.settle_at = out_dir, settle_at
        self.namer = Namer()
        self.skip_until = None
        self.rows, self.score_pending, self.score_done, self.fed = [], [], [], []

    def restore_pending(self, rows):
        for r in rows:
            r["place"], r["restored"] = None, True
            self.score_pending.append(r)

    def feed(self, path, c, now_wall=None):
        self.fed.append(c["seq"])
        self.score_done, out = [], []
        if c["seq"] == 3:
            if self.skip_until is not None and 50.0 <= self.skip_until + 1.0:
                for r in self.score_pending:                  # the re-read found it again: its place is back
                    r["place"] = 7
            else:
                r = {"name": "2026mttd_qm1", "naming": {"name": "2026mttd_qm1", "key": "2026mttd_qm1",
                                                        "teams_read": ["5026"]},
                     "start": 25.0, "end": 50.0, "place": 7, "partial": False, "numbers_shown": [],
                     "clip": {"path": str(self.out_dir / "2026mttd_qm1.mp4")}}
                self.rows.append(r)
                self.score_pending.append(r)
                out.append(r)
        if c["seq"] == self.settle_at and self.score_pending:
            r = self.score_pending.pop(0)
            assert r["place"] == 7                            # the timer place was found again before it settled
            r["score_clip"] = {"path": str(self.out_dir / "2026mttd_qm1_score.mp4")}
            self.score_done = [r]
        return out


def crash_after(pipe, n):
    def stop():
        if len(pipe.fed) >= n:
            raise RuntimeError("power cut")
        return False
    return stop


def test_open_score_search_survives_a_crash_and_is_taken_up_again(roots, monkeypatch):
    """Review 2026-11-04 (high): a crash before the results-screen search settled lost the match on YouTube. Now
    follower.json keeps the search; the next run re-reads from the match start, settles it, and the score version
    goes to YouTube and threads under the Slack clip posted before (not posted again)."""
    monkeypatch.setattr(live, "REREAD_S", 30.0)
    folder = live.live_folder("2026mttd")
    index = write_index(folder, 4)
    out = roots / "clips"
    out.mkdir()
    pub, box = real_publisher(roots)
    pipe = SearchPipe(out)
    with pytest.raises(RuntimeError):
        live.follow(index, pipe, poll_s=0.01, stop=crash_after(pipe, 4), log=lambda s, **_: None, publisher=pub,
                    notifier=FakeNotifier())
    state = json.loads((folder / "follower.json").read_text())
    (rec,) = state["score_pending"]
    assert rec["name"] == "2026mttd_qm1" and rec["start"] == 25.0 and rec["jobs"]["slack_clip"]
    assert [j["id"] for j in box.jobs()] == ["2026mttd_qm1@25.0:clip:slack"]       # YouTube waits for the score
    box.close()

    index = write_index(folder, 8)
    pub, box = real_publisher(roots)                         # a new process
    pipe = SearchPipe(out, settle_at=7)
    lines = []
    live.follow(index, pipe, poll_s=0.01, stop=lambda: len(pipe.fed) >= 8, log=lambda s, **_: lines.append(s),
                publisher=pub, notifier=FakeNotifier())
    assert pipe.fed[0] == 1                                  # re-read from the match start, not from t1 - REREAD_S
    jobs = {j["id"]: j for j in box.jobs()}
    assert set(jobs) == {"2026mttd_qm1@25.0:clip:slack", "2026mttd_qm1@25.0:score:slack",
                         "2026mttd_qm1@25.0:score:youtube"}
    assert jobs["2026mttd_qm1@25.0:score:slack"]["job"].state["after"] == "2026mttd_qm1@25.0:clip:slack"
    assert any("taking up the results-screen search of 2026mttd_qm1" in s for s in lines)
    assert json.loads((folder / "follower.json").read_text())["score_pending"] == []
    box.close()


def test_open_score_search_whose_video_is_gone_is_settled_without_a_score(roots, monkeypatch):
    folder = live.live_folder("2026mttd")
    index = write_index(folder, 4)
    out = roots / "clips"
    out.mkdir()
    pub, box = real_publisher(roots)
    pipe = SearchPipe(out)
    with pytest.raises(RuntimeError):
        live.follow(index, pipe, poll_s=0.01, stop=crash_after(pipe, 4), log=lambda s, **_: None, publisher=pub,
                    notifier=FakeNotifier())
    box.close()
    data = json.loads(index.read_text())                     # chunks 1-2 pruned meanwhile
    data["chunks"] = [c for c in data["chunks"] if c["seq"] > 2]
    for c in ("r001_00001.ts", "r001_00002.ts"):
        (folder / c).unlink()
    index.write_text(json.dumps(data), encoding="utf-8")
    pub, box = real_publisher(roots)
    pipe = SearchPipe(out)
    live.follow(index, pipe, poll_s=0.01, stop=lambda: len(pipe.fed) >= 2, log=lambda s, **_: None, publisher=pub,
                notifier=FakeNotifier())
    ids = [j["id"] for j in box.jobs()]
    assert "2026mttd_qm1@25.0:clip:youtube" in ids and pipe.score_pending == []    # the match clip, not nothing
    box.close()


def test_clean_stop_settles_open_score_searches(roots):
    """A clean stop queues YouTube's match clip for a search still open (rather than whenever it runs again), and
    follower.json then holds no open search, so a restart does not upload that match a second time."""
    folder = live.live_folder("2026mttd")
    index = write_index(folder, 4)
    out = roots / "clips"
    out.mkdir()
    pub, box = real_publisher(roots)
    pipe = SearchPipe(out)
    assert live.follow(index, pipe, poll_s=0.01, stop=lambda: len(pipe.fed) >= 4, log=lambda s, **_: None,
                       publisher=pub, notifier=FakeNotifier()) == "stopped"
    assert [j["id"] for j in box.jobs("youtube")] == ["2026mttd_qm1@25.0:clip:youtube"]
    assert json.loads((folder / "follower.json").read_text())["score_pending"] == []
    box.close()


def test_damaged_follower_json_falls_back_to_its_copy(roots, monkeypatch):
    """Review 2026-11-04: after a power cut follower.json can be empty; the follower re-read every chunk and re-cut
    everything as replays. Now it is written with fsync and the copy before is kept."""
    monkeypatch.setattr(live, "REREAD_S", 10.0)
    folder = live.live_folder("2026mttd")
    index = write_index(folder, 5)
    (folder / "follower.json").write_text("", encoding="utf-8")                       # what a power cut left
    (folder / "follower.json.bak").write_text(json.dumps({"last_seq": 3, "last_t1": 60.0, "last_end": 50.0}))
    pipe = SearchPipe(roots / "clips")
    live.follow(index, pipe, poll_s=0.01, stop=lambda: len(pipe.fed) >= 3, log=lambda s, **_: None,
                notifier=FakeNotifier())
    assert pipe.fed == [3, 4, 5] and pipe.skip_until == 50.0
    state = json.loads((folder / "follower.json").read_text())
    assert state["last_seq"] == 5
    assert json.loads((folder / "follower.json.bak").read_text())["last_seq"] >= 3      # a whole copy beside it


def test_lost_follower_json_carries_on_after_the_newest_clip(roots, monkeypatch):
    monkeypatch.setattr(live, "REREAD_S", 10.0)
    folder = live.live_folder("2026mttd")
    index = write_index(folder, 5)
    (folder / "follower.json").write_text("{\"last_seq\": 4, \"la", encoding="utf-8")     # torn, no copy
    out = roots / "clips"
    out.mkdir()
    clip = out / "2026mttd_qm1.mp4"
    clip.write_bytes(b"x")
    os.utime(clip, (1e9 + 61, 1e9 + 61))                       # written just after chunk 3 was closed
    (out / "2026mttd_qm1_score.mp4").write_bytes(b"x")         # (a score version says nothing about progress)
    pipe, note = SearchPipe(out), FakeNotifier()
    live.follow(index, pipe, poll_s=0.01, stop=lambda: len(pipe.fed) >= 3, log=lambda s, **_: None, notifier=note)
    assert pipe.fed == [3, 4, 5] and pipe.skip_until == 60.0  # not from chunk 1: nothing is cut twice
    assert note.keys("warn") == ["follower_state"] and "2026mttd_qm1" in note.msgs[0][1]


def test_failed_score_cut_settles_its_row_and_not_the_ones_after(roots, monkeypatch):
    """Review 2026-11-04: a row whose score cut kept raising stayed first and no later row settled (no YouTube
    upload after it). Now it settles without a score version and the next row still gets its own."""
    from streamsplitter import pipeline

    class Reader:
        full_every_s = 1.0

        def full_reads(self, a, b):                          # the results screen, from just after each match
            return [(float(t), [{"t": float(t), "text": n} for n in ("5026", "254", "1678", "604")])
                    for t in range(int(a) + 1, int(b))]

        def timer_read_near(self, place, t):
            return False

        def arrived(self, t, prev):
            return t

        def left(self, last_on, t):
            return t

    def cut_score(chunks, start, end, a, b, out, pad=3.0):
        if "qm1" in out.name:
            raise ValueError("no recorded chunks cover this window")
        return {"path": str(out)}
    monkeypatch.setattr(pipeline.cutter, "cut_score", cut_score)
    pipe = pipeline.Pipeline(roots / "clips", reader=Reader(), fms_sounds=False)

    def row(name, end):
        return {"name": name, "naming": {"name": name, "key": None, "teams_read": ["5026", "254", "1678", "604"]},
                "start": end - 150.0, "end": end, "place": None, "numbers_shown": [], "partial": False,
                "clip": {"path": str(roots / "clips" / f"{name}.mp4")}}
    bad, good = row("2026mttd_qm1", 200.0), row("2026mttd_qm2", 600.0)
    pipe.score_pending = [bad, good]
    assert pipe._scores(400.0) == [bad]                      # qm1 settles (without a score), qm2 still searching
    assert "score_clip" not in bad and bad["score_error"].startswith("ValueError") and bad["score_piece"] is None
    assert pipe._scores(800.0) == [good] and good["score_clip"]["path"].endswith("2026mttd_qm2_score.mp4")
    assert pipe.score_pending == []


def test_build_publishing_refuses_the_old_events_playlist(roots, monkeypatch):
    """Lead, 2026-11-04: after the event changed, [youtube] playlist_id is the old event's playlist: nothing goes
    up, and the contact is told to run the YouTube step again."""
    sections = {"event": {"key": "2026mttd"},
                "youtube": {"enabled": True, "playlist_id": "PL_OLD", "playlist_title": "2025 Madtown Throwdown",
                            "playlist_event": "2025mttd"}}
    monkeypatch.setattr(config, "section", lambda name: dict(sections.get(name, {})))
    monkeypatch.setattr(config, "get_secret", lambda name: None)
    lines = []
    notifier, outbox, publisher, hb_s, summary = live.build_publishing("2026mttd", log=lambda s, **_: lines.append(s))
    try:
        assert "youtube" not in outbox.destinations
        text = "\n".join(lines)
        assert "is for 2025mttd" in text and "setup --section youtube" in text and "not logged in" not in text
    finally:
        outbox.close()


def test_build_publishing_off(roots, monkeypatch):
    monkeypatch.setattr(config, "section", lambda name: {})
    notifier, outbox, publisher, hb_s, summary = live.build_publishing("t", log=lambda s, **_: None)
    assert outbox is None and publisher is None and summary == "publishing is off"


def test_no_publish_flag():
    import argparse
    ap = argparse.ArgumentParser()
    live.add_arguments(ap)
    a = ap.parse_args(["https://twitch.tv/x", "--no-publish"])
    assert a.no_publish is True and ap.parse_args(["u"]).no_publish is False
    assert ap.parse_args(["--event", "2026mttd"]).url is None        # the link comes from config.toml or TBA


# ---------------------------------------------------------------------------------------------------------------
# Event-day monitoring (user, 2026-11-04): the alarm every minute, the stream link from TBA, the morning and
# stream-started notices, and what the Slack commands do. Fake Slack, fake TBA, fake recorder process, fixed clock.

import datetime as dt  # noqa: E402
import types  # noqa: E402

from streamsplitter import streams  # noqa: E402
from streamsplitter.publish import commands, notify  # noqa: E402
from streamsplitter.publish.base import Job, Match  # noqa: E402

PST = dt.timezone(dt.timedelta(hours=-8))                 # the event's zone (no tzdata needed in the tests)


def at(day: int, hour: int, minute: int = 0) -> float:
    """Unix time of November <day> 2026, <hour>:<minute> at the event (PST)."""
    return dt.datetime(2026, 11, day, hour, minute, tzinfo=PST).timestamp()


V1, V2 = "AAAAAAAAAA1", "BBBBBBBBBB2"
URL1, URL2 = f"https://www.youtube.com/watch?v={V1}", f"https://www.youtube.com/watch?v={V2}"
MTTD = {"key": "2026mttd", "name": "Madtown Throwdown", "timezone": "America/Los_Angeles",
        "start_date": "2026-11-06", "end_date": "2026-11-08",
        "webcasts": [{"type": "youtube", "channel": V1, "date": "2026-11-06"},
                     {"type": "youtube", "channel": V2, "date": "2026-11-07"},
                     {"type": "twitch", "channel": "madtownrobotics"}]}


class AlarmSlack:
    """chat.scheduleMessage / chat.deleteScheduledMessage as Slack keeps them, on the test's clock: a delete of a
    message due within 60 s is refused (False, as Slack's invalid_scheduled_message_id) and counted; `delay` is how
    long the next schedule call takes."""

    def __init__(self, clock):
        self.clock = clock
        self.scheduled: dict[str, int] = {}
        self.calls, self.dms, self.refused = [], [], []
        self.deleted_ahead: list[float] = []      # how far from due each deleted alarm was
        self.delay = 0.0
        self.down = False
        self.n = 0

    def open_dm(self, user):
        return "D1"

    def dm(self, user, text):
        self.dms.append(text)
        return {"ts": "1.1"}

    def schedule_message(self, channel, text, post_at):
        if self.down:
            raise notify.TransientError("no connection to Slack")
        self.clock.t += self.delay
        self.n += 1
        sid = f"Q{self.n}"
        self.scheduled[sid] = post_at
        self.calls.append(("schedule", sid, self.clock.t))
        return sid

    def delete_scheduled(self, channel, sid):
        if self.down:
            raise notify.TransientError("no connection to Slack")
        self.clock.t += 0.3
        due = self.scheduled.get(sid)
        self.calls.append(("delete", sid, self.clock.t))
        if due is None:
            return False
        if due - self.clock.t <= 60:
            self.refused.append(sid)
            return False
        self.deleted_ahead.append(due - self.clock.t)
        del self.scheduled[sid]
        return True

    def post_due(self):
        """Slack posts what came due."""
        for sid, due in list(self.scheduled.items()):
            if due <= self.clock.t:
                self.dms.append(f"ALARM {sid}")
                del self.scheduled[sid]


def alarm_notifier(clock, slack):
    hb, al, _ = notify.alarm_timing(None, None)
    n = notify.Notifier(slack, "U1", host="pit", tag="2026mttd", alarm_after_s=al, clock=clock,
                        log=lambda s: None, sleep=lambda s: None, threaded=False)
    beat = live.Heartbeat(n, types.SimpleNamespace(tick=1e18), every_s=hb, clock=clock)   # follower always alive
    return n, beat


def run_beats(clock, slack, n, beat, count):
    """`count` beats as live.Heartbeat's thread times them (each next_wait after the start of the one before)."""
    for _ in range(count):
        t0 = clock.t
        ok = beat.beat()
        n.pump()
        slack.post_due()
        clock.t += beat.next_wait(ok, clock.t - t0)
        slack.post_due()
        n.pump()


def test_alarm_timing_defaults_and_safe_bounds():
    assert notify.alarm_timing(None, None) == (60.0, 420.0, None)
    assert notify.alarm_timing(120, 600) == (60.0, 420.0, None)        # what the setup wrote before: not a choice
    assert notify.alarm_timing(60, 180) == (60.0, 420.0, None)         # ... and until 2026-11-05 (alarms misfired)
    assert notify.alarm_timing(60, 600) == (60.0, 420.0, None)         # ... and until the cutoff was measured
    assert notify.alarm_timing(90, 900) == (90.0, 900.0, None)         # a person's override is kept
    hb, al, note = notify.alarm_timing(5, 60)
    assert (hb, al) == (30.0, 390.0) and "changed" in note             # >= heartbeat + 330 + 30
    assert notify.alarm_timing(60, 100)[1] == 420.0 and notify.alarm_timing(60, 99999)[1] == 3600.0


def test_heartbeat_every_minute_deletes_each_alarm_six_minutes_before_it_is_due():
    clock = Clock()
    slack = AlarmSlack(clock)
    n, beat = alarm_notifier(clock, slack)
    run_beats(clock, slack, n, beat, 60)                              # an hour
    scheds = [c for c in slack.calls if c[0] == "schedule"]
    assert len(scheds) == 60 and not slack.refused and not slack.dms   # no alarm, no false alarm
    assert min(slack.deleted_ahead) > notify.SAFE_DELETE_S + 25       # ~359 s ahead (Slack posts ones deleted <= 270 s)
    assert len(slack.scheduled) == 1                                  # one alarm waiting
    gaps = [b[2] - a[2] for a, b in zip(scheds, scheds[1:])]
    assert all(59 <= g <= 61 for g in gaps)
    # Slack's limit: 30 messages due in one channel within 5 min; here at most 5 scheduled in any 5 min at all
    assert max(sum(1 for s in scheds if t <= s[2] < t + 300) for _, _, t in scheds) <= 6


def test_a_slow_slack_call_keeps_the_alarm_and_a_false_alarm_note_follows():
    clock = Clock()
    slack = AlarmSlack(clock)
    n, beat = alarm_notifier(clock, slack)
    run_beats(clock, slack, n, beat, 1)                               # Q1 due in 420 s
    slack.delay = 275.0                                               # the next beat is that late (a stalled PC)
    run_beats(clock, slack, n, beat, 1)
    assert ("delete", "Q1") not in [c[:2] for c in slack.calls]       # due in 85 s: not tried (Slack: 60 s)
    slack.delay = 0.0
    run_beats(clock, slack, n, beat, 4)
    assert not slack.refused                                          # never asked Slack for an impossible delete
    assert "ALARM Q1" in slack.dms                                    # it went off ...
    assert any(d.startswith("False alarm: StreamSplitter on pit is still running") for d in slack.dms)   # ... explained
    assert [d for d in slack.dms if d.startswith("ALARM")] == ["ALARM Q1"]   # the slow call did not cost a second one


def test_an_alarm_that_cannot_be_cancelled_says_why_in_the_log():
    """2026-11-05: three listener alarms went off in a row and the log had no line saying why."""
    clock = Clock()
    slack = AlarmSlack(clock)
    said = []
    hb, al, _ = notify.alarm_timing(None, None)
    n = notify.Notifier(slack, "U1", host="pit", tag="2026mttd", alarm_after_s=al, clock=clock, log=said.append,
                        sleep=lambda s: None, threaded=False)
    beat = live.Heartbeat(n, types.SimpleNamespace(tick=1e18), every_s=hb, clock=clock)
    run_beats(clock, slack, n, beat, 1)                               # Q1 due in 420 s
    real = slack.delete_scheduled
    slack.delete_scheduled = lambda ch, sid: (_ for _ in ()).throw(notify.TransientError("ratelimited"))
    run_beats(clock, slack, n, beat, 1)
    assert any("could not cancel stopped-running alarm Q1 (ratelimited)" in s for s in said)
    run_beats(clock, slack, n, beat, 5)                               # Q1 now due within 90 s: kept, said once
    run_beats(clock, slack, n, beat, 1)
    assert sum("Q1 is due in" in s and "too close to cancel" in s for s in said) == 1
    slack.delete_scheduled = real


def test_slack_down_for_ten_minutes_raises_the_alarm_then_says_false_alarm():
    clock = Clock()
    slack = AlarmSlack(clock)
    n, beat = alarm_notifier(clock, slack)
    run_beats(clock, slack, n, beat, 2)
    slack.down = True
    run_beats(clock, slack, n, beat, 40)                              # retried every 15 s, 10 min in all
    assert beat.next_wait(False, 0.5) == pytest.approx(14.5)
    slack.down = False
    run_beats(clock, slack, n, beat, 3)
    assert any(d.startswith("ALARM") for d in slack.dms)
    assert any(d.startswith("False alarm") for d in slack.dms)
    assert len(slack.scheduled) == 1


# -- the stream link from TBA ----------------------------------------------------------------------------------

def test_tba_suggestion_today_then_live_page_then_first():
    today = streams.pick(MTTD, dt.date(2026, 11, 7))
    assert today["url"] == URL2 and today["recommended"] and "Today's stream" in today["label"]
    other_day = streams.pick(MTTD, dt.date(2026, 11, 8), channel_live=lambda u: "https://www.youtube.com/@MadTown/live")
    assert other_day["url"] == "https://www.youtube.com/@MadTown/live"          # follows each day's stream
    assert streams.pick(MTTD, dt.date(2026, 11, 8), channel_live=None)["url"] == "https://www.twitch.tv/madtownrobotics"
    chan = {"webcasts": [{"type": "youtube", "channel": V1, "date": "2026-11-06"},
                         {"type": "youtube", "channel": "UC" + "x" * 22}]}
    assert streams.pick(chan, dt.date(2026, 11, 8))["url"] == "https://www.youtube.com/channel/UC" + "x" * 22 + "/live"
    single = {"webcasts": [{"type": "twitch", "channel": "firstinspires"}, {"type": "youtube", "channel": V1}]}
    assert streams.pick(single, None)["url"] == URL1                    # one video: that video, before Twitch
    assert streams.pick({"webcasts": [{"type": "iframe", "channel": "<iframe>"}]}, None) is None
    assert streams.pick({}, None) is None


def test_suggest_streams_for_the_wizard():
    rows = streams.suggest_streams("2026mttd", tba_get=lambda p: MTTD if p == "/event/2026mttd" else {},
                                   today=dt.date(2026, 11, 6), channel_live=None)
    assert [r["url"] for r in rows] == [URL1, URL2, "https://www.twitch.tv/madtownrobotics"]
    assert set(rows[0]) == {"url", "label", "date", "recommended"}
    assert rows[0]["recommended"] and not any(r["recommended"] for r in rows[1:])
    assert rows[1]["date"] == "2026-11-07"
    assert streams.suggest_streams("2026mttd", tba_get=tba_offline) == []


# -- EventWatch: the link, the morning notice, the stream notices ------------------------------------------------

class FakeProc:
    def __init__(self, url):
        self.url, self.returncode = url, None

    def poll(self):
        return self.returncode


def fake_recorder(enabled=True):
    started, stopped = [], []

    def start(a, tag, url, also=()):
        started.append((tag, url))
        rec.started_also.append(list(also))
        return FakeProc(url)
    rec = live.RecorderProc(types.SimpleNamespace(event="2026mttd"), "2026mttd", enabled=enabled, start=start,
                            stop=stopped.append)
    rec.started_also = []
    return rec, started, stopped


def event_watch(roots, clock, event=MTTD, cli_url=None, tba=None):
    rec, started, stopped = fake_recorder()
    note = FakeNotifier()
    folder = live.live_folder("2026mttd")
    folder.mkdir(parents=True, exist_ok=True)
    mon = live.Monitor(folder, note, tag="2026mttd", clock=clock)
    get = tba or (lambda path: dict(event) if path == "/event/2026mttd" else {})
    w = live.EventWatch(folder, rec, note, mon, event_key="2026mttd", cli_url=cli_url, channel_name="errata",
                        clips_dir=roots / "clips", clock=clock, tba_get=get, channel_live=None, tz=PST,
                        log=lambda s, **_: None)
    w.can_reply = True
    return w, rec, started, stopped, note, mon


def infos(note):
    return [t for lv, t, _ in note.msgs if lv == "info"]


def test_link_from_tba_when_none_is_set(roots):
    clock = Clock()
    clock.t = at(7, 6)
    w, rec, started, _, note, _ = event_watch(roots, clock)
    url = w.choose()
    rec.start(url)
    assert url == URL2 and w.source == live.SRC_TBA and w.mode == "auto"    # today's dated webcast
    assert started == [("2026mttd", URL2)]
    assert json.loads((w.folder / "tba_event.json").read_text())["timezone"] == "America/Los_Angeles"


def test_no_link_anywhere_says_how_to_set_one_and_picks_up_tba_later(roots):
    clock = Clock()
    clock.t = at(6, 7)
    event = {**MTTD, "webcasts": []}
    w, rec, started, _, note, _ = event_watch(roots, clock, tba=lambda p: dict(event))
    assert w.choose() is None
    text = w.no_link_text()
    assert "No stream link for Madtown Throwdown" in text and "reply here: stream <link>" in text
    w.tick()
    assert started == [] and not note.msgs
    event["webcasts"] = MTTD["webcasts"]                              # TBA lists it now
    clock.t += live.TBA_RECHECK_S
    w.tick()
    assert started == [("2026mttd", URL1)] and w.url == URL1
    assert "The Blue Alliance now lists a stream for Madtown Throwdown" in infos(note)[0]


def test_run_without_any_link_starts_and_dms_the_contact(roots, monkeypatch):
    note, box, pub = RecNotifier(), StubOutbox(), StubPublisher()
    pub.outbox = box

    class Pipe(StubPipe):
        score = True

        def __init__(self, out, **kw):
            super().__init__(Path(out))
            self.namer.schedule = []
    monkeypatch.setattr(live, "Pipeline", Pipe)
    monkeypatch.setattr(live, "follow", lambda index, pipe, **kw: "stop requested")
    monkeypatch.setattr(live, "keep_awake", lambda: None)
    monkeypatch.setattr(live.signal, "signal", lambda *a: None)
    monkeypatch.setattr(live.tba, "get", lambda path, key=None: {**MTTD, "webcasts": []})
    monkeypatch.setattr(live, "build_publishing", lambda tag, log=print: (note, box, pub, 60.0, "Slack #errata"))
    import argparse
    ap = argparse.ArgumentParser()
    live.add_arguments(ap)
    assert live.run_from_args(ap.parse_args(["--event", "2026mttd", "--no-record"])) == 0
    warn = [t for lv, t, k in note.msgs if k == "no_stream_link"]
    assert warn and "No stream link for Madtown Throwdown" in warn[0]
    assert any("StreamSplitter started" in t and "no stream link yet" in t for t in infos(note))
    assert live.run_from_args(ap.parse_args([])) == 2                 # neither a link nor an event


def test_morning_notice_at_8_event_time_once_and_only_while_not_live(roots):
    config.save_sections({"event": {"key": "2026mttd", "stream_url": URL1}})        # set up on day 1
    clock = Clock()
    clock.t = at(7, 6)
    w, rec, started, stopped, note, _ = event_watch(roots, clock)
    rec.start(w.choose())
    assert w.url == URL1 and w.source == live.SRC_CONFIG and w.mode == "fixed"
    clock.t = at(7, 7, 59)
    w.tick()
    assert not infos(note)
    clock.t = at(7, 8, 0)
    w.tick()
    (text,) = infos(note)
    # not live at 8: today's webcast from TBA replaces the day-1 link, and the contact is told where it looks
    assert text.startswith("Switched to today's stream from The Blue Alliance (it was " + URL1)
    assert ("Waiting for the stream to start. Checking the suspected stream link every minute: " + URL2
            + ". To use a different link, reply here: stream <link>") in text
    assert started[-1] == ("2026mttd", URL2) and len(stopped) == 1                  # same tag: the index continues
    clock.t = at(7, 11)
    w.tick()
    assert len(infos(note)) == 1                                                    # once a day
    (w.folder / "index.json").write_text(json.dumps({"status": "recording", "runs": []}))
    clock.t = at(8, 8, 1)
    w.tick()
    assert len(infos(note)) == 1                                                    # live at 8: nothing to say
    (w.folder / "index.json").write_text(json.dumps({"status": "waiting", "runs": []}))
    clock.t = at(9, 8, 1)
    w.tick()
    assert len(infos(note)) == 1                                                    # the event is over


def test_morning_notice_waits_for_the_recorder_after_a_late_start_and_keeps_a_slack_link(roots):
    clock = Clock()
    clock.t = at(6, 10)                                                             # started at 10 on day 1
    mine = "https://www.youtube.com/watch?v=CCCCCCCCCC3"
    w, rec, started, stopped, note, _ = event_watch(roots, clock, cli_url=mine)
    w.switch(mine, live.SRC_SLACK)                                                  # the contact chose it
    w.tick()
    assert not infos(note)                                                          # give the recorder 2 min
    clock.t += live.WAIT_GRACE_S
    w.tick()
    (text,) = infos(note)
    assert text.startswith(f"(The Blue Alliance lists {URL1} for today; reply stream auto to use it.) Waiting")
    assert w.url == mine


def test_morning_keeps_a_channel_live_page_that_has_not_started_yet(roots):
    page = "https://www.youtube.com/@MadTown/live"
    config.save_sections({"event": {"key": "2026mttd", "stream_url": page}})
    clock = Clock()
    clock.t = at(7, 6)
    w, rec, started, stopped, note, _ = event_watch(roots, clock)
    rec.start(w.choose())
    clock.t = at(7, 8)
    w.tick()
    (text,) = infos(note)
    assert text.startswith(f"Waiting for the stream to start. Checking the suspected stream link every minute: {page}")
    assert w.url == page and not stopped                              # not swapped for TBA's dated video


def write_runs(folder, runs, chunks=(), status="recording"):
    (folder / "index.json").write_text(json.dumps({"status": status, "source_url": URL2, "runs": runs,
                                                   "chunks": list(chunks)}))


def test_stream_started_once_per_stream_and_back_after_a_long_gap(roots):
    clock = Clock()
    clock.t = at(7, 8)
    w, rec, *_, note, mon = event_watch(roots, clock, cli_url=URL2)
    w.choose()
    old = {"run": 0, "video": "old", "title": "Yesterday", "started": clock.t - 86400}
    write_runs(w.folder, [old])
    w.tick()
    assert not infos(note)                                            # recorded before this run: not news
    r1 = {"run": 1, "video": V2, "title": "Madtown Throwdown  Day 2", "webpage_url": URL2, "started": clock.t + 30}
    clock.t += 40
    write_runs(w.folder, [old, r1])
    w.tick()
    w.tick()
    assert infos(note) == [f"Stream started: Madtown Throwdown Day 2 {URL2}. Recording; your team's matches will "
                           f"be posted to #errata."]
    assert mon.stream["live"] is True and mon.stream["video_url"] == URL2
    c1 = {"seq": 5, "run": 1, "wall_closed": clock.t + 600}
    r2 = {**r1, "run": 2, "started": clock.t + 640}                   # a reconnect 40 s later: quiet
    write_runs(w.folder, [old, r1, r2], [c1])
    w.tick()
    assert len(infos(note)) == 1
    c2 = {"seq": 9, "run": 2, "wall_closed": clock.t + 1000}
    r3 = {**r1, "run": 3, "started": clock.t + 1000 + 20 * 60}        # the same video back after 20 min
    mon._no_chunk_dm = True                                           # the "no new video" warning went out
    write_runs(w.folder, [old, r1, r2, r3], [c1, c2])
    w.tick()
    assert infos(note)[-1] == f"Stream back after 20 min: Madtown Throwdown Day 2 {URL2}. Recording again."
    mon.chunk_read({"seq": 10})
    assert not any("arriving again" in t for t in infos(note))        # said once, not twice
    r4 = {"run": 4, "video": "CCCCCCCCCC3", "title": "Day 3", "webpage_url": "https://youtu.be/CCCCCCCCCC3",
          "started": clock.t + 90000}
    write_runs(w.folder, [old, r1, r2, r3, r4], [c1, c2])
    w.tick()
    assert infos(note)[-1].startswith("Stream started: Day 3 https://youtu.be/CCCCCCCCCC3. Recording")
    assert len(infos(note)) == 3


def test_no_video_warning_waits_until_the_stream_was_live(roots):
    clock, note = Clock(), FakeNotifier()
    mon = live.Monitor(roots / "f", note, clock=clock)
    clock.t += 3600                                                   # started the night before: nothing live
    mon.poll({"status": "waiting"})
    assert "no_chunks" not in note.keys()
    mon.poll({"status": "recording"})                                 # live now; its first chunk takes ~20 s
    assert "no_chunks" not in note.keys()
    clock.t += live.NO_CHUNK_WARN_S
    mon.poll({"status": "waiting"})
    assert note.keys().count("no_chunks") == 1


# -- what the Slack commands do (LiveControls) ------------------------------------------------------------------

def controls_for(roots, clock, publisher=None, cli_url=None):
    w, rec, started, stopped, note, mon = event_watch(roots, clock, cli_url=cli_url)
    rec.start(w.choose())
    ctl = live.LiveControls(w, mon, publisher=publisher, clips_dir=roots / "clips", channel_name="errata",
                            clock=clock)
    return ctl, w, rec, started, stopped


def test_stream_command_restarts_the_recorder_on_the_new_link_and_saves_it(roots):
    config.save_sections({"event": {"key": "2026mttd", "stream_url": URL1}})
    clock = Clock()
    clock.t = at(7, 9)
    ctl, w, rec, started, stopped = controls_for(roots, clock)
    assert started == [("2026mttd", URL1)]
    reply = commands.dispatch(commands.parse("stream <https://www.youtube.com/@MadTown|youtube.com/@MadTown>"), ctl)
    new = "https://www.youtube.com/@MadTown/live"
    assert reply.startswith(f"OK: the stream link is now {new} (saved in config.toml). The recorder restarted")
    assert started[-1] == ("2026mttd", new) and len(stopped) == 1 and stopped[0].url == URL1
    assert config.section("event")["stream_url"] == new and w.source == live.SRC_SLACK
    assert "checks already" in ctl.set_stream(new) and len(started) == 2                # the same link: no restart
    reply = commands.dispatch(commands.parse("stream auto"), ctl)                        # back to TBA's
    assert reply == (f"OK: switched to The Blue Alliance's link: {URL2} (Today's stream (2026-11-07): "
                     f"www.youtube.com/watch?v={V2}). The recorder restarted on it.")
    assert "stream_url" not in config.section("event") and w.mode == "auto" and started[-1][1] == URL2


def test_stream_command_never_writes_another_events_config(roots):
    config.save_sections({"event": {"key": "2025mttd", "stream_url": URL1}})
    clock = Clock()
    clock.t = at(7, 9)
    ctl, w, rec, started, _ = controls_for(roots, clock, cli_url="https://www.twitch.tv/x")
    assert w.source == live.SRC_CLI                                   # 2025's link is not this event's
    assert "for this run only" in ctl.set_stream("https://www.twitch.tv/y")
    assert config.section("event")["stream_url"] == URL1 and started[-1][1] == "https://www.twitch.tv/y"


def test_teams_command_changes_where_matches_go(roots):
    pub, box = real_publisher(roots)
    clock = Clock()
    ctl, *_ = controls_for(roots, clock, publisher=pub, cli_url="https://www.twitch.tv/x")
    assert "Matches of 5026 go to #errata" in ctl.teams_text()
    reply = commands.dispatch(commands.parse("teams 254, 1323"), ctl)
    assert reply.startswith("OK: matches of 254, 1323 go to #errata from the next match on")
    assert config.section("slack")["teams"] == ["254", "1323"] and pub.cfg_slack["teams"] == ["254", "1323"]
    row = {"name": "2026mttd_qm3", "naming": {"name": "2026mttd_qm3", "key": "2026mttd_qm3", "teams_read": ["254"]},
           "start": 10.0, "end": 160.0, "partial": False, "clip": {"path": str(roots / "qm3.mp4")}}
    pub.on_clip(row)
    assert [j["id"] for j in box.jobs("slack")] == ["2026mttd_qm3@10.0:clip:slack"]
    box.close()


def test_pause_and_resume_youtube_hold_and_release_the_uploads(roots):
    yt = Dest("youtube")
    box = live.PausableOutbox(roots / "data" / "outbox" / "p.sqlite", {"youtube": yt},
                              notify.Notifier(log=lambda s: None), stage=False)
    pub = types.SimpleNamespace(outbox=box, cfg_slack={"teams": []})
    m = Match(key="2026mttd_qm1", name="2026mttd_qm1")
    clock = Clock()
    ctl, *_ = controls_for(roots, clock, publisher=pub, cli_url="https://www.twitch.tv/x")
    reply = commands.dispatch(commands.parse("pause youtube"), ctl)
    assert reply.startswith("OK: YouTube uploads are paused") and config.section("youtube")["paused"] is True
    box.add(Job(id="qm1:clip:youtube", destination="youtube", kind="clip", path="x.mp4", match=m))
    assert box.step("youtube") == live.IDLE_S and yt.sent == []                      # held, still queued
    assert box.status()["youtube"]["queued"] == 1 and box.status()["youtube"]["paused"] is True
    assert "(paused: reply resume youtube to send them)" in ctl.status_text()
    reply = commands.dispatch(commands.parse("Resume YouTube"), ctl)
    assert reply == "OK: YouTube uploads resumed; 1 waiting go out now, in order."
    assert "paused" not in config.section("youtube")
    box.step("youtube")
    assert [j.id for j in yt.sent] == ["qm1:clip:youtube"]
    box.close()


def test_status_reply(roots):
    clock = Clock()
    clock.t = at(7, 9)
    ctl, w, *_ = controls_for(roots, clock, cli_url=URL2)
    text = ctl.status_text()
    assert "running since" in text and f"Stream: waiting for it to start; checking {URL2} every minute" in text
    assert "No video from the stream yet" in text and "Clips cut today: 0." in text


def test_build_publishing_keeps_youtube_paused_across_a_restart(roots, monkeypatch):
    pytest.importorskip("google.oauth2.credentials")
    sections = {"event": {"key": "2026mttd"}, "youtube": {"enabled": True, "playlist_id": "PL1", "paused": True}}
    secrets = {"youtube_token": json.dumps({"token": "t", "refresh_token": "r", "client_id": "c.apps",
                                            "client_secret": "s", "token_uri": "https://oauth2.googleapis.com/token"}),
               "youtube_client": json.dumps({"installed": {"client_id": "c.apps", "client_secret": "s"}})}
    monkeypatch.setattr(config, "section", lambda name: dict(sections.get(name, {})))
    monkeypatch.setattr(config, "get_secret", lambda name: secrets.get(name))
    notifier, outbox, publisher, hb_s, summary = live.build_publishing("2026mttd", log=lambda s, **_: None)
    try:
        assert outbox.paused == {"youtube"} and "paused" in summary
    finally:
        outbox.close()
