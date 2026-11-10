"""Slack commands (publish/commands.py): parsing what the contact types (Slack's link wrapping included), the help
text, and the poller that reads the contact's DM with the app, with a fake Slack client. No network.

    .venv\\Scripts\\python.exe -m pytest -q tests\\test_commands.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from streamsplitter import streams  # noqa: E402
from streamsplitter.publish import commands  # noqa: E402
from streamsplitter.publish.base import PermanentError, TransientError  # noqa: E402

CONTACT = "U0CONTACT1"


# ---------------------------------------------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, want", [
    # Slack wraps links it recognises; with a label, and without; & arrives as &amp;
    ("stream <https://www.youtube.com/watch?v=dQw4w9WgXcQ|youtube.com/watch?v=dQw4w9WgXcQ>",
     "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ("Stream <https://www.youtube.com/watch?v=dQw4w9WgXcQ&amp;t=30s>", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ("STREAM <http://youtube.com/@FIRSTinspires|youtube.com/@FIRSTinspires>",
     "https://www.youtube.com/@FIRSTinspires/live"),
    ("stream youtube.com/@FIRSTinspires/streams", "https://www.youtube.com/@FIRSTinspires/live"),
    ("stream https://youtu.be/dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ"),
    ("stream https://www.youtube.com/live/dQw4w9WgXcQ?si=abc", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ("stream <https://www.twitch.tv/firstinspires/videos>", "https://www.twitch.tv/firstinspires"),
    ("stream twitch.tv/FRC_Events", "https://www.twitch.tv/FRC_Events"),
])
def test_stream_link_is_unwrapped_and_normalized(text, want):
    cmd = commands.parse(text)
    assert cmd.name == "stream" and cmd.error is None and cmd.arg == want


@pytest.mark.parametrize("text", ["stream <https://example.com/live|example.com/live>", "stream not a link",
                                  "stream https://www.youtube.com/watch?v=short", "stream javascript:alert(1)",
                                  "stream https://user:pw@www.youtube.com/@x/live"])
def test_stream_rejects_what_is_not_a_youtube_or_twitch_link(text):
    cmd = commands.parse(text)
    assert cmd.name == "stream" and cmd.arg is None and "YouTube or Twitch" in cmd.error


def test_parse_the_other_commands():
    assert commands.parse("HELP").name == "help" and commands.parse("  status ").name == "status"
    assert commands.parse("stream").name == "stream" and commands.parse("stream").arg is None
    assert commands.parse("Stream Auto").name == "stream_auto"
    assert commands.parse("teams 5026, 254").arg == ["5026", "254"]
    assert commands.parse("Teams frc5026 254b and 1323").arg == ["5026", "254B", "1323"]
    assert commands.parse("teams none").arg == []
    assert commands.parse("teams").arg is None and commands.parse("teams").error is None
    assert "team numbers" in commands.parse("teams the blue ones").error
    assert (commands.parse("Pause YouTube").name, commands.parse("pause youtube").arg) == ("pause", "youtube")
    assert (commands.parse("resume youtube").name, commands.parse("resume yt").arg) == ("resume", "youtube")
    assert "Only YouTube" in commands.parse("pause slack").error
    assert commands.parse("stop").name == "stop"
    assert commands.parse("thanks!").name == "ack" and commands.parse("").name == "unknown"
    assert commands.parse("make coffee").name == "unknown"


class Controls:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def f(*a):
            self.calls.append((name, *a))
            return f"did {name}"
        return f


def test_dispatch_and_replies():
    c = Controls()
    assert commands.dispatch(commands.parse("stream https://twitch.tv/x"), c) == "did set_stream"
    assert c.calls[-1] == ("set_stream", "https://www.twitch.tv/x")
    commands.dispatch(commands.parse("teams 5026"), c)
    assert c.calls[-1] == ("set_teams", ["5026"])
    commands.dispatch(commands.parse("pause youtube"), c)
    assert c.calls[-1] == ("pause", "youtube")
    assert commands.dispatch(commands.parse("stream"), c).startswith("did stream_text\nTo record another link")
    assert commands.dispatch(commands.parse("what is going on"), c) == commands.UNKNOWN
    stop = commands.dispatch(commands.parse("stop"), c)
    assert "cannot be stopped from Slack" in stop and "streamsplitter stop" in stop
    assert "stream <link>" in commands.dispatch(commands.parse("help"), c, "errata")


def test_help_text_has_the_exact_commands():
    plain = commands.help_text("errata", slack=False)
    for cmd in ("help", "status", "stream <link>", "stream auto", "teams <numbers>", "pause youtube",
                "resume youtube", "teams 5026, 254", "#errata"):
        assert cmd in plain
    mrkdwn = commands.help_text("errata")
    assert "`set stream &lt;link&gt;`" in mrkdwn and "<link>" not in mrkdwn and "`set teams 5026, 254`" in mrkdwn
    assert "the team's Slack channel" in commands.help_text()
    assert "`set stream auto`" in commands.short_help()


def test_normalize_stream_url_without_slack():
    assert streams.normalize_stream_url("www.youtube.com/channel/UCabcdefghijklmnopqrstuv") == \
        "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv/live"
    assert streams.normalize_stream_url("https://m.youtube.com/watch?v=dQw4w9WgXcQ&feature=share") == \
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert streams.normalize_stream_url("https://vimeo.com/123") is None
    assert streams.normalize_stream_url("https://www.youtube.com/results?search_query=frc") is None


# ---------------------------------------------------------------------------------------------------------------
# the poller
# ---------------------------------------------------------------------------------------------------------------

class FakeClient:
    """The SlackClient calls CommandPoller makes. `messages`: the DM, newest first (as conversations.history);
    `threads`: parent ts -> replies."""

    def __init__(self, scopes=("chat:write", "im:write", "im:history")):
        self.scopes = list(scopes)
        self.messages: list[dict] = []
        self.threads: dict[str, list[dict]] = {}
        self.posts: list[tuple[str, str, str | None]] = []
        self.history_error: Exception | None = None
        self.calls: list[str] = []

    def auth_test(self):
        self.calls.append("auth_test")
        return {"scopes": list(self.scopes),
                "missing_scopes": [s for s in ("chat:write", "im:write", "im:history") if s not in self.scopes]}

    def open_dm(self, user):
        assert user == CONTACT
        return "D0CONTACT1"

    def history(self, channel, limit=15, oldest=None):
        self.calls.append("history")
        assert channel == "D0CONTACT1" and limit <= 15
        if self.history_error is not None:
            raise self.history_error
        return [dict(m) for m in self.messages[:limit]]

    def replies(self, channel, ts, oldest=None, limit=15):
        self.calls.append("replies")
        parent = next(m for m in self.messages if m["ts"] == ts)
        return [dict(parent)] + [dict(r) for r in self.threads.get(ts, []) if float(r["ts"]) > float(oldest or 0)]

    def post_message(self, channel, text, thread_ts=None):
        self.posts.append((channel, text, thread_ts))
        return {"ts": "9.9", "channel": channel}

    def say(self, ts, text, user=CONTACT, **kw):
        self.messages.insert(0, {"ts": ts, "user": user, "text": text, **kw})


class Note:
    def __init__(self):
        self.msgs = []

    def info(self, text, key=None, dm=True):
        self.msgs.append(("info", text, key))
        return True

    def warn(self, text, key=None, dm=True):
        self.msgs.append(("warn", text, key))
        return True


def poller(client, tmp_path=None, controls=None, note=None, clock=lambda: 1730000000.0, listen=lambda: False):
    """`listen`: whether the Slack listener (listener.py) runs on this computer; off unless a test says so."""
    return commands.CommandPoller(client, CONTACT, controls or Controls(), notifier=note or Note(),
                                  channel_name="errata", log=lambda s: None, clock=clock,
                                  state_path=(tmp_path / "cmd.json") if tmp_path else None, listen=listen)


def test_poller_answers_only_the_contacts_new_messages(tmp_path):
    c = FakeClient()
    c.say("1730000000.000100", "status")                      # before StreamSplitter started: not a command
    ctl = Controls()
    p = poller(c, tmp_path, ctl)
    assert p.poll() == commands.POLL_S and c.posts == [] and ctl.calls == []
    c.say("1730000010.000100", "status")
    c.say("1730000011.000100", "stream auto", user="U0SOMEONE9")          # someone else: ignored
    c.say("1730000012.000100", "Stream started: ...", user=None, bot_id="B0BOT")   # the app's own post
    c.say("1730000013.000100", "status", subtype="message_changed")      # an edit
    c.say("1730000014.000100", "pause youtube")
    p.poll()
    assert [x[0] for x in ctl.calls] == ["status_text", "pause"]
    assert [t.split("\n")[0] for _, t, th in c.posts] == ["did status_text", "did pause"]
    assert all(th is None for *_, th in c.posts)
    assert "Slack listener: not running on" in c.posts[0][1]   # status says plainly that no listener runs
    p.poll()                                                  # nothing new: nothing done twice
    assert len(ctl.calls) == 2
    c.say("1730000020.000100", "make coffee")
    p.poll()
    unknown = c.posts[-1][1]
    assert unknown.startswith(commands.UNKNOWN) and "coffee" not in unknown        # nothing of what was typed
    assert "`status`" in unknown and "help all" in unknown                          # what can be done now
    assert json.loads((tmp_path / "cmd.json").read_text())["last_ts"] == "1730000020.000100"


def test_a_reply_in_a_thread_is_answered_in_that_thread(tmp_path):
    c = FakeClient()
    c.say("1730000001.000100", "Waiting for the stream to start ...", user=None, bot_id="B0BOT")
    p = poller(c, tmp_path)
    p.poll()
    c.messages[0].update(reply_count=1, latest_reply="1730000005.000100")
    c.threads["1730000001.000100"] = [{"ts": "1730000005.000100", "user": CONTACT, "text": "help",
                                       "thread_ts": "1730000001.000100"}]
    p.poll()
    assert c.posts[-1][2] == "1730000001.000100" and "stream &lt;link&gt;" in c.posts[-1][1]
    n = len(c.posts)
    p.poll()                                                  # the thread is not read again for nothing
    assert len(c.posts) == n and c.calls.count("replies") == 1


def test_a_restart_handles_commands_sent_just_before_it(tmp_path):
    c = FakeClient()
    (tmp_path / "cmd.json").write_text(json.dumps({"last_ts": "1730000000.000100"}))
    c.say("1729990000.000100", "status")                      # long before the restart: ignored
    c.say("1730000005.000100", "status")                      # while it was restarting: handled
    ctl = Controls()
    p = poller(c, tmp_path, ctl, clock=lambda: 1730000010.0)
    p.poll()
    assert ctl.calls == [("status_text",)]


def test_missing_im_history_dms_the_fix_once_and_the_rest_works(tmp_path):
    c, note = FakeClient(scopes=("chat:write", "im:write")), Note()
    p = poller(c, tmp_path, note=note)
    assert p.poll() == commands.SCOPE_RETRY_S
    assert "history" not in c.calls                         # auth.test said so: the DM is not even tried
    assert note.msgs == [("warn", commands.SCOPE_FIX, "slack_im_history")]
    assert "Add im:history > Reinstall to Workspace" in commands.SCOPE_FIX
    c.history_error = PermanentError("lacks im:history")
    c.history_error.code = "missing_scope"
    assert p.poll() == commands.SCOPE_RETRY_S and len(note.msgs) == 1        # said once
    c.history_error = None                                    # the person added it and reinstalled
    c.say("1730000050.000100", "status")
    p.poll()
    assert note.msgs[-1] == ("info", commands.SCOPE_OK, None) and p.scope_ok is True


def test_slack_unreachable_waits_and_tries_again(tmp_path):
    c = FakeClient()
    p = poller(c, tmp_path)
    c.history_error = TransientError("no connection", retry_after=120)
    assert p.poll() == 120
    c.history_error = TransientError("no connection")
    assert p.poll() == commands.ERROR_WAIT_S


def test_a_command_that_raises_is_answered_without_secrets(tmp_path):
    class Boom(Controls):
        def status_text(self):
            raise RuntimeError("token xoxb-123-456-secret refused")
    c = FakeClient()
    p = poller(c, tmp_path, Boom())
    p.poll()
    c.say("1730000010.000100", "status")
    p.poll()
    assert "That did not work" in c.posts[-1][1] and "xoxb-123" not in c.posts[-1][1]


# ---------------------------------------------------------------------------------------------------------------
# what Slack delivers: formatting, other words, near misses (user, 2026-11-09: `set youtube privacy public` pasted in
# code formatting was not understood, and "I did not understand that" was a dead end)
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("typed, cleaned", [
    ("`set youtube privacy public`", "set youtube privacy public"),
    ("```pause youtube```", "pause youtube"),
    ("```\nstatus\n```", "status"),
    ("*pause youtube*", "pause youtube"),
    ("_status_", "status"),
    ("~stop~", "stop"),
    ("set youtube privacy *public*", "set youtube privacy public"),
    ("Pause YouTube.", "Pause YouTube"),
    ("status?", "status"),
    ("resume youtube!!", "resume youtube"),
    ("/status", "status"),
    ("!help", "help"),
    ("<@U0BOT1234|streamsplitter> status", "status"),
    ("&gt; status", "status"),
    ("“split 2026mttd”", "split 2026mttd"),
    ("​status ", "status"),
    ("?", "help"),
    ("teams 5026, 254.", "teams 5026, 254"),
    ("stream <https://www.twitch.tv/x|twitch.tv/x>.", "stream <https://www.twitch.tv/x|twitch.tv/x>"),
    ("*stream <https://www.twitch.tv/x|twitch.tv/x>*", "stream <https://www.twitch.tv/x|twitch.tv/x>"),
    ("add <@U0BOB|bob>", "add <@U0BOB|bob>"),
    # a free-text value is kept as typed: its formatting, quotes and punctuation are the person's
    ("set youtube title *{match_title}* | “{event_name}”!", "set youtube title *{match_title}* | "
                                                                     "“{event_name}”!"),
    ("`set youtube description Line 1\nLine 2`", "set youtube description Line 1\nLine 2"),
    ("set drive names {code}_{match_title}", "set drive names {code}_{match_title}"),
])
def test_clean_drops_slacks_formatting(typed, cleaned):
    assert commands.clean(typed) == cleaned


@pytest.mark.parametrize("typed, name, arg, confirm", [
    ("`set youtube privacy public`", "admin", None, False),
    ("start 2026mttd", "split", "2026mttd", False),
    ("Record madtown", "split", "madtown", False),
    ("split 2026mttd yes", "split", "2026mttd", True),
    ("*split 2026MTTD now*", "split", "2026MTTD", True),
    ("start recording", "split", "", False),
    ("list", "status", None, False),
    ("info", "status", None, False),
    ("ok", "yes", "ok", False),
    ("Yes!", "yes", "yes", False),
    ("yes please", "yes", "yes", False),
    ("sure", "yes", "sure", False),
    ("cancel", "no", "cancel", False),
    ("nope", "no", "nope", False),
    ("thank you", "ack", None, False),
    ("got it", "ack", None, False),
    ("stop yes", "stop", None, True),
    ("stop 2026mttd", "stop", "2026mttd", False),
    ("stop recording", "stop", None, False),
    ("stop listener", "admin", None, False),
    ("set stream auto", "stream_auto", None, False),
    ("stream TBA", "stream_auto", None, False),
    ("set teams 254", "teams", ["254"], False),
    ("set event 2026mttd yes", "set_event", "2026mttd", True),
    ("people", "admin", None, False),
    ("admins", "admin", None, False),
    ("hi", "help", None, False),
    ("hey there", "help", None, False),
    ("help me", "help", None, False),
    ("help all", "help", "all", False),
    ("3", "number", 3, False),
])
def test_parse_the_words_a_person_would_guess(typed, name, arg, confirm):
    cmd = commands.parse(typed)
    assert (cmd.name, cmd.arg, cmd.confirm) == (name, arg, confirm)


def test_a_misspelt_destination_says_what_it_meant():
    assert commands.parse("pause youtub").error == "Did you mean `pause youtube`?"
    assert commands.parse("resume drvie").error == "Did you mean `resume drive`?"


CTX = commands.HelpContext(host="HOMEPC", recording="MadTown Throwdown (2026mttd)", since=1730000000.0)


@pytest.mark.parametrize("typed, said", [
    ("puase youtube", "Did you mean `pause youtube`?"),
    ("staus", "Did you mean `status`?"),
    ("hlep", "Did you mean `help`?"),
    ("spilt 2026mttd", "Did you mean `split 2026mttd`?"),
    ("TBA", "Did you mean `set stream auto` (record the stream link The Blue Alliance lists)?"),
    ("https://www.thebluealliance.com/event/2026mttd", "Did you mean `split 2026mttd` (start recording that event)?"),
    ("youtube.com/@Madtown_Throwdown", "Did you mean `set stream https://www.youtube.com/@Madtown_Throwdown/live` "
                                       "(record that link)?"),
])
def test_a_near_miss_says_what_it_probably_meant(typed, said):
    assert commands.unknown_reply(typed, CTX) == f"{commands.UNKNOWN} {said}"


def test_no_near_miss_lists_the_few_commands_for_now():
    reply = commands.unknown_reply("make coffee", CTX)
    lines = reply.splitlines()
    assert lines[0] == "I did not understand that. What you can do now:"
    assert lines[1].startswith("• `status`") and "• `pause youtube` / `resume youtube`" in reply
    assert lines[-1] == "`help all` lists every command." and len(lines) <= 6 and "coffee" not in reply
    member = commands.HelpContext(host="HOMEPC", admin=False, recording="MadTown Throwdown (2026mttd)")
    assert "Did you mean" not in commands.unknown_reply("TBA", member)            # an admin's command: not offered
    assert "pause youtube" not in commands.unknown_reply("TBA", member)


# ---------------------------------------------------------------------------------------------------------------
# help: a short first screen for the state; help all for the rest
# ---------------------------------------------------------------------------------------------------------------

def test_first_help_is_short_and_fits_the_state():
    rec = commands.first_help(commands.HelpContext(host="HOMEPC", recording="MadTown Throwdown (2026mttd)",
                                                   since=1730000000.0, drive=True, react=True))
    lines = rec.splitlines()
    assert lines[0].startswith("Recording MadTown Throwdown (2026mttd) on HOMEPC since ")
    assert [ln.split(":")[0] for ln in lines[1:5]] == ["• `status`", "• `pause youtube` / `resume youtube`",
                                                       "• `set stream <link>`", "• `stop`"]
    assert "pause drive works the same" in rec and len(lines) <= 8
    assert lines[-2] == "`help all` lists every command; `help <command>` explains one, e.g. `help split`."
    assert lines[-1] == ("Replies come within about 20 s, and within a few seconds while you keep writing. :eyes: on "
                         "your message means it was read.")
    member = commands.first_help(commands.HelpContext(host="HOMEPC", admin=False, admins=("Ian H", "Jamie Lee"),
                                                      recording="MadTown Throwdown (2026mttd)", every_s=36))
    assert "pause youtube" not in member and "• `stop`" in member and "• `people`" in member
    assert "Admins (Ian H and Jamie Lee) change the stream, the uploads and the settings." in member
    assert "Replies come within about 36 s" in member and ":eyes:" not in member
    idle = commands.first_help(commands.HelpContext(host="HOMEPC", event="2026mttd", event_name="MadTown Throwdown"))
    assert idle.splitlines()[0] == "Nothing is recording on HOMEPC. The event set up is MadTown Throwdown (2026mttd)."
    assert idle.splitlines()[1].startswith("• `split 2026mttd`: start recording MadTown Throwdown, the event set up")
    solo = commands.first_help(commands.HelpContext(host="HOMEPC", listener=False, recording="X (2026mttd)"))
    assert "Start from Slack is not running on HOMEPC, so recordings cannot be started or stopped" in solo
    assert "• `stop`" not in solo and "• `settings`" in solo


def test_help_all_lists_every_command_once_verb_first():
    full = commands.help_text("errata", drive=True, wiki=True, listen=True, escape=False)
    for c in ("split <event>", "stop", "status", "yes", "set stream <link>", "set stream auto", "set teams <numbers>",
              "pause youtube", "resume youtube", "pause drive", "resume drive", "pause wiki", "resume wiki",
              "settings", "set <area> <name> <value>", "people", "add @name", "add admin @name", "remove @name",
              "set owner @name", "help", "help all", "help <command>"):
        assert f"`{c}`" in full, c
    assert "`stream <link>`" not in full and "`teams <numbers>`" not in full            # the shortcuts still work
    assert all(line.count("`") % 2 == 0 and "(admins)`" not in line for line in full.splitlines())   # whole spans
    member = commands.help_text("errata", listen=True, owner=False, owner_name="Ian H and Jamie Lee", escape=False)
    assert "`set stream <link>`" not in member and "`add @name`" not in member and "`people`" in member
    assert "Admins (Ian H and Jamie Lee) change the stream" in member


# ---------------------------------------------------------------------------------------------------------------
# who reads: alone, beside an older listener, or not at all beside this version's listener
# ---------------------------------------------------------------------------------------------------------------

def test_beside_this_versions_listener_the_recording_reads_nothing(tmp_path):
    c = FakeClient()
    p = poller(c, tmp_path, listen=lambda: commands.PROTOCOL)
    c.say("1730000010.000100", "status")
    assert p.poll() == commands.POLL_S and p.mode() == commands.OFF
    assert "history" not in c.calls and c.posts == []


def test_beside_an_older_listener_it_answers_exactly_what_that_listener_does_not(tmp_path):
    c = FakeClient()
    ctl = Controls()
    p = poller(c, tmp_path, ctl, listen=lambda: 1)
    p.poll()
    for i, text in enumerate(("split 2026mttd", "list", "yes", "stop", "people", "set event 2026mttd", "3",
                              "status", "help", "pause youtube", "make coffee", "start 2026mttd")):
        c.say(f"{1730000010 + i}.000100", text)
        n = len(c.posts)
        p.poll()
        answered = len(c.posts) - n
        assert answered == (0 if commands.legacy_listener_command(text) else 1), text
    assert ("pause", "youtube") in ctl.calls
    assert commands.legacy_listener_command("split 2026mttd") and not commands.legacy_listener_command("start x")


def test_taking_over_from_the_listener_starts_after_what_it_answered(tmp_path):
    c = FakeClient()
    shared = tmp_path / "listen" / f"commands_{CONTACT}.json"
    proto = [commands.PROTOCOL]
    ctl = Controls()
    (tmp_path / "own.json").write_text(json.dumps({"last_ts": "1730000000.000100"}))
    p = commands.CommandPoller(c, CONTACT, ctl, notifier=Note(), log=lambda s: None, clock=lambda: 1730000100.0,
                               state_path=tmp_path / "own.json", shared_path=shared, listen=lambda: proto[0])
    p.poll()                                                  # the listener reads: nothing here
    c.say("1730000050.000100", "pause youtube")               # ... and answered this one
    shared.parent.mkdir(parents=True)
    shared.write_text(json.dumps({"last_ts": "1730000050.000100"}))
    proto[0] = 0                                              # the listener stopped
    c.say("1730000060.000100", "status")
    p.poll()
    assert ctl.calls == [("status_text",)]                   # not the pause again
    assert json.loads(shared.read_text())["last_ts"] == "1730000060.000100"         # kept for the listener
    assert len(c.posts) == 1                                  # and no "not done" note for what the listener did


def test_a_recording_that_started_beside_the_listener_reports_nothing_as_missed(tmp_path):
    c = FakeClient()
    shared = tmp_path / "listen" / f"commands_{CONTACT}.json"
    (tmp_path / "own.json").write_text(json.dumps({"last_ts": "1729000000.000100"}))     # yesterday's run
    proto = [commands.PROTOCOL]
    p = commands.CommandPoller(c, CONTACT, Controls(), notifier=Note(), log=lambda s: None,
                               clock=lambda: 1730000100.0, state_path=tmp_path / "own.json", shared_path=shared,
                               listen=lambda: proto[0])
    p.poll()
    c.say("1729500000.000100", "status")                      # the listener answered these ...
    c.say("1730000050.000100", "help")
    shared.parent.mkdir(parents=True)
    shared.write_text(json.dumps({"last_ts": "1730000050.000100"}))
    proto[0] = 0                                              # ... then stopped
    p.poll()
    assert c.posts == []


def test_hot_after_a_message_then_back_to_normal(tmp_path):
    t = [1730000000.0]
    c = FakeClient()
    p = poller(c, tmp_path, clock=lambda: t[0])
    assert p.poll() == commands.POLL_S
    t[0] += 10
    c.say(f"{t[0]:.6f}", "status")
    assert p.poll() == commands.FAST_S                        # the person is writing: read again soon
    t[0] += commands.HOT_S + 1
    assert p.poll() == commands.POLL_S


class ReactingClient(FakeClient):
    def __init__(self, scopes=("chat:write", "im:write", "im:history", "reactions:write"), refuse=None):
        super().__init__(scopes)
        self.reactions: list[tuple[str, str, str]] = []
        self.refuse = refuse

    def add_reaction(self, channel, ts, name):
        if self.refuse:
            e = PermanentError("Slack refused")
            e.code = self.refuse
            raise e
        self.reactions.append((channel, ts, name))


def test_a_message_read_gets_eyes_and_thanks_gets_a_thumb(tmp_path):
    c = ReactingClient()
    p = poller(c, tmp_path)
    p.poll()
    c.say("1730000010.000100", "status")
    c.say("1730000011.000100", "thanks!")
    p.poll()
    assert c.reactions == [("D0CONTACT1", "1730000010.000100", "eyes"), ("D0CONTACT1", "1730000011.000100", "+1")]
    assert len(c.posts) == 1                                  # the thanks: the thumb, no message
    assert "eyes" in commands.first_help(p.help_context())


def test_without_reactions_write_it_just_answers(tmp_path):
    c = ReactingClient(scopes=("chat:write", "im:write", "im:history"))
    p = poller(c, tmp_path)
    p.poll()
    c.say("1730000010.000100", "status")
    c.say("1730000011.000100", "thanks")
    p.poll()
    assert c.reactions == [] and p.can_react is False         # auth.test said so: not even tried
    assert [t for _, t, _ in c.posts][1] == ":+1:"            # the thanks still gets an answer
    c2 = ReactingClient(refuse="missing_scope")
    c2.scopes = None                                          # Slack did not say which scopes the token has
    c2.auth_test = lambda: {}
    p2 = poller(c2, tmp_path / "2")
    p2.poll()
    c2.say("1730000010.000100", "status")
    c2.say("1730000011.000100", "help")
    p2.poll()
    assert p2.can_react is False and len(c2.posts) == 2       # tried once, then answers only


def test_messages_from_before_a_long_gap_get_one_note(tmp_path):
    c = FakeClient()
    (tmp_path / "cmd.json").write_text(json.dumps({"last_ts": "1729990000.000100"}))
    c.say("1729995000.000100", "split 2026mttd")              # sent while nothing read Slack, long ago
    c.say("1729996000.000100", "status")
    c.say("1730000005.000100", "status")                      # within the restart grace: done
    ctl = Controls()
    p = poller(c, tmp_path, ctl, clock=lambda: 1730000010.0)
    p.poll()
    assert ctl.calls == [("status_text",)]
    note = c.posts[0][1]
    assert note.startswith("StreamSplitter was not reading Slack when you sent 2 messages (") and \
        note.endswith("so they were not done. Send again what you still want.")
    assert "split" not in note and len(c.posts) == 2          # one note, nothing typed repeated
    p.poll()
    assert len(c.posts) == 2


# ---------------------------------------------------------------------------------------------------------------
# the recording's mailbox (the listener's commands for the running recording)
# ---------------------------------------------------------------------------------------------------------------

def test_the_mailbox_does_a_request_and_hands_the_reply_back(tmp_path):
    import threading
    lock = threading.Lock()                                  # live.py's Watcher holds it for EventWatch.tick()

    class Locked(Controls):
        def pause(self, dest):
            assert lock.locked(), "a Slack command ran beside EventWatch"
            return super().__getattr__("pause")(dest)
    ctl = Locked()
    box = commands.Mailbox(tmp_path / "live", ctl, channel_name="errata", replies=tmp_path / "replies",
                           log=lambda s: None, lock=lock)
    box.advertise()
    ad = commands.advertised(tmp_path / "live")
    assert ad["protocol"] == commands.PROTOCOL and ad["pid"] > 0
    req = {"id": "1730000010.000100-U0IAN", "user": "U0IAN", "text": "`pause youtube`", "channel": "DU0IAN",
           "thread": None, "append": "Slack listener: running"}
    (box.inbox / f"{req['id']}.json").write_text(json.dumps(req))
    gone = dict(req, id="1730000011.000100-U0IAN", text="resume youtube")
    (box.inbox / f"{gone['id']}.json").write_text(json.dumps(gone))
    assert commands.claim(box.inbox / f"{gone['id']}.json", ".expired") is not None    # the listener gave up first
    assert box.process() == 1
    assert ctl.calls == [("pause", "youtube")]
    rep = json.loads((tmp_path / "replies" / f"{req['id']}.json").read_text())
    assert rep == {"id": req["id"], "user": "U0IAN", "channel": "DU0IAN", "thread": None,
                   "text": "did pause\nSlack listener: running"}
    assert not list(box.inbox.glob("*.json")) and not list(box.inbox.glob("*.taken"))
    box.withdraw()
    assert commands.advertised(tmp_path / "live") is None
