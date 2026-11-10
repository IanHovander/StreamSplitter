"""Settings and people from Slack (streamsplitter/slackadmin.py, and how listener.py and publish/commands.py route
them): roles, adding and removing people with nobody messaged unasked, every setting changed from Slack with its
check, the questions before risky changes, what stays on the computer, tokens typed by mistake, help, exactly one
answer per message with a listener and a recording both reading the owner's DM, and the polling budget. Fake Slack,
fake YouTube, fake recording runner (test_listener.py's); config.toml and secrets in the test's own folder.

    .venv\\Scripts\\python.exe -m pytest -q -p no:cacheprovider tests\\test_slackadmin.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from streamsplitter import config, listener, slackadmin  # noqa: E402
from streamsplitter.publish import commands, slack, tbalink  # noqa: E402
from streamsplitter.publish.base import TransientError  # noqa: E402
from test_commands import Controls  # noqa: E402
from test_listener import (IAN, SAM, Clock, FakeRunner, FakeSlack, FakeYouTube, Note, World, env,  # noqa: E402,F401
                           plain)

BOB, BOBBY, EVE = "U0BOB", "U0BOBBY", "U0EVE"
MEMBERS = [{"id": IAN, "name": "ian", "real_name": "Ian H", "display_name": None, "email": "ian@example.com"},
           {"id": SAM, "name": "sam", "real_name": "Sam K", "display_name": None, "email": "sam@example.com"},
           {"id": BOB, "name": "bob", "real_name": "Bob Builder", "display_name": None, "email": "bob@example.com"},
           {"id": BOBBY, "name": "bobby", "real_name": "Bobby Tables", "display_name": None,
            "email": "bobby@example.com"},
           {"id": EVE, "name": "eve", "real_name": "Eve N", "display_name": None, "email": "eve@example.com"}]
CHANNELS = {"errata": {"id": "C0ERRATA", "name": "errata", "is_private": False, "is_member": True},
            "new-videos": {"id": "C0NEW", "name": "new-videos", "is_private": False, "is_member": False},
            "secret-room": {"id": "G0SECRET", "name": "secret-room", "is_private": True, "is_member": False}}


class PeopleSlack(FakeSlack):
    """FakeSlack plus the people and channel lookups (users:read, users:read.email, conversations.*)."""

    def __init__(self, clock):
        super().__init__(clock)
        self.joined: list[str] = []
        self.history_calls: dict[str, int] = {}
        self.limit_once: set[str] = set()

    def find_users(self, query):
        q = query.strip().lstrip("@")
        if q[:2] in ("U0", "W0"):
            return [dict(m) for m in MEMBERS if m["id"] == q]
        if "@" in q:
            return [dict(m) for m in MEMBERS if m["email"] == q.lower()]
        return [dict(m) for m in MEMBERS if q.casefold() in f"{m['name']} {m['real_name']}".casefold()]

    def find_channel(self, name):
        name = name.lstrip("#")
        return next((dict(c) for c in CHANNELS.values() if name in (c["id"], c["name"])), None)

    def join_channel(self, channel_id):
        self.joined.append(channel_id)

    def history(self, channel, limit=15, oldest=None):
        self.history_calls[channel] = self.history_calls.get(channel, 0) + 1
        if channel in self.limit_once:
            self.limit_once.discard(channel)
            e = TransientError("Slack asked StreamSplitter to slow down", retry_after=120)
            e.code = "ratelimited"
            raise e
        return super().history(channel, limit, oldest)


class RecordingYouTube(FakeYouTube):
    def __init__(self):
        super().__init__()
        self.privacy: list[tuple[str, str]] = []

    def set_playlist_privacy(self, playlist_id, privacy):
        self.privacy.append((playlist_id, privacy))
        return {"id": playlist_id, "privacy": privacy}


class AdminWorld(World):
    def __init__(self):
        self.clock = Clock()
        self.slack = PeopleSlack(self.clock)
        self.runner = FakeRunner(self.clock)
        self.note = Note()
        self.yt = RecordingYouTube()
        self.controls = Controls()
        self.mailboxes = {}
        self.ls = self.make()
        self.ls.admin.tba_check = lambda auth_id, secret, key: None       # never The Blue Alliance itself
        self.poll()


@pytest.fixture
def w(env):
    return AdminWorld()


def dms(w, user):
    """What `user` got without writing first: the app's messages to them outside a reply (FakeSlack.dm)."""
    return [t for u, t in w.slack.dms_sent if u == user]


def one(w, user, text, after=5.0) -> str:
    replies = w.send(user, text, after)
    assert len(replies) == 1, replies
    return plain(replies[0])


# ---------------------------------------------------------------------------------------------------------------
# people
# ---------------------------------------------------------------------------------------------------------------

def test_add_by_mention_sends_nothing_until_yes(w):
    w.slack.say(BOB, "split 2026mttd", w.clock.t + 1)                   # before Bob may use it: never answered
    reply = one(w, IAN, "add <@U0BOB|bob>")
    assert reply.startswith("Added Bob Builder: they may now start and stop recordings")
    assert reply.endswith("Send them a short how-to note? Reply yes or no.")
    assert config.section("listen")["users"] == [SAM, BOB]
    assert config.section("listen")["user_names"] == [SAM, "Bob Builder"]
    assert dms(w, BOB) == [] and w.slack.to(BOB) == []                   # nothing to Bob
    assert one(w, IAN, "no") == "OK, no message sent to Bob Builder."
    assert dms(w, BOB) == [] and w.slack.to(BOB) == []
    assert BOB in [p.user for p in w.ls.pollers]                         # read from now on
    assert one(w, BOB, "list").startswith("Recording: nothing is recording")
    assert one(w, BOB, "settings") == ("Only admins can change settings and people: Ian H. Ask them to do it, or to "
                                       "make you an admin (they reply add admin @Bob Builder). Reply help for what "
                                       "you can do.")
    assert one(w, BOB, "add <@U0EVE>").startswith("Only admins can change settings and people: Ian H.")
    assert one(w, BOB, "people").startswith("Who may use the StreamSplitter app on HOMEPC:")   # anyone may look
    assert BOB in config.section("listen")["users"] and EVE not in config.section("listen")["users"]
    assert dms(w, EVE) == [] and w.slack.to(EVE) == []


def test_add_by_name_with_several_matches_then_the_note(w):
    reply = one(w, IAN, "add bob")
    assert reply.splitlines() == ["Several people match. Reply with the number of the one you mean, or no:",
                                  "  1. Bob Builder (@bob, bob@example.com)",
                                  "  2. Bobby Tables (@bobby, bobby@example.com)"]
    assert one(w, IAN, "7") == "Reply with the number of the one you mean (1 to 2), or no."
    assert one(w, IAN, "2").startswith("Added Bobby Tables")
    assert dms(w, BOBBY) == []
    assert one(w, IAN, "yes") == "Sent Bobby Tables a short how-to note."
    [note] = dms(w, BOBBY)
    assert note.startswith("Hi Bobby Tables! Ian H added you to StreamSplitter, the match video recorder on HOMEPC.")
    assert "message this app split 2025mttd;" in note and "help" in note and "within about 20 s" in note
    assert dms(w, BOB) == [] and dms(w, SAM) == [] and dms(w, EVE) == []   # nobody else, ever
    assert one(w, IAN, "1") == commands.NOTHING_TO_PICK                  # no question waiting any more


def test_add_by_email_and_the_refusals(w):
    assert one(w, IAN, "add <mailto:eve@example.com|eve@example.com>").startswith("Added Eve N")
    assert one(w, IAN, "no").startswith("OK, no message")
    assert one(w, IAN, "add eve").startswith("Eve N may use the app already. To make them an admin: add admin @Eve N.")
    assert one(w, IAN, "add <@U0IAN>") == "Ian H is the owner already (an admin, and gets the alerts)."
    assert one(w, IAN, "add nobody-of-that-name").startswith("Nobody in the workspace matches that.")
    assert "nobody-of-that-name" not in w.slack.to(IAN)[-1]             # nothing typed comes back
    assert one(w, IAN, "add").startswith("Say who")
    people = one(w, IAN, "people").splitlines()
    assert people[1].startswith("*Admins*: settings, people and the running recording")
    assert people[2] == "• Ian H (the owner: gets the alerts)"
    assert people[3] == "*People*: split, stop, status, help" and "• Eve N" in people
    assert people[-1] == "Messages are read within about 20 s; after someone writes, every few seconds."
    crowd(12)                                                            # more people: theirs are read less often
    w.tick()
    assert one(w, IAN, "people").splitlines()[-1].startswith("Admins' messages are read within about 20 s, everyone "
                                                             "else's within about 36 s")


def test_remove_drops_the_person_their_question_and_their_position(w, env):
    one(w, IAN, "add <@U0BOB>")
    one(w, IAN, "no")
    w.poll()
    one(w, BOB, "list")
    state = listener.listen_folder() / f"commands_{BOB}.json"
    assert state.is_file()
    assert one(w, IAN, "remove <@U0IAN>") == ("Ian H is the owner (gets the alerts), so they stay an admin. To send "
                                              "the alerts to someone else first: set owner @name.")
    assert one(w, IAN, "remove nobody").startswith("Nobody of that name may use the app.")
    assert one(w, IAN, "remove bob") == "Removed Bob Builder: the app no longer answers them. They got no message."
    assert BOB not in config.section("listen")["users"] and BOB not in [p.user for p in w.ls.pollers]
    assert not state.exists() and dms(w, BOB) == []
    assert w.send(BOB, "list") == []                                     # not read any more


def test_people_from_an_earlier_setup_keep_working_and_setup_changes_are_picked_up(w):
    assert one(w, SAM, "list").startswith("Recording: nothing")         # [listen] users from before: still allowed
    config.save_sections({"listen": {"users": [SAM, EVE], "user_names": ["Sam K", "Eve N"]}})
    w.tick()                                                             # the next tick reads config.toml again
    w.poll()
    assert one(w, EVE, "list").startswith("Recording: nothing")
    assert w.ls.names[EVE] == "Eve N"


# ---------------------------------------------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------------------------------------------

def test_settings_shows_values_and_commands_and_no_secret(w):
    replies = w.send(IAN, "settings")
    text = plain("\n".join(replies))
    assert all(len(r) <= commands.MESSAGE_MAX for r in replies)
    for title in ("*Event and stream*", "*Slack*", "*YouTube*", "*Google Drive*", "*Team wiki*", "*Owner and people*",
                  "*Slack listener*"):
        assert title in text
    assert ("• stream: https://www.youtube.com/watch?v=OLDvideo123  ·  `set stream <link>  or  set stream auto`"
            in text)
    assert "• teams: 5026  ·  `set teams 5026, 254`" in text
    assert "• privacy: unlisted  ·  `set youtube privacy unlisted | public | private`" in text
    assert "• owner (gets the alerts): Ian H  ·  `set owner @name`" in text
    assert "• other admins: none  ·  `add admin @name  /  remove admin @name`" in text
    assert "On the computer only: the YouTube login and YouTube's Terms of Service" in text
    for secret in ("xoxb-test-notreal", "tba-notreal", '"token"'):
        assert secret not in text
    youtube = plain(one(w, IAN, "settings youtube"))
    assert "*YouTube*" in youtube and "*Slack*" not in youtube
    assert one(w, IAN, "settings nonsense").startswith("There is no such area.")


def test_youtube_public_asks_first_and_changes_the_playlist(w):
    assert one(w, IAN, "set youtube privacy loud").startswith("Reply set youtube privacy unlisted")
    assert one(w, IAN, "set youtube privcy public").startswith("There is no such youtube setting. Did you mean "
                                                              "`set youtube privacy unlisted | public | private`?")
    # user, 2026-11-09: pasted in code formatting, it was "not understood"
    assert one(w, IAN, "`set youtube privacy public`").startswith("Public means every match video")
    assert one(w, IAN, "no") == "OK, nothing changed."
    ask = one(w, IAN, "set youtube privacy public")
    assert ask.startswith("Public means every match video can be found by anyone searching YouTube.")
    assert config.section("youtube")["privacy"] == "unlisted"           # nothing until yes
    assert one(w, IAN, "no") == "OK, nothing changed."
    one(w, IAN, "set youtube privacy public")
    reply = one(w, IAN, "yes")
    assert reply == ("OK: new videos are public. The playlist is public now. It applies when the next recording "
                     "starts.")
    assert config.section("youtube")["privacy"] == "public" and w.yt.privacy == [("PL25", "public")]
    assert one(w, IAN, "set youtube privacy unlisted").startswith("OK: new videos are unlisted.")   # no question


def test_a_question_expires_after_15_minutes(w):
    one(w, IAN, "set youtube privacy public")
    assert one(w, IAN, "yes", after=901) == ("That question expired after 15 minutes, so nothing was done. Reply set "
                                            "youtube privacy public to ask again.")
    assert config.section("youtube")["privacy"] == "unlisted"


def test_youtube_formats_route_and_playlist_are_checked(w):
    assert one(w, IAN, "set youtube matches others") == ("OK: YouTube gets only the matches without your teams (yours "
                                                        "go to Slack). It applies when the next recording starts.")
    assert config.section("youtube")["route"] == "others"
    reply = one(w, IAN, "set youtube title {match_title} | {event_name} {year}")
    assert reply.splitlines()[0] == "OK: the video title, for a sample match:"
    assert config.section("youtube")["title_format"] == "{match_title} | {event_name} {year}"
    assert one(w, IAN, "set youtube title {nope}").startswith("That title cannot be used:")
    assert config.section("youtube")["title_format"] == "{match_title} | {event_name} {year}"
    one(w, IAN, "set youtube description Match: {match_title}\nRecorded by Team 5026")
    assert config.section("youtube")["description_format"] == "Match: {match_title}\nRecorded by Team 5026"
    reply = one(w, IAN, "set youtube playlist {year} MadTown Replays")
    assert reply.startswith("OK: this event's matches go to the playlist \"2025 MadTown Replays\"")
    assert ("2025 MadTown Replays", "unlisted") in w.yt.made
    assert config.section("youtube")["playlist_title"] == "2025 MadTown Replays"


def test_youtube_off_and_on(w):
    assert one(w, IAN, "set youtube off").startswith("OK: nothing is uploaded to YouTube.")
    assert config.section("youtube")["enabled"] is False
    config.save_sections({"youtube": {"terms_accepted": "2026-11-04"}})
    assert one(w, IAN, "set youtube on").startswith("OK: matches upload to YouTube, unlisted, to the playlist "
                                                    "\"2025 MadTown Throwdown\".")
    assert config.section("youtube")["enabled"] is True
    config.set_secret("youtube_token", None)                             # no login on this computer
    one(w, IAN, "set youtube off")
    assert "run.bat --section youtube" in one(w, IAN, "set youtube on")
    assert config.section("youtube")["enabled"] is False


def test_tba_links_need_the_saved_key_and_a_yes(w):
    from streamsplitter.publish import tbalink as tl
    reply = one(w, IAN, "set youtube tbalinks on")
    assert "run.bat --section tbalinks" in reply and "tba_links" not in config.section("youtube")
    config.set_secret("tba_write_id", "id-notreal")
    config.set_secret("tba_write_secret", "secret-notreal")
    ask = one(w, IAN, "set youtube tbalinks on")
    assert ask.startswith(tl.WARNING) and ask.endswith("Reply yes or no.")
    assert one(w, IAN, "yes").startswith("OK: each YouTube video is added to its match on The Blue Alliance")
    assert config.section("youtube")["tba_links"] is True
    w.ls.admin.tba_check = lambda auth_id, secret, key: "the key has no match_video access"
    one(w, IAN, "set youtube tbalinks off")
    one(w, IAN, "set youtube tbalinks on")
    assert one(w, IAN, "yes").startswith("The saved write key did not work for 2025mttd (the key has no "
                                         "match_video access), so the links stay off.")
    assert config.section("youtube")["tba_links"] is False
    w.ls.admin.tba_check = lambda auth_id, secret, key: None
    one(w, IAN, "set youtube tbalinks on")
    one(w, IAN, "yes")
    assert config.section("youtube")["tba_links"] is True
    reply = one(w, IAN, "set youtube privacy private")
    assert "The Blue Alliance video links are off now" in reply
    assert reply.endswith("It applies when the next recording starts.")
    assert config.section("youtube")["tba_links"] is False                # a private video plays for nobody there


def test_slack_drive_wiki_and_listener_settings(w):
    assert one(w, IAN, "set slack size 720") == ("OK: matches go to Slack at 720p (about 40 MB). It applies when the "
                                                "next recording starts.")
    assert config.section("slack")["clip_height"] == 720
    assert one(w, IAN, "set slack channel <#C0NEW|new-videos>").startswith("OK: your team's matches go to #new-videos.")
    assert config.section("slack")["channel_id"] == "C0NEW" and w.slack.joined == ["C0NEW"]
    assert one(w, IAN, "set slack channel #secret-room").startswith("Invite the app first")
    assert one(w, IAN, "set slack channel #nowhere").startswith("No such channel was found.")
    assert one(w, IAN, "set slack off").startswith("OK: no match goes to the Slack channel.")
    assert one(w, IAN, "set drive size 540").startswith("OK: Drive gets the matches at 540p.")
    assert config.section("drive")["height"] == 540
    reply = one(w, IAN, "set drive names {code} {match_title}")
    assert reply.splitlines()[1].endswith(".mp4") and config.section("drive")["name_format"] == "{code} {match_title}"
    assert "run.bat --section drive" in one(w, IAN, "set drive on")      # no Drive login here
    assert "run.bat --section wiki" in one(w, IAN, "set wiki on")        # no wiki password here
    assert one(w, IAN, "set wiki private off").startswith("OK: the page and its videos are for every reader.")
    # Slack sends < and > as &lt; &gt; (a bare <...> is its own link markup)
    assert one(w, IAN, "set wiki embed &lt;video&gt;File:{file}&lt;/video&gt;").startswith("OK: the videos show on the "
                                                                                          "page")
    assert config.section("wiki")["embed_format"] == "<video>File:{file}</video>"
    assert one(w, IAN, "set wiki embed nothing").startswith("That markup cannot be used")
    assert one(w, IAN, "set listener alarm off") == ("OK: no DM if the listener stops. It applies the next time the "
                                                    "Slack listener starts.")
    assert config.section("listen")["alarm"] is False
    assert one(w, IAN, "set youtube pause") == "Reply pause youtube to hold the uploads, or resume youtube to send them."
    assert one(w, IAN, "set nothing here").startswith("There is no setting called that.")
    assert one(w, IAN, "set youtube colour red").startswith("There is no such youtube setting.")


def test_score_or_clip_version_per_destination(w):
    """User, 2026-11-07: `set drive version clip` was refused ("There is no such drive setting")."""
    assert one(w, IAN, "set drive version clip").startswith("OK: Google Drive gets each match alone, about 1-1.5 min")
    assert config.section("drive")["version"] == "clip"
    assert one(w, IAN, "set youtube version score").startswith("OK: YouTube gets each match with its results screen")
    assert config.section("youtube")["version"] == "score"
    assert one(w, IAN, "set wiki version clip").startswith("OK: the team wiki gets each match alone")
    assert one(w, IAN, "set drive version fast").startswith("Reply set drive version score")
    assert config.section("drive")["version"] == "clip"
    assert "version: the match alone (clip)" in one(w, IAN, "settings drive")
    assert "set drive version score | clip" in one(w, IAN, "settings drive")


def test_while_recording_the_reply_says_the_running_one_keeps_the_old_setting(w):
    w.runner.running["2025mttd"] = {"started": w.clock.t}
    assert one(w, IAN, "set drive size 720").endswith("It applies when the next recording starts; the one running now "
                                                      "keeps the old setting.")
    assert w.send(IAN, "teams 254") == []                                # the recording answers that one itself


def test_computer_only_settings_and_tokens_typed_into_slack(w):
    reply = one(w, IAN, "set slack token please")
    assert reply.startswith("The Slack app's token can only be changed on the computer")
    assert "`run.bat --section slack`" in reply
    assert "run.bat --section tba" in one(w, IAN, "set tba key abc")
    assert "run.bat --section listen" in one(w, IAN, "set listener mode window")
    assert "run.bat --section youtube" in one(w, IAN, "set youtube login")
    for typed, step in (("set slack token xoxb-1234-5678-abcdefabcdef", "slack"),
                        ("here it is: ya29.a0AfH6SMBexample", "youtube"),
                        ("set tba key " + "aB3" * 21 + "x", "tba"),
                        ("0123456789abcdef0123456789abcdef", "wiki")):
        reply = one(w, IAN, typed)
        assert reply.startswith("That looks like a password or key, so I did not use it or repeat it.")
        assert f"run.bat --section {step}" in reply and typed.split()[-1] not in reply
    assert w.send(SAM, "xoxb-1234-5678-abcdefabcdef")[0].startswith("That looks like a password")  # anyone
    for fine in ("stream https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                 "set wiki page MadTown-Throwdown-2026-match-videos-of-team-5026-and-friends",
                 "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"):
        assert slackadmin.looks_secret(fine) is None, fine


def test_set_owner_hands_over_the_alerts_and_the_old_owner_stays_an_admin(w):
    # user, 2026-11-09: after "Set owner @Jamie Lee" and yes, the person who set it up could no longer change a setting
    # during the event ("Only Jamie Lee (the owner) can change settings ...")
    ask = one(w, IAN, "set owner <@U0SAM>")
    assert ask == ("Make Sam K the owner? The alerts (the stopped-running alarm too) go to them from now on, and they "
                   "become an admin. You stay an admin, so you can still change settings from this chat. Reply yes "
                   "or no.")
    assert one(w, SAM, "yes") == commands.NOTHING_TO_CONFIRM               # only the asker answers
    reply = one(w, IAN, "yes")
    assert reply.startswith("OK: Sam K is the owner now and was told so. You stay an admin, so you can still change "
                            "settings.")
    nt, ls = config.section("notify"), config.section("listen")
    assert (nt["slack_user"], nt["slack_user_name"]) == (SAM, "Sam K")
    assert (ls["users"], ls["user_names"], ls["admins"]) == ([IAN], ["Ian H"], [IAN])
    assert slackadmin.admin_ids() == [SAM, IAN]
    [told] = dms(w, SAM)
    assert told.startswith("Ian H made you the owner of StreamSplitter on HOMEPC")
    assert w.ls.contact == SAM and w.ls.pollers[0].user == SAM
    assert one(w, IAN, "settings").startswith("StreamSplitter settings on HOMEPC")    # still an admin
    assert one(w, SAM, "settings").startswith("StreamSplitter settings on HOMEPC")
    assert one(w, IAN, "set slack size 720").startswith("OK: matches go to Slack at 720p")
    assert one(w, IAN, "set owner <@U0IAN>").startswith("Make Ian H the owner?")     # and can take the alerts back
    assert one(w, IAN, "yes").startswith("OK: Ian H is the owner now")
    assert slackadmin.admin_ids() == [IAN, SAM]                          # Sam stays an admin in turn


def test_admins_and_people_and_every_refusal_says_what_to_do(w):
    one(w, IAN, "add <@U0BOB>")
    one(w, IAN, "no")
    [no] = w.send(BOB, "set youtube privacy public")
    assert plain(no) == ("Only admins can change settings and people: Ian H. Ask them to do it, or to make you an "
                         "admin (they reply add admin @Bob Builder). Reply help for what you can do.")
    assert one(w, IAN, "add admin @Bob Builder").startswith("OK: Bob Builder is an admin now")
    assert one(w, BOB, "set youtube matches others").startswith("OK: YouTube gets only the matches without your teams")
    assert one(w, BOB, "add admin <@U0BOB>").startswith("Bob Builder is an admin already.")
    reply = one(w, IAN, "add admin <@U0EVE>")
    assert reply.startswith("Added Eve N as an admin: they may now change settings, people and the running recording")
    one(w, IAN, "no")
    assert slackadmin.admin_ids() == [IAN, BOB, EVE]
    assert one(w, EVE, "remove admin bob") == ("OK: Bob Builder is no longer an admin; they may still start and stop "
                                               "recordings. They got no message.")
    assert one(w, BOB, "settings").startswith("Only admins can change settings and people: Ian H and Eve N. Ask one "
                                              "of them")
    assert one(w, IAN, "remove eve").startswith("Removed Eve N")
    assert slackadmin.admin_ids() == [IAN] and "admins" not in config.section("listen")


def test_admins_are_read_as_often_as_the_owner(w):
    config.save_sections({"listen": {"users": [SAM, BOB], "user_names": ["Sam K", "Bob Builder"], "admins": [BOB]}})
    w.tick()
    assert w.ls.admins == [IAN, BOB]
    assert w.ls.usual_interval(BOB) == commands.POLL_S and w.ls.usual_interval(SAM) == commands.POLL_S
    assert w.ls._active(w.clock.t) == [BOB]


def test_set_listener_off_asks_then_stops_listening(w):
    ask = one(w, IAN, "stop listener")
    assert ask.startswith("Turn Start from Slack off on HOMEPC?")
    assert w.runner.stops == []                                          # not the recording's stop
    assert one(w, IAN, "no") == "OK, it keeps listening."
    one(w, IAN, "set listener off")
    assert one(w, IAN, "yes").startswith("OK: Start from Slack is off on HOMEPC. It stops listening now")
    assert config.section("listen")["enabled"] is False
    assert w.ls.quit == "turned off from Slack by Ian H"
    assert w.ls.loop(lambda: None, sleep=lambda s: None) == "turned off from Slack by Ian H"


def test_set_event_saves_without_starting(w):
    reply = one(w, IAN, "set event 2026mttd")
    assert reply.startswith("MadTown Throwdown - Madera, CA - Nov 6-8, 2026 (2026mttd)")
    assert reply.endswith("*Reply yes to save it as the event* (nothing starts recording), or no. (Next time, set "
                          "event 2026mttd yes saves it without asking.)")
    reply = one(w, IAN, "yes")
    assert reply.startswith("Saved MadTown Throwdown (2026mttd) as the event on HOMEPC; nothing records yet.")
    assert config.section("event")["key"] == "2026mttd" and w.runner.started == []
    assert config.section("youtube")["playlist_event"] == "2026mttd"
    assert one(w, SAM, "set event 2026casj").startswith("Only admins can change the event: Ian H.")
    w.runner.running["2026mttd"] = {"started": w.clock.t}
    assert one(w, IAN, "set event 2026casj").endswith("One recording per computer: reply stop first, then set event "
                                                      "again.")


# ---------------------------------------------------------------------------------------------------------------
# help
# ---------------------------------------------------------------------------------------------------------------

def test_help_topics_and_sizes(w):
    full = commands.help_text("errata", drive=True, wiki=True, listen=True)
    assert len(full) < commands.MESSAGE_MAX
    for c in commands.SHORT_LIST:                                        # the test post's line matches the help
        assert f"`{commands._esc(c)}`" in full, c
    parts = w.send(IAN, "help set")                                      # long: a few messages, each readable
    assert len(parts) == 2 and all(len(plain(p)) <= commands.MESSAGE_MAX for p in parts)
    sets = plain("\n".join(parts))
    for s in slackadmin.SETTINGS:
        assert s.usage in sets, s.usage
    assert "applies at the next recording start" in sets
    assert "{match_title}" in plain(one(w, IAN, "help youtube"))
    computer = plain(one(w, IAN, "help computer"))
    assert "the team settings file: `run.bat --section team`" in computer
    assert one(w, IAN, "help add").startswith("people lists who may use the app")
    assert one(w, IAN, "help nonsense") == ("There is no help on that. Reply help for what you can do now, or help "
                                            "with one of: " + slackadmin.TOPIC_LIST + ".")
    assert one(w, SAM, "help split").startswith("split <event> starts recording an event")
    assert one(w, SAM, "help set stream").startswith("set stream <link> records this YouTube or Twitch link")
    assert one(w, SAM, "help admins").startswith("Admins change settings")
    for topic in ("split", "stop", "stream", "teams", "pause", "settings", "people", "owner", "listener", "admin",
                  "computer", "help", "yes", "list", "status", "youtube", "drive", "slack", "wiki"):
        assert len(slackadmin.help_topic(topic)) < commands.MESSAGE_MAX, topic     # only help set takes two


def test_long_replies_are_split_at_line_ends():
    text = "\n".join(f"line {i} " + "x" * 90 for i in range(100))
    parts = commands.split_message(text)
    assert len(parts) > 1 and all(len(p) <= commands.MESSAGE_MAX for p in parts)
    assert "\n".join(parts) == text
    assert commands.split_message("short") == ["short"]
    assert commands.split_message("y" * 8000) == ["y" * 3500, "y" * 3500, "y" * 1000]


# ---------------------------------------------------------------------------------------------------------------
# one answer per message: the listener and the recording both read the owner's DM
# ---------------------------------------------------------------------------------------------------------------

EVERY_KIND = ("settings", "set youtube matches others", "add <@U0BOB>", "no", "people", "help add", "status", "list",
              "teams 254", "set teams 1323", "xoxb-1234-5678-abcdefabcdef", "1", "stop listener", "no",
              "set event 2026mttd", "no", "Resume youtube", "`pause youtube`", "TBA", "TBA", "thanks", "make coffee",
              "stop", "no", "help")


def test_exactly_one_answer_with_a_listener_and_a_recording(w):
    """This version's listener reads the owner's DM; the recording (its CommandPoller beside it, protocol 2) reads
    nothing and takes the recording's commands from its mailbox. Every message gets one answer (user, 2026-11-09:
    "Resume youtube" got an OK and a refusal, "TBA" got "I did not understand that" twice)."""
    w.record("2025mttd")
    rec = commands.CommandPoller(w.slack, IAN, w.controls, channel_name="errata", log=lambda s: None, clock=w.clock,
                                 listen=lambda: commands.PROTOCOL)
    rec.poll()
    for text in EVERY_KIND:
        n = len(w.slack.to(IAN))
        w.slack.say(IAN, text, w.clock.t + 5)
        w.clock.t += 5
        w.poll()
        rec.poll()
        got = w.slack.to(IAN)[n:]
        assert len(got) == 1, (text, got)
    assert rec.reads == 0                                                # the recording read no Slack at all
    assert ("set_teams", ["254"]) in w.controls.calls and ("set_teams", ["1323"]) in w.controls.calls
    assert ("resume", "youtube") in w.controls.calls and ("pause", "youtube") in w.controls.calls
    assert w.ls.state["forwards"] == {}


def test_the_madtown_hand_off_gets_one_answer_each(w):
    """The DM history of 2026-11-07..09: Ian set a teammate (Sam here) as the owner while a recording ran; then Ian's
    resume youtube, TBA and settings each got two answers, one of them a refusal."""
    w.record("2025mttd")
    one(w, IAN, "set owner <@U0SAM>")
    one(w, IAN, "yes")
    for text, want in (("Resume youtube", "did resume"), ("TBA", "I did not understand that. Did you mean `set stream "
                                                                 "auto`"),
                       ("set youtube privacy public", "Public means"), ("no", "OK, nothing changed.")):
        assert one(w, IAN, text).startswith(want), text
    assert one(w, SAM, "status").startswith("did status_text\nSlack listener: running on HOMEPC")


def test_a_command_the_recording_does_not_take_is_answered_once(w):
    w.record("2025mttd")
    w.mailboxes.pop("2025mttd")                                          # stuck: it takes nothing
    assert w.send(IAN, "pause youtube") == []                            # passed on; its answer comes later
    w.tick(after=listener.FORWARD_WAIT_S + 1)
    [said] = w.slack.to(IAN)[-1:]
    assert said == ("The recording on HOMEPC did not take that within 20 s, so nothing was done (it may be busy or "
                    "stopping). Reply status to see how it is doing, then try again.")
    assert w.controls.calls == [] and w.ls.state["forwards"] == {}
    w.tick(after=600)
    assert w.slack.to(IAN)[-1] == said                                   # said once


def test_a_listener_restarted_between_question_and_answer_still_posts_it(w, env):
    w.record("2025mttd")
    box = w.mailboxes.pop("2025mttd")
    assert w.send(IAN, "status") == []
    w.ls = w.make()                                                      # the listener restarted meanwhile
    box.process()
    w.ls.deliver()
    assert w.slack.to(IAN)[-1].startswith("did status_text")


def test_a_reaction_shows_each_message_was_read(env):
    class Reacting(PeopleSlack):
        def __init__(self, clock):
            super().__init__(clock)
            self.reactions = []

        def auth_test(self):
            return {"scopes": ["chat:write", "im:write", "im:history", "reactions:write"], "missing_scopes": []}

        def add_reaction(self, channel, ts, name):
            self.reactions.append((channel, name))

    class W(AdminWorld):
        def __init__(self):
            self.clock = Clock()
            self.slack = Reacting(self.clock)
            self.runner = FakeRunner(self.clock)
            self.note = Note()
            self.yt = RecordingYouTube()
            self.controls = Controls()
            self.mailboxes = {}
            self.ls = self.make()
            self.poll()
    w = W()
    one(w, IAN, "status")
    assert w.send(IAN, "thanks") == []                                   # a thumb, no message
    one(w, SAM, "help")
    assert w.slack.reactions == [("D" + IAN, "eyes"), ("D" + IAN, "+1"), ("D" + SAM, "eyes")]
    assert ":eyes: on your message means it was read." in one(w, SAM, "help")


def test_with_no_listener_the_recording_answers_settings_and_people(env):
    clock = Clock()
    s = PeopleSlack(clock)
    yt = RecordingYouTube()
    gone = []
    admin = slackadmin.Admin(s, host="HOMEPC", recording=lambda: True, youtube=lambda: yt,
                             login_off=lambda: gone.append(1))
    rec = commands.CommandPoller(s, IAN, Controls(), channel_name="errata", log=lambda x: None, clock=clock,
                                 listen=lambda: False, admin=admin)
    rec.poll()

    def say(text):
        clock.t += 5
        n = len(s.to(IAN))
        s.say(IAN, text, clock.t)
        rec.poll()
        got = s.to(IAN)[n:]
        assert len(got) == 1, (text, got)
        return plain(got[0])
    assert say("settings").startswith("StreamSplitter settings on HOMEPC (a recording runs now):")
    assert say("set youtube privacy public").startswith("Public means")
    assert say("yes").startswith("OK: new videos are public.") and config.section("youtube")["privacy"] == "public"
    added = say("add <@U0BOB>")
    assert "Start from Slack is not running on HOMEPC" in added and s.dms_sent == []
    assert say("yes") == "Sent Bob Builder a short how-to note." and [u for u, _ in s.dms_sent] == [BOB]
    assert say("yes") == commands.NOTHING_TO_CONFIRM and say("3") == commands.NOTHING_TO_PICK
    say("set listener off")
    assert say("yes").startswith("OK: Start from Slack is off on HOMEPC: it no longer starts at log-in.")
    assert config.section("listen")["enabled"] is False and gone == [1]
    assert say("set event 2026mttd").startswith("A recording runs on HOMEPC, so the event changes once it is stopped")
    assert say("xoxb-1234-5678-abcdefabcdef").startswith("That looks like a password or key")


# ---------------------------------------------------------------------------------------------------------------
# polling budget
# ---------------------------------------------------------------------------------------------------------------

def crowd(n: int) -> None:
    others = [f"U0P{i:03d}" for i in range(n)]
    config.save_sections({"listen": {"users": others, "user_names": [f"Person {i}" for i in range(n)]}})


def run_loop(w, seconds: int) -> dict[str, int]:
    """The listener's loop for `seconds` of fake time; the conversations.history calls per DM."""
    t = [0.0]
    w.ls.monotonic = lambda: t[0]
    w.slack.history_calls.clear()
    left = [seconds]

    def stop():
        left[0] -= 1
        return "done" if left[0] < 0 else None

    def sleep(s):
        t[0] += s
        w.clock.t += s
    w.ls.loop(stop, sleep=sleep)
    return dict(w.slack.history_calls)


@pytest.mark.parametrize("people", [1, 12, 40])
def test_polling_stays_under_slacks_limit(env, people):
    crowd(people)
    w = AdminWorld()
    assert len(w.ls.users) == people + 1
    calls = run_loop(w, 600)
    per_min = sum(calls.values()) / 10
    assert per_min <= 3 + listener.ACTIVE_MAX * 3 + listener.OTHERS_PER_MIN + 1
    assert calls["D" + IAN] >= 29                                        # the owner: every 20 s
    if people == 12:
        assert w.ls.read_every() == 36.0 and per_min < 25
    if people == 40:
        assert w.ls.read_every() == 120.0
    # people who wrote lately are read as often as the owner, at most ACTIVE_MAX of them
    for u in w.ls.users[1:8]:
        w.ls.heard[u] = w.clock.t
    fast = [u for u in w.ls.users[1:] if w.ls.usual_interval(u) == commands.POLL_S]
    assert len(fast) == min(listener.ACTIVE_MAX, people)
    busy = run_loop(w, 300)                                              # some of them still writing: read faster
    assert sum(busy.values()) / 5 <= listener.READS_PER_MIN


@pytest.mark.parametrize("people", [3, 40])
def test_after_someone_writes_their_chat_is_read_every_few_seconds_within_the_limit(env, people):
    """User, 2026-11-09: replies took 10-20 s. After a message, that chat is read every 3 s for 2 minutes (at most
    HOT_MAX chats at once, sharing HOT_PER_MIN), and whatever happens no more than READS_PER_MIN reads go out in any
    minute, the hot chats first."""
    crowd(people)
    w = AdminWorld()
    writers = w.ls.users[:5]
    t = [0.0]
    w.ls.monotonic = lambda: t[0]
    reads: list[tuple[float, str]] = []
    real = w.slack.history

    def history(channel, limit=15, oldest=None):
        reads.append((t[0], channel))
        return real(channel, limit, oldest)
    w.slack.history = history

    def stop():
        if t[0] == 180.0:                                                # well after the first reads of a start
            for u in writers:                                            # five people write at once
                w.slack.say(u, "status", w.clock.t)
        return "done" if t[0] >= 480.0 else None

    def sleep(s):
        t[0] += s
        w.clock.t += s
    w.ls.loop(stop, sleep=sleep)
    stamps = [s for s, _ in reads]
    assert max(sum(1 for x in stamps if a <= x < a + 60) for a in stamps) <= listener.READS_PER_MIN
    for u in writers:
        mine = [s for s, ch in reads if ch == "D" + u and s >= 180.0]
        assert mine[0] - 180.0 <= w.ls.read_every() + listener.STAGGER_S + 1      # its usual interval at most
        assert mine[1] - mine[0] <= 60 * listener.HOT_MAX / listener.HOT_PER_MIN + 1  # then every few seconds
        assert len(w.slack.to(u)) == 1                                   # one answer each
    assert len(w.ls._hot(w.clock.t)) <= listener.HOT_MAX
    one_writer = AdminWorld()
    run_loop(one_writer, 30)
    one_writer.ls.heard[IAN] = one_writer.clock.t
    assert one_writer.ls.interval(IAN) == commands.FAST_S
    assert run_loop(one_writer, 60)["D" + IAN] >= 60 / commands.FAST_S - 1
    one_writer.clock.t += commands.HOT_S
    assert one_writer.ls.interval(IAN) == commands.POLL_S                # two minutes later: back to normal


def test_ratelimited_holds_every_read(env):
    crowd(3)
    w = AdminWorld()
    run_loop(w, 30)
    w.slack.limit_once.add("DU0P000")
    calls = run_loop(w, 100)                       # the owner, then U0P000: Slack asks for 120 s, so nobody else
    assert calls == {"D" + IAN: 1, "DU0P000": 1}
    p = commands.CommandPoller(w.slack, IAN, Controls(), log=lambda s: None)
    p.poll()
    w.slack.limit_once.add("D" + IAN)
    assert p.poll() == 120 and p.limited == 120


# ---------------------------------------------------------------------------------------------------------------
# the Slack app
# ---------------------------------------------------------------------------------------------------------------

def test_the_manifest_lets_people_dm_the_app_with_the_scopes_needed():
    m = slack.manifest()
    home = m["features"]["app_home"]
    assert home["messages_tab_enabled"] is True and home["messages_tab_read_only_enabled"] is False
    for scope in ("chat:write", "im:write", "im:history", "users:read", "users:read.email", "channels:read",
                  "groups:read", "channels:join"):
        assert scope in slack.REQUIRED_SCOPES
    assert json.loads(json.dumps(m)) == m
    assert tbalink.WARNING                                               # shown before the links go on
