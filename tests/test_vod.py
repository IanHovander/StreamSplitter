"""Saved streams (vod.py) and the event's other streams (user, 2026-11-06: MadTown 2026 streamed on Twitch only):
the copy into chunks and its index, a copy restarted after a failure, the follower ending on a finished index, the
clip file dated to its match, --to, the upload wait; streams.alternates, the recorder trying them, live passing them.
Fakes only: no network, no ffmpeg run.

    .venv\\Scripts\\python.exe -m pytest -q tests\\test_vod.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from streamsplitter import config, live, record, streams, vod  # noqa: E402
from tests.test_live_publish import FakeNotifier, StubPipe, StubPublisher, roots  # noqa: E402,F401

START = 1794017759.0          # the practice VOD's created_at: 2026-11-06 18:15:59 PST
TWITCH = {"id": "v2921730738", "title": "Madtown Throwdown 2026", "start_ts": START, "duration": 100.0,
          "webpage_url": "https://www.twitch.tv/videos/2921730738", "fmt": "1080p60", "height": 1080,
          "inputs": [{"url": "https://example.invalid/index-dvr.m3u8", "headers": {"User-Agent": "UA"}}]}
MTTD = {"key": "2026mttd", "webcasts": [
    {"type": "youtube", "channel": "WPfTIUtaTzo", "date": "2026-11-06"},
    {"type": "youtube", "channel": "QEtvEsSF9_I", "date": "2026-11-07"},
    {"type": "youtube", "channel": "y7wcg1gn8-M", "date": "2026-11-08"},
    {"type": "twitch", "channel": "madtownthrowdown"}]}


# ---------------------------------------------------------------------------------------------------------------
# The copy
# ---------------------------------------------------------------------------------------------------------------

def test_format_and_command_for_one_muxed_input_and_for_separate_audio():
    sel = vod.format_selector(1080)
    assert sel.split("/")[0] == "bv[height<=1080][vcodec^=avc1]+ba[ext=m4a]"     # YouTube: video + m4a
    assert sel.split("/")[1] == "b[height<=1080][vcodec^=avc1]"                  # Twitch: "1080p60", muxed
    out = Path("o")
    cmd = vod.copy_cmd("ff", TWITCH, out, "v00", 0.0, None)
    assert cmd.count("-i") == 1 and "-ss" not in cmd and "-t" not in cmd
    assert cmd[cmd.index("-user_agent") + 1] == "UA"
    assert ["-map", "0:v:0", "-map", "0:a:0?"] == cmd[cmd.index("-map"):cmd.index("-map") + 4]
    assert cmd[-1] == str(out / "v00_%05d.ts") and str(out / "v00.csv") in cmd and "copy" in cmd
    yt = dict(TWITCH, inputs=[{"url": "v", "headers": {}}, {"url": "a", "headers": {}}])
    cmd = vod.copy_cmd("ff", yt, out, "v01", 120.0, 300.0)
    assert cmd.count("-i") == 2 and cmd.count("-ss") == 2 and cmd[cmd.index("-t") + 1] == "180.000"
    assert "1:a:0" in cmd


class FakeCopy:
    """An ffmpeg copy that writes its chunks and segment list at once, then exits with `code`."""

    def __init__(self, cmd, plan, **_):
        csv_path = Path(cmd[cmd.index("-segment_list") + 1])
        pattern = cmd[-1]
        rows, self.returncode = plan.pop(0)
        with csv_path.open("w", newline="") as f:
            for k, (a, b) in enumerate(rows):
                name = Path(pattern.replace("%05d", f"{k:05d}")).name
                (csv_path.parent / name).write_bytes(b"ts")
                f.write(f"{name},{a},{b}\n")
        self.cmd = cmd

    def poll(self):
        return self.returncode

    def wait(self):
        return self.returncode

    def kill(self):
        pass


def copier(tmp_path, plan, monkeypatch, **kw):
    monkeypatch.setattr(vod.ffmpeg, "find", lambda: "ffmpeg")
    cmds = []

    def popen(cmd, **k):
        cmds.append(cmd)
        return FakeCopy(cmd, plan, **k)
    c = vod.Copier(dict(TWITCH, **kw.pop("src", {})), tmp_path, "t", event="2026mttd", source_url="u",
                   log=lambda *a, **k: None, popen=popen, poll_s=0.0, retry_s=0.0, **kw)
    return c, cmds


def run_copy(c):
    c.start()
    c._thread.join(10)
    assert not c._thread.is_alive()
    return json.loads(c.index_path.read_text(encoding="utf-8"))


def test_copy_lists_every_chunk_with_the_videos_wall_time_and_finishes(tmp_path, monkeypatch):
    c, cmds = copier(tmp_path, [([(0, 20.02), (20.02, 40), (40, 60), (60, 80), (80, 100)], 0)], monkeypatch)
    idx = run_copy(c)
    ch = idx["chunks"]
    assert [x["seq"] for x in ch] == [0, 1, 2, 3, 4] and idx["next_seq"] == 5
    assert idx["finished"] is True and idx["status"] == "stopped" and idx["vod"] is True and not idx["live"]
    assert all(x["wall0"] == START + x["t0"] for x in ch) and idx["release_timestamp"] == START
    assert ch[0]["gap_before_s"] is None and all(x["gap_before_s"] == 0.0 for x in ch[1:])
    assert all((tmp_path / x["path"]).is_file() for x in ch) and len(cmds) == 1


def test_a_failed_copy_starts_again_where_it_stopped(tmp_path, monkeypatch):
    plan = [([(0, 20), (20, 40)], 1),                  # the connection dropped at 40 s
            ([(0, 20.5), (20.5, 40.5), (40.5, 60)], 0)]  # -ss 40: starts a little before (a keyframe), ends at 100
    c, cmds = copier(tmp_path, plan, monkeypatch)
    idx = run_copy(c)
    ch = idx["chunks"]
    assert [x["path"] for x in ch] == ["v00_00000.ts", "v00_00001.ts", "v01_00000.ts", "v01_00001.ts",
                                       "v01_00002.ts"]
    assert cmds[1][cmds[1].index("-ss") + 1] == "40.000"
    assert [(x["t0"], x["t1"]) for x in ch] == [(0, 20), (20, 40), (40, 60.5), (60.5, 80.5), (80.5, 100)]
    assert idx["finished"] and "failed" not in idx and [r["run"] for r in idx["runs"]] == [0, 1]


def test_a_copy_that_keeps_failing_gives_up_and_says_so(tmp_path, monkeypatch):
    plan = [([], 1) for _ in range(vod.COPY_TRIES)]
    c, _ = copier(tmp_path, plan, monkeypatch)
    idx = run_copy(c)
    assert idx["finished"] and "failed 4 times" in idx["failed"] and c.error


def test_a_short_copy_counts_as_failed(tmp_path, monkeypatch):
    """ffmpeg exit 0 but the video stopped early (a platform cut-off): copied again from there."""
    plan = [([(0, 20)], 0), ([(0, 20), (20, 40), (40, 60), (60, 80)], 0)]
    c, cmds = copier(tmp_path, plan, monkeypatch)
    idx = run_copy(c)
    assert len(cmds) == 2 and idx["chunks"][-1]["t1"] == 100 and idx["finished"]


def test_a_rerun_reuses_a_finished_copy_and_redoes_an_unfinished_one(tmp_path, monkeypatch):
    c, _ = copier(tmp_path, [([(0, 50), (50, 100)], 0)], monkeypatch)
    run_copy(c)
    again, _ = copier(tmp_path, [], monkeypatch)
    assert again.done_before() and len(again.index["chunks"]) == 2
    other, _ = copier(tmp_path, [], monkeypatch, until_s=60.0)      # another range: copied again
    assert not other.done_before()
    (tmp_path / "v00_00001.ts").unlink()                               # a chunk gone: copied again
    assert not vod.Copier(TWITCH, tmp_path, "t", event=None, source_url="u").done_before()
    other.clear()
    assert not list(tmp_path.glob("v00*")) and not other.index_path.exists()


# ---------------------------------------------------------------------------------------------------------------
# The follower on a finished index; the clip dated to its match; --to; the upload wait
# ---------------------------------------------------------------------------------------------------------------

def write_vod_index(folder: Path, n: int, finished: bool) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    chunks = []
    for i in range(1, n + 1):
        (folder / f"v00_{i:05d}.ts").write_bytes(b"x")
        chunks.append({"seq": i, "path": f"v00_{i:05d}.ts", "t0": 20.0 * (i - 1), "t1": 20.0 * i,
                       "wall0": START + 20 * (i - 1), "wall_closed": 2e9})
    p = folder / "index.json"
    p.write_text(json.dumps({"status": "stopped" if finished else "recording", "vod": True, "finished": finished,
                             "chunks": chunks}), encoding="utf-8")
    return p


def test_follow_ends_once_a_finished_index_is_read_and_dates_the_clips(roots):
    folder = live.live_folder("vod")
    index = write_vod_index(folder, 3, finished=True)
    out = roots / "clips"
    out.mkdir()
    for name in ("2026mttd_qm1", "2026mttd_qm2"):
        (out / f"{name}.mp4").write_bytes(b"mp4")
    pipe, pub = StubPipe(out), StubPublisher()
    reason = live.follow(index, pipe, poll_s=0.01, log=lambda s, **_: None, publisher=pub, notifier=FakeNotifier())
    assert reason == "the whole recording is read" and pipe.fed == [1, 2, 3]
    assert pub.clips == ["2026mttd_qm1", "2026mttd_qm2"]
    assert abs(os.path.getmtime(out / "2026mttd_qm1.mp4") - (START + 30.0)) < 1      # its end, not tonight
    assert pub.scores == ["2026mttd_qm1", "2026mttd_qm2"]


def test_a_live_index_is_followed_until_stopped_and_its_clips_keep_their_time(roots):
    folder = live.live_folder("live")
    index = write_vod_index(folder, 2, finished=False)
    data = json.loads(index.read_text())
    data.pop("vod")
    index.write_text(json.dumps(data))
    out = roots / "clips"
    out.mkdir()
    (out / "2026mttd_qm1.mp4").write_bytes(b"mp4")
    before = os.path.getmtime(out / "2026mttd_qm1.mp4")
    pipe = StubPipe(out)
    assert live.follow(index, pipe, poll_s=0.01, stop=lambda: len(pipe.fed) >= 2 and pipe.fed.count(2) > 0,
                       log=lambda s, **_: None, notifier=FakeNotifier()) == "stopped"
    assert os.path.getmtime(out / "2026mttd_qm1.mp4") == before


def test_to_turns_the_other_destinations_off(roots, monkeypatch):
    sections = {"event": {"key": "2026mttd"}, "slack": {"enabled": True, "teams": ["5026"], "channel_id": "C1"},
                "youtube": {"enabled": True, "playlist_id": "PL1"}, "drive": {"enabled": False}}
    monkeypatch.setattr(config, "section", lambda name: dict(sections.get(name, {})))
    monkeypatch.setattr(config, "get_secret", lambda name: None)
    lines = []
    notifier, outbox, publisher, _, summary = live.build_publishing("t", log=lambda s, **_: lines.append(s),
                                                                     only={"drive"})
    assert outbox is None and summary == "publishing is off"
    assert not any("YouTube" in s or "Slack" in s for s in lines)           # no complaint about what is off
    with pytest.raises(ValueError, match="unknown destination"):
        vod._restrict({"drive", "dropbox"})


def test_another_events_vod_never_uses_this_events_drive_folder(roots, monkeypatch):
    sections = {"event": {"key": "2026mttd"}, "drive": {"enabled": True, "folder_id": "F", "event_folder_id": "E",
                                                         "event_folder_event": "2026mttd"}}
    monkeypatch.setattr(config, "section", lambda name: dict(sections.get(name, {})))
    monkeypatch.setattr(config, "get_secret", lambda name: "{}" if name in ("drive_token", "youtube_client") else None)
    seen = {}
    monkeypatch.setattr(live, "_drive_destination", lambda dr, ev, *a: seen.update(dr=dr, ev=ev))
    live.build_publishing("t", log=lambda s, **_: None, event="2025mttd")
    assert seen["ev"] == {"key": "2025mttd"} and "event_folder_id" not in seen["dr"] and seen["dr"]["folder_id"] == "F"


class DrainBox:
    def __init__(self, states):
        self.states = states

    def status(self):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]


def st(queued=0, running=0, failing_since=None, active=True, paused=False):
    return {"queued": queued, "running": running, "done": 0, "failed": 0, "failing_since": failing_since,
            "active": active, "paused": paused, "last_error": None}


def test_drain_waits_for_the_uploads_but_not_for_a_dead_destination():
    sleeps = []
    box = DrainBox([{"drive": st(2)}, {"drive": st(1, 1)}, {"drive": st()}])
    vod.drain(box, log=lambda *a, **k: None, sleep=sleeps.append, clock=lambda: 0.0)
    assert len(sleeps) == 2
    box = DrainBox([{"drive": st(3, failing_since=0.0), "youtube": st(5, active=False)}])
    vod.drain(box, log=lambda *a, **k: None, sleep=sleeps.append, clock=lambda: 700.0)   # failing for 700 s
    assert len(sleeps) == 2


def test_vod_command_line():
    ap = argparse.ArgumentParser()
    vod.add_arguments(ap)
    a = ap.parse_args(["https://www.twitch.tv/videos/2921730738", "--event", "2026mttd", "--to", "drive"])
    assert a.to == "drive" and a.from_s == 0.0 and a.until_s is None and not a.no_publish
    assert vod.default_tag("2026mttd", "v2921730738") == "2026mttd_v2921730738"
    with pytest.raises(SystemExit):
        ap.parse_args(["https://www.twitch.tv/videos/1"])                    # --event is required


# ---------------------------------------------------------------------------------------------------------------
# The event's other streams
# ---------------------------------------------------------------------------------------------------------------

def test_alternates_today_video_then_channel_pages_then_twitch():
    sat = dt.date(2026, 11, 7)
    alt = streams.alternates(MTTD, sat, "https://www.youtube.com/@Madtown_Throwdown/live")
    assert alt == ["https://www.youtube.com/watch?v=QEtvEsSF9_I", "https://www.twitch.tv/madtownthrowdown"]
    assert streams.alternates(MTTD, sat, "twitch.tv/madtownthrowdown") == [
        "https://www.youtube.com/watch?v=QEtvEsSF9_I"]                       # the link itself is never repeated
    ev = {"webcasts": MTTD["webcasts"] + [{"type": "youtube", "channel": "UCWVgs9ep6oWNVa6Rdpza_cg"}]}
    assert streams.alternates(ev, None, None) == ["https://www.youtube.com/channel/UCWVgs9ep6oWNVa6Rdpza_cg/live",
                                                  "https://www.twitch.tv/madtownthrowdown"]
    assert streams.alternates(None, sat, "x") == [] and streams.alternates({"webcasts": None}, sat, "x") == []


def test_the_recorder_records_the_first_live_link():
    tried = []

    def resolver(url, h):
        tried.append(url)
        if "twitch" not in url:
            raise record.NotLive(f"{url}: is_upcoming")
        return "twitch source"
    rec = record.Recorder("yt", Path("."), "t", resolver=resolver, also=["https://www.twitch.tv/madtownthrowdown",
                                                                         "yt"])
    assert rec.also == ["https://www.twitch.tv/madtownthrowdown"]          # the link itself dropped
    assert rec._resolve_live() == ("twitch source", None) and tried == ["yt", "https://www.twitch.tv/madtownthrowdown"]
    rec.also = []
    src, why = rec._resolve_live()
    assert src is None and why == "yt: is_upcoming"

    def broken(url, h):
        raise RuntimeError("boom")
    rec = record.Recorder("a", Path("."), "t", resolver=broken, also=["b"])
    assert rec._resolve_live() == (None, "resolve failed: boom; resolve failed: boom")


def test_record_command_line_takes_also():
    ap = argparse.ArgumentParser()
    record.add_arguments(ap)
    a = ap.parse_args(["yt", "--also", "https://www.twitch.tv/a", "--also", "b"])
    assert a.also == ["https://www.twitch.tv/a", "b"] and ap.parse_args(["yt"]).also == []


def test_live_starts_the_recorder_with_the_events_other_streams(monkeypatch):
    cmds = []
    monkeypatch.setattr(live.subprocess, "Popen", lambda cmd, **kw: cmds.append(cmd) or types.SimpleNamespace())
    rec = live.RecorderProc(types.SimpleNamespace(event="2026mttd"), "2026mttd",
                            also_for=lambda url: ["https://www.twitch.tv/madtownthrowdown"])
    rec.start("https://www.youtube.com/@Madtown_Throwdown/live")
    cmd = cmds[0]
    assert cmd[cmd.index("--also") + 1] == "https://www.twitch.tv/madtownthrowdown"
    assert rec.also == ["https://www.twitch.tv/madtownthrowdown"]

    def broken(url):
        raise OSError("no TBA")
    rec = live.RecorderProc(types.SimpleNamespace(event=None), "t", also_for=broken)
    rec.start("u")                                                          # the link alone still records
    assert "--also" not in cmds[1] and rec.also == []


def test_event_watch_lists_tbas_other_streams_for_the_link(roots):
    from tests.test_live_publish import PST, event_watch
    sat_7am = dt.datetime(2026, 11, 7, 7, 0, tzinfo=PST).timestamp()
    w, *_ = event_watch(roots, lambda: sat_7am, event=MTTD)
    assert w.alternates("https://www.youtube.com/@Madtown_Throwdown/live") == [
        "https://www.youtube.com/watch?v=QEtvEsSF9_I", "https://www.twitch.tv/madtownthrowdown"]
