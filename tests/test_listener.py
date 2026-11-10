"""Start from Slack (streamsplitter/listener.py): split / yes / no / stop / list in the DMs, staying silent while a
recording answers, keeping a Slack-started event going (restart, the 3-an-hour cap, the auto-stop), never handling a
message twice across a restart, the window mode (its goodbye DM, a recording that outlives the window), the
recording's own answers when no listener is alive, and the log-in start. Fake Slack, fake TBA, fake YouTube and a fake
recording runner: no network, no real recorder or listener, config.toml and secrets in the test's own folder, the
log-in start in a temp folder.

    .venv\\Scripts\\python.exe -m pytest -q -p no:cacheprovider tests\\test_listener.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import plistlib
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from streamsplitter import config, listener  # noqa: E402
from streamsplitter.publish import commands  # noqa: E402
from streamsplitter.tba import TBAError  # noqa: E402
from test_commands import CONTACT as REC_CONTACT, Controls, FakeClient  # noqa: E402

IAN, SAM, EVE = "U0IAN", "U0SAM", "U0EVE"          # the contact, another allowed person, someone not allowed
NOW = dt.datetime(2026, 11, 5, 20, 0).timestamp()  # Thursday evening before MadTown
CHANNEL_LIVE = "https://www.youtube.com/@Madtown_Throwdown/live"
MTTD = {"key": "2026mttd", "name": "MadTown Throwdown", "short_name": "MadTown", "year": 2026, "event_code": "mttd",
        "event_type": 99, "start_date": "2026-11-06", "end_date": "2026-11-08", "city": "Madera", "state_prov": "CA",
        "timezone": "America/Los_Angeles",
        "webcasts": [{"type": "youtube", "channel": "WPfTIUtaTzo", "date": "2026-11-06"},
                     {"type": "youtube", "channel": "QEtvEsSF9_I", "date": "2026-11-07"},
                     {"type": "twitch", "channel": "madtownthrowdown"}]}
CTTD = {"key": "2026cttd", "name": "CT Throwdown", "year": 2026, "event_code": "cttd", "event_type": 99,
        "start_date": "2026-10-03", "end_date": "2026-10-03", "city": "Hartford", "state_prov": "CT", "webcasts": []}
CASJ = {"key": "2026casj", "name": "Silicon Valley Regional", "short_name": "Silicon Valley", "year": 2026,
        "event_code": "casj", "event_type": 0, "start_date": "2026-11-12", "end_date": "2026-11-14",
        "city": "San Jose", "state_prov": "CA", "webcasts": [{"type": "twitch", "channel": "firstinspires"}]}
OLD = {"key": "2025mttd", "name": "MadTown Throwdown", "year": 2025, "event_type": 99, "start_date": "2025-11-07",
       "end_date": "2025-11-09", "city": "Madera", "state_prov": "CA", "webcasts": []}
EVENTS = {e["key"]: e for e in (MTTD, CTTD, CASJ, OLD)}


# ---------------------------------------------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------------------------------------------

class Clock:
    def __init__(self, t: float = NOW):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeSlack:
    """The SlackClient calls the listener makes: one DM per person ("D" + user id), newest message first."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.dms: dict[str, list[dict]] = {}
        self.posts: list[tuple[str, str, str | None]] = []
        self.dms_sent: list[tuple[str, str]] = []

    def auth_test(self):
        return {"scopes": ["chat:write", "im:write", "im:history"], "missing_scopes": []}

    def open_dm(self, user):
        return "D" + user

    def history(self, channel, limit=15, oldest=None):
        return [dict(m) for m in self.dms.get(channel, [])[:limit]]

    def replies(self, channel, ts, oldest=None, limit=15):
        return []

    def post_message(self, channel, text, thread_ts=None):
        self.posts.append((channel, text, thread_ts))
        return {"ts": f"{self.clock():.6f}", "channel": channel}

    def dm(self, user, text):                       # the notifier's DMs to the contact
        self.dms_sent.append((user, text))
        return {"ts": f"{self.clock():.6f}", "channel": "D" + user}

    def say(self, user, text, ts: float):
        self.dms.setdefault("D" + user, []).insert(0, {"ts": f"{ts:.6f}", "user": user, "text": text})

    def to(self, user) -> list[str]:
        return [t for c, t, _ in self.posts if c == "D" + user]


class Note:
    """The listener's notifier: what it DMs the contact."""

    def __init__(self):
        self.msgs: list[tuple[str, str]] = []

    def _add(self, level, text, key=None, dm=True):
        self.msgs.append((level, text))
        return True

    def info(self, text, key=None, dm=True):
        return self._add("info", text)

    def warn(self, text, key=None, dm=True):
        return self._add("warn", text)

    def error(self, text, key=None, dm=True):
        return self._add("error", text)


class FakeRunner:
    """The recordings on this computer. start() records what config.toml says, as `streamsplitter start` does.
    `boxes`: the folder holding each recording's mailbox (publish/commands.Mailbox); a recording without one is of an
    older version (it reads its owner's DM itself)."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.running: dict[str, dict] = {}
        self.ended: dict[str, dict] = {}
        self.started: list[tuple[str, str | None]] = []
        self.stops: list[str] = []
        self.stop_files: set[str] = set()
        self.clip_count: dict[str, int] = {}
        self.fail: Exception | None = None
        self.boxes: dict[str, Path] = {}

    def mailbox(self, tag):
        return self.boxes[tag] / commands.INBOX_DIR if tag in self.boxes and tag in self.running else None

    def recordings(self):
        return [{"running": True, **st, "tag": t} for t, st in self.running.items()]

    def status(self, tag):
        if tag in self.running:
            return {"running": True, **self.running[tag]}
        return dict(self.ended.get(tag) or {"running": False, "reason": "no status file: it has not run for this tag"})

    def stop_waiting(self, tag):
        return tag in self.stop_files

    def start(self):
        if self.fail is not None:
            raise self.fail
        ev = config.section("event")
        tag = ev.get("tag") or ev["key"]
        self.started.append((tag, ev.get("stream_url")))
        self.running[tag] = {"started": self.clock(), "clips_cut": 0,
                             "stream": {"url": ev.get("stream_url"), "live": False}}
        return 4242

    def request_stop(self, tag, by="streamsplitter stop"):
        self.stops.append(tag)
        self.stop_files.add(tag)
        self.stopped_by = by

    def clips(self, tag):
        return self.clip_count.get(tag, 0)

    def event_name(self, tag):
        return None

    # what happens on the computer
    def end(self, tag, reason="stop requested (streamsplitter stop)", waiting=0):
        self.running.pop(tag, None)
        self.stop_files.discard(tag)
        self.ended[tag] = {"running": False, "stopped_reason": reason, "reason": reason,
                           "outbox": {"youtube": {"queued": waiting, "running": 0, "done": 30}}}

    def crash(self, tag):
        self.running.pop(tag, None)
        self.ended[tag] = {"running": False, "stopped_reason": None,
                           "reason": "its process has ended"}


class FakeYouTube:
    def __init__(self, fail: Exception | None = None):
        self.fail = fail
        self.made: list[tuple[str, str]] = []

    def find_playlist(self, title):
        if self.fail:
            raise self.fail
        return None

    def create_playlist(self, title, description, privacy="unlisted"):
        self.made.append((title, privacy))
        return {"id": "PL26", "title": title, "url": "https://www.youtube.com/playlist?list=PL26", "privacy": privacy}

    def set_playlist_privacy(self, playlist_id, privacy):
        return {"id": playlist_id, "privacy": privacy}


def fake_tba(path):
    if path == "/events/2026":
        return [{k: v for k, v in e.items() if k != "webcasts"} for e in (MTTD, CTTD, CASJ)]
    key = path.removeprefix("/event/")
    if key in EVENTS:
        return dict(EVENTS[key])
    raise TBAError(f"TBA {path}: HTTP 404")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """config.toml, secrets and the data folder of the test's own; never the real ones or the credential store."""
    monkeypatch.setenv("STREAMSPLITTER_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("STREAMSPLITTER_DATA", str(tmp_path / "data"))
    for k in ("TBA_API_KEY", "STREAMSPLITTER_DELIVER", "STREAMSPLITTER_WORK",
              *(f"STREAMSPLITTER_{s.upper()}" for s in config.SECRETS)):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(config, "_keyring", lambda: None)
    for name in ("startup_folder", "launch_agents_folder", "autostart_folder"):
        monkeypatch.setattr(listener, name, lambda n=name: tmp_path / "login" / n)
    config.save_sections({
        "event": {"key": "2025mttd", "name": "MadTown Throwdown", "year": 2025, "tag": "2025mttd",
                  "stream_url": "https://www.youtube.com/watch?v=OLDvideo123"},
        "slack": {"enabled": True, "teams": ["5026"], "channel_id": "C0ERRATA", "channel_name": "errata"},
        "youtube": {"enabled": True, "route": "all", "privacy": "unlisted", "playlist_id": "PL25",
                    "playlist_title": "2025 MadTown Throwdown", "playlist_event": "2025mttd"},
        "drive": {"enabled": True, "folder_id": "F0", "folder_title": "FRC match videos",
                  "event_folder_id": "F25", "event_folder_event": "2025mttd",
                  "event_folder_title": "2025 MadTown Throwdown"},
        "notify": {"slack_user": IAN, "slack_user_name": "Ian H"},
        "listen": {"enabled": True, "users": [SAM]}})
    for name, value in (("slack_bot_token", "xoxb-test-notreal"), ("tba_key", "tba-notreal"),
                        ("youtube_token", '{"token": "x"}'), ("youtube_client", '{"installed": {}}')):
        config.set_secret(name, value)
    return tmp_path


class World:
    """A listener with fakes around it, and a way to talk to it. record(tag): a recording of this version runs, with
    its mailbox (the recording's side runs in poll(), with test_commands' Controls)."""

    def __init__(self, yt: FakeYouTube | None = None):
        self.clock = Clock()
        self.slack = FakeSlack(self.clock)
        self.runner = FakeRunner(self.clock)
        self.note = Note()
        self.yt = yt or FakeYouTube()
        self.controls = Controls()
        self.mailboxes: dict[str, commands.Mailbox] = {}
        self.ls = self.make()
        self.poll()                                   # the first look: what is in the DMs already is not a command

    def make(self) -> listener.Listener:
        return listener.Listener(self.slack, IAN, listener.allowed_users(), contact_name="Ian H", runner=self.runner,
                                 notifier=self.note, tba_get=fake_tba, channel_live=lambda url: CHANNEL_LIVE,
                                 youtube=lambda: self.yt, clock=self.clock, log=lambda s: None, host="HOMEPC")

    def record(self, tag: str = "2026mttd", mailbox: bool = True) -> None:
        self.runner.running[tag] = {"started": self.clock.t}
        if mailbox:
            folder = config.work_root() / "live" / tag
            self.runner.boxes[tag] = folder
            self.mailboxes[tag] = commands.Mailbox(folder, self.controls, channel_name="errata", log=lambda s: None,
                                                   replies=listener.listen_folder() / listener.REPLIES)
            self.mailboxes[tag].advertise()

    def poll(self):
        for p in self.ls.pollers:
            p.poll()
        for tag, box in self.mailboxes.items():       # the recording takes its mailbox ...
            if tag in self.runner.running:
                box.process()
        self.ls.deliver()                             # ... and the listener posts its replies

    def send(self, user: str, text: str, after: float = 5.0) -> list[str]:
        """`user` types `text` `after` seconds later; the replies in their DM."""
        self.clock.t += after
        n = len(self.slack.to(user))
        self.slack.say(user, text, self.clock.t)
        self.poll()
        return self.slack.to(user)[n:]

    def tick(self, after: float = 0.0):
        self.clock.t += after
        self.ls.tick()


@pytest.fixture
def w(env):
    return World()


def plain(text: str) -> str:
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


# ---------------------------------------------------------------------------------------------------------------
# split, yes, no
# ---------------------------------------------------------------------------------------------------------------

def test_split_by_the_code_of_an_event_on_now_starts_at_once(w, env):
    # user, 2026-11-09: the yes split waited for was missed twice and nothing was recorded. The exact code of an event
    # on today or tomorrow is not in doubt: it starts, and the one reply says what started and how to undo it
    [reply] = w.send(IAN, "split 2026mttd")
    assert plain(reply).splitlines() == [
        "Starting MadTown Throwdown (2026mttd) on HOMEPC now; its \"StreamSplitter started\" message comes here in "
        "about 30 s. YouTube playlist: https://www.youtube.com/playlist?list=PL26",
        f"Stream: {CHANNEL_LIVE} (from The Blue Alliance).",
        "If that is not live, it tries https://www.twitch.tv/madtownthrowdown.",
        "Slack: your team's matches (5026) go to #errata.",
        "YouTube: every match, unlisted, to the playlist \"2026 MadTown Throwdown\".",
        "Google Drive: every match, to the folder \"FRC match videos/2026 MadTown Throwdown\".",
        "Wrong event or setting? Reply stop to stop it."]
    assert w.runner.started == [("2026mttd", CHANNEL_LIVE)] and IAN not in w.ls.state["pending"]
    ev, yt, dr = config.section("event"), config.section("youtube"), config.section("drive")
    assert {k: ev.get(k) for k in ("key", "name", "year", "start_date", "end_date", "event_type", "tag",
                                   "stream_url")} == {
        "key": "2026mttd", "name": "MadTown Throwdown", "year": 2026, "start_date": "2026-11-06",
        "end_date": "2026-11-08", "event_type": 99, "tag": "2026mttd", "stream_url": CHANNEL_LIVE}
    assert (yt["playlist_id"], yt["playlist_title"], yt["playlist_event"], yt["enabled"]) == \
        ("PL26", "2026 MadTown Throwdown", "2026mttd", True)
    assert w.yt.made == [("2026 MadTown Throwdown", "unlisted")]
    assert not any(k.startswith("event_folder") for k in dr)          # the old event's Drive folder is forgotten
    active = w.ls.state["active"]
    assert active["key"] == "2026mttd" and active["stop_at"] == dt.datetime(2026, 11, 9, 6, 0).timestamp()
    # the recording starts after the messages the listener answered: it does not answer them again
    saved = json.loads((env / "data" / "outbox" / "2026mttd.commands.json").read_text())
    assert float(saved["last_ts"]) == pytest.approx(w.clock.t)


@pytest.mark.parametrize("typed", ["split <https://www.thebluealliance.com/event/2026mttd|thebluealliance.com/event/"
                                   "2026mttd>", "split thebluealliance.com/event/2026mttd",
                                   "split https://thebluealliance.com/event/2026MTTD#rankings", "Split 2026MTTD"])
def test_split_by_link(w, typed):
    [reply] = w.send(IAN, typed)
    assert reply.startswith("Starting MadTown Throwdown (2026mttd) on HOMEPC now")


def test_split_asks_only_when_in_doubt_and_says_why(w):
    [one] = w.send(IAN, "split MadTown Throwdown")
    assert one.startswith("MadTown Throwdown - Madera, CA - Nov 6-8, 2026 (2026mttd)")
    assert one.splitlines()[-1] == ("*Reply yes to start recording, or no.* Nothing records until you do. (Asking "
                                    "because it was found by its name. Next time, split 2026mttd yes starts without "
                                    "asking.)")
    assert "(made when you reply yes)" in one and w.runner.started == []
    [later] = w.send(IAN, "split 2026casj")                     # the code, but not on today or tomorrow
    assert "(Asking because it is not on today or tomorrow (Nov 12-14, 2026)." in later
    [now] = w.send(IAN, "split 2026casj yes")                   # the yes in the same message
    assert now.startswith("Starting Silicon Valley Regional (2026casj) on HOMEPC now") and len(w.runner.started) == 1
    w.runner.end("2026casj")
    [bare] = w.send(IAN, "start")                                # no event named: the one set up, asked first
    assert bare.startswith("Silicon Valley Regional - San Jose, CA") and "no event was named" in bare
    assert w.send(IAN, "no") == ["OK, nothing started."]
    config.save_sections({"event": {"key": None, "tag": None, "name": None}})
    assert plain(w.send(IAN, "split")[0]) == listener.SPLIT_HOW
    assert len(w.runner.started) == 1


def test_split_by_name_one_several_none(w):
    [one] = w.send(IAN, "split MadTown Throwdown")
    assert one.startswith("MadTown Throwdown - Madera") and "Reply yes to start recording, or no." in one
    [one] = w.send(IAN, "split madera")                          # the city
    assert "(2026mttd)" in one
    [several] = w.send(IAN, "split throwdown")
    assert plain(several).splitlines() == ["Several 2026 events match. Reply split with the code of yours:",
                                           "  split 2026mttd   (MadTown Throwdown - Madera, CA - Nov 6-8, 2026)",
                                           "  split 2026cttd   (CT Throwdown - Hartford, CT - Oct 3, 2026)"]
    [none] = w.send(IAN, "split nowhere-at-all")
    assert none.startswith("No 2026 event matches that.") and "nowhere" not in none     # nothing typed comes back
    [token] = w.send(IAN, "split xoxb-pasted-by-mistake")             # a token is never used, nor repeated
    assert token.startswith("That looks like a password or key") and "xoxb" not in token
    [bad] = w.send(IAN, "split 2026nope")
    assert bad.startswith("The Blue Alliance has no event 2026nope.")
    assert w.runner.started == []


def test_yes_only_from_the_person_who_asked_and_only_for_15_minutes(w):
    w.send(SAM, "split madtown")
    assert w.send(IAN, "yes") == [listener.NOTHING_TO_CONFIRM]          # Ian did not ask
    assert w.send(EVE, "yes") == []                                    # not allowed: not even read
    assert w.send(SAM, "yes", after=901) == ["That question expired after 15 minutes, so nothing was done. Reply "
                                             "split 2026mttd yes to start it now."]
    assert w.runner.started == []
    w.send(SAM, "split madtown")
    assert w.send(SAM, "cancel") == ["OK, nothing started."]
    assert w.send(SAM, "yes") == [listener.NOTHING_TO_CONFIRM] and w.runner.started == []
    w.send(SAM, "split madtown")
    [reply] = w.send(SAM, "ok")
    assert reply.startswith("Starting MadTown Throwdown (2026mttd) on HOMEPC; Ian H gets its messages.")
    assert w.runner.started == [("2026mttd", CHANNEL_LIVE)]


def test_a_split_question_reminds_once_then_says_it_expired(w):
    w.send(SAM, "split madtown")
    w.tick(after=listener.REMIND_S - 10)
    assert len(w.slack.to(SAM)) == 1
    w.tick(after=20)
    remind = plain(w.slack.to(SAM)[-1])
    assert remind.startswith("Still waiting for your yes: reply yes to start MadTown Throwdown (2026mttd), or no. "
                             "Nothing records until you do; the question ends at Thu 20:15.")
    w.tick(after=60)
    assert len(w.slack.to(SAM)) == 2                                   # once
    w.tick(after=listener.CONFIRM_S)
    assert plain(w.slack.to(SAM)[-1]) == ("split 2026mttd at Thu 20:00 was not answered yes within 15 minutes, so "
                                          "MadTown Throwdown (2026mttd) was not started. Reply split 2026mttd yes to "
                                          "start it now.")
    w.tick(after=60)
    assert len(w.slack.to(SAM)) == 3 and w.runner.started == []


def test_official_event_has_youtube_off_and_the_next_offseason_event_has_it_back(w):
    [summary] = w.send(IAN, "split 2026casj")
    assert "YouTube: off (official FIRST event)." in summary.splitlines()
    assert "Stream: https://www.twitch.tv/firstinspires (from The Blue Alliance)." in summary.splitlines()
    w.send(IAN, "yes")
    assert w.yt.made == [] and config.section("youtube")["enabled"] is False
    assert config.section("listen")["youtube_off_for"] == "2026casj"
    w.runner.end("2026casj")
    [reply] = w.send(IAN, "split 2026mttd")
    assert "YouTube: every match, unlisted, to the playlist \"2026 MadTown Throwdown\"." in plain(reply).splitlines()
    assert config.section("youtube")["enabled"] is True and "youtube_off_for" not in config.section("listen")


def test_a_playlist_that_cannot_be_made_records_without_youtube(env):
    w = World(FakeYouTube(fail=RuntimeError("quotaExceeded")))
    [reply] = w.send(IAN, "split 2026mttd")
    lines = reply.splitlines()
    yt = [ln for ln in lines if ln.startswith("YouTube")]
    assert len(yt) == 1 and yt[0].startswith("YouTube: the playlist could not be made (quotaExceeded), so this event "
                                             "records without YouTube")              # one YouTube line, not two
    assert w.runner.started == [("2026mttd", CHANNEL_LIVE)]                 # recording anyway
    assert config.section("youtube")["enabled"] is False


def test_an_event_already_over_records_until_stop(w):
    [summary] = w.send(IAN, "split 2025mttd")
    assert "That event is over: it records until you reply stop." in summary
    assert "Stream: https://www.youtube.com/watch?v=OLDvideo123 (the link set before)." in summary   # same event
    w.send(IAN, "yes")
    assert w.ls.state["active"]["stop_at"] is None
    w.tick(after=86400 * 3)
    assert w.runner.stops == []


# ---------------------------------------------------------------------------------------------------------------
# one recording per computer, stop, list, silence
# ---------------------------------------------------------------------------------------------------------------

def test_split_while_recording(w):
    w.runner.running["2026mttd"] = {"started": NOW}
    config.save_sections({"event": {"key": "2026mttd", "name": "MadTown Throwdown", "tag": "2026mttd"}})
    assert w.send(IAN, "split 2026mttd") == ["MadTown Throwdown (2026mttd) is already recording on HOMEPC. Reply "
                                             "status to see how it is going."]
    assert w.send(SAM, "split madtown") == w.send(IAN, "split 2026mttd")
    assert w.send(IAN, "split 2026casj") == ["HOMEPC is recording MadTown Throwdown (2026mttd). One recording per "
                                             "computer: reply stop first, then split again."]
    assert w.runner.started == []


def test_stop_asks_then_stops_and_says_when_it_has(w):
    w.send(IAN, "split 2026mttd")
    assert w.send(IAN, "stop") == ["Stop recording MadTown Throwdown (2026mttd)? Reply yes (or no to keep it "
                                   "recording)."]
    [reply] = w.send(IAN, "yes")
    assert reply.startswith("Stopping MadTown Throwdown (2026mttd): it finishes what it is doing first")
    assert w.runner.stops == ["2026mttd"] and w.ls.state["active"] is None
    assert w.runner.stopped_by == f"from Slack by {w.ls.names[IAN]}"   # the recording's stop DM says so
    n = len(w.slack.posts)
    w.tick(after=5)                                                    # still finishing an upload
    assert len(w.slack.posts) == n
    w.runner.clip_count["2026mttd"] = 12
    w.runner.end("2026mttd")
    w.tick(after=5)
    assert w.slack.to(IAN)[-1] == "Stopped recording MadTown Throwdown (2026mttd). 12 clips made; no uploads waiting."
    w.runner.crash("2026mttd")                                         # stopped from Slack: never restarted
    w.tick(after=120)
    assert w.runner.started == [("2026mttd", CHANNEL_LIVE)]


def test_stop_that_takes_too_long_says_so(w):
    w.runner.running["2026mttd"] = {"started": NOW}
    w.send(SAM, "stop")
    w.send(SAM, "yes")
    w.tick(after=listener.STOP_WAIT_S + 1)
    assert "has not stopped after 3 minutes (an upload may be finishing)" in w.slack.to(SAM)[-1]
    assert w.send(IAN, "no") == [listener.NOTHING_TO_CONFIRM]


def test_stop_yes_or_the_events_code_stops_at_once(w):
    w.send(IAN, "split 2026mttd")
    assert w.send(SAM, "stop 2026casj") == ["That is not what is recording: HOMEPC is recording MadTown Throwdown "
                                            "(2026mttd). Reply stop to stop it."]
    assert w.runner.stops == []
    [reply] = w.send(SAM, "stop 2026mttd")
    assert reply.startswith("Stopping MadTown Throwdown (2026mttd)") and w.runner.stops == ["2026mttd"]
    w.runner.end("2026mttd")
    w.send(IAN, "split 2026mttd")
    [reply] = w.send(IAN, "Stop yes!")
    assert reply.startswith("Stopping MadTown Throwdown (2026mttd)") and w.runner.stops == ["2026mttd", "2026mttd"]


def test_stop_and_status_with_nothing_recording(w):
    assert w.send(IAN, "stop") == ["Nothing is recording. The event set up on HOMEPC is MadTown Throwdown (2025mttd): "
                                   "reply split 2025mttd to start recording it."]
    [status] = w.send(SAM, "list")                                     # list is status
    first, second = status.splitlines()
    assert first.startswith("Recording: nothing is recording on HOMEPC.") and first.endswith(
        "The event set up on HOMEPC is MadTown Throwdown (2025mttd): reply split 2025mttd to start recording it.")
    assert second == ("Slack listener: running on HOMEPC since Thu 20:00. It reads your messages every 20 s, and every "
                      "3 s for 2 minutes after you write.")
    assert w.send(IAN, "yes") == [listener.NOTHING_TO_CONFIRM]


def test_a_split_left_waiting_is_said_until_it_expires_then_once(w):
    # user, 2026-11-07: split 2026mttd, then pause youtube, resume drive and teams instead of yes; it never started
    w.send(IAN, "split madtown")
    [reply] = w.send(IAN, "pause youtube")
    assert reply.splitlines()[-1] == "(MadTown Throwdown (2026mttd) has not started yet: reply yes to start it, or no.)"
    [status] = w.send(IAN, "status")
    assert status.splitlines()[-1] == ("(MadTown Throwdown (2026mttd) has not started yet: reply yes to start it, or "
                                       "no.)")
    assert "The event set up" in w.send(SAM, "status")[0]               # only the asker's question
    [status] = w.send(IAN, "status", after=901)
    assert status.splitlines()[-1] == ("(split 2026mttd at Thu 20:00 was not answered yes within 15 minutes, so "
                                       "MadTown Throwdown (2026mttd) was not started. Reply split 2026mttd yes to "
                                       "start it now.)")
    [status] = w.send(IAN, "status")
    assert "not started" not in status and "The event set up on HOMEPC is MadTown Throwdown (2025mttd)" in status
    assert w.send(IAN, "yes") == ["That question expired after 15 minutes, so nothing was done. Reply split 2026mttd "
                                  "yes to start it now."]
    assert w.runner.started == []
    w.send(IAN, "split madtown")                                      # its own summary has no reminder under it
    [reply] = w.send(IAN, "yes")
    assert reply.startswith("Starting MadTown Throwdown (2026mttd)") and "not started yet" not in reply


def test_split_summary_says_what_is_paused_and_the_other_streams(w):
    config.save_sections({"event": {"key": "2026mttd", "name": "MadTown Throwdown", "tag": "2026mttd",
                                    "stream_url": CHANNEL_LIVE},      # as on the user's computer, 2026-11-07
                          "youtube": {"paused": True}, "drive": {"paused": True}})
    w.clock.t = dt.datetime(2026, 11, 7, 1, 52).timestamp()           # Saturday: TBA dates a YouTube video today
    [summary] = w.send(IAN, "split 2026mttd")
    lines = summary.splitlines()
    assert lines[1:3] == [f"Stream: {CHANNEL_LIVE} (the link set before).",
                          "If that is not live, it tries https://www.youtube.com/watch?v=QEtvEsSF9_I and "
                          "https://www.twitch.tv/madtownthrowdown."]
    assert lines[4].endswith(" Paused: the uploads wait until you reply resume youtube.")
    assert lines[5].endswith(" Paused: the uploads wait until you reply resume drive.")


def test_status_of_a_recording_of_an_older_version(w):
    w.send(IAN, "split 2026mttd")
    w.runner.running["2026mttd"].update(stream={"url": CHANNEL_LIVE, "live": True, "live_since": NOW + 60,
                                                "title": "Madtown Throwdown 2026 Day 1"}, clips_cut=3,
                                        last_clip="Qualification 2")
    [text] = w.send(SAM, "list")
    line, last = text.splitlines()
    assert line.startswith("Recording MadTown Throwdown (2026mttd) on HOMEPC since Thu 20:")
    assert "Stream: live since Thu 20:01: Madtown Throwdown 2026 Day 1. Clips cut since then: 3 (last: Qualification " \
           "2)." in line and line.endswith("Started from Slack: restarted if it stops, and stops by itself Mon 06:00.")
    assert last.startswith("Slack listener: running on HOMEPC since Thu 20:00. It reads your messages every 20 s")


def test_silent_in_the_contacts_dm_while_a_recording_of_an_older_version_answers(w):
    """A recording started before this version reads its owner's DM itself (no mailbox): there the listener answers
    only what the older listener answered (commands.legacy_listener_command), so each message still gets one answer."""
    w.record(mailbox=False)
    for text in ("status", "help", "pause youtube", "stream auto", "make coffee"):
        assert w.send(IAN, text) == [], text                          # the recording answers these there
    assert w.send(IAN, "people")[0].startswith("Who may use the StreamSplitter app")        # the older listener's
    # other allowed people's DMs are read only by the listener
    [status] = w.send(SAM, "status")
    assert status.startswith("Recording 2026mttd on HOMEPC since")
    [helps] = w.send(SAM, "help")                                      # what Sam may use, and who does the rest
    helps = plain(helps)
    assert helps.startswith("Recording 2026mttd on HOMEPC since") and "• `stop`" in helps and "• `status`" in helps
    assert "set stream" not in helps and "• `settings`" not in helps and "add @name" not in helps
    assert "Admins (Ian H) change the stream, the uploads and the settings." in helps
    [no] = w.send(SAM, "teams 254")
    assert no.startswith("Only admins can change the running recording (the stream, the teams, the uploads): Ian H. "
                         "Ask them to do it, or to make you an admin (they reply add admin @")
    config.save_sections({"listen": {"admins": [SAM]}})               # an admin, but the old recording reads only Ian
    w.tick()
    [old] = w.send(SAM, "pause youtube")
    assert old.startswith("The recording running now started before an update, so it takes this command only from "
                          "Ian H")


def test_answers_everything_when_nothing_records(w):
    [helps] = w.send(IAN, "help")
    lines = plain(helps).splitlines()
    assert lines[0] == "Nothing is recording on HOMEPC. The event set up is MadTown Throwdown (2025mttd)."
    assert lines[1].startswith("• `split 2025mttd`: start recording MadTown Throwdown, the event set up")
    assert lines[-1].startswith("Replies come within about 20 s") and len(lines) <= 8
    [full] = w.send(IAN, "help all")
    full = plain(full)
    for group in ("*Recording*", "*The running recording (admins)*", "*Settings (admins)*",
                  "*People (adding, removing and the owner: admins)*", "*Help*"):
        assert group in full
    assert "`set stream <link>`" in full and "`add @name`" in full and "`pause drive`" in full
    [status] = w.send(IAN, "status")
    assert status.startswith("Recording: nothing is recording on HOMEPC.")
    assert "Slack listener: running on HOMEPC since Thu 20:00" in status
    # a recording's own commands while nothing records: saved for the next start
    assert w.send(IAN, "pause youtube")[0].startswith("OK: YouTube uploads stay paused when the next recording starts")
    assert config.section("youtube")["paused"] is True
    assert w.send(IAN, "resume youtube")[0] == "OK: YouTube uploads go out when the next recording starts."
    assert "paused" not in config.section("youtube")
    assert w.send(IAN, "teams 254, 1323")[0] == ("OK: matches of 254, 1323 go to #errata. It applies when the next "
                                                "recording starts.")
    assert config.section("slack")["teams"] == ["254", "1323"]
    [reply] = w.send(IAN, "set stream youtube.com/@Madtown_Throwdown")             # the settings form, the same
    assert reply == f"OK: the next recording records {CHANNEL_LIVE} (saved in config.toml)."
    assert config.section("event")["stream_url"] == CHANNEL_LIVE
    assert w.send(IAN, "stream auto")[0].startswith("OK: the next recording follows the link The Blue Alliance")
    assert "stream_url" not in config.section("event")
    [unknown] = w.send(IAN, "make coffee")
    assert unknown.startswith(commands.UNKNOWN + " What you can do now:") and "`split 2025mttd`" in unknown


# ---------------------------------------------------------------------------------------------------------------
# restarts of the listener itself
# ---------------------------------------------------------------------------------------------------------------

def test_no_message_is_handled_twice_after_a_restart(w):
    w.send(IAN, "split madtown")
    w.send(IAN, "list")
    n = len(w.slack.posts)
    w.clock.t += 30
    w.ls = w.make()                                                    # the listener restarted (a reboot, say)
    w.poll()
    assert len(w.slack.posts) == n                                     # nothing answered again
    [reply] = w.send(IAN, "yes")                                       # the question survived the restart
    assert reply.startswith("Starting MadTown Throwdown") and len(w.runner.started) == 1
    w.ls = w.make()
    w.poll()
    assert len(w.runner.started) == 1


def test_a_command_sent_while_it_was_restarting_is_handled_once(w):
    w.clock.t += 10
    w.slack.say(IAN, "list", w.clock.t)                               # sent while the listener was down
    w.clock.t += 60
    w.ls = w.make()
    w.poll()
    w.poll()
    [status] = w.slack.to(IAN)
    assert status.startswith("Recording: nothing is recording on HOMEPC.") and "reply split 2025mttd" in status


def test_after_a_long_stop_older_messages_get_one_note_and_the_event_is_kept(w):
    # user, 2026-11-09: after a restart, list showed no event until it was set up again (ac5acd2 kept the event)
    w.send(IAN, "split 2026mttd")
    w.runner.end("2026mttd", reason="stop requested (streamsplitter stop)")
    w.clock.t += 60
    w.slack.say(IAN, "pause youtube", w.clock.t)                      # the listener is down from here ...
    w.slack.say(SAM, "status", w.clock.t + 1)
    w.clock.t += 3600                                                  # ... for an hour
    w.ls = w.make()
    w.poll()
    [note] = w.slack.to(IAN)[-1:]
    assert note.startswith("StreamSplitter was not reading Slack when you sent a message (Thu 20:01), so it was not "
                           "done.")
    assert w.slack.to(SAM)[-1].startswith("StreamSplitter was not reading Slack when you sent a message")
    assert config.section("youtube").get("paused") is None            # not done
    [status] = w.send(IAN, "status")
    assert "MadTown Throwdown (2026mttd) was stopped on the computer; reply split 2026mttd to start it again." in status
    [helps] = w.send(SAM, "help")
    assert "The event set up is MadTown Throwdown (2026mttd)." in helps


def test_a_yes_older_than_its_question_does_nothing(w):
    w.send(IAN, "split madtown")
    asked = float(w.ls.state["pending"][IAN]["ts"])
    poller = w.ls.pollers[0]
    assert w.ls.handle(poller, {"ts": f"{asked - 1:.6f}", "user": IAN, "text": "yes"}) is None
    assert w.runner.started == [] and IAN in w.ls.state["pending"]


# ---------------------------------------------------------------------------------------------------------------
# keeping it going
# ---------------------------------------------------------------------------------------------------------------

def start_mttd(w):
    w.send(IAN, "split 2026mttd")
    w.send(IAN, "yes")
    assert len(w.runner.started) == 1


def test_a_crash_is_restarted_a_deliberate_stop_is_not(w):
    start_mttd(w)
    w.runner.crash("2026mttd")
    w.tick(after=10)                                                   # just started: left alone a minute
    assert len(w.runner.started) == 1
    w.tick(after=60)
    assert len(w.runner.started) == 2
    assert w.note.msgs[-1] == ("info", "Restarted the recording of MadTown Throwdown (2026mttd) on HOMEPC: it had "
                                       "stopped (its process has ended).")
    w.runner.end("2026mttd", "stop requested (streamsplitter stop)")   # `streamsplitter stop` or the run.bat menu
    w.tick(after=120)
    assert len(w.runner.started) == 2
    w.runner.running["2026mttd"] = {"started": w.clock.t}             # started again by hand
    w.runner.stop_files.add("2026mttd")                               # and asked to stop: not taken yet
    w.runner.running.pop("2026mttd")
    w.runner.ended["2026mttd"] = {"running": False, "reason": "no update for 3 min"}
    w.tick(after=120)
    assert len(w.runner.started) == 2


def test_at_most_3_restarts_an_hour(w):
    start_mttd(w)
    for i in range(3):
        w.runner.crash("2026mttd")
        w.tick(after=90)
    assert len(w.runner.started) == 4
    w.runner.crash("2026mttd")
    w.tick(after=90)
    assert len(w.runner.started) == 4 and w.ls.state["gave_up"]
    level, text = w.note.msgs[-1]
    assert level == "warn" and "It was restarted 3 times in the last hour, so the listener stops trying" in text
    assert "Reply split 2026mttd to start it again." in text
    w.tick(after=3600)
    assert len(w.runner.started) == 4
    assert "stopped 3 times within an hour" in w.send(IAN, "status")[0]
    start_mttd_again = w.send(IAN, "split 2026mttd") and w.send(IAN, "yes")
    assert start_mttd_again and len(w.runner.started) == 5 and not w.ls.state["gave_up"]


def test_no_restart_when_the_event_changed_or_another_records(w):
    start_mttd(w)
    w.runner.crash("2026mttd")
    w.runner.running["other"] = {"started": NOW}                      # recording something else by hand
    w.tick(after=90)
    assert len(w.runner.started) == 1
    w.runner.running.pop("other")
    config.save_sections({"event": {"key": "2026casj", "tag": "2026casj"}})     # the setup changed the event
    w.tick(after=90)
    assert len(w.runner.started) == 1 and w.ls.state["active"] is None
    assert "The event set up on HOMEPC is now 2026casj, so it was not restarted" in w.note.msgs[-1][1]


def test_after_a_reboot_the_active_event_is_restarted(w):
    start_mttd(w)
    w.runner.crash("2026mttd")
    w.runner.ended["2026mttd"]["reason"] = "no update for 14 min: it stopped without saying so"
    w.clock.t += 900
    w.ls = w.make()                                                    # logged in again: the listener starts
    w.ls.tick()
    assert len(w.runner.started) == 2 and "Restarted the recording" in w.note.msgs[-1][1]


def test_auto_stop_the_morning_after_the_last_day(w):
    assert listener.stop_time(MTTD) == dt.datetime(2026, 11, 9, 6, 0).timestamp()
    assert listener.stop_time({"start_date": "2026-11-06"}) == dt.datetime(2026, 11, 7, 6, 0).timestamp()
    assert listener.stop_time({}) is None
    start_mttd(w)
    w.runner.running["2026mttd"]["started"] = w.clock.t
    w.clock.t = dt.datetime(2026, 11, 9, 5, 59).timestamp()
    w.ls.tick()
    assert w.runner.stops == []
    w.clock.t = dt.datetime(2026, 11, 9, 6, 0).timestamp()
    w.ls.tick()
    assert w.runner.stops == ["2026mttd"] and w.ls.state["active"] is None
    assert w.runner.stopped_by == "the event ended"
    w.runner.clip_count["2026mttd"] = 98
    w.runner.end("2026mttd", waiting=2)
    w.tick(after=5)
    assert w.note.msgs[-1] == ("info", "Stopped recording MadTown Throwdown (2026mttd): the event ended (2026-11-08). "
                                       "98 clips made; 2 uploads still waiting (they go out the next time it "
                                       "records: split 2026mttd).")
    w.runner.crash("2026mttd")
    w.tick(after=600)
    assert len(w.runner.started) == 1                                  # auto-stopped: never restarted


# ---------------------------------------------------------------------------------------------------------------
# the recording's own poller, the alarm text, the files
# ---------------------------------------------------------------------------------------------------------------

def test_the_recording_answers_stop_itself_unless_a_listener_is_alive(env):
    """[listen] is on in this config, but being set up is not enough: only a listener holding its lock (in the
    background or in a window) takes split / stop / list / yes / no from the recording's own poller."""
    c = FakeClient()
    p = commands.CommandPoller(c, REC_CONTACT, Controls(), channel_name="errata", log=lambda s: None,
                               clock=lambda: 1730000000.0)             # the default: commands.listener_on()
    p.poll()

    def ask(t0):
        for i, text in enumerate(("stop", "split 2026mttd", "yes", "help")):
            c.say(f"{t0 + i}.000100", text)
        n = len(c.posts)
        p.poll()
        return [t for _, t, _ in c.posts[n:]]
    assert commands.listener_on() is False and commands.listener_protocol() == 0
    stop, split, yes, helps = ask(1730000010)                          # nobody listens: one answer each, no silence
    assert stop == commands.STOP_HOW and split == commands._esc(commands.SPLIT_NEEDS)
    assert yes == commands.NOTHING_TO_CONFIRM
    assert "Start from Slack is not running on" in helps and "• `stop`" not in helps
    lock = listener._Lock(listener.listen_folder() / listener.LOCK_FILE)
    assert lock.acquire()
    try:
        # a listener of an older version (its status names no protocol): the recording answers what it does not
        assert listener.is_running() is True and commands.listener_protocol() == 1
        [helps] = ask(1730000020)
        assert "• `stop`" in helps and "Start from Slack is not running" not in helps
        # this version's listener: it reads this DM, so the recording reads none of it
        listener._write_json(listener.listen_folder() / listener.STATUS_FILE, {"protocol": commands.PROTOCOL})
        assert commands.listener_protocol() == commands.PROTOCOL
        assert ask(1730000030) == []
    finally:
        lock.release()
    assert listener.is_running() is False and commands.listener_on() is False


def test_window_mode_closing_the_window_says_goodbye_and_the_recording_keeps_going(env, monkeypatch):
    config.save_sections({"listen": {"mode": "window"}})
    w = World()
    note = listener.ListenerNotifier(w.slack, IAN, host="HOMEPC", window=True, threaded=False, log=lambda s: None)
    w.ls.notifier, w.ls.window = note, True
    w.runner.running["2026mttd"] = {"started": NOW}                   # a recording split started earlier
    handlers, outcome = [], []
    monkeypatch.setattr(listener, "_register_console_handler", lambda h: handlers.append(h) or "registered")
    monkeypatch.setattr(listener, "_unregister_console_handler", lambda fn: None)
    monkeypatch.setattr(listener.signal, "signal", lambda *a: None)    # pytest keeps its own Ctrl+C handler
    closer = []

    def sleep(s):                                                      # Windows: the person closes the window
        if not closer:
            closer.append(threading.Thread(target=lambda: outcome.append(handlers[0](2))))
            closer[0].start()
            time.sleep(0.3)
    assert listener.serve(w.ls, note, listener.listen_folder(), window=True, alarm=False, awake=False,
                          sleep=sleep) == 0
    closer[0].join(5.0)
    assert outcome == [True]                                           # held the closing window for the clean exit
    assert [t for _, t in w.slack.dms_sent] == [
        "Listening for Slack commands while its window is open on HOMEPC: reply split &lt;event&gt; to start a "
        "recording (e.g. split 2026mttd), or help. Replies come within about 20 s. [StreamSplitter listener on "
        "HOMEPC]",
        "No longer listening for Slack commands on HOMEPC (its window was closed). A recording that runs keeps "
        "going."]
    assert w.runner.stops == [] and "2026mttd" in w.runner.running    # the recording does not depend on the window
    assert not listener.login_start_installed()
    assert listener.read_status()["stopped_reason"] == "its window was closed"
    assert handlers[0](0) is False                                     # Ctrl+C is left to Python's own handler


def test_window_mode_ctrl_c_says_goodbye(env):
    config.save_sections({"listen": {"mode": "window"}})
    w = World()
    note = listener.ListenerNotifier(w.slack, IAN, host="HOMEPC", window=True, threaded=False, log=lambda s: None)
    w.ls.notifier = note

    def sleep(s):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        listener.serve(w.ls, note, listener.listen_folder(), window=True, alarm=False, awake=False, sleep=sleep,
                       signals=False)
    assert w.slack.dms_sent[-1][1] == ("No longer listening for Slack commands on HOMEPC (Ctrl+C in its window). A "
                                       "recording that runs keeps going.")


@pytest.mark.parametrize("before, said", [
    ({"running": False, "stopped_reason": "stop requested (the setup, or streamsplitter listen stop)"}, True),
    ({"running": True}, False),                                       # killed: no stop DM was sent, its alarm speaks
    (None, False)])                                                   # the first run on this computer
def test_background_restart_says_running_again_after_a_stop_dm(env, before, said):
    # user, 2026-11-07: a restart for an update left "stopped ... until it runs again" as the last word
    w = World()
    note = listener.ListenerNotifier(w.slack, IAN, host="HOMEPC", threaded=False, log=lambda s: None,
                                     clock=lambda: NOW)
    w.ls.notifier = note
    folder = listener.listen_folder()
    if before is not None:
        listener._write_json(folder / listener.STATUS_FILE, before)

    def sleep(s):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        listener.serve(w.ls, note, folder, alarm=False, awake=False, sleep=sleep, signals=False)
    texts = [t for _, t in w.slack.dms_sent]
    again = ("The StreamSplitter listener on HOMEPC is running again (since Thu 20:00). Replies to your messages come "
             "within about 20 s.")
    assert texts[:-1] == ([again] if said else [])
    assert texts[-1].startswith("The StreamSplitter listener on HOMEPC stopped: stopped from the console (Ctrl+C).")


def test_window_mode_never_listens_in_the_background(env, monkeypatch, capsys):
    config.save_sections({"listen": {"mode": "window"}})
    assert listener.mode_of() == listener.WINDOW
    monkeypatch.setattr(listener, "start_detached", lambda: pytest.fail("started in the background"))
    assert listener.start() == 2                                       # e.g. a log-in shortcut left from before
    assert "only while its window is open" in capsys.readouterr().out
    config.save_sections({"listen": {"mode": None}})
    assert listener.mode_of() == listener.BACKGROUND


def test_listener_alarm_has_its_own_words(env):
    n = listener.ListenerNotifier(None, IAN, host="HOMEPC")
    t = dt.datetime(2026, 11, 6, 9, 30).timestamp()
    assert n.alarm_text(t) == ("The StreamSplitter listener on HOMEPC stopped at Fri 09:30. Slack can't start or "
                               "restart recordings until it runs again: log in to the computer, or double-click "
                               "run.bat (Mac/Linux: sh run.sh) and choose Start listening for Slack commands.")
    # a recording's alarm sweep (its own first words) never takes the listener's for its own, nor the other way
    from streamsplitter.publish.notify import Notifier
    rec = Notifier(None, IAN, host="HOMEPC", tag="2026mttd")
    assert not n.alarm_text(t).startswith(rec._alarm_marker()) and not rec.alarm_text(t).startswith(n._alarm_marker())
    assert rec.stopped_text("x") == "StreamSplitter on HOMEPC stopped: x."              # the recording's words stay


def test_status_and_stop_file(env):
    folder = listener.listen_folder()
    assert listener.read_status()["running"] is False
    path = listener.request_stop()
    assert path == folder / listener.STOP_FILE and path.is_file()
    assert listener.setup_problem() is None
    config.save_sections({"listen": {"enabled": False}})
    assert "Start from Slack is off" in listener.setup_problem()
    assert listener.allowed_users() == [IAN, SAM]


def test_login_start_install_and_remove_go_to_the_folder_given(env):
    base = env / "login"
    if os.name == "nt":
        path = listener.install_login_start("win32")
        assert path == base / "startup_folder" / listener.SHORTCUT_NAME and path.is_file()
        link = listener.read_shortcut(path)
        assert link["target"].lower().endswith("pythonw.exe")       # no console window: nothing anyone can close
        assert link["arguments"] == "-m streamsplitter.cli listen start"
        assert Path(link["workdir"]) == config.ROOT
        assert listener.login_start_installed("win32") and listener.remove_login_start("win32")
        assert not path.exists() and listener.remove_login_start("win32") is False
    path = listener.install_login_start("darwin", python="/Users/me/StreamSplitter/.venv/bin/python")
    data = plistlib.loads(path.read_bytes())
    assert path.parent == base / "launch_agents_folder" and data["Label"] == listener.AGENT_LABEL
    assert data["ProgramArguments"] == ["/Users/me/StreamSplitter/.venv/bin/python", "-m", "streamsplitter.cli",
                                        "listen", "run"]
    assert data["RunAtLoad"] is True and data["StandardOutPath"].endswith("listener.log")
    assert listener.remove_login_start("darwin") and not path.exists()
    path = listener.install_login_start("linux", python="/home/a b/StreamSplitter/.venv/bin/python")
    text = path.read_text()
    assert path.parent == base / "autostart_folder" and path.name == listener.AUTOSTART_NAME
    assert 'Exec="/home/a b/StreamSplitter/.venv/bin/python" -m streamsplitter.cli listen start' in text
    assert "Terminal=false" in text
    assert listener.remove_login_start("linux") and not path.exists()
