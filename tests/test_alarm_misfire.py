"""The stopped-running alarm against a Slack that posts cancelled alarms (notify.py, 2026-11-05).

On 2026-11-05 the listener's alarm went off once a minute while it ran: "stopped at 03:13", "03:14", "03:15", and again
"04:08", "04:09" on a new app. Each alarm had been deleted about 2 min before due; Slack answered ok, dropped it from
chat.scheduledMessages.list, and posted it anyway. Measured that morning: deletes 240 and 270 s ahead posted, 300-360 s
ahead held (and ~480 s on 2026-11-04). MisfireSlack models that: a delete made less than `ignore_s` before due is
answered ok and posted all the same.

    .venv\\Scripts\\python.exe -m pytest -q tests\\test_alarm_misfire.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from streamsplitter import listener  # noqa: E402
from streamsplitter.publish import notify  # noqa: E402

T0 = 1_793_877_207.0          # 2026-11-05 03:13:27 PST, the listener's start


class Clock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class MisfireSlack:
    """Scheduled messages, deletes, the DM's history and edits, on the test's clock. A delete less than `ignore_s`
    before due says ok but the message still posts; ignore_s=None: every delete holds."""

    def __init__(self, clock, ignore_s: float | None = 300.0):
        self.clock, self.ignore_s = clock, ignore_s
        self.scheduled: dict[str, dict] = {}     # what Slack will post: id -> {"post_at", "text"}
        self.posted: list[dict] = []             # the DM: {"ts", "text", "bot_id", "alarm"?}
        self.edits: list[tuple[str, str]] = []
        self.dms: list[str] = []
        self.history_calls = 0
        self.n = 0

    def open_dm(self, user):
        return "D1"

    def dm(self, user, text):
        self.dms.append(text)
        self.posted.append({"ts": f"{self.clock.t:.6f}", "text": text, "bot_id": "B1"})
        return {"ts": f"{self.clock.t:.6f}"}

    def schedule_message(self, channel, text, post_at):
        self.n += 1
        sid = f"Q{self.n}"
        self.scheduled[sid] = {"post_at": post_at, "text": text}
        return sid

    def delete_scheduled(self, channel, sid):
        m = self.scheduled.get(sid)
        if m is None:
            return False
        if m["post_at"] - self.clock.t <= 60:
            return False                         # Slack's own rule: refused
        if self.ignore_s is None or m["post_at"] - self.clock.t >= self.ignore_s:
            del self.scheduled[sid]
        else:
            m["deleted"] = True                  # "ok", and it posts anyway
        return True

    def list_scheduled(self, channel=None):
        return [{"id": k, "channel_id": "D1", "post_at": m["post_at"], "text": m["text"]}
                for k, m in self.scheduled.items() if not m.get("deleted")]

    def history(self, channel, limit=15, oldest=None):
        self.history_calls += 1
        lo = float(oldest or 0)
        return [m for m in reversed(self.posted) if float(m["ts"]) > lo][:limit]

    def update_message(self, channel, ts, text):
        self.edits.append((ts, text))
        for m in self.posted:
            if m["ts"] == ts:
                m["text"] = text

    def post_due(self):
        for sid, m in sorted(self.scheduled.items(), key=lambda kv: kv[1]["post_at"]):
            if m["post_at"] <= self.clock.t:
                self.posted.append({"ts": f"{m['post_at'] + 0.6:.6f}", "text": m["text"], "bot_id": "B1",
                                    "alarm": sid})
                del self.scheduled[sid]

    def alarms_posted(self) -> list[dict]:
        return [m for m in self.posted if m.get("alarm")]


def make(clock, slack, cls=notify.Notifier, **kw):
    hb, al, _ = notify.alarm_timing(kw.pop("heartbeat_s", None), kw.pop("alarm_after_s", None))
    n = cls(slack, "U1", host="HOMEPC", alarm_after_s=al, clock=clock, log=kw.pop("log", lambda s: None),
            sleep=lambda s: None, threaded=False, **kw)
    return n, hb


def run(clock, slack, n, every_s, seconds, alive=True):
    """Beats every `every_s` for `seconds` (none when not `alive`: the PC died), Slack posting what comes due."""
    end = clock.t + seconds
    while clock.t < end:
        if alive:
            n.heartbeat()
            n.pump()
        step = min(every_s, end - clock.t) if alive else min(10.0, end - clock.t)
        clock.t += step
        slack.post_due()


def test_the_11_05_timing_reproduces_one_alarm_a_minute():
    """60 s beats with alarms 180 s ahead (what ran on 2026-11-05): every alarm is deleted ~120 s before due and Slack
    posts it, "stopped at 03:13", "03:14", ... That timing is no longer reachable from [notify] (alarm_timing)."""
    clock = Clock()
    slack = MisfireSlack(clock, ignore_s=300.0)
    n = notify.Notifier(slack, "U1", host="HOMEPC", alarm_after_s=180, clock=clock, log=lambda s: None,
                        sleep=lambda s: None, threaded=False)
    run(clock, slack, n, 60, 600)
    posted = slack.alarms_posted()
    times = [round(float(m["ts"]) - T0) for m in posted]
    assert times[:3] == [181, 241, 301] and len(times) >= 7              # 03:16:28, 03:17:28, 03:18:28, ...
    assert all(b - a == 60 for a, b in zip(times, times[1:]))          # one a minute
    looked_at = [m for m in posted if float(m["ts"]) < clock.t - notify.CHECK_S - notify.CHECK_GRACE_S - 60]
    assert looked_at and all(m["text"].startswith("False alarm: StreamSplitter on HOMEPC was still running")
                             for m in looked_at)                       # edited once the check has seen them
    assert not n.alarm_off and not slack.dms           # deleted inside 330 s: expected to misfire, edited, not off
    assert notify.alarm_timing(60, 180)[1] == 420.0


def test_default_timing_deletes_far_enough_ahead_that_nothing_posts():
    clock = Clock()
    slack = MisfireSlack(clock, ignore_s=300.0)                       # anything deleted < 5 min ahead would post
    n, hb = make(clock, slack)
    assert (hb, n.alarm_after_s) == (60.0, 420.0)
    run(clock, slack, n, hb, 3 * 3600)                                 # three hours of a running program
    assert slack.alarms_posted() == [] and slack.dms == [] and slack.edits == []
    assert len(slack.scheduled) == 1                                   # one alarm waiting, ~6-7 min ahead
    assert slack.history_calls <= 3 * 3600 / notify.CHECK_S + 1       # the check reads the DM every 2 min at most


def test_default_timing_holds_with_slacks_cutoff_a_little_worse_than_measured():
    """Held at 300 s on 2026-11-05; a cutoff at 330 s (or a beat up to 30 s late) still leaves every delete in time."""
    clock = Clock()
    slack = MisfireSlack(clock, ignore_s=notify.SAFE_DELETE_S)
    n, hb = make(clock, slack)
    run(clock, slack, n, hb + 25, 3600)                                # every beat 25 s late
    assert slack.alarms_posted() == [] and slack.dms == [] and not n.alarm_off


def test_a_slack_that_posts_every_cancelled_alarm_costs_a_few_false_alarms_then_one_dm():
    clock = Clock()
    slack = MisfireSlack(clock, ignore_s=1e9)                          # no delete ever holds
    said = []
    n, hb = make(clock, slack, log=said.append)
    run(clock, slack, n, hb, 4 * 3600)
    posted = slack.alarms_posted()
    assert len(posted) <= (420 + notify.CHECK_GRACE_S + 60) / 60 + 1   # the ones in flight at the first: not 240
    assert all(m["text"].startswith("False alarm") for m in posted)    # each edited (no new notification)
    assert n.alarm_off
    off = [d for d in slack.dms if "turned its stopped-running alarm off" in d]
    assert len(off) == 1 and len(slack.dms) == 1                       # one DM in all
    assert max(float(m["ts"]) for m in posted) < T0 + 1080             # silence after the first 18 min
    assert any("although it was cancelled" in s for s in said)


def test_one_stop_gives_one_alarm():
    clock = Clock()
    slack = MisfireSlack(clock, ignore_s=300.0)
    n, hb = make(clock, slack)
    run(clock, slack, n, hb, 1800)                                     # runs 30 min ...
    died = clock.t
    run(clock, slack, n, hb, 3600, alive=False)                        # ... then the PC dies
    posted = slack.alarms_posted()
    assert len(posted) == 1                                            # one alarm, no repeats
    assert died + 5 * 60 <= float(posted[0]["ts"]) <= died + 7 * 60 + 5
    assert posted[0]["text"].startswith("StreamSplitter on HOMEPC has not checked in since")


def test_the_listener_running_for_hours_sends_nothing():
    clock = Clock()
    slack = MisfireSlack(clock, ignore_s=300.0)
    n, hb = make(clock, slack, cls=listener.ListenerNotifier)
    run(clock, slack, n, hb, 6 * 3600)
    assert slack.alarms_posted() == [] and slack.dms == []


def test_the_listener_says_a_misfired_alarm_was_false():
    clock = Clock()
    slack = MisfireSlack(clock, ignore_s=1e9)
    n = listener.ListenerNotifier(slack, "U1", host="HOMEPC", alarm_after_s=420, clock=clock, log=lambda s: None,
                                  sleep=lambda s: None, threaded=False)
    run(clock, slack, n, 60, 1200)
    first = slack.alarms_posted()[0]
    assert first["text"].startswith("False alarm: StreamSplitter listener on HOMEPC was still running after")
    assert "Slack sent this alarm although it had been cancelled" in first["text"]


def test_no_history_scope_turns_the_check_off_quietly():
    clock = Clock()
    slack = MisfireSlack(clock, ignore_s=None)
    said = []

    def no_scope(*a, **kw):
        raise notify.PermanentError("missing_scope im:history")
    slack.history = no_scope
    n, hb = make(clock, slack, log=said.append)
    run(clock, slack, n, hb, 3600)
    assert sum("cannot look for cancelled alarms" in s for s in said) == 1
    assert slack.alarms_posted() == [] and slack.dms == []


def test_an_unreachable_dm_is_tried_again_and_said_once():
    clock = Clock()
    slack = MisfireSlack(clock, ignore_s=None)
    said, calls = [], []

    def flaky(*a, **kw):
        calls.append(clock.t)
        raise notify.TransientError("no connection to Slack")
    slack.history = flaky
    n, hb = make(clock, slack, log=said.append)
    run(clock, slack, n, hb, 1800)
    assert sum("could not look for cancelled alarms" in s for s in said) == 1
    assert len(calls) >= 5                                             # still looking, every CHECK_S
    assert all(b - a >= notify.CHECK_S - 1 for a, b in zip(calls, calls[1:]))
