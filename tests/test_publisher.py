"""Notifier (DM rate limit and retries, the stopped-running alarm), router (who gets which clip) and Publisher (the
jobs a pipeline row turns into, TBA's score before a YouTube upload, the describe follow-up). Fake Slack client, fake
TBA, real Outbox with fake destinations; no network.

    .venv\\Scripts\\python.exe -m pytest -q tests\\test_publisher.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from streamsplitter.publish import notify  # noqa: E402
from streamsplitter.publish.base import PermanentError, TransientError  # noqa: E402
from streamsplitter.publish.notify import DELETE_MARGIN_S, Notifier, redact  # noqa: E402
from streamsplitter.publish.outbox import Outbox  # noqa: E402
from streamsplitter.publish.publisher import Publisher, playoff_alliances  # noqa: E402
from streamsplitter.publish.router import match_from_row, route, row_teams  # noqa: E402


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeSlack:
    """The SlackClient calls the notifier uses, recorded in order."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.dms: list[str] = []
        self.scheduled: dict[str, tuple[int, str]] = {}
        self.extra_listed: list[dict] = []
        self.fail_dm: list[Exception] = []
        self.fail_schedule: Exception | None = None
        self.n = 0

    def open_dm(self, user):
        return "D1"

    def dm(self, user, text):
        if self.fail_dm:
            raise self.fail_dm.pop(0)
        self.calls.append(("dm", text))
        self.dms.append(text)
        return {"ts": "1.1", "channel": "D1"}

    def schedule_message(self, channel, text, post_at):
        if self.fail_schedule:
            raise self.fail_schedule
        self.n += 1
        sid = f"Q{self.n}"
        self.scheduled[sid] = (post_at, text)
        self.calls.append(("schedule", sid, post_at))
        return sid

    def delete_scheduled(self, channel, sid):
        self.calls.append(("delete", sid))
        return self.scheduled.pop(sid, None) is not None

    def list_scheduled(self, channel=None):
        return ([{"id": k, "channel_id": "D1", "post_at": v[0], "text": v[1]} for k, v in self.scheduled.items()]
                + self.extra_listed)


def notifier(clock, slack=None, **kw):
    lines = []
    n = Notifier(slack or FakeSlack(), "U1", host="pit-laptop", tag="2026mttd", clock=clock, log=lines.append,
                 sleep=lambda s: None, threaded=False, **kw)
    return n, lines


# ---------------------------------------------------------------------------------------------------------------
# messages

def test_console_always_dm_once_per_key_per_30_min():
    clock = Clock()
    n, lines = notifier(clock)
    assert n.warn("disk low", key="disk") is True
    assert n.warn("disk low", key="disk") is False       # same key within 30 min: log only
    n.info("started")                                     # no key: always
    n.pump()
    assert len(n.client.dms) == 2 and n.client.dms[0].startswith("Warning: disk low")
    clock.t += 1801
    assert n.warn("disk low", key="disk") is True
    n.pump()
    assert len(n.client.dms) == 3
    assert sum("disk low" in s for s in lines) == 3       # the console got every one
    assert n.last_warning["text"] == "disk low"


def test_dm_retried_with_backoff_not_lost():
    clock = Clock()
    slack = FakeSlack()
    slack.fail_dm = [TransientError("offline"), TransientError("offline")]
    n, lines = notifier(clock, slack)
    n.error("the recorder exited")
    assert n.pump() == pytest.approx(5)
    assert n.pump() == pytest.approx(5)                   # not due yet
    clock.t += 5
    assert n.pump() == pytest.approx(10)
    clock.t += 10
    n.pump()
    assert slack.dms == ["Error: the recorder exited [StreamSplitter on pit-laptop]"]
    assert n.pending() == 0


def test_dm_permanent_error_drops_the_message():
    slack = FakeSlack()
    slack.fail_dm = [PermanentError("cannot message that person")]
    n, lines = notifier(Clock(), slack)
    n.warn("x")
    n.pump()
    assert n.pending() == 0 and any("could not DM" in s for s in lines)


def test_tokens_never_reach_the_log_or_a_dm():
    n, lines = notifier(Clock())
    n.error("failed with xoxb-123-456-abcdef and Bearer ya29.secretvalue")
    n.pump()
    text = "\n".join(lines + n.client.dms)
    assert "xoxb-123" not in text and "ya29." not in text and "[redacted]" in text
    assert redact("GOCSPX-abc_def") == "[redacted]"


def test_without_slack_console_only():
    lines = []
    n = Notifier(None, None, log=lines.append)
    assert n.warn("hello", key="k") is False
    assert n.heartbeat() is True
    n.stopped("done")
    assert any("hello" in s for s in lines) and any("stopping: done" in s for s in lines)


# ---------------------------------------------------------------------------------------------------------------
# the stopped-running alarm

def test_heartbeat_schedules_the_next_alarm_before_deleting_the_last():
    clock = Clock()
    n, _ = notifier(clock, alarm_after_s=600)
    assert n.heartbeat()
    clock.t += 120
    assert n.heartbeat()
    s = n.client.calls
    assert [c[0] for c in s] == ["schedule", "schedule", "delete"]
    assert s[2][1] == s[0][1]                             # the first alarm, deleted after the second exists
    assert s[1][2] == int(clock.t + 600)
    assert list(n.client.scheduled) == [s[1][1]]
    assert "pit-laptop (2026mttd) has not checked in since" in n.client.scheduled[s[1][1]][1]


def test_alarm_too_close_to_due_is_kept_and_followed_by_an_all_clear():
    clock = Clock()
    n, _ = notifier(clock, alarm_after_s=600)
    n.heartbeat()                                         # Q1 due at +600
    clock.t += 600 - DELETE_MARGIN_S + 10                 # late heartbeat: Q1 due in 80 s, Slack would refuse
    n.heartbeat()
    assert ("delete", "Q1") not in n.client.calls
    assert {a["id"] for a in n._alarms} == {"Q1", "Q2"}
    clock.t += 120                                        # Q1 has gone off while we are alive
    n.heartbeat()
    n.pump()
    assert any(d.startswith("False alarm: StreamSplitter on pit-laptop is still running") for d in n.client.dms)
    assert ("delete", "Q2") in n.client.calls
    assert {a["id"] for a in n._alarms} == {"Q3"}


def test_heartbeat_offline_keeps_the_alarm_already_scheduled():
    clock = Clock()
    slack = FakeSlack()
    n, lines = notifier(clock, slack)
    n.heartbeat()
    slack.fail_schedule = TransientError("no network")
    clock.t += 120
    assert n.heartbeat() is False
    assert list(slack.scheduled) == ["Q1"] and ("delete", "Q1") not in slack.calls
    assert sum("could not push the stopped-running alarm" in s for s in lines) == 1
    clock.t += 30
    n.heartbeat()                                         # logged once per outage
    assert sum("could not push the stopped-running alarm" in s for s in lines) == 1


def test_stopped_cancels_the_alarm_and_says_why():
    clock = Clock()
    n, _ = notifier(clock)
    n.heartbeat()
    n.stopped("stop requested (streamsplitter stop)")
    assert ("delete", "Q1") in n.client.calls and not n.client.scheduled
    assert n.client.dms[-1] == "StreamSplitter on pit-laptop stopped: stop requested (streamsplitter stop)."


def test_restart_deletes_the_previous_runs_alarm(tmp_path):
    clock = Clock()
    slack = FakeSlack()
    state = tmp_path / "alarm.json"
    first, _ = notifier(clock, slack, state_path=state)
    first.heartbeat()                                     # Q1, then the process dies (no stopped())
    slack.scheduled["Q-lost"] = (int(clock.t + 500), first.alarm_text(clock.t - 60))   # not in the state file
    slack.scheduled["Q-other"] = (int(clock.t + 500), "StreamSplitter on pit-laptop (2026chcmp) has not checked "
                                                      "in since Fri 10:00; it may have stopped.")
    clock.t += 30
    second, _ = notifier(clock, slack, state_path=state)
    second.heartbeat()
    assert set(slack.scheduled) == {"Q2", "Q-other"}      # the other recording's alarm stays
    order = [c for c in slack.calls if c[0] in ("schedule", "delete")]
    assert order[1][0] == "schedule" and {c[1] for c in order[2:]} == {"Q1", "Q-lost"}
    assert json.loads(state.read_text())["alarms"][0]["id"] == "Q2"


def test_previous_runs_alarm_that_went_off_gets_running_again(tmp_path):
    clock = Clock()
    slack = FakeSlack()
    state = tmp_path / "alarm.json"
    first, _ = notifier(clock, slack, state_path=state)
    first.heartbeat()
    slack.scheduled.clear()                               # it was posted
    clock.t += 900
    second, _ = notifier(clock, slack, state_path=state)
    second.heartbeat()
    second.pump()
    assert any("is running again" in d for d in slack.dms)


def test_dm_text_is_escaped_for_slack():
    n, _ = notifier(Clock())
    n.warn("a <b> & c")
    n.pump()
    assert n.client.dms[0].startswith("Warning: a &lt;b&gt; &amp; c")


# ---------------------------------------------------------------------------------------------------------------
# routing

SLACK = {"enabled": True, "teams": ["5026"], "score_in_thread": True}
YT = {"enabled": True, "route": "others", "version": "score"}


def entry(key, red, blue, red_score=-1, blue_score=-1):
    """A TBA match record."""
    lv, a, b = re.search(r"_(qm|sf|f)(\d+)(?:m(\d+))?$", key).groups()
    set_n, num = (1, int(a)) if lv == "qm" else (int(a), int(b or 1))
    return {"key": key, "event_key": key.split("_")[0], "comp_level": lv, "set_number": set_n, "match_number": num,
            "alliances": {"red": {"team_keys": [f"frc{t}" for t in red], "score": red_score},
                          "blue": {"team_keys": [f"frc{t}" for t in blue], "score": blue_score}}}


SCHEDULE = [entry("2026mttd_qm1", ["5026", "254", "1678"], ["604", "8033", "199"]),
            entry("2026mttd_qm2", ["100", "200", "300"], ["400", "500", "600"], 88, 77)]


def row(name, key=None, teams_read=(), shown=(), clip="", score=None, partial=False):
    r = {"name": name, "naming": {"name": name, "key": key, "teams_read": list(teams_read)}, "start": 0.0,
         "end": 150.0, "clip": {"path": clip or f"C:/clips/{name or 't00000000.0'}.mp4"},
         "numbers_shown": list(shown), "partial": partial}
    if score:
        r["score_clip"] = {"path": score}
    return r


def test_route_rules():
    assert route({"5026", "254"}, SLACK, YT) == ["slack"]
    assert route({"100"}, SLACK, YT) == ["youtube"]
    assert route({"5026"}, SLACK, {**YT, "route": "all"}) == ["slack", "youtube"]
    assert route({"5026"}, {**SLACK, "enabled": False}, YT) == ["youtube"]     # nowhere else to go
    assert route({"5026"}, SLACK, {**YT, "enabled": False}) == ["slack"]
    assert route(set(), SLACK, YT) == ["youtube"]
    assert route({"frc5026"}, {**SLACK, "teams": [5026]}, YT) == ["slack"]
    no_route = {k: v for k, v in YT.items() if k != "route"}       # the default: own matches on Slack and YouTube
    assert route({"5026", "254"}, SLACK, no_route) == ["slack", "youtube"]
    assert route({"100"}, SLACK, no_route) == ["youtube"]


def test_row_teams_schedule_overlay_and_stand_ins():
    named = row("2026mttd_qm2", "2026mttd_qm2", teams_read=["5026"])          # misnamed? the read team still counts
    assert "5026" in row_teams(named, SCHEDULE[1])
    unnamed = row(None, shown=["9993", "5026", "12"])
    assert row_teams(unnamed, None) == {"9993", "5026", "12"}
    assert "9993" not in row_teams(row("2026mttd_qm2", "2026mttd_qm2", shown=["9993"]), SCHEDULE[1])


def test_match_from_row():
    m = match_from_row(row("2026mttd_qm1_r2", "2026mttd_qm1"), SCHEDULE, {"key": "2026mttd", "name": "Madtown",
                                                                          "year": 2026})
    assert (m.key, m.name, m.replay, m.comp_level, m.match_number) == ("2026mttd_qm1", "2026mttd_qm1_r2", 2, "qm", 1)
    assert m.red == ["5026", "254", "1678"] and m.blue == ["604", "8033", "199"]
    assert (m.red_score, m.blue_score) == (None, None)    # -1: not posted
    assert m.event_name == "Madtown" and m.year == 2026
    m2 = match_from_row(row("2026mttd_qm2", "2026mttd_qm2"), SCHEDULE, {})
    assert (m2.red_score, m2.blue_score) == (88, 77) and m2.year == 2026
    p = match_from_row(row("2026mttd_p3", "2026mttd_p3"), SCHEDULE, {})
    assert (p.comp_level, p.match_number, p.red) == ("p", 3, [])
    u = match_from_row(row(None, clip="C:/clips/t00012345.6.mp4"), SCHEDULE, {"key": "2026mttd"})
    assert u.key is None and u.name == "t00012345.6" and u.event_key == "2026mttd"


# ---------------------------------------------------------------------------------------------------------------
# publisher

class Dest:
    def __init__(self, name, result=None):
        self.name = name
        self.result = result or {}
        self.sent = []
        self.playoff_alliances = 8

    def send(self, job):
        self.sent.append(job)
        if self.name == "youtube":
            job.state.setdefault("video_id", "v1")       # as YouTubeDestination keeps it
        return dict(self.result)


def publisher(tmp_path, slack=SLACK, yt=YT, schedule=SCHEDULE, tba=None, clock=None, **kw):
    clock = clock or Clock()
    dests = {"slack": Dest("slack", {"ts": "11.1"}), "youtube": Dest("youtube", {"video_id": "v1"})}
    box = Outbox(tmp_path / "box.sqlite", dests, notify.Notifier(log=lambda s: None), clock=clock, stage=False)
    pub = Publisher(box, cfg_event={"key": "2026mttd", "name": "Madtown Throwdown", "year": 2026}, cfg_slack=slack,
                    cfg_youtube=yt, schedule=lambda: schedule, tba_get=tba or (lambda path: {}), clock=clock,
                    log=lambda s: None, **kw)
    return pub, box, dests, clock


def ids(box):
    return [(r["id"], r["job"].path) for r in box.jobs()]


def test_own_match_goes_to_slack_with_the_score_in_its_thread(tmp_path):
    pub, box, dests, clock = publisher(tmp_path)
    r = row("2026mttd_qm1", "2026mttd_qm1", clip="C:/clips/2026mttd_qm1.mp4")
    pub.on_clip(r)
    assert ids(box) == [("2026mttd_qm1@0.0:clip:slack", "C:/clips/2026mttd_qm1.mp4")]
    r["score_clip"] = {"path": "C:/clips/2026mttd_qm1_score.mp4"}
    pub.on_score(r)
    jobs = box.jobs()
    assert [j["id"] for j in jobs] == ["2026mttd_qm1@0.0:clip:slack", "2026mttd_qm1@0.0:score:slack"]
    assert jobs[1]["job"].state == {"after": "2026mttd_qm1@0.0:clip:slack"}
    while box.step("slack") == 0:
        pass
    assert dests["slack"].sent[1].state["thread_ts"] == "11.1"


def test_other_match_goes_to_youtube_once_settled(tmp_path):
    pub, box, dests, clock = publisher(tmp_path)
    r = row("2026mttd_qm2", "2026mttd_qm2", clip="C:/clips/2026mttd_qm2.mp4")
    pub.on_clip(r)
    assert box.jobs() == []                              # YouTube waits for the score version
    r["score_clip"] = {"path": "C:/clips/2026mttd_qm2_score.mp4"}
    pub.on_score(r)
    assert ids(box) == [("2026mttd_qm2@0.0:score:youtube", "C:/clips/2026mttd_qm2_score.mp4")]


def test_no_results_screen_uploads_the_match_clip(tmp_path):
    pub, box, dests, clock = publisher(tmp_path)
    r = row("2026mttd_qm2", "2026mttd_qm2", clip="C:/clips/2026mttd_qm2.mp4")
    pub.on_score(r)
    assert ids(box) == [("2026mttd_qm2@0.0:clip:youtube", "C:/clips/2026mttd_qm2.mp4")]
    pub2, box2, _, _ = publisher(tmp_path / "b", yt={**YT, "version": "match"})
    r["score_clip"] = {"path": "C:/clips/2026mttd_qm2_score.mp4"}
    pub2.on_score(r)
    assert [j["id"] for j in box2.jobs()] == ["2026mttd_qm2@0.0:clip:youtube"]


def test_unnamed_clips(tmp_path):
    pub, box, dests, clock = publisher(tmp_path)
    ours = row(None, shown=["5026", "1", "2", "3"], clip="C:/clips/t00000100.0.mp4")
    theirs = row(None, shown=["11", "22"], clip="C:/clips/t00000900.0.mp4")
    for r in (ours, theirs):
        pub.on_clip(r)
        pub.on_score(r)
    got = [j["id"] for j in box.jobs()]
    assert got == ["t00000100.0@0.0:clip:slack", "t00000900.0@0.0:clip:youtube"]
    yt = box.get("t00000900.0@0.0:clip:youtube")["job"].match
    assert yt.key is None and yt.name == "t00000900.0"


def test_rename_by_the_results_screen(tmp_path):
    pub, box, dests, clock = publisher(tmp_path)
    r = row("2026mttd_qm2", "2026mttd_qm2", teams_read=["5026"], clip="C:/clips/2026mttd_qm2.mp4")
    pub.on_clip(r)                                       # routed by the read team although misnamed
    r.update(name="2026mttd_qm1", naming={"name": "2026mttd_qm1", "key": "2026mttd_qm1", "teams_read": ["5026"]},
             clip={"path": "C:/clips/2026mttd_qm1.mp4"}, score_clip={"path": "C:/clips/2026mttd_qm1_score.mp4"})
    pub.on_score(r)
    jobs = {j["id"]: j for j in box.jobs()}
    assert set(jobs) == {"2026mttd_qm2@0.0:clip:slack", "2026mttd_qm1@0.0:score:slack"}
    assert jobs["2026mttd_qm2@0.0:clip:slack"]["job"].match.name == "2026mttd_qm1"     # still queued: corrected
    assert jobs["2026mttd_qm1@0.0:score:slack"]["job"].state["after"] == "2026mttd_qm2@0.0:clip:slack"


def test_a_name_freed_by_a_rename_is_posted_for_the_real_match(tmp_path):
    """Review 2026-11-04: qm13 posted, renamed qm12 by its results screen; the real qm13 (the name is free again)
    must get its own post, and its score reply must thread under its own clip."""
    pub, box, dests, clock = publisher(tmp_path)
    first = row("2026mttd_qm1", "2026mttd_qm1", clip="C:/clips/2026mttd_qm1.mp4")
    first["start"] = 100.0
    pub.on_clip(first)
    while box.step("slack") == 0:                         # posted
        pass
    first.update(name="2026mttd_qm2", naming={"name": "2026mttd_qm2", "key": "2026mttd_qm2", "teams_read": ["5026"]},
                 clip={"path": "C:/clips/2026mttd_qm2.mp4"}, score_clip={"path": "C:/clips/2026mttd_qm2_score.mp4"})
    pub.on_score(first)
    real = row("2026mttd_qm1", "2026mttd_qm1", clip="C:/clips/2026mttd_qm1.mp4")   # the name, used again
    real["start"] = 520.0
    pub.on_clip(real)
    real["score_clip"] = {"path": "C:/clips/2026mttd_qm1_score.mp4"}
    pub.on_score(real)
    slack = [j["id"] for j in box.jobs("slack")]
    assert slack == ["2026mttd_qm1@100.0:clip:slack", "2026mttd_qm2@100.0:score:slack",
                     "2026mttd_qm1@520.0:clip:slack", "2026mttd_qm1@520.0:score:slack"]
    assert box.get("2026mttd_qm1@520.0:clip:slack")["status"] == "queued"          # not taken as already sent
    assert box.get("2026mttd_qm1@520.0:score:slack")["job"].state["after"] == "2026mttd_qm1@520.0:clip:slack"
    assert box.get("2026mttd_qm2@100.0:score:slack")["job"].state["after"] == "2026mttd_qm1@100.0:clip:slack"


def test_score_reply_finds_its_clip_job_after_a_restart(tmp_path):
    """The row as follower.json keeps it (its Slack clip job id in row["jobs"]) settles in a new process with a new
    Publisher: the reply threads under the clip posted before, which is not posted again."""
    pub, box, dests, clock = publisher(tmp_path)
    r = row("2026mttd_qm1", "2026mttd_qm1", clip="C:/clips/2026mttd_qm1.mp4")
    pub.on_clip(r)
    saved = json.loads(json.dumps(r))                     # what live.follow writes to follower.json
    box.close()
    pub2, box2, _, _ = publisher(tmp_path)                # same outbox file, new process
    saved.update(name="2026mttd_qm2", naming={"name": "2026mttd_qm2", "key": "2026mttd_qm2", "teams_read": ["5026"]},
                 score_clip={"path": "C:/clips/2026mttd_qm2_score.mp4"})
    pub2.on_score(saved)
    slack = {j["id"]: j for j in box2.jobs("slack")}
    assert set(slack) == {"2026mttd_qm1@0.0:clip:slack", "2026mttd_qm2@0.0:score:slack"}
    assert slack["2026mttd_qm2@0.0:score:slack"]["job"].state["after"] == "2026mttd_qm1@0.0:clip:slack"
    assert slack["2026mttd_qm1@0.0:clip:slack"]["job"].match.name == "2026mttd_qm2"   # retargeted


def test_partial_clip_goes_to_youtube_as_partial(tmp_path):
    """Lead, 2026-11-04: a partial clip (the overlay lost early, or the stream dropped near the end) goes to YouTube
    too, from on_clip (it has no results-screen search), titled "(Partial)"; Slack unchanged."""
    from streamsplitter.publish import templates
    pub, box, dests, clock = publisher(tmp_path)
    pub.on_clip(row("2026mttd_qm2_partial", "2026mttd_qm2", partial=True))
    pub.on_clip(row("2026mttd_qm1_partial", "2026mttd_qm1", partial=True))
    assert [j["id"] for j in box.jobs()] == ["2026mttd_qm2_partial@0.0:clip:youtube",
                                             "2026mttd_qm1_partial@0.0:clip:slack"]
    m = box.get("2026mttd_qm2_partial@0.0:clip:youtube")["job"].match
    assert templates.render("{match_title}", m) == "Qualification 2 (Partial)"


def test_no_score_search_uploads_from_on_clip(tmp_path):
    pub, box, dests, clock = publisher(tmp_path, score_search=False)
    pub.on_clip(row("2026mttd_qm2", "2026mttd_qm2"))
    assert [j["id"] for j in box.jobs()] == ["2026mttd_qm2@0.0:clip:youtube"]


def test_youtube_gets_tba_score_before_upload_and_describe_follows_when_late(tmp_path):
    posted = {"now": False}

    def tba(path):
        assert path == "/match/2026mttd_qm3"
        s = (120, 99) if posted["now"] else (-1, -1)
        return {"alliances": {"red": {"team_keys": ["frc1"], "score": s[0]}, "blue": {"team_keys": ["frc2"],
                                                                                      "score": s[1]}}}
    pub, box, dests, clock = publisher(tmp_path, tba=tba)
    pub.on_score(row("2026mttd_qm3", "2026mttd_qm3"))     # not on the schedule yet
    box.step("youtube")
    up = dests["youtube"].sent[0]
    assert up.match.red == ["1"] and up.match.red_score is None
    d = box.get("2026mttd_qm3@0.0:describe:youtube")
    assert d["status"] == "queued" and d["job"].state["video_id"] == "v1" and d["job"].kind == "describe"
    assert box.status()["youtube"]["background"] == 1 and box.status()["youtube"]["queued"] == 0
    clock.t += 600
    box.step("youtube")                                   # TBA has nothing yet: waits, not sent
    assert len(dests["youtube"].sent) == 1
    posted["now"] = True
    clock.t += 600
    box.step("youtube")
    desc = dests["youtube"].sent[1]
    assert desc.kind == "describe" and (desc.match.red_score, desc.match.blue_score) == (120, 99)
    assert box.get("2026mttd_qm3@0.0:describe:youtube")["status"] == "done"


def test_score_from_the_refreshed_schedule_without_asking_tba(tmp_path):
    sched = [dict(e) for e in SCHEDULE]
    calls = []
    pub, box, dests, clock = publisher(tmp_path, schedule=sched, tba=lambda p: calls.append(p) or {})
    pub.on_score(row("2026mttd_qm2", "2026mttd_qm2"))
    box.step("youtube")
    assert (dests["youtube"].sent[0].match.red_score, calls) == (88, [])
    assert box.get("2026mttd_qm2@0.0:describe:youtube") is None


def test_playoff_bracket_size():
    assert playoff_alliances({"playoff_type": 11}) == 4
    assert playoff_alliances({"playoff_type": 10}) == 8
    assert playoff_alliances({"playoff_type_string": "Double Elimination Bracket (4 Alliances)"}) == 4
    assert playoff_alliances({}) is None


def test_playoff_bracket_set_on_the_youtube_destination(tmp_path):
    seen = []

    def tba(path):
        seen.append(path)
        return {"playoff_type": 11} if path.startswith("/event/") else {}
    sched = [entry("2026mttd_sf2m1", ["1", "2", "3"], ["4", "5", "6"])]
    pub, box, dests, clock = publisher(tmp_path, schedule=sched, tba=tba)
    pub.on_score(row("2026mttd_sf2m1", "2026mttd_sf2m1"))
    box.step("youtube")
    assert dests["youtube"].playoff_alliances == 4 and "/event/2026mttd" in seen
