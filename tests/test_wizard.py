"""The setup wizard (streamsplitter/setup_wizard.py) and the setup/start/status/stop commands, with scripted answers
and fake TBA, yt-dlp, Slack, YouTube, Google Drive (tests/fake_drive.py), wiki (tests/fake_mediawiki.py) and live
runner: no network, no logins, no credential store (secrets go to a secrets.json in the test's folder), no downloads.

    .venv\\Scripts\\python.exe -m pytest -p no:cacheprovider tests\\test_wizard.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import types
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_drive import FakeDrive  # noqa: E402
from fake_mediawiki import FakeMediaWiki  # noqa: E402
from streamsplitter import cli, config, setup_wizard, teamkit  # noqa: E402
from streamsplitter.publish.base import PermanentError  # noqa: E402
from streamsplitter.setup_wizard import Background, SetupFailed, Task, Wizard  # noqa: E402
from streamsplitter.tba import TBAError  # noqa: E402

GOOD_TBA = "tbaKEY" + "x" * 58
GOOD_SLACK = "xoxb-1111-2222-GOODslackTOKEN"
TOKEN_JSON = json.dumps({"token": "ya29.ACCESSsecretVALUE", "refresh_token": "1//REFRESHsecretVALUE"})
CLIENT = {"installed": {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "GOCSPX-clientSECRET",
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": ["http://localhost"]}}
# what Google's Download JSON gives: the project's ID inside, which the fix links use
CLIENT_P = {"installed": {**CLIENT["installed"], "project_id": "streamsplitter-5026"}}
WEB_CLIENT = {"web": {"client_id": "999-web.apps.googleusercontent.com", "client_secret": "GOCSPX-webSECRET"}}
PASS = "k7m2q-x9rtw-ab3cd-ef4gh"
WIKI_PASS = "goodpassword"                   # the fake wiki's bot password for Ian@StreamSplitter
DRIVE_TOKEN = json.dumps({"token": "tok0", "refresh_token": "1//DRIVErefreshSECRET"})     # the fake Drive's
SECRET_VALUES = (GOOD_TBA, GOOD_SLACK, "ACCESSsecretVALUE", "REFRESHsecretVALUE", "GOCSPX-clientSECRET", PASS,
                 WIKI_PASS, "DRIVErefreshSECRET")
CHANNEL_LIVE = "https://www.youtube.com/@Madtown_Throwdown/live"
CONSOLE = "https://console.cloud.google.com/"

MTTD = {"key": "2026mttd", "name": "MadTown Throwdown", "year": 2026, "event_code": "mttd", "event_type": 99,
        "start_date": "2026-11-06", "end_date": "2026-11-08", "city": "Madera", "state_prov": "CA", "country": "USA"}
EVENTS = [MTTD,
          {"key": "2026cttd", "name": "CT Throwdown", "year": 2026, "event_code": "cttd", "start_date": "2026-10-03",
           "end_date": "2026-10-03", "city": "Hartford", "state_prov": "CT"},
          {"key": "2026casj", "name": "Silicon Valley Regional", "year": 2026, "event_code": "casj",
           "start_date": "2026-03-25", "end_date": "2026-03-28", "city": "San Jose", "state_prov": "CA"}]
WEBCASTS = [{"type": "youtube", "channel": "WPfTIUtaTzo", "date": "2026-11-06"},
            {"type": "youtube", "channel": "QEtvEsSF9_I", "date": "2026-11-07"},
            {"type": "twitch", "channel": "madtownthrowdown"}]


# ---------------------------------------------------------------------------------------------------------------------
# Scripted answers and fakes

class ScriptedIO(setup_wizard.BaseIO):
    """Answers questions from a script of (words the question must contain, answer). Records everything shown. An
    answer can be a function: it runs when the question is asked (a file "downloaded" meanwhile) and returns the
    answer."""

    def __init__(self, script):
        self.script = list(script)
        self.out: list[str] = []
        self.prompts: list[str] = []
        self.opened: list[str] = []

    def _write(self, text):
        self.out.append(text)

    def _next(self, shown, question, secret=False):
        self.prompts.append(shown)
        if not self.script:
            raise AssertionError(f"unexpected question: {question!r} / {shown!r}\n--- output so far:\n{self.text}")
        expect, answer = self.script.pop(0)
        assert expect.lower() in f"{question} {shown}".lower(), \
            f"expected a question about {expect!r}, got {question!r} / {shown!r}\n--- output so far:\n{self.text}"
        if callable(answer):
            answer = answer()
        self.out.append(shown + ("(hidden)" if secret else answer))
        return answer

    def _line(self, shown, question):
        return self._next(shown, question)

    def _hidden(self, shown, question):
        return self._next(shown, question, secret=True)

    def _browse(self, url):
        self.opened.append(url)

    @property
    def text(self) -> str:
        return "\n".join(self.out)


class FakeLive:
    def __init__(self):
        self.started, self.stopped, self.status, self.calls = [], [], {}, []

    def start_detached(self, args):
        self.started.append(list(args))
        self.calls.append(("start_detached",))
        self.status = {"running": True, "last_chunk": 12, "outbox": {"slack": {"queued": 0, "done": 3}}}
        return 4242

    def read_status(self, tag):
        return self.status

    def request_stop(self, tag):
        self.stopped.append(tag)
        self.calls.append(("request_stop", tag))
        self.status = {**self.status, "running": False}

    def wait_stopped(self, tag, timeout_s=180.0):
        self.calls.append(("wait_stopped", tag))
        if self.status.get("stopping"):                  # the run that was stopping has ended now
            self.status = {**self.status, "running": False, "stopping": False}
        return not self.status.get("running")


class FakeListener:
    """The listener module as the wizard uses it (listener.py): no real log-in start, no process."""
    MENU_START = "Start listening for Slack commands"
    MENU_STOP = "Stop listening for Slack commands"
    MENU_WINDOW = "Listen for Slack commands in this window"

    def __init__(self):
        self.running, self.installed, self.calls = False, False, []

    def run(self):                                   # listening in this window, until it is closed
        self.calls.append("run")
        return 0

    def is_running(self):
        return self.running

    def login_start_installed(self):
        return self.installed

    def install_login_start(self):
        self.calls.append("install")
        self.installed = True
        return Path("Startup") / "StreamSplitter listener.lnk"

    def remove_login_start(self):
        self.calls.append("remove")
        was, self.installed = self.installed, False
        return was

    def start_detached(self):
        self.calls.append("start")
        self.running = True
        return 5150

    def request_stop(self):
        self.calls.append("stop")
        self.running = False

    def wait_stopped(self, timeout_s=30.0):
        return not self.running


def fake_slack(log: list, users=None, auths=None, required_scopes=None):
    """`auths`: what successive auth_test calls add to the answer (missing_scopes, scopes), then a complete app."""
    users = users if users is not None else [{"id": "U0IAN", "name": "ian", "real_name": "Ian H",
                                              "display_name": None, "email": "ian@example.com"}]
    auths = list(auths or [])

    class SlackClient:
        def __init__(self, token, timeout=30.0):
            self.token = token

        def auth_test(self):
            log.append(("auth_test",))
            if self.token != GOOD_SLACK:
                raise PermanentError("Slack no longer accepts StreamSplitter's token (invalid_auth)")
            return {"team": "Karma 5026", "team_id": "T1", "bot_user_id": "B1", "missing_scopes": [],
                    **(auths.pop(0) if auths else {})}

        def find_channel(self, name):
            log.append(("find_channel", name))
            if name == "errata":
                return {"id": "C0ERRATA", "name": "errata", "is_private": False, "is_member": False}
            return None

        def join_channel(self, channel_id):
            log.append(("join", channel_id))

        def find_users(self, query):
            return [u for u in users if query.lower() in (u["real_name"] + u["name"] + u["email"]).lower()]

        def dm(self, user_id, text):
            log.append(("dm", user_id, text))
            return {"ts": "1.1", "channel": "D0IAN"}

        def schedule_message(self, channel_id, text, post_at):
            log.append(("schedule", channel_id, text))
            return "Q0ALARM"

        def open_dm(self, user_id):
            log.append(("open_dm", user_id))
            return "D0IAN"

        def upload_file(self, channel_id, path, title, comment, thread_ts=None):
            log.append(("slack_upload", channel_id, Path(path).name, comment))
            return {"file_id": "F1", "permalink": "https://karma.slack.com/archives/C0ERRATA/p1", "ts": "1.2"}

    ns = types.SimpleNamespace(SlackClient=SlackClient, TEST_FORMAT="StreamSplitter setup test: sample video",
                               create_app_url=lambda: "https://api.slack.com/apps?new_app=1&manifest_json=%7B%7D")
    if required_scopes is not None:
        ns.REQUIRED_SCOPES = list(required_scopes)
    return ns


KARMA = {"id": "UC5026", "title": "Karma Robotics", "url": "https://www.youtube.com/@karma5026"}


def fake_youtube(log: list, privacy_after="unlisted", playlist=None, logins=None, channels=None):
    """`logins` / `channels`: what successive login() / channel() calls do (an exception to raise, a function to
    call, or a channel to return), then the happy answer."""
    from streamsplitter.publish import youtube as real
    logins, channels = list(logins or []), list(channels or [])

    class YouTubeClient:
        def __init__(self, token_json, client_config=None, on_token=None):
            assert token_json == TOKEN_JSON
            self.on_token = on_token

        def channel(self):
            log.append(("channel",))
            c = channels.pop(0) if channels else KARMA
            if isinstance(c, Exception):
                raise c
            return c

        def find_playlist(self, title):
            return playlist

        def create_playlist(self, title, description, privacy="unlisted"):
            log.append(("create_playlist", title, privacy))
            return {"id": "PL1", "title": title, "url": "https://www.youtube.com/playlist?list=PL1", "privacy": privacy}

        def set_playlist_privacy(self, playlist_id, privacy):
            log.append(("set_playlist_privacy", playlist_id, privacy))
            return {**(playlist or {}), "id": playlist_id, "privacy": privacy}

        def upload(self, path, title, description, privacy, state, progress=None):
            log.append(("yt_upload", Path(path).name, title, privacy))
            if progress:
                progress(5, 10)
                progress(10, 10)
            state["video_id"] = "vid1"
            return {"video_id": "vid1", "url": "https://www.youtube.com/watch?v=vid1", "privacy": privacy,
                    "locked_private": False}

        def add_to_playlist(self, playlist_id, video_id):
            log.append(("add", playlist_id, video_id))

        def video_status(self, video_id):
            return {"privacyStatus": privacy_after, "uploadStatus": "uploaded"}

        def delete_video(self, video_id):
            log.append(("delete", video_id))

    def login(client_config):
        log.append(("login", client_config["installed"]["client_id"]))
        step = logins.pop(0) if logins else None
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step()
        return TOKEN_JSON

    return types.SimpleNamespace(login=login, YouTubeClient=YouTubeClient,
                                 check_client_config=real.check_client_config,
                                 read_client_config=real.read_client_config, LoginError=real.LoginError,
                                 TEST_TITLE="StreamSplitter setup test", TEST_DESCRIPTION="A test upload.")


class FakeServices(setup_wizard.Services):
    def __init__(self, log=None, privacy_after="unlisted", probe=None):
        self.log = log if log is not None else []
        self._slack = fake_slack(self.log)
        self._yt = fake_youtube(self.log, privacy_after)
        self._live = FakeLive()
        self._listener = FakeListener()
        self.wiki_site = FakeMediaWiki()
        self.drive_site = FakeDrive()
        self.drive_logins: list = []                 # what successive Drive logins do, then DRIVE_TOKEN
        self.probe = probe or {}
        # what the runtime's suggest_streams gives; None: the wizard's own TBA lookup (deterministic: the runtime's
        # choice depends on today's date and looks channels up with yt-dlp)
        self.streams = None

    def suggest_streams(self, event_key):
        self.log.append(("suggest_streams", event_key))
        return self.streams(event_key) if callable(self.streams) else self.streams

    def tba_get(self, path, key):
        self.log.append(("tba", path))
        if key != GOOD_TBA:
            raise TBAError(f"TBA {path}: HTTP 401")
        if path == "/status":
            return {"current_season": 2026}
        if path == "/events/2026/simple":
            return EVENTS
        for e in EVENTS:
            if path == f"/event/{e['key']}/simple":
                return e
        if path == "/event/2026mttd":
            return {**MTTD, "webcasts": WEBCASTS}
        if path == "/event/2026mttd/teams/keys":
            return ["frc5026", "frc254", "frc1323"]
        raise TBAError(f"TBA {path}: HTTP 404")

    def probe_stream(self, url):
        self.log.append(("probe", url))
        if url in self.probe:
            return self.probe[url]
        if "WPfTIUtaTzo" in url or url == CHANNEL_LIVE:
            return {"status": "upcoming", "title": "Madtown Throwdown 2026 Friday Practice Matches",
                    "channel": "Madtown Throwdown", "handle": "@Madtown_Throwdown", "start": 1794016800}
        return {"status": "error", "message": "Unsupported URL"}

    def slack(self):
        return self._slack

    def youtube(self):
        return self._yt

    def live(self):
        return self._live

    def listener(self):
        return self._listener

    def wiki(self):
        """The real wiki module, its clients talking to the fake MediaWiki."""
        from streamsplitter.publish import wiki as real
        site = self.wiki_site

        class WikiClient(real.WikiClient):
            def __init__(self, api_url, username=None, password=None, **kw):
                super().__init__(api_url, username, password, session=site, min_interval=0, sleep=lambda s: None,
                                 **kw)
        return types.SimpleNamespace(**{**{k: getattr(real, k) for k in dir(real) if not k.startswith("__")},
                                        "WikiClient": WikiClient})

    def drive(self):
        """The real Drive module, its clients talking to the fake Drive and its login replaced."""
        from streamsplitter.publish import drive as real
        site, log, logins = self.drive_site, self.log, self.drive_logins

        class DriveClient(real.DriveClient):
            def __init__(self, token_json, client_config=None, on_token=None, **kw):
                super().__init__(token_json, client_config, on_token, session=site, chunk_size=256 * 1024, **kw)

        def login(client_config):
            log.append(("drive_login", client_config["installed"]["client_id"]))
            step = logins.pop(0) if logins else None
            if isinstance(step, Exception):
                raise step
            return DRIVE_TOKEN
        return types.SimpleNamespace(**{**{k: getattr(real, k) for k in dir(real) if not k.startswith("__")},
                                        "DriveClient": DriveClient, "login": login})

    def notifier_alarm_text(self):
        return "StreamSplitter on testpc has not checked in since Fri 10:00; it may have stopped."


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A config.toml and secrets.json of the test's own; never the real credential store or the real config."""
    monkeypatch.setenv("STREAMSPLITTER_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("STREAMSPLITTER_DATA", str(tmp_path / "data"))
    for k in ("TBA_API_KEY", "STREAMSPLITTER_DELIVER", "STREAMSPLITTER_WORK",
              *(f"STREAMSPLITTER_{s.upper()}" for s in config.SECRETS)):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(config, "_keyring", lambda: None)
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "client_secret_123-abc.apps.googleusercontent.com.json").write_text(json.dumps(CLIENT),
                                                                                    encoding="utf-8")
    monkeypatch.setattr(setup_wizard, "downloads_dir", lambda: downloads)
    (tmp_path / "Desktop").mkdir()
    monkeypatch.setattr(setup_wizard, "desktop_dir", lambda: tmp_path / "Desktop")
    return tmp_path


def secrets_file(tmp: Path) -> dict:
    return json.loads((tmp / "secrets.json").read_text(encoding="utf-8"))


def assert_no_secrets(io: ScriptedIO, tmp: Path) -> None:
    shown = io.text + "\n".join(io.opened)
    cfg = (tmp / "config.toml").read_text(encoding="utf-8")
    for s in SECRET_VALUES:
        assert s not in shown, f"a secret was shown: {s[:6]}..."
        assert s not in cfg, f"a secret is in config.toml: {s[:6]}..."


# The answers of a whole first run, step by step (the happy path).
TEAM_NO = [("team settings file from a teammate", "n")]
EVENT = [("Paste the key", GOOD_TBA), ("Event code", "madtown"), ("events matching", "1")]
STREAM = [("pick another", "")]
SLACK_APP = [("already have the StreamSplitter Slack app", ""), ("Basic Information page", ""),
             ("it is installed", ""), ("Bot User OAuth Token", GOOD_SLACK)]
SLACK = ([("Post your team's matches to Slack", "y"), ("Which teams", "5026")] + SLACK_APP
         + [("Slack channel", "errata"), ("Video size for Slack", ""), ("name or email", "ian"), ("Send them to Ian H", "y")])
YOUTUBE_ON = [("Upload matches to YouTube", "y"), ("Terms of Service", "y"), ("made it already", ""),
              ("client_secret", ""), ("team's channel", "y")]
FORMATS = [("Playlist name", ""), ("Who can see the playlist", ""), ("The title", ""), ("The description", "2"),
           ("line to add", "Go Karma!"), ("The description", "")]
NOTIFY = [("sample alert now", "y"), ("stopped-running alarm", "y"), ("get the sample alert", "y")]
LISTEN_NO = [("Slack start recordings on this computer", "")]    # Start from Slack: off unless chosen
WIKI_NO = [("team's wiki", "")]               # the team wiki step (setup --section wiki): off unless chosen
DRIVE_NO = [("Upload every match to Google Drive", "n")]
# the Drive step with YouTube's sign-in app saved: enable the API, log in, the folder, sharing, size, names
DRIVE_ON = [("Upload every match to Google Drive", "y"), ("Press Enter when it is enabled", ""),
            ("Keep the videos in this account's Drive", "y"), ("Name of the folder", ""),
            ("Press Enter when it is shared", ""), ("Video size in Google Drive", ""), ("Name the files like this", "y")]


def full_run(svc):
    script = (TEAM_NO + EVENT + STREAM + SLACK + YOUTUBE_ON + [("own team's matches to YouTube", "")] + FORMATS
              + DRIVE_NO + NOTIFY + LISTEN_NO + [("Send a test video", "y"), ("Slack test go", ""),
                                     ("can you see the test video", "y"), ("Delete the YouTube test", "y"),
                                     ("Start recording now", "y")])
    io = ScriptedIO(script)
    assert Wizard(io, svc).run() == 0
    assert not io.script, f"answers left over: {io.script}"
    return io


# ---------------------------------------------------------------------------------------------------------------------
# Whole runs

def test_happy_path_writes_the_design_sections_and_starts(env):
    svc = FakeServices()
    io = full_run(svc)
    ev, sl, yt, nt = (config.section(s) for s in ("event", "slack", "youtube", "notify"))
    assert ev["key"] == "2026mttd" and ev["name"] == "MadTown Throwdown" and ev["year"] == 2026
    assert ev["tag"] == "2026mttd" and ev["stream_url"] == CHANNEL_LIVE
    assert sl == {"teams": ["5026"], "enabled": True, "channel_id": "C0ERRATA", "channel_name": "errata",
                  "score_in_thread": True, "clip_height": 0}
    assert nt == {"slack_user": "U0IAN", "slack_user_name": "Ian H", "heartbeat_s": 60, "alarm_after_s": 420}
    # the team's own matches go to Slack and YouTube, every other match to YouTube only
    assert yt["enabled"] is True and yt["route"] == "all" and yt["version"] == "score"
    assert yt["privacy"] == "unlisted" and yt["playlist_id"] == "PL1" and yt["channel_id"] == "UC5026"
    assert yt["playlist_title"] == "2026 MadTown Throwdown" and yt["terms_accepted"]
    assert yt["title_format"] == "{match_title} - {year} {event_name}"
    assert yt["description_format"].endswith("\n\nGo Karma!") and "{tba_url}" in yt["description_format"]
    # what happened outside
    log = svc.log
    assert ("join", "C0ERRATA") in log
    assert ("create_playlist", "2026 MadTown Throwdown", "unlisted") in log
    hello = [e for e in log if e[0] == "dm"][0]
    # the hello DM lists the commands (publish/commands.py, Slack formatting), and so does the summary (plain)
    assert hello[1] == "U0IAN" and "`set stream auto`" in hello[2] and "#errata" in hello[2]
    assert "Ian H can reply to the StreamSplitter app in Slack with" in io.text
    assert "stream <link>" in io.text and "pause youtube" in io.text
    assert any(e[0] == "schedule" and e[1] == "D0IAN" for e in log)           # the sample alarm, to the DM channel
    # the test goes to the contact by DM unless the channel is chosen, so a test does not post where the team reads
    assert ("slack_upload", "D0IAN", "sample_match_2024mttd_qm6.mp4", "StreamSplitter setup test: sample video") \
        in log
    up = next(e for e in log if e[0] == "yt_upload")
    assert up[2] == "StreamSplitter setup test: Qualification 6 - 2024 Madtown Throwdown" and up[3] == "unlisted"
    assert ("add", "PL1", "vid1") in log and ("delete", "vid1") in log
    assert svc._live.started == [[CHANNEL_LIVE, "--event", "2026mttd"]]
    # secrets: stored, never shown, never in config.toml
    sec = secrets_file(env)
    assert sec["tba_key"] == GOOD_TBA and sec["slack_bot_token"] == GOOD_SLACK
    assert sec["youtube_token"] == TOKEN_JSON and json.loads(sec["youtube_client"]) == CLIENT
    assert_no_secrets(io, env)
    # the person was walked through it
    assert "https://www.thebluealliance.com/account" in io.opened and any("manifest_json" in u for u in io.opened)
    assert "Slack step 3 of 3" in io.text and "Request to Install" in io.text
    assert "The Blue Alliance lists this stream for the event: " + CHANNEL_LIVE in io.text
    assert "https://www.youtube.com/t/terms" in io.text
    assert "says the test video is unlisted" in io.text and "The test worked: Slack and YouTube" in io.text
    assert "process 4242" in io.text
    io.text.encode("ascii")                                                   # survives a cp1252 console


def test_slack_skipped(env):
    svc = FakeServices()
    script = (TEAM_NO + [("Paste the key", GOOD_TBA), ("Event code", "2026mttd"), ("Is this your event", "y")] + STREAM
              + [("Post your team's matches to Slack", "n")] + YOUTUBE_ON + FORMATS + DRIVE_NO
              + [("Send a test video", "y"), ("can you see", "y"), ("Delete the YouTube test", "n"),
                 ("Start recording now", "n")])
    io = ScriptedIO(script)
    Wizard(io, svc).run()
    assert not io.script
    assert config.section("slack") == {"enabled": False}
    assert config.section("youtube")["route"] == "all"                        # Slack is off: every match to YouTube
    assert "Slack is off, so warnings and errors only show" in io.text
    assert not any(e[0] in ("slack_upload", "dm") for e in svc.log)
    assert any(e[0] == "yt_upload" for e in svc.log) and ("delete", "vid1") not in svc.log
    assert svc._live.started == []
    assert "Slack:    off" in io.text
    assert_no_secrets(io, env)


def test_youtube_skipped(env):
    svc = FakeServices()
    script = (TEAM_NO + EVENT + STREAM + SLACK + [("Upload matches to YouTube", "n")] + DRIVE_NO + NOTIFY + LISTEN_NO
              + [("Send a test video", "y"), ("Slack test go", "2"), ("can you see", "y"), ("Start recording now", "n")])
    io = ScriptedIO(script)
    Wizard(io, svc).run()
    assert not io.script
    assert config.section("youtube") == {"enabled": False}
    assert [e[1] for e in svc.log if e[0] == "slack_upload"] == ["C0ERRATA"]      # the channel, when chosen
    assert not any(e[0] in ("login", "yt_upload", "create_playlist") for e in svc.log)
    assert any(e[0] == "slack_upload" for e in svc.log)
    assert "YouTube:  off" in io.text and "Terms of Service" not in io.text
    assert_no_secrets(io, env)


def test_bad_tba_key_then_a_good_one(env):
    config.set_secret("tba_key", "an-old-revoked-key")
    io = ScriptedIO([("Paste the key", "not-a-real-key"), ("Paste the key", GOOD_TBA)])
    Wizard(io, FakeServices()).run("tba")
    assert not io.script
    assert "refused the saved key" in io.text and "did not accept that key" in io.text
    assert secrets_file(env)["tba_key"] == GOOD_TBA
    assert GOOD_TBA not in io.text and "not-a-real-key" not in io.text


def test_rerun_offers_previous_values_and_a_menu(env):
    svc = FakeServices()
    full_run(svc)
    before = {s: config.section(s) for s in ("event", "slack", "youtube", "notify")}
    io = ScriptedIO([("What would you like to do", "4"), ("Which setting", "1"), ("Event code", ""),
                     ("Is this your event", ""), ("Restart it now", "n"),
                     ("What would you like to do", "4"), ("Which setting", "4"), ("Post your team's", ""),
                     ("Which teams", ""), ("Keep using this Slack connection", ""), ("Slack channel", ""),
                     ("Video size for Slack", ""), ("Keep sending them to Ian H", ""), ("Restart it now", "n"),
                     ("What would you like to do", "2"), ("What would you like to do", "3"),
                     ("What would you like to do", "9")])
    assert Wizard(io, svc).run() == 0
    assert not io.script
    prompts = "\n".join(io.prompts)
    assert "Event code, or part of its name [2026mttd]" in prompts
    assert "[5026]" in prompts and "Slack channel for the match videos [errata]" in prompts
    assert "Choose 1-9 [2]" in prompts                                       # running: "Show status" is the default
    assert {s: config.section(s) for s in before} == before
    assert "running with the old settings" in io.text and "last chunk: 12" in io.text
    assert svc._live.stopped == ["2026mttd"]
    assert_no_secrets(io, env)


def test_section_youtube_alone(env):
    config.set_secret("tba_key", GOOD_TBA)
    config.save_sections({"event": {"key": "2026mttd", "name": "MadTown Throwdown", "year": 2026, "tag": "2026mttd",
                                    "stream_url": CHANNEL_LIVE},
                          "slack": {"enabled": False, "teams": ["5026"]}})
    other = tmp_client = env / "elsewhere" / "my client.json"
    other.parent.mkdir()
    tmp_client.write_text(json.dumps(CLIENT), encoding="utf-8")
    io = ScriptedIO([("Upload matches to YouTube", "y"), ("Terms of Service", "y"), ("made it already", ""),
                     ("client_secret", "nonsense.json"), ("client_secret", f'"{other}"'),
                     ("team's channel", "y"), ("Playlist name", "MadTown <2026>"), ("Playlist name", ""),
                     ("Who can see the playlist", ""),
                     ("The title", "2"), ("New title format", "{nope}"), ("The title", "2"),
                     ("New title format", "{match_title} | {event_name}"), ("The title", ""),
                     ("The description", "")])
    Wizard(io, FakeServices()).run("youtube")
    assert not io.script
    yt = config.section("youtube")
    assert yt["title_format"] == "{match_title} | {event_name}" and yt["route"] == "all"
    assert yt["playlist_title"] == "2026 MadTown Throwdown" and yt["playlist_event"] == "2026mttd"
    assert config.section("slack") == {"enabled": False, "teams": ["5026"]}   # untouched
    assert config.section("event")["stream_url"] == CHANNEL_LIVE
    assert "That file cannot be used" in io.text and "unknown field {nope}" in io.text
    assert "That playlist cannot be used" in io.text
    assert "Qualification 12 | MadTown Throwdown" in io.text                  # the new format, shown filled in
    assert_no_secrets(io, env)


def test_terms_declined_means_no_youtube(env):
    io = ScriptedIO([("Upload matches to YouTube", "y"), ("Terms of Service", "")])
    svc = FakeServices()
    Wizard(io, svc).run("youtube")
    assert config.section("youtube") == {"enabled": False}
    assert not any(e[0] == "login" for e in svc.log)


def test_locked_private_test_upload_is_reported(env):
    svc = FakeServices(privacy_after="private")
    config.set_secret("youtube_token", TOKEN_JSON)
    config.set_secret("youtube_client", json.dumps(CLIENT))
    config.save_sections({"youtube": {"enabled": True, "playlist_id": "PL1", "playlist_title": "2026 MadTown Throwdown",
                                      "privacy": "unlisted", "terms_accepted": "2026-11-04"},
                          "slack": {"enabled": False}})
    io = ScriptedIO([("Send a test video", "y"), ("can you see", "y"), ("Delete the YouTube test", "y")])
    Wizard(io, svc).run("test")
    assert not io.script
    assert "PRIVATE instead of unlisted" in io.text and "Not working yet: YouTube" in io.text
    assert "The test worked" not in io.text


def test_stream_checks(env):
    config.save_sections({"event": {"key": "2026xx", "start_date": "2026-11-06", "end_date": "2026-11-08"}})
    svc = FakeServices(probe={
        "https://twitch.tv/nobody": {"status": "error", "message": "nobody does not exist"},
        "https://www.twitch.tv/quiet": {"status": "offline", "message": "The channel is not currently live"},
        "https://youtube.com/watch?v=abcdefghijk": {"status": "live", "title": "Day 1", "channel": "Event"}})
    io = ScriptedIO([("Stream link", "twitch.tv/nobody"), ("Use it anyway", ""),
                     ("Stream link", "https://www.twitch.tv/quiet"), ("Use this link", "y")])
    Wizard(io, svc).run("stream")                    # no TBA key: no suggestions, the link is typed
    assert config.section("event")["stream_url"] == "https://www.twitch.tv/quiet"
    assert "That link does not work: nobody does not exist" in io.text and "waits for the stream" in io.text
    io = ScriptedIO([("Stream link", "youtube.com/watch?v=abcdefghijk")])
    Wizard(io, svc).run("stream")
    assert "Live now: Day 1 (Event)" in io.text and "this is one video" in io.text


def test_cancel_keeps_what_was_saved(env):
    io = ScriptedIO(TEAM_NO + [("Paste the key", GOOD_TBA), ("Event code", "2026mttd"), ("Is this your event", "y")])

    def stop(shown, question):
        if not io.script:
            raise setup_wizard.Cancelled()
        return ScriptedIO._line(io, shown, question)
    io._line = stop
    assert setup_wizard.main(io=io, svc=FakeServices(), background=Background([])) == 130
    assert config.section("event")["key"] == "2026mttd" and "Setup stopped" in io.text


# ---------------------------------------------------------------------------------------------------------------------
# Background downloads

def test_background_reports_failures_with_fixes():
    def ok(t):
        t.working = True
        return "installed"

    def broken(t):
        raise SetupFailed("downloading ffmpeg failed (timed out)", "Run setup again to retry.")

    def optional(t):
        raise RuntimeError("no wheel for this computer")
    first = Task("detection packages", ok)
    bg = Background([first, Task("ffmpeg", broken), Task("Deno", optional, required=False, after=first)]).start()
    io = ScriptedIO([])
    bg.wait(io, poll_s=0.01)
    assert [t.state for t in bg.tasks] == ["done", "failed", "failed"]
    assert [t.name for t in bg.failures(required_only=True)] == ["ffmpeg"]
    assert "ffmpeg: FAILED - downloading ffmpeg failed (timed out)" in io.text
    assert "Fix: Run setup again to retry." in io.text and "(optional)" in io.text
    bg.report(io)
    assert "detection packages: ready" in io.out[-1]


def test_install_ffmpeg_unpacks_and_checks_the_checksum(env, monkeypatch):
    exe = ".exe" if os.name == "nt" else ""
    z = env / "ff.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr(f"ffmpeg-9.0-essentials_build/bin/ffmpeg{exe}", b"FFMPEG")
        zf.writestr(f"ffmpeg-9.0-essentials_build/bin/ffprobe{exe}", b"FFPROBE")
        zf.writestr("ffmpeg-9.0-essentials_build/README.txt", b"hi")
    good = hashlib.sha256(z.read_bytes()).hexdigest()
    sums = {"sha": good}

    def fetch(url, dest=None, task=None, label=""):
        if url.endswith(".sha256"):
            return url, f"{sums['sha']}  ff.zip\n".encode()
        dest.write_bytes(z.read_bytes())
        return url, b""
    bin_dir = env / "bin"
    monkeypatch.setattr(setup_wizard, "_fetch", fetch)
    monkeypatch.setattr(setup_wizard, "ffmpeg_downloads", lambda: [("https://example.invalid/ff.zip",
                                                                    ["ffmpeg", "ffprobe"])])
    monkeypatch.setattr(setup_wizard, "tools_bin", lambda: bin_dir)
    monkeypatch.setattr(setup_wizard, "prepare_environment", lambda: None)
    monkeypatch.setattr(setup_wizard, "_ffmpeg_version",
                        lambda: "9.0 (test)" if (bin_dir / f"ffprobe{exe}").is_file() else None)
    t = Task("ffmpeg", setup_wizard.install_ffmpeg)
    assert setup_wizard.install_ffmpeg(t) == "ffmpeg 9.0 (test), downloaded"
    assert (bin_dir / f"ffmpeg{exe}").read_bytes() == b"FFMPEG" and not (bin_dir / "README.txt").exists()
    for p in bin_dir.iterdir():
        p.unlink()
    sums["sha"] = "0" * 64
    with pytest.raises(SetupFailed) as e:
        setup_wizard.install_ffmpeg(t)
    assert "damaged" in str(e.value) and "winget install Gyan.FFmpeg" in e.value.fix
    assert not list(bin_dir.iterdir())


def test_install_detection_uses_the_detect_extra(monkeypatch):
    calls = []
    monkeypatch.setattr(setup_wizard.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(setup_wizard, "find_uv", lambda: "uv")
    monkeypatch.setattr(setup_wizard, "_run_logged", lambda cmd, log, task, what: calls.append(cmd))
    checked = []
    monkeypatch.setattr(setup_wizard, "_import_problem", lambda mods, check="": checked.append(check))
    assert setup_wizard.install_detection(Task("detection packages", None)) == "installed"
    assert calls[0][:5] == ["uv", "pip", "install", "--python", sys.executable]
    assert "onnxruntime>=1.20" in calls[0] and "rapidocr>=3.10" in calls[0]
    assert "_engines()" in checked[0]                  # the OCR engines are loaded, not just imported
    monkeypatch.setattr(setup_wizard, "_import_problem",
                        lambda mods, check="": "ImportError: DLL load failed while importing")
    with pytest.raises(SetupFailed) as e:
        setup_wizard.install_detection(Task("detection packages", None))
    if os.name == "nt":
        assert "vc_redist" in e.value.fix


@pytest.mark.skipif(os.name != "nt", reason="Windows' path limit")
def test_a_folder_too_deep_for_windows_says_to_unzip_into_a_short_one(monkeypatch):
    """2026-11-06, a fresh copy 175 characters deep: the OCR engine's DLLs did not load, though `import rapidocr`
    passed, so the setup said "installed" and a recording would have failed on event day."""
    monkeypatch.setattr(setup_wizard.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(setup_wizard, "_import_problem", lambda mods, check="": (
        "ImportError: DLL load failed while importing lib: The filename or extension is too long."))
    monkeypatch.setattr(setup_wizard, "ROOT", Path("C:/Users/someone/" + "deep/" * 30 + "StreamSplitter"))
    with pytest.raises(SetupFailed) as e:
        setup_wizard.install_detection(Task("detection packages", None))
    assert "C:\\StreamSplitter" in e.value.fix and "vc_redist" not in e.value.fix
    assert "C:\\StreamSplitter" in setup_wizard.path_too_long()
    monkeypatch.setattr(setup_wizard, "ROOT", Path("C:/StreamSplitter"))
    assert setup_wizard.path_too_long() is None


# ---------------------------------------------------------------------------------------------------------------------
# Small pieces

@pytest.mark.parametrize("given, want", [
    ("youtube.com/@Madtown_Throwdown", CHANNEL_LIVE),
    ("https://www.youtube.com/@Madtown_Throwdown/streams", CHANNEL_LIVE),
    (" 'https://www.youtube.com/channel/UCWVgs9ep6oWNVa6Rdpza_cg' ",
     "https://www.youtube.com/channel/UCWVgs9ep6oWNVa6Rdpza_cg/live"),
    (CHANNEL_LIVE, CHANNEL_LIVE),
    ("https://www.youtube.com/watch?v=WPfTIUtaTzo", "https://www.youtube.com/watch?v=WPfTIUtaTzo"),
    ("twitch.tv/madtownthrowdown", "https://twitch.tv/madtownthrowdown"),
])
def test_normalize_stream_url(given, want):
    assert setup_wizard.normalize_stream_url(given) == want


def test_parse_teams():
    assert setup_wizard.parse_teams("5026, frc254 254b;1323 5026") == ["5026", "254", "254B", "1323"]
    assert setup_wizard._bad_teams("5026, abc") == "not a team number: ABC"


def test_extra_requirements_from_pyproject():
    assert "numpy" in setup_wizard.extra_requirements("detect")
    assert setup_wizard.extra_requirements("deno") == ["deno>=2.3"]


def test_prepare_environment_puts_the_venv_scripts_first(monkeypatch):
    monkeypatch.setenv("PATH", os.pathsep.join(["/somewhere/else"]))
    monkeypatch.setenv("SSL_CERT_FILE", "x")
    monkeypatch.delenv("SSL_CERT_FILE")
    setup_wizard.prepare_environment()
    scripts = os.path.normcase(os.path.normpath(setup_wizard.sysconfig.get_path("scripts")))
    first = os.path.normcase(os.path.normpath(os.environ["PATH"].split(os.pathsep)[0]))
    assert first == scripts
    setup_wizard.prepare_environment()                   # twice: not added again
    assert os.environ["PATH"].count(os.pathsep) == 1
    if os.name != "nt":
        assert os.environ.get("SSL_CERT_FILE")


def test_cli_setup_and_start_status_stop(env, monkeypatch, capsys):
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    got = {}
    monkeypatch.setattr(setup_wizard, "main", lambda **kw: got.update(kw) or 0)
    assert cli.main(["setup", "--section", "youtube"]) == 0
    assert got == {"section": "youtube", "install_only": False}
    assert cli.main(["setup", "--section", "team"]) == 0 and got["section"] == "team"
    assert cli.main(["start"]) == 1                                          # nothing set up yet
    live = FakeLive()
    monkeypatch.setattr(cli, "_live", lambda: live)
    config.save_sections({"event": {"key": "2026mttd", "tag": "mttd", "stream_url": CHANNEL_LIVE}})
    assert cli.main(["start"]) == 0
    assert live.started == [[CHANNEL_LIVE, "--event", "2026mttd", "--tag", "mttd"]]
    assert cli.main(["status"]) == 0 and "running: True" in capsys.readouterr().out
    assert cli.main(["stop"]) == 0 and live.stopped == ["mttd"]
    assert cli.main(["stop"]) == 0 and "not running" in capsys.readouterr().out


def test_invisible_characters_in_answers_are_ignored(env):
    # PowerShell pipes a byte-order mark; a paste can carry zero-width or no-break spaces
    config.set_secret("tba_key", GOOD_TBA)
    io = ScriptedIO([("Event code", "﻿mad​town\xa0"), ("events matching", "﻿")])
    Wizard(io, FakeServices()).run("event")
    assert config.section("event")["key"] == "2026mttd"


def test_single_webcast_on_a_shared_channel(env):
    """One video for the event: that video is recommended, not the channel's /live page, which on a channel that
    streams several events can show another one (seen on New England FIRST's channel, 2026-11-04)."""
    config.set_secret("tba_key", GOOD_TBA)
    config.save_sections({"event": {"key": "2026nhalt1", "name": "Girls Behind the Glass", "year": 2026,
                                    "start_date": "2026-11-08", "end_date": "2026-11-08"}})
    svc = FakeServices(probe={
        "https://www.youtube.com/watch?v=OzGARHxTJZ8": {"status": "upcoming", "title": "Girls Behind the Glass 2026",
                                                        "channel": "New England FIRST", "handle": "@NewEnglandFIRST"},
        "https://www.youtube.com/@NewEnglandFIRST/live": {"status": "upcoming", "title": "Battle of the Bay 2026",
                                                          "channel": "New England FIRST"}})
    real_get = svc.tba_get

    def tba_get(path, key):
        if path == "/event/2026nhalt1":
            return {"key": "2026nhalt1", "webcasts": [{"type": "youtube", "channel": "OzGARHxTJZ8",
                                                       "date": "2026-11-08"}]}
        return real_get(path, key)
    svc.tba_get = tba_get
    io = ScriptedIO([("pick another", "2"), ("Use this link", "n"), ("pick another", "")])
    Wizard(io, svc).run("stream")
    assert not io.script
    assert "1) The stream of 2026-11-08 (www.youtube.com/watch?v=OzGARHxTJZ8; recommended)" in io.text
    assert "does not look like Girls Behind the Glass" in io.text
    assert config.section("event")["stream_url"] == "https://www.youtube.com/watch?v=OzGARHxTJZ8"


def test_changing_the_event_asks_for_its_stream(env):
    svc = FakeServices()
    full_run(svc)
    io = ScriptedIO([("Event code", "2026casj"), ("Is this your event", "y"),
                     ("Stream link", "https://www.twitch.tv/firstinspires"), ("Use it anyway", "y")])
    svc.probe["https://www.twitch.tv/firstinspires"] = {"status": "video", "title": "old VOD"}
    Wizard(io, svc).run("event")
    assert not io.script
    ev = config.section("event")
    assert ev["key"] == "2026casj" and ev["tag"] == "2026casj"
    assert ev["stream_url"] == "https://www.twitch.tv/firstinspires"      # not the old event's link
    assert "Stream link (YouTube or Twitch):" in "\n".join(io.prompts)     # no old link offered as the default
    assert "make its YouTube playlist too" in io.text and "Nothing is uploaded until then" in io.text
    assert "lists no stream for this event yet" in io.text
    yt = config.section("youtube")                    # the old event's playlist is forgotten (lead, 2026-11-04)
    assert yt["enabled"] and not {"playlist_id", "playlist_title", "playlist_event"} & set(yt)


def test_own_matches_can_stay_off_youtube(env):
    config.set_secret("tba_key", GOOD_TBA)
    config.set_secret("slack_bot_token", GOOD_SLACK)
    config.save_sections({"event": {"key": "2026mttd", "name": "MadTown Throwdown", "year": 2026, "tag": "2026mttd",
                                    "stream_url": CHANNEL_LIVE},
                          "slack": {"enabled": True, "teams": ["5026"], "channel_id": "C0ERRATA",
                                    "channel_name": "errata"}})
    io = ScriptedIO(YOUTUBE_ON + [("own team's matches to YouTube too (they also go to Slack)? [Y/n]", "n")]
                    + FORMATS)
    Wizard(io, FakeServices()).run("youtube")
    assert not io.script and config.section("youtube")["route"] == "others"
    assert "the matches without your teams upload" in io.text
    Wizard(ScriptedIO([]), FakeServices()).summary()


def test_summary_says_own_matches_go_to_both(env):
    svc = FakeServices()
    io = full_run(svc)
    assert "matches with 5026 are posted to #errata (and go to YouTube too)" in io.text
    assert "YouTube:  every match, unlisted" in io.text
    assert "every match upload (yours go to Slack too)" in io.text


def test_slack_app_already_created_opens_the_app_list_not_the_create_page(env):
    """A workspace short of app slots must not get a second StreamSplitter app (user, 2026-11-04): when the app
    exists, the wizard opens the list of Slack apps for its token instead of the create-from-manifest page."""
    svc = FakeServices()
    io = ScriptedIO([("Post your team's matches to Slack", "y"), ("Which teams", "5026"),
                     ("already have the StreamSplitter Slack app", "2"), ("Bot User OAuth Token", GOOD_SLACK),
                     ("Slack channel", "errata"), ("Video size for Slack", ""), ("name or email", "ian"),
                     ("Send them to Ian H", "y")])
    Wizard(io, svc).run("slack")
    assert not io.script
    assert "https://api.slack.com/apps" in io.opened
    assert not any("manifest_json" in u for u in io.opened)
    assert config.section("slack")["channel_id"] == "C0ERRATA"
    assert_no_secrets(io, env)


# ---------------------------------------------------------------------------------------------------------------------
# Fixes of 2026-11-04 (review, lead and user)

def youtube_ready(playlist=None):
    """A saved event (Slack off) and a FakeServices whose channel has `playlist` already."""
    config.set_secret("tba_key", GOOD_TBA)
    config.save_sections({"event": {"key": "2026mttd", "name": "MadTown Throwdown", "year": 2026, "tag": "2026mttd",
                                    "stream_url": CHANNEL_LIVE},
                          "slack": {"enabled": False, "teams": ["5026"]}})
    svc = FakeServices()
    svc._yt = fake_youtube(svc.log, playlist=playlist)
    return svc


def test_private_chosen_for_an_existing_unlisted_playlist(env):
    """User, 2026-11-04: ask who can see the playlist and its videos (one answer for both). An existing playlist
    with another privacy is changed to it; the test upload stays unlisted (it is deleted after)."""
    svc = youtube_ready({"id": "PL9", "title": "2026 MadTown Throwdown", "privacy": "unlisted",
                         "url": "https://www.youtube.com/playlist?list=PL9"})
    io = ScriptedIO(YOUTUBE_ON + [("Playlist name", ""), ("Who can see the playlist and its videos", "3"),
                                  ("The title", ""), ("The description", "")])
    Wizard(io, svc).run("youtube")
    assert not io.script
    yt = config.section("youtube")
    assert yt["privacy"] == "private" and yt["playlist_id"] == "PL9"
    assert ("set_playlist_privacy", "PL9", "private") in svc.log and not any(e[0] == "create_playlist"
                                                                            for e in svc.log)
    assert "Changed the playlist from unlisted to private" in io.text and "as private videos" in io.text
    assert "Unlisted: only people with the link (recommended)" in io.text
    io = ScriptedIO([("Send a test video", "y"), ("can you see", "y"), ("Delete the YouTube test", "y")])
    Wizard(io, svc).run("test")
    assert next(e for e in svc.log if e[0] == "yt_upload")[3] == "unlisted"


def test_public_playlist_is_made_public_with_a_warning(env):
    svc = youtube_ready()
    io = ScriptedIO(YOUTUBE_ON + [("Playlist name", ""), ("Who can see the playlist", "2"), ("The title", ""),
                                  ("The description", "")])
    Wizard(io, svc).run("youtube")
    assert ("create_playlist", "2026 MadTown Throwdown", "public") in svc.log
    assert config.section("youtube")["privacy"] == "public" and "found by anyone searching YouTube" in io.text
    io = ScriptedIO(YOUTUBE_ON[:1] + [("Use the Google sign-in app", "")]
                    + [("Playlist name", ""), ("Who can see the playlist", ""), ("The title", ""),
                       ("The description", "")])
    Wizard(io, svc).run("youtube")                    # run again: the saved choice is the default
    assert "Choose 1-3 [2]" in "\n".join(io.prompts)


def test_restart_after_a_settings_change_waits_for_the_old_run(env):
    """Review 2026-11-04: "stop it and start it again" could start a second run while the first was still sending.
    Now the wizard offers the restart, asks the old run (the old event's, when the event changed) to stop, waits
    until it has really stopped, then starts."""
    svc = FakeServices()
    full_run(svc)
    live = svc._live
    live.calls.clear()
    real_stop = live.request_stop

    def stop_slowly(tag):                             # a real run says "stopping" until its last upload is done
        real_stop(tag)
        live.status = {**live.status, "running": True, "stopping": True}
    live.request_stop = stop_slowly
    io = ScriptedIO([("What would you like to do", "4"), ("Which setting", "1"), ("Event code", "2026casj"),
                     ("Is this your event", "y"), ("Stream link", "https://www.twitch.tv/firstinspires"),
                     ("Use it anyway", "y"), ("Restart it now", "y"), ("What would you like to do", "9")])
    svc.probe["https://www.twitch.tv/firstinspires"] = {"status": "video", "title": "old VOD"}
    assert Wizard(io, svc).run() == 0
    assert not io.script
    assert live.calls == [("request_stop", "2026mttd"), ("wait_stopped", "2026mttd"), ("start_detached",)]
    assert live.started[-1] == ["https://www.twitch.tv/firstinspires", "--event", "2026casj"]


def test_start_waits_for_a_run_that_is_still_stopping(env):
    svc = FakeServices()
    full_run(svc)
    svc._live.status = {"running": True, "stopping": True}
    svc._live.calls.clear()
    io = ScriptedIO([])
    assert Wizard(io, svc).start() is True
    assert svc._live.calls == [("wait_stopped", "2026mttd"), ("start_detached",)]
    assert "still stopping" in io.text


def test_slack_video_size_choice(env):
    """The fix Slack's "too large" error now points to: setup --section slack, the smaller 720p copies."""
    io = ScriptedIO([("Post your team's matches to Slack", "y"), ("Which teams", "5026"),
                     ("already have the StreamSplitter Slack app", "2"), ("Bot User OAuth Token", GOOD_SLACK),
                     ("Slack channel", "errata"), ("Video size for Slack", "2"), ("name or email", "ian"),
                     ("Send them to Ian H", "y")])
    Wizard(io, FakeServices()).run("slack")
    assert not io.script and config.section("slack")["clip_height"] == 720
    from streamsplitter.publish import slack
    assert "setup --section slack" in slack._FIX_SIZE


def test_first_setup_assumes_no_team_or_channel(env):
    """Other teams install this too: a first setup offers nobody's team number or channel as the answer to Enter
    (it used to offer the authors'), so Enter alone is asked again."""
    io = ScriptedIO([("Post your team's matches to Slack", "y"), ("Which teams", ""), ("Which teams", "254"),
                     ("already have the StreamSplitter Slack app", "2"), ("Bot User OAuth Token", GOOD_SLACK),
                     ("Slack channel", ""), ("Slack channel", "errata"), ("Video size for Slack", ""),
                     ("name or email", "ian"), ("Send them to Ian H", "y")])
    Wizard(io, FakeServices()).run("slack")
    assert not io.script
    prompts = "\n".join(io.prompts)
    assert "[5026]" not in prompts and "[errata]" not in prompts
    assert config.section("slack")["teams"] == ["254"] and config.section("slack")["channel_name"] == "errata"
    assert setup_wizard.DEFAULT_TEAMS == [] and setup_wizard.DEFAULT_CHANNEL == ""


def test_config_typo_is_one_clear_line(env, monkeypatch, capsys):
    """Lead, 2026-11-04: a typo in config.toml crashed every command with a raw TOML error."""
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))      # cli.main puts the installer's tools on PATH
    path = env / "config.toml"
    path.write_text('[event]\nkey = "2026mttd"\nname = Madtown Throwdown\n', encoding="utf-8")
    with pytest.raises(config.ConfigError) as e:
        config.section("event")
    assert "line 3" in str(e.value) and "Invalid value" in str(e.value)
    assert cli.main(["status"]) == 2
    err = capsys.readouterr().err
    assert "config.toml" in err and "mistake on line 3" in err and "run `streamsplitter setup` again" in err
    assert "Traceback" not in err
    path.write_text('\ufeff[event]\nkey = "2026mttd"\n', encoding="utf-8")       # Notepad's byte-order mark is fine
    assert config.section("event") == {"key": "2026mttd"}


def test_client_file_by_name_quoted_or_with_tilde(env, monkeypatch):
    """User, 2026-11-04: typing only the file name failed (FileNotFoundError): Downloads had been moved to
    D:\\Downloads. A bare name or relative path is looked for in the current folder, then Downloads."""
    name = "client_secret_123-abc.apps.googleusercontent.com.json"
    dl = setup_wizard.downloads_dir()                 # the test's own (env fixture)
    here = env / "here"
    here.mkdir()
    monkeypatch.chdir(here)
    assert setup_wizard.find_user_file(name) == dl / name
    assert setup_wizard.find_user_file(name.removesuffix(".json")) == dl / name      # Explorer hides ".json"
    assert setup_wizard.find_user_file(f'"{dl / name}"') == dl / name                  # "Copy as path"
    monkeypatch.setenv("HOME", str(env))
    monkeypatch.setenv("USERPROFILE", str(env))
    assert setup_wizard.find_user_file(f"~/Downloads/{name}") == env / "Downloads" / name
    (here / name).write_text("{}", encoding="utf-8")
    assert setup_wizard.find_user_file(name) == here / name                           # the current folder first
    (here / name).unlink()
    io = ScriptedIO([("made it already", "2"), ("client_secret", name)])
    w = Wizard(io, FakeServices())
    assert w.youtube_client_config(w.svc.youtube()) == CLIENT and "Saved the Google sign-in app" in io.text


def test_downloads_folder_from_the_system(env, monkeypatch):
    monkeypatch.setenv("HOME", str(env))
    monkeypatch.setenv("USERPROFILE", str(env))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(env / "xdg"))
    assert setup_wizard._xdg_downloads() is None
    (env / "xdg").mkdir()
    (env / "xdg" / "user-dirs.dirs").write_text('# written by xdg-user-dirs-update\nXDG_DESKTOP_DIR="$HOME/Desktop"\n'
                                               'XDG_DOWNLOAD_DIR="$HOME/Stuff/Incoming"\n', encoding="utf-8")
    assert setup_wizard._xdg_downloads() == env / "Stuff" / "Incoming"
    if os.name == "nt":                               # the Known Folder API: wherever this PC keeps Downloads
        got = setup_wizard._windows_downloads()
        assert got is not None and got.is_absolute()
    assert setup_wizard._xdg_downloads("XDG_DESKTOP_DIR") == env / "Desktop"


# ---------------------------------------------------------------------------------------------------------------------
# Walkthroughs for teams setting up from scratch, and the team settings file (2026-11-04)

def google_pages_opened(io: ScriptedIO) -> list[str]:
    return [u for u in io.opened if u.startswith(CONSOLE)]


def download(path: Path, cfg: dict):
    """A scripted answer that "downloads" a client file while the wizard waits for it, then presses Enter."""
    def answer():
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return ""
    return answer


def login_error(text):
    from streamsplitter.publish.youtube import LoginError
    return LoginError(text)


def test_youtube_walkthrough_opens_each_page_and_picks_up_the_download(env):
    """A team's first time: one Google page at a time, the project picked by its ID, and the client file taken from
    the Downloads folder by itself (an older client file already there is not mistaken for it)."""
    svc = youtube_ready()
    dl = env / "Downloads"
    io = ScriptedIO([("Upload matches to YouTube", "y"), ("Terms of Service", "y"), ("made it already", "1"),
                     ("Project ID", "My Project!"), ("Project ID", "streamsplitter-5026"),
                     ("Press Enter when it is enabled", ""), ("Press Enter when it is created", ""),
                     ("says In production", ""),
                     ("Waiting for the downloaded file", download(dl / "client_secret_new.json", CLIENT_P)),
                     ("team's channel", "y"), ("Playlist name", ""), ("Who can see", ""), ("The title", ""),
                     ("The description", "")])
    Wizard(io, svc).run("youtube")
    assert not io.script
    q = "?project=streamsplitter-5026"
    assert google_pages_opened(io) == [CONSOLE + "projectcreate", CONSOLE + "apis/library/youtube.googleapis.com" + q,
                                       CONSOLE + "auth/branding" + q, CONSOLE + "auth/audience" + q,
                                       CONSOLE + "auth/clients" + q]
    assert "does not look like a project ID" in io.text
    assert "Publish app" in io.text and "logins stop" in io.text and "Desktop app" in io.text
    assert "Got the client file: client_secret_new.json" in io.text
    assert json.loads(secrets_file(env)["youtube_client"]) == CLIENT_P
    assert config.section("youtube")["playlist_id"] == "PL1"
    assert_no_secrets(io, env)


def test_web_client_is_rejected_and_a_desktop_client_taken(env):
    svc = youtube_ready()
    dl = env / "Downloads"
    io = ScriptedIO([("Upload matches to YouTube", "y"), ("Terms of Service", "y"), ("made it already", "1"),
                     ("Project ID", ""), ("enabled", ""), ("created", ""), ("In production", ""),
                     ("Waiting for the downloaded file", download(dl / "client_secret_web.json", WEB_CLIENT)),
                     ("Waiting for the downloaded file", download(dl / "client_secret_desktop.json", CLIENT_P)),
                     ("team's channel", "y"), ("Playlist name", ""), ("Who can see", ""), ("The title", ""),
                     ("The description", "")])
    Wizard(io, svc).run("youtube")
    assert not io.script
    assert "client_secret_web.json is from a 'Web application' client" in io.text
    assert "project picker" in io.text                       # no project ID typed: check the picker instead
    assert google_pages_opened(io)[-2:] == [CONSOLE + "auth/clients", CONSOLE + "auth/clients"]
    assert json.loads(secrets_file(env)["youtube_client"]) == CLIENT_P
    assert_no_secrets(io, env)


def test_access_denied_explained_then_the_login_works(env):
    """User, 2026-11-04: "Error 403: access_denied ... The app is currently being tested". The app was in Testing;
    the fix is Audience > Publish app. Both ways it can show: the login comes back refused, or Google shows its error
    page (the login never comes back) and the person types what it says."""
    svc = FakeServices()
    yt_mod = svc._yt = fake_youtube(svc.log, logins=[
        login_error("the YouTube login was cancelled or refused (access_denied)")])
    io = ScriptedIO([("Press Enter when that is done", ""), ("team's channel", "y")])
    yt, ch = Wizard(io, svc).youtube_login(yt_mod, CLIENT_P)
    assert ch["id"] == "UC5026" and not io.script
    assert "still in Testing" in io.text and "Publish app" in io.text
    assert io.opened == [CONSOLE + "auth/audience?project=streamsplitter-5026"]
    assert [e[0] for e in svc.log].count("login") == 2

    config.set_secret("youtube_token", None)
    release = threading.Event()
    svc._yt = yt_mod = fake_youtube(svc.log, logins=[lambda: release.wait(10) and TOKEN_JSON])
    io = ScriptedIO([("Waiting for the login", "Error 403: access_denied. The app is currently being tested"),
                     ("Press Enter when that is done", ""), ("team's channel", "y")])
    w = Wizard(io, svc)
    w.LOGIN_GRACE_S = 0.2
    try:
        yt, ch = w.youtube_login(yt_mod, CLIENT_P)
    finally:
        release.set()
    assert ch["id"] == "UC5026" and not io.script
    assert "only lets in the testers" in io.text and io.opened == [CONSOLE + "auth/audience?project=streamsplitter-5026"]
    assert secrets_file(env)["youtube_token"] == TOKEN_JSON


def test_login_problems_each_get_their_fix(env):
    """A redirect mismatch needs a Desktop client (its file is waited for again); the API not enabled opens its
    page and keeps the login; an account with no channel logs in again."""
    svc = FakeServices()
    dl = env / "Downloads"
    svc._yt = yt_mod = fake_youtube(svc.log, logins=[login_error("Error 400: redirect_uri_mismatch")],
                                    channels=[PermanentError("YouTube channel lookup: the YouTube Data API v3 is not "
                                                             "enabled in the Google Cloud project; enable it"),
                                              PermanentError("YouTube the Google account that logged in has no "
                                                             "YouTube channel; pick the team's channel")])
    io = ScriptedIO([("Waiting for the downloaded file", download(dl / "client_secret_2.json", CLIENT_P)),
                     ("Press Enter when it is enabled", ""), ("Log in again", "y"), ("team's channel", "y")])
    yt, ch = Wizard(io, svc).youtube_login(yt_mod, CLIENT_P)
    assert ch["id"] == "UC5026" and not io.script
    assert "not a working Desktop app client" in io.text and "has no YouTube channel" in io.text
    assert io.opened == [CONSOLE + "auth/clients?project=streamsplitter-5026",
                         CONSOLE + "apis/library/youtube.googleapis.com?project=streamsplitter-5026"]
    assert [e[0] for e in svc.log].count("login") == 3        # the mismatch, the first good one, after "no channel"


def test_wrong_channel_is_caught_when_the_team_channel_is_known(env):
    svc = FakeServices()
    team = {"id": "UC9999", "title": "Team Channel", "url": "https://www.youtube.com/@team"}
    svc._yt = yt_mod = fake_youtube(svc.log, channels=[KARMA, team])
    io = ScriptedIO([("Log in again", "y")])
    yt, ch = Wizard(io, svc).youtube_login(yt_mod, CLIENT_P, expect="UC9999", expect_title="Team Channel")
    assert ch == team and not io.script
    assert "That is not the team's channel (Team Channel)" in io.text
    assert "Is that the team's channel" not in io.text            # known: no need to ask


def test_existing_slack_app_missing_im_history_is_walked_through(env):
    """The app needs im:history since 2026-11-04 (replies to the app). An older app is shown how to add the scope
    and reinstall; the token stays the same. REQUIRED_SCOPES is used when slack.py has it."""
    svc = FakeServices()
    svc._slack = fake_slack(svc.log, required_scopes=["chat:write", "im:history"],
                            auths=[{"scopes": ["chat:write"]}, {"scopes": ["chat:write", "im:history"]}])
    io = ScriptedIO([("Post your team's matches to Slack", "y"), ("Which teams", "5026"),
                     ("already have the StreamSplitter Slack app", "2"), ("Bot User OAuth Token", GOOD_SLACK),
                     ("Press Enter when it is reinstalled", ""), ("Slack channel", "errata"), ("Video size", ""),
                     ("name or email", "ian"), ("Send them to Ian H", "y")])
    Wizard(io, svc).run("slack")
    assert not io.script
    assert "lacks the permission im:history" in io.text and "Reinstall to Workspace" in io.text
    assert "The token stays the same" in io.text and "every permission it needs now" in io.text
    assert io.opened == ["https://api.slack.com/apps", "https://api.slack.com/apps"]
    assert not any("manifest_json" in u for u in io.opened)    # never a second app
    assert_no_secrets(io, env)


def test_slack_new_app_can_wait_for_an_admin(env):
    io = ScriptedIO([("Post your team's matches to Slack", "y"), ("Which teams", "5026"),
                     ("already have the StreamSplitter Slack app", "1"), ("Basic Information page", ""),
                     ("it is installed", "later")])
    Wizard(io, FakeServices()).run("slack")
    assert not io.script and config.section("slack")["enabled"] is False
    assert "do not create a second one" in io.text


def make_team_file(folder: Path, login: bool = False, passphrase: str | None = PASS) -> Path:
    kit = {"created": "2026-11-03 20:00", "from_host": "karma-laptop",
           "slack": {"token": GOOD_SLACK, "channel_id": "C0ERRATA", "channel_name": "errata", "teams": ["5026"],
                     "score_in_thread": True, "clip_height": 720},
           "youtube": {"client": CLIENT_P, "route": "all", "version": "score", "privacy": "unlisted",
                       "title_format": "{match_title} | {event_name}",
                       "description_format": "{match_title} - {year} {event_name}\nRecorded by 5026",
                       "channel_id": "UC5026", "channel_title": "Karma Robotics", "terms_accepted": "2026-11-01"},
           "tba_key": GOOD_TBA}
    if login:
        kit["youtube"]["login"] = TOKEN_JSON
    return teamkit.write(folder / "StreamSplitter-team-5026-2026-11-03", kit, passphrase)


def test_first_run_with_a_team_settings_file_skips_the_console_parts(env):
    """A teammate's file: no TBA key to make, no Slack app, no Google project. Asked: this laptop's alert contact,
    this event's playlist and privacy, and the YouTube login (the file has none)."""
    make_team_file(env / "Downloads")
    svc = FakeServices()
    io = ScriptedIO([("team settings file from a teammate", "y"), ("Where is the team settings file", ""),
                     ("passphrase", "not-the-passphrase"), ("passphrase", PASS), ("Use these settings", "y"),
                     ("Event code", "madtown"), ("events matching", "1"), ("pick another", ""),
                     ("name or email", "ian"), ("Send them to Ian H", "y"),
                     ("Playlist name", ""), ("Who can see", ""), ("Google Drive", "n"),
                     ("sample alert now", "n"), *LISTEN_NO, ("Send a test video", "n"),
                     ("Start recording now", "n")])
    assert Wizard(io, svc).run() == 0
    assert not io.script
    prompts = "\n".join(io.prompts)
    for asked_before in ("Paste the key", "already have the StreamSplitter Slack app", "Bot User OAuth Token",
                         "Slack channel", "Video size", "made it already", "Terms of Service", "The title"):
        assert asked_before not in prompts, asked_before
    assert google_pages_opened(io) == [] and not any("slack.com" in u for u in io.opened)
    assert "Wrong passphrase" in io.text and "Karma Robotics" in io.text and "#errata" in io.text
    sl, yt, nt = config.section("slack"), config.section("youtube"), config.section("notify")
    assert sl["channel_id"] == "C0ERRATA" and sl["clip_height"] == 720 and sl["enabled"]
    assert yt["title_format"] == "{match_title} | {event_name}" and yt["playlist_id"] == "PL1"
    assert yt["playlist_event"] == "2026mttd" and yt["channel_id"] == "UC5026"
    assert nt["slack_user"] == "U0IAN"
    assert ("login", CLIENT["installed"]["client_id"]) in svc.log      # this laptop logs in once
    sec = secrets_file(env)
    assert sec["tba_key"] == GOOD_TBA and sec["slack_bot_token"] == GOOD_SLACK and sec["youtube_token"] == TOKEN_JSON
    assert_no_secrets(io, env)


def test_team_file_with_the_login_and_from_the_slack_question(env):
    """The file can also be loaded where Slack asks whether the app exists (found on the desktop by its name); with
    the login in it, YouTube asks only for the playlist."""
    make_team_file(env / "Desktop", login=True)
    config.set_secret("tba_key", GOOD_TBA)
    config.save_sections({"event": {"key": "2026mttd", "name": "MadTown Throwdown", "year": 2026, "tag": "2026mttd",
                                    "stream_url": CHANNEL_LIVE}})
    svc = FakeServices()
    io = ScriptedIO([("Post your team's matches to Slack", "y"), ("Which teams", "5026"),
                     ("already have the StreamSplitter Slack app", "3"),
                     ("Where is the team settings file", "StreamSplitter-team-5026-2026-11-03"),
                     ("passphrase", PASS), ("Use these settings", "y"), ("name or email", "ian"),
                     ("Send them to Ian H", "y")])
    w = Wizard(io, svc)
    w.run("slack")
    assert not io.script and config.section("slack")["channel_id"] == "C0ERRATA"
    io = ScriptedIO([("Playlist name", ""), ("Who can see", "")])
    w.io = io
    w.step_youtube()
    assert not io.script and not any(e[0] == "login" for e in svc.log)
    assert config.section("youtube")["playlist_id"] == "PL1"


def test_share_team_settings_hides_the_passphrase_and_writes_the_file(env):
    svc = FakeServices()
    full_run(svc)
    io = ScriptedIO([("Team settings file", "1"),
                     ("Passphrase for the file", "2"), ("Type a passphrase", "short"),
                     ("Type a passphrase", PASS), ("Type it again", PASS + "x"),
                     ("Type a passphrase", PASS), ("Type it again", PASS), ("Include the YouTube login", "y"),
                     ("Save the file in", "")])
    Wizard(io, svc).run("team")
    assert not io.script
    files = list((env / "Downloads").glob("*" + teamkit.SUFFIX))
    assert len(files) == 1 and files[0].name.startswith("StreamSplitter-team-5026-")
    kit = teamkit.read(files[0], PASS)
    assert kit["slack"]["token"] == GOOD_SLACK and kit["youtube"]["login"] == TOKEN_JSON
    assert "use at least 12" in io.text and "do not match" in io.text
    assert "different routes" in io.text and "post to your Slack" in io.text
    assert all("(hidden)" in o for o in io.out if o.startswith("Type"))
    assert_no_secrets(io, env)                             # the passphrase too


def test_share_from_the_menu_with_a_made_up_passphrase(env, monkeypatch):
    svc = FakeServices()
    full_run(svc)
    monkeypatch.setattr(teamkit, "suggest_passphrase", lambda: "abcde-fghij-kmnpq-rstuv")
    io = ScriptedIO([("What would you like to do", "6"),
                     ("Passphrase for the file", ""), ("Press Enter when you have it", ""),
                     ("Include the YouTube login", ""), ("Save the file in", "2"), ("What would you like to do", "9")])
    Wizard(io, svc).run()
    assert not io.script
    files = list((env / "Desktop").glob("*" + teamkit.SUFFIX))
    assert len(files) == 1
    kit = teamkit.read(files[0], "abcde-fghij-kmnpq-rstuv")
    assert "login" not in kit["youtube"] and kit["tba_key"] == GOOD_TBA
    assert "The passphrase is:  abcde-fghij-kmnpq-rstuv" in io.text   # shown once, so it can be passed on
    for s in SECRET_VALUES:
        assert s not in io.text


def test_share_without_a_passphrase_warns_and_says_it_can_be_sent_as_is(env):
    """User, 2026-11-06: no passphrase. One warning, the login question says what it means then (default no), and
    the file is plain."""
    svc = FakeServices()
    full_run(svc)
    io = ScriptedIO([("Team settings file", "1"), ("Passphrase for the file", "3"), ("Include the YouTube login", ""),
                     ("Save the file in", "")])
    Wizard(io, svc).run("team")
    assert not io.script
    path = next((env / "Downloads").glob("*" + teamkit.SUFFIX))
    assert not teamkit.needs_passphrase(path)
    kit = teamkit.read(path)
    assert kit["slack"]["token"] == GOOD_SLACK and "login" not in kit["youtube"]       # the default: no login
    assert "3) No passphrase (anyone who gets the file can use these keys)" in io.text
    assert io.text.count("No passphrase: the file is plain text") == 1
    assert "could upload to or delete the channel's videos" in io.text
    assert any("Include the YouTube login? [y/N]" in p for p in io.prompts)
    assert "Anyone with the file can post to your Slack" in io.text and "can be sent as is" in io.text
    assert "different routes" not in io.text and "Type a passphrase" not in "\n".join(io.prompts)
    for s in SECRET_VALUES:
        assert s not in io.text


def test_a_file_without_a_passphrase_loads_with_no_passphrase_question(env):
    make_team_file(env / "Downloads", passphrase=None)
    io = ScriptedIO([("Where is the team settings file", ""), ("Use these settings", "y")])
    assert Wizard(io, FakeServices()).import_team_settings(follow=False) is True and not io.script
    assert not any("passphrase" in p.lower() for p in io.prompts)
    assert "Karma Robotics" in io.text and "#errata" in io.text
    sec = secrets_file(env)
    assert sec["slack_bot_token"] == GOOD_SLACK and sec["tba_key"] == GOOD_TBA
    assert config.section("slack")["channel_id"] == "C0ERRATA"
    assert_no_secrets(io, env)


def test_a_damaged_file_without_a_passphrase_is_refused_in_the_setup(env):
    path = make_team_file(env / "Downloads", passphrase=None)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["kit"]["slack"]["token"] = "xoxb-changed"
    path.write_text(json.dumps(doc), encoding="utf-8")
    io = ScriptedIO([("Where is the team settings file", ""), ("Where is the team settings file", "skip")])
    assert Wizard(io, FakeServices()).import_team_settings(follow=False) is False and not io.script
    assert "damaged" in io.text and not any("passphrase" in p.lower() for p in io.prompts)
    assert not config.section("slack").get("channel_id") and config.get_secret("slack_bot_token") is None


def test_stream_uses_the_runtime_suggestions(env):
    """When the runtime offers suggest_streams (the same choice as `stream auto` in Slack), the wizard proposes its
    first stream; none listed: said plainly, and the link is typed."""
    config.set_secret("tba_key", GOOD_TBA)
    config.save_sections({"event": {"key": "2026mttd", "name": "MadTown Throwdown", "start_date": "2026-11-06",
                                    "end_date": "2026-11-08"}})
    svc = FakeServices()
    svc.streams = [{"url": CHANNEL_LIVE, "label": "Madtown Throwdown live page (recommended)"}]
    io = ScriptedIO([("Use it?", "")])
    Wizard(io, svc).run("stream")
    assert not io.script and config.section("event")["stream_url"] == CHANNEL_LIVE
    assert ("suggest_streams", "2026mttd") in svc.log and ("tba", "/event/2026mttd") not in svc.log
    assert "reply 'stream <link>' to the StreamSplitter app" in io.text
    svc.streams = []
    svc.probe["https://www.twitch.tv/quiet"] = {"status": "offline", "message": "not live"}
    io = ScriptedIO([("Stream link", "https://www.twitch.tv/quiet"), ("Use this link", "y")])
    Wizard(io, svc).run("stream")
    assert not io.script and "lists no stream for this event yet" in io.text


def test_console_wait_without_a_real_console_asks_one_line(monkeypatch):
    """Piped input (or a test): ConsoleIO waits as the base IO does: check first, else one typed line."""
    monkeypatch.setattr(setup_wizard.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr("builtins.input", lambda shown: "D:/Downloads/client.json")
    io = setup_wizard.ConsoleIO()
    assert io.wait_until("Waiting", lambda: "found.json") == ("found.json", None)
    assert io.wait_until("Waiting", lambda: None) == (None, "D:/Downloads/client.json")


def test_services_use_the_runtime_streams_and_commands(env, monkeypatch):
    """streams.suggest_streams gets a TBA reader with this laptop's key; without streams.py the wizard falls back to
    its own TBA lookup (None). The commands come from publish/commands.py: Slack formatting for the DM, plain text
    (ASCII, for a cp1252 console) for the summary."""
    from streamsplitter import streams
    config.set_secret("tba_key", GOOD_TBA)
    asked = []

    class Svc(setup_wizard.Services):
        def tba_get(self, path, key):
            asked.append((path, key))
            return {"key": "2026mttd", "webcasts": []}
    monkeypatch.setattr(streams, "suggest_streams", lambda key, tba_get: [{"url": "u", "got": tba_get(f"/event/{key}")}])
    assert Svc().suggest_streams("2026mttd") == [{"url": "u", "got": {"key": "2026mttd", "webcasts": []}}]
    assert asked == [("/event/2026mttd", GOOD_TBA)]
    real = setup_wizard.importlib.util.find_spec
    monkeypatch.setattr(setup_wizard.importlib.util, "find_spec",
                        lambda name, *a: None if name.endswith(".streams") else real(name, *a))
    assert Svc().suggest_streams("2026mttd") is None
    plain = setup_wizard.Services().slack_commands_help("errata", slack=False)
    assert "stream auto" in plain and "#errata" in plain
    plain.encode("ascii")
    assert "`status`" in setup_wizard.Services().slack_commands_help("errata")


def test_stream_step_with_the_runtime_suggestions(env):
    """The real streams.suggestions format ({"url", "label", "date", "recommended"}) goes through the stream step."""
    from streamsplitter import streams
    config.set_secret("tba_key", GOOD_TBA)
    config.save_sections({"event": {"key": "2026mttd", "name": "MadTown Throwdown", "start_date": "2026-11-06",
                                    "end_date": "2026-11-08"}})
    svc = FakeServices()
    svc.streams = lambda key: streams.suggestions({**MTTD, "webcasts": WEBCASTS}, None,
                                                  channel_live=lambda url: CHANNEL_LIVE)
    io = ScriptedIO([("pick another", "")])
    Wizard(io, svc).run("stream")
    assert not io.script and config.section("event")["stream_url"] == CHANNEL_LIVE
    assert "The Blue Alliance lists this stream for the event: " + CHANNEL_LIVE in io.text


def test_official_first_event_defaults_youtube_off(env):
    """User, 2026-11-04: at official FIRST events FIRST posts every match itself and its webcast can be claimed by
    Content ID, so YouTube is off unless chosen; an offseason event (TBA event_type 99) keeps the normal default."""
    config.set_secret("tba_key", GOOD_TBA)
    config.save_sections({"event": {"key": "2026casj", "name": "Silicon Valley Regional", "year": 2026,
                                    "tag": "2026casj", "event_type": 0}})
    io = ScriptedIO([("Upload matches to YouTube anyway", "")])
    Wizard(io, FakeServices()).run("youtube")
    assert not io.script
    assert config.section("youtube")["enabled"] is False
    assert "official FIRST event" in io.text and "frcteams@firstinspires.org" in io.text
    assert setup_wizard.is_official_event({"event_type": 0}) and setup_wizard.is_official_event({"event_type": 2})
    assert not setup_wizard.is_official_event({"event_type": 99}) and not setup_wizard.is_official_event({})


# ---------------------------------------------------------------------------------------------------------------------
# The team wiki (2026-11-04)

WIKI_API = "https://wiki.example.org/api.php"
MATCH_PAGE = "Madtown Throwdown (2026) match videos"


def wiki_ready(svc, **wiki):
    """A saved event (Slack and YouTube off), the fake wiki holding the event's own pages."""
    config.set_secret("tba_key", GOOD_TBA)
    config.save_sections({"event": {"key": "2026mttd", "name": "MadTown Throwdown", "year": 2026, "tag": "2026mttd",
                                    "stream_url": CHANNEL_LIVE},
                          "slack": {"enabled": False, "teams": ["5026"]}, "youtube": {"enabled": False},
                          **({"wiki": wiki} if wiki else {})})
    svc.wiki_site.url = WIKI_API
    svc.wiki_site.pages.update({"Madtown Throwdown (2026)": "The 2026 event.", "Madtown Throwdown (2025)": "Older."})


def test_wiki_step_walks_through_the_bot_password_and_finds_the_event_page(env):
    svc = FakeServices()
    wiki_ready(svc)
    io = ScriptedIO([("Upload every match to your team's wiki", "y"),
                     ("Wiki address", ""), ("Try another address", "y"), ("Wiki address", "wiki.example.org"),
                     ("bot's user name", "Ian"), ("bot's user name", "Ian@StreamSplitter"),
                     ("Paste the bot password", "not-the-password"),
                     ("bot's user name", ""), ("Paste the bot password", WIKI_PASS),
                     ("Is 'Madtown Throwdown (2026)' the wiki's page for MadTown Throwdown", "y"),
                     ("Page for the match videos", ""), ("members-only", ""),
                     ("Video markup", "<video>File:x.mp4</video>"), ("Video markup", "<video>File:{file}</video>")])
    Wizard(io, svc).run("wiki")
    assert not io.script
    assert config.section("wiki") == {"enabled": True, "api_url": WIKI_API, "username": "Ian@StreamSplitter",
                                      "version": "score", "private": True, "embed_format": "<video>File:{file}</video>",
                                      "page_format": "{event_name} ({year}) match videos", "page_title": MATCH_PAGE,
                                      "page_event": "2026mttd", "event_page": "Madtown Throwdown (2026)"}
    assert secrets_file(env)["wiki_bot_password"] == WIKI_PASS
    prompts = "\n".join(io.prompts)
    assert "Wiki address [https://www.team5026.com/api.php]" in prompts      # offered: 5026 is the team
    assert f"Page for the match videos [{MATCH_PAGE}]" in prompts            # the wiki's spelling, not TBA's
    assert "https://wiki.example.org/index.php/Special:BotPasswords" in io.opened
    for grant in ("Edit existing pages", "Create, edit, and move pages", "Upload new files",
                  "Upload, replace, and move files"):
        assert grant in io.text
    assert "shows it only once" in io.text and "with the @ in it" in io.text
    assert "The wiki did not accept that" in io.text and "does not exist yet" in io.text
    assert "must contain {file}" in io.text
    assert "Ian@StreamSplitter" in io.text and "(hidden)" in io.text
    assert_no_secrets(io, env)
    io = ScriptedIO([])
    Wizard(io, svc).summary()
    assert f"Wiki:     every match, members-only, to '{MATCH_PAGE}' on wiki.example.org" in io.text


def test_wiki_step_skipped_and_kept(env):
    svc = FakeServices()
    wiki_ready(svc)
    io = ScriptedIO([("Upload every match to your team's wiki? [y/N]", "")])
    Wizard(io, svc).run("wiki")
    assert not io.script and config.section("wiki") == {"enabled": False}
    Wizard(ScriptedIO([]), svc).summary()
    # set up already: the saved login is kept, the page and choices offered as they are
    config.set_secret("wiki_bot_password", WIKI_PASS)
    config.save_sections({"wiki": {"enabled": True, "api_url": WIKI_API, "username": "Ian@StreamSplitter",
                                   "page_title": MATCH_PAGE, "page_event": "2026mttd", "private": False}})
    svc.wiki_site.pages[MATCH_PAGE] = "{{Private}}\n\n== Qualification 1 ==\n\nx"
    io = ScriptedIO([("Upload every match to your team's wiki? [Y/n]", ""), ("Keep using this wiki login", ""),
                     ("Page for the match videos", ""), ("members-only", ""), ("Video markup", "")])
    Wizard(io, svc).run("wiki")
    assert not io.script
    wk = config.section("wiki")
    assert wk["page_title"] == MATCH_PAGE and wk["private"] is False and wk["embed_format"] == "[[File:{file}|800px]]"
    assert "That page exists (1 section)" in io.text and "members-only" in "\n".join(io.prompts)
    assert "[y/N]" in next(p for p in io.prompts if "members-only" in p)      # the saved answer is the default


def test_test_upload_to_the_wiki(env):
    svc = FakeServices()
    wiki_ready(svc, enabled=True, api_url=WIKI_API, username="Ian@StreamSplitter", page_title=MATCH_PAGE,
               page_event="2026mttd", private=True, embed_format="[[File:{file}|800px]]")
    config.set_secret("wiki_bot_password", WIKI_PASS)
    io = ScriptedIO([("Send a test video", "y"), ("can you see the test video on the wiki", "y")])
    Wizard(io, svc).run("test")
    assert not io.script
    site = svc.wiki_site
    page = site.pages["StreamSplitter setup test"]
    assert page.startswith("{{Private}}") and "== Qualification 6 - 2024 Madtown Throwdown (sample) ==" in page
    assert "[[File:StreamSplitter_setup_test.mp4|800px]]" in page and "Red 1323, 6619, 254B - 81" in page
    assert site.file("StreamSplitter_setup_test.mp4")["data"] == setup_wizard.SAMPLE_CLIP.read_bytes()
    assert site.pages["File:StreamSplitter setup test.mp4"].startswith("{{Private}}")
    assert MATCH_PAGE not in site.pages                                       # the event's page is not touched
    assert "The test worked: the wiki is hooked up" in io.text
    assert "delete the wiki test page and its file" in io.text
    assert "https://wiki.example.org/index.php/StreamSplitter_setup_test" in io.text
    # a bot that may delete: offered, and done
    site.rights.add("delete")
    io = ScriptedIO([("Send a test video", "y"), ("can you see", "y"), ("Delete the wiki test page", "y")])
    Wizard(io, svc).run("test")
    assert not io.script and "StreamSplitter setup test" not in site.pages
    assert site.file("StreamSplitter_setup_test.mp4") is None and "Deleted the wiki test page" in io.text


def test_menu_has_the_wiki_and_a_new_event_forgets_its_page(env):
    svc = FakeServices()
    full_run(svc)
    config.set_secret("wiki_bot_password", WIKI_PASS)
    config.save_sections({"wiki": {"enabled": True, "api_url": WIKI_API, "username": "Ian@StreamSplitter",
                                   "page_title": MATCH_PAGE, "page_event": "2026mttd"}})
    io = ScriptedIO([("What would you like to do", "4"), ("Which setting", "7"),
                     ("Upload every match to your team's wiki", "n"), ("Restart it now", "n"),
                     ("What would you like to do", "9")])
    assert Wizard(io, svc).run() == 0 and not io.script
    assert "6) Google Drive" in io.text and "7) Team website (wiki)" in io.text
    assert config.section("wiki")["enabled"] is False
    config.save_sections({"wiki": {"enabled": True}})
    io = ScriptedIO([("Event code", "2026casj"), ("Is this your event", "y"),
                     ("Stream link", "https://www.twitch.tv/firstinspires"), ("Use it anyway", "y")])
    svc.probe["https://www.twitch.tv/firstinspires"] = {"status": "video", "title": "old VOD"}
    Wizard(io, svc).run("event")
    assert not io.script and "check its wiki page too" in io.text
    assert not {"page_title", "page_event"} & set(config.section("wiki"))


def test_team_settings_file_carries_the_wiki(env):
    svc = FakeServices()
    wiki_ready(svc)
    kit = {"created": "2026-11-03 20:00", "from_host": "karma-laptop", "tba_key": GOOD_TBA,
           "wiki": {"password": WIKI_PASS, "api_url": WIKI_API, "username": "Ian@StreamSplitter", "private": True,
                    "embed_format": "<video>File:{file}</video>", "page_format": "{event_name} ({year}) match videos"}}
    teamkit.write(env / "Downloads" / "StreamSplitter-team-5026-2026-11-03", kit, PASS)
    io = ScriptedIO([("Where is the team settings file", ""), ("passphrase", PASS), ("Use these settings", "y"),
                     ("Is 'Madtown Throwdown (2026)' the wiki's page", "y"), ("Page for the match videos", "")])
    w = Wizard(io, svc)
    assert w.import_team_settings() is True and not io.script
    wk = config.section("wiki")
    assert wk["embed_format"] == "<video>File:{file}</video>" and wk["page_title"] == MATCH_PAGE and wk["enabled"]
    assert secrets_file(env)["wiki_bot_password"] == WIKI_PASS
    assert "Team wiki: every match to wiki.example.org as Ian@StreamSplitter" in io.text
    for asked_before in ("Wiki address", "bot's user name", "Paste the bot password", "Video markup"):
        assert asked_before not in "\n".join(io.prompts), asked_before
    assert_no_secrets(io, env)


# ---------------------------------------------------------------------------------------------------------------------
# Google Drive (2026-11-05)

def drive_ready(client=True, schedule=False) -> FakeServices:
    """A saved event (Slack and YouTube off), the Google sign-in app saved by the YouTube step when `client`, and TBA's
    schedule for the event when `schedule`."""
    config.set_secret("tba_key", GOOD_TBA)
    config.save_sections({"event": {"key": "2026mttd", "name": "MadTown Throwdown", "year": 2026, "tag": "2026mttd",
                                    "stream_url": CHANNEL_LIVE},
                          "slack": {"enabled": False, "teams": ["5026"]}, "youtube": {"enabled": False}})
    if client:
        config.set_secret("youtube_client", json.dumps(CLIENT_P))
    svc = FakeServices()
    if schedule:
        real_get = svc.tba_get

        def tba_get(path, key):
            if path == "/event/2026mttd/matches/simple":
                return [{"key": f"2026mttd_qm{n}", "comp_level": "qm", "set_number": 1, "match_number": n,
                         "alliances": {"red": {"team_keys": ["frc1323", "frc254", "frc5026"]},
                                       "blue": {"team_keys": ["frc604", "frc8033", "frc199"]}}}
                        for n in range(72, 0, -1)]
            return real_get(path, key)
        svc.tba_get = tba_get
    return svc


def test_drive_step_logs_in_makes_the_folders_and_explains_sharing(env):
    svc = drive_ready(schedule=True)
    io = ScriptedIO(DRIVE_ON)
    Wizard(io, svc).run("drive")
    assert not io.script
    site = svc.drive_site
    top, ev = site.folder("FRC match videos"), site.folder("2026 MadTown Throwdown")
    assert top["appProperties"] == {"ss": "top"} and ev["parents"] == [top["id"]]
    dr = config.section("drive")
    assert dr == {"enabled": True, "version": "score", "folder_id": top["id"],
                  "folder_url": f"https://drive.google.com/drive/folders/{top['id']}", "folder_title": "FRC match videos",
                  "event_folder_id": ev["id"], "event_folder_event": "2026mttd",
                  "event_folder_title": "2026 MadTown Throwdown",
                  "event_folder_url": f"https://drive.google.com/drive/folders/{ev['id']}", "height": 720,
                  "name_format": "{code} {match_title}[ - {teams}]"}
    # the login (renewed at once: a token without an expiry counts as expired), never the YouTube one
    assert json.loads(secrets_file(env)["drive_token"])["refresh_token"] == "1//DRIVErefreshSECRET"
    assert "youtube_token" not in secrets_file(env)
    # what was said and opened: the space, the API page in the client's project, the folder to share
    assert "15 GB, shared with its Gmail and Photos" in io.text and "about 3 events" in io.text
    assert CONSOLE + "apis/library/drive.googleapis.com?project=streamsplitter-5026" in io.opened
    assert "Logged in to Google Drive as team5026@example.com (13.0 GB free of 15.0 GB)." in io.text
    assert dr["folder_url"] in io.opened and "Anyone with the link" in io.text and "Viewer" in io.text
    assert "Q01 Qualification 1 - 1323 254 5026 vs 604 8033 199.mp4" in io.text   # from the event's schedule
    assert "P03 Match 3 (R1).mp4" in io.text
    assert "Saved: every match goes to 'FRC match videos' (this event: '2026 MadTown Throwdown'), 720p." in io.text
    assert ("drive_login", CLIENT["installed"]["client_id"]) in svc.log
    assert_no_secrets(io, env)
    io = ScriptedIO([])
    Wizard(io, svc).summary()
    assert f"Drive:    every match, 720p, to the folder 'FRC match videos': {dr['folder_url']}" in io.text


def test_drive_without_youtube_takes_the_client_file_and_custom_choices(env):
    svc = drive_ready(client=False)
    io = ScriptedIO([("Upload every match to Google Drive", "y"), ("made it already", ""), ("client_secret", ""),
                     ("Press Enter when it is enabled", ""), ("Keep the videos", "y"),
                     ("Name of the folder", "Karma match videos"), ("Press Enter when it is shared", ""),
                     ("Video size in Google Drive", "2"), ("Name the files like this", "n"),
                     ("File name format", "{code} {nope}"), ("File name format", "{code} {match_title}"),
                     ("Use this", "y")])
    Wizard(io, svc).run("drive")
    assert not io.script
    dr = config.section("drive")
    assert dr["folder_title"] == "Karma match videos" and dr["height"] == 540
    assert dr["name_format"] == "{code} {match_title}"
    assert "Skip Google Drive for now" in io.text and "unknown field {nope}" in io.text
    assert "Q12 Qualification 12.mp4" in io.text                    # no schedule yet: the sample match
    assert json.loads(secrets_file(env)["youtube_client"]) == CLIENT
    assert_no_secrets(io, env)


def test_drive_walkthrough_turns_on_the_drive_api(env):
    svc = drive_ready(client=False)
    dl = env / "Downloads"
    io = ScriptedIO([("Upload every match to Google Drive", "y"), ("made it already", "1"),
                     ("Project ID", "streamsplitter-5026"), ("Press Enter when it is enabled", ""),
                     ("Press Enter when it is created", ""), ("says In production", ""),
                     ("Waiting for the downloaded file", download(dl / "client_secret_new.json", CLIENT_P)),
                     ("Keep the videos", "y"), ("Name of the folder", ""), ("Press Enter when it is shared", ""),
                     ("Video size", ""), ("Name the files like this", "")])
    Wizard(io, svc).run("drive")
    assert not io.script
    q = "?project=streamsplitter-5026"
    assert google_pages_opened(io) == [CONSOLE + "projectcreate", CONSOLE + "apis/library/drive.googleapis.com" + q,
                                       CONSOLE + "auth/branding" + q, CONSOLE + "auth/audience" + q,
                                       CONSOLE + "auth/clients" + q]   # the Drive API page is not opened twice
    assert "turn on the Google Drive API" in io.text and "owns the team's YouTube channel" not in io.text
    assert config.section("drive")["folder_id"]


def test_another_account_cannot_see_the_folder_and_gets_a_new_one(env):
    svc = drive_ready()
    config.save_sections({"drive": {"enabled": True, "folder_id": "fo999", "folder_title": "FRC match videos",
                                    "event_folder_id": "fo998", "event_folder_event": "2026mttd", "height": 540}})
    io = ScriptedIO([("Upload every match to Google Drive", ""), ("Press Enter when it is enabled", ""),
                     ("Keep the videos", "y"), ("Make a new folder in this account's Drive", "y"),
                     ("Name of the folder", ""), ("Press Enter when it is shared", ""), ("Video size", ""),
                     ("Name the files like this", "")])
    Wizard(io, svc).run("drive")
    assert not io.script
    dr = config.section("drive")
    top = svc.drive_site.folder("FRC match videos")
    assert dr["folder_id"] == top["id"] != "fo999" and dr["event_folder_id"] != "fo998" and dr["height"] == 540
    assert "This Google account cannot see the folder 'FRC match videos'" in io.text
    assert "Choose 1-3 [2]" in "\n".join(io.prompts)                        # the saved size is the default


def test_test_upload_to_drive_then_the_trash(env):
    svc = drive_ready()
    top = svc.drive_site.add_folder("FRC match videos", props={"ss": "top"})
    config.set_secret("drive_token", DRIVE_TOKEN)
    config.save_sections({"drive": {"enabled": True, "folder_id": top["id"], "folder_title": "FRC match videos",
                                    "height": 720}})
    io = ScriptedIO([("Send a test video", "y"), ("can you see the test video in Google Drive", "y"),
                     ("Move the Google Drive test video to the trash", "y")])
    Wizard(io, svc).run("test")
    assert not io.script
    [v] = svc.drive_site.videos()
    assert v["parents"] == [top["id"]] and v["trashed"] is True
    assert v["data"] == setup_wizard.SAMPLE_CLIP.read_bytes()             # the sample as it is: no ffmpeg needed
    assert v["name"].startswith("StreamSplitter setup test - Q06 Qualification 6")
    assert "The test worked: Google Drive is hooked up" in io.text and "Moved it to the trash" in io.text
    assert f"Google Drive: https://drive.google.com/file/d/{v['id']}" in io.text


def test_first_run_has_drive_not_the_wiki(env):
    svc = FakeServices()
    io = full_run(svc)
    assert "=== Step 5: Google Drive" in io.text and "team's wiki" not in "\n".join(io.prompts)
    assert "  Drive:    off" in io.text and "  Wiki:" not in io.text
    assert config.section("drive") == {"enabled": False}


def test_team_file_carries_the_drive_folder_and_the_login_when_asked(env, monkeypatch):
    svc = drive_ready(schedule=True)
    Wizard(ScriptedIO(DRIVE_ON), svc).run("drive")
    io = ScriptedIO([("Team settings file", "1"),
                     ("Passphrase for the file", "2"), ("Type a passphrase", PASS), ("Type it again", PASS),
                     ("Include the Google Drive login", "y"), ("Save the file in", "")])
    Wizard(io, svc).run("team")
    assert not io.script and "and your Google Drive folder" in io.text
    path = next((env / "Downloads").glob("*" + teamkit.SUFFIX))
    kit = teamkit.read(path, PASS)
    d = kit["drive"]
    assert json.loads(d["login"])["refresh_token"] == "1//DRIVErefreshSECRET" and d["client"] == CLIENT_P
    assert d["folder_title"] == "FRC match videos"
    assert not {k for k in d if k.startswith("event_folder")}
    # a second laptop on the same account: no Google console, no login, no folder question
    other = env / "laptop2"
    other.mkdir()
    monkeypatch.setenv("STREAMSPLITTER_CONFIG", str(other / "config.toml"))
    config.save_sections({"event": {"key": "2026mttd", "name": "MadTown Throwdown", "year": 2026}})
    io = ScriptedIO([("Where is the team settings file", str(path)), ("passphrase", PASS), ("Use these settings", "y"),
                     ("Keep the videos", "y"), ("Open the folder to check", "")])
    assert Wizard(io, svc).import_team_settings() is True and not io.script
    dr = config.section("drive")
    assert dr["folder_id"] == d["folder_id"] and dr["event_folder_event"] == "2026mttd"
    assert "Google Drive (logged in)" in io.text and "Using the folder 'FRC match videos'" in io.text
    assert google_pages_opened(io) == []
    assert_no_secrets(io, other)


# ---------------------------------------------------------------------------------------------------------------------
# Start from Slack (listener.py): the listen step and its menu entry

SAM = {"id": "U0SAM", "name": "sam", "real_name": "Sam K", "display_name": None, "email": "sam@example.com"}
IAN_USER = {"id": "U0IAN", "name": "ian", "real_name": "Ian H", "display_name": None, "email": "ian@example.com"}


def test_listen_in_the_background_installs_the_log_in_start_and_says_hello(env, monkeypatch):
    svc = FakeServices()
    svc._slack = fake_slack(svc.log, users=[IAN_USER, SAM])
    full_run(svc)
    monkeypatch.setattr(setup_wizard.platform, "node", lambda: "HOMEPC")
    io = ScriptedIO([("Slack start recordings on this computer", "1")])     # nobody else is asked for here
    assert Wizard(io, svc).run("listen") == 0 and not io.script
    assert "1) In the background: it starts by itself when you log in" in io.text and "3) Off" in io.text
    assert config.section("listen") == {"enabled": True, "mode": "background"}
    assert "Let someone else" not in io.text and "Who is it" not in io.text
    assert "Ian H can let others start and stop recordings later, from Slack: message the app add @name" in io.text
    ln = svc._listener
    assert ln.calls == ["install", "start"] and ln.installed and ln.running
    hellos = [e for e in svc.log if e[0] == "dm" and e[2].startswith("Listening on")]
    assert [e[1] for e in hellos] == ["U0IAN"]                 # the contact only: nobody else is messaged unasked
    assert not [e for e in svc.log if e[0] == "dm" and e[1] != "U0IAN"]
    assert hellos[0][2] == ("Listening on HOMEPC: reply split &lt;event&gt; to start a recording (e.g. split 2026mttd). "
                            "Reply help for every command; add @name lets someone else start and stop recordings too.")
    assert "Set Windows Update's active hours, or pause" in io.text
    # people a setup added before are kept, and still get no message
    config.save_sections({"listen": {"users": ["U0SAM"], "user_names": ["Sam K"]}})
    io = ScriptedIO([("Slack start recordings on this computer", "1")])
    assert Wizard(io, svc).run("listen") == 0 and not io.script
    assert config.section("listen")["users"] == ["U0SAM"] and "Already allowed: Sam K." in io.text
    assert not [e for e in svc.log if e[0] == "dm" and e[1] == "U0SAM"]
    # the menu stops it; its entry says what it does now
    io = ScriptedIO([("What would you like to do", "8"), ("The Slack listener is running", "1"),
                     ("What would you like to do", "9")])
    assert Wizard(io, svc).run() == 0 and not io.script
    assert "8) Stop listening for Slack commands" in io.text and "8) Start listening for Slack commands" in io.text
    assert not ln.running and ln.installed                     # until the next log-in


def test_listen_switches_between_background_window_and_off(env):
    svc = FakeServices()
    full_run(svc)
    ln = svc._listener
    Wizard(ScriptedIO([("Slack start recordings", "1")]), svc).run("listen")
    assert ln.installed and ln.running
    io = ScriptedIO([("Slack start recordings", "2")])
    Wizard(io, svc).run("listen")
    assert not io.script
    assert config.section("listen")["mode"] == "window" and config.section("listen")["enabled"] is True
    assert not ln.installed and not ln.running and ln.calls[2:] == ["remove", "stop"]   # nothing at log-in or behind
    assert "choose Listen for Slack commands in this window" in io.text and "`streamsplitter listen`" in io.text
    # the menu listens in its own window; nothing is installed or started in the background
    io = ScriptedIO([("What would you like to do", "8"), ("What would you like to do", "9")])
    assert Wizard(io, svc).run() == 0 and not io.script
    assert "8) Listen for Slack commands in this window" in io.text and ln.calls[-1] == "run"
    assert "install" not in ln.calls[2:] and "start" not in ln.calls[2:]
    ln.installed = True                                          # a log-in start left from somewhere: off removes it
    io = ScriptedIO([("Slack start recordings", "3")])
    Wizard(io, svc).run("listen")
    assert config.section("listen")["enabled"] is False and not ln.installed
    assert "Start from Slack is off on this computer" in io.text
    assert Wizard(ScriptedIO([]), svc).listen_menu_label() == "Start from Slack: start and stop recordings from Slack"


def test_listen_needs_the_im_history_permission(env):
    svc = FakeServices()
    full_run(svc)
    svc._slack = fake_slack(svc.log, auths=[{"missing_scopes": ["im:history"]}] * 3)
    io = ScriptedIO([("Slack start recordings", "1"), ("it is reinstalled", "skip")])
    Wizard(io, svc).run("listen")
    assert not io.script and "needs the app's im:history permission" in io.text
    assert "Add an OAuth Scope and pick im:history" in io.text                # the existing fix, walked through
    assert svc._listener.calls == [] and not config.section("listen").get("enabled")
