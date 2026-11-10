"""Team settings file (streamsplitter/teamkit.py): one laptop exports Slack + YouTube, another imports them."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from streamsplitter import config, teamkit  # noqa: E402

TOKEN = "xoxb-1111-2222-notreal"
CLIENT = {"installed": {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "GOCSPX-notreal"}}
LOGIN = json.dumps({"refresh_token": "1//notreal", "token": "ya29.notreal"})
PASS = "k7m2q-x9rtw-ab3cd-ef4gh"


@pytest.fixture
def laptop(tmp_path, monkeypatch):
    """A config.toml and secrets.json of the test's own; never the real credential store."""
    def use(name: str) -> Path:
        d = tmp_path / name
        d.mkdir(exist_ok=True)
        monkeypatch.setenv("STREAMSPLITTER_CONFIG", str(d / "config.toml"))
        return d
    for s in config.SECRETS:
        monkeypatch.delenv(f"STREAMSPLITTER_{s.upper()}", raising=False)
    monkeypatch.delenv("TBA_API_KEY", raising=False)
    monkeypatch.setattr(config, "_keyring", lambda: None)
    return use


def set_up_first_laptop():
    config.save_sections({"event": {"key": "2026mttd", "stream_url": "https://www.youtube.com/@x/live"},
                          "slack": {"enabled": True, "teams": ["5026"], "channel_id": "C0ERRATA",
                                    "channel_name": "errata", "score_in_thread": True, "clip_height": 0},
                          "youtube": {"enabled": True, "route": "all", "privacy": "unlisted", "channel_id": "UC5026",
                                      "channel_title": "Team 5026", "playlist_id": "PL1",
                                      "playlist_title": "2026 MadTown Throwdown", "playlist_event": "2026mttd",
                                      "title_format": "{match_title} - {year} {event_name}"},
                          "notify": {"slack_user": "U0IAN", "slack_user_name": "Ian"}})
    config.set_secret("slack_bot_token", TOKEN)
    config.set_secret("youtube_client", json.dumps(CLIENT))
    config.set_secret("youtube_token", LOGIN)
    config.set_secret("tba_key", "tba-notreal")


def test_round_trip_to_a_second_laptop(laptop, tmp_path):
    laptop("a")
    set_up_first_laptop()
    path = teamkit.write(tmp_path / "team5026", teamkit.collect(include_youtube_login=True), PASS)
    assert path.name.endswith(teamkit.SUFFIX)
    raw = path.read_text(encoding="utf-8")
    for secret in (TOKEN, "GOCSPX-notreal", "1//notreal", "tba-notreal"):
        assert secret not in raw                       # encrypted, not just encoded

    laptop("b")
    config.save_sections({"event": {"key": "2026cc"}, "notify": {"slack_user": "U0SAM"}})
    kit = teamkit.read(path, PASS)
    done = teamkit.apply(kit)
    assert done[0] == "Slack" and done[1].startswith("YouTube (logged in)")
    assert config.get_secret("slack_bot_token") == TOKEN
    assert json.loads(config.get_secret("youtube_client")) == CLIENT
    assert config.get_secret("youtube_token") == LOGIN and config.tba_key() == "tba-notreal"
    sl, yt = config.section("slack"), config.section("youtube")
    assert sl["channel_id"] == "C0ERRATA" and sl["teams"] == ["5026"] and sl["enabled"]
    assert yt["channel_title"] == "Team 5026" and yt["title_format"].startswith("{match_title}")
    assert "playlist_id" not in yt                     # the playlist belongs to the other laptop's event
    assert config.section("event")["key"] == "2026cc"  # this laptop's event and contact are kept
    assert config.section("notify")["slack_user"] == "U0SAM"


def test_youtube_login_only_when_asked(laptop, tmp_path):
    laptop("a")
    set_up_first_laptop()
    kit = teamkit.read(teamkit.write(tmp_path / "k", teamkit.collect(), PASS), PASS)
    assert "login" not in kit["youtube"] and kit["youtube"]["client"] == CLIENT
    laptop("b")
    assert teamkit.apply(kit)[1] == "YouTube (log in once with setup --section youtube)"
    assert config.get_secret("youtube_token") is None


def test_wrong_passphrase_damaged_and_foreign_files(laptop, tmp_path):
    laptop("a")
    set_up_first_laptop()
    path = teamkit.write(tmp_path / "k", teamkit.collect(), PASS)
    with pytest.raises(teamkit.TeamKitError, match="Wrong passphrase"):
        teamkit.read(path, PASS + "x")
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["kdf"]["n"] = 2 ** 14                          # the header is authenticated: changing it is refused
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(teamkit.TeamKitError):
        teamkit.read(path, PASS)
    other = tmp_path / "other.json"
    other.write_text('{"hello": 1}', encoding="utf-8")
    with pytest.raises(teamkit.TeamKitError, match="not a StreamSplitter team settings file"):
        teamkit.read(other, PASS)
    with pytest.raises(teamkit.TeamKitError, match="No file"):
        teamkit.read(tmp_path / "missing", PASS)


def test_a_file_without_a_passphrase_round_trips(laptop, tmp_path):
    """User, 2026-11-06: "I don't want it password protected." Plain JSON that says so, read with no passphrase."""
    laptop("a")
    set_up_first_laptop()
    made = teamkit.collect(include_youtube_login=True)
    path = teamkit.write(tmp_path / "team5026", made, None)
    assert path.name.endswith(teamkit.SUFFIX) and not teamkit.needs_passphrase(path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["format"] == teamkit.FORMAT and doc["version"] == teamkit.PLAIN_VERSION and doc["encrypted"] is False
    assert "NOT encrypted" in doc["note"] and "anyone with this file" in doc["note"]
    laptop("b")
    config.save_sections({"notify": {"slack_user": "U0SAM"}})
    kit = teamkit.read(path)
    assert kit == made
    done = teamkit.apply(kit)
    assert done[0] == "Slack" and done[1].startswith("YouTube (logged in)")
    assert config.get_secret("slack_bot_token") == TOKEN and config.get_secret("youtube_token") == LOGIN
    assert config.tba_key() == "tba-notreal" and config.section("notify")["slack_user"] == "U0SAM"


@pytest.mark.parametrize("damage", ["cut short", "token changed", "token removed", "says encrypted", "kit missing"])
def test_a_damaged_file_without_a_passphrase_is_refused_whole(laptop, tmp_path, damage):
    laptop("a")
    set_up_first_laptop()
    path = teamkit.write(tmp_path / "k", teamkit.collect(), None)
    text = path.read_text(encoding="utf-8")
    doc = json.loads(text)
    if damage == "cut short":
        path.write_text(text[: len(text) // 2], encoding="utf-8")
    else:
        if damage == "token changed":
            doc["kit"]["slack"]["token"] = "xoxb-someone-elses"
        elif damage == "token removed":                 # even with a matching digest, apply() could not use it
            del doc["kit"]["slack"]["token"]
            doc["sha256"] = teamkit._digest(doc["kit"])
        elif damage == "says encrypted":
            doc["encrypted"] = True
        else:
            del doc["kit"]
        path.write_text(json.dumps(doc), encoding="utf-8")
    laptop("b")
    with pytest.raises(teamkit.TeamKitError, match="damaged"):
        teamkit.read(path)
    assert config.get_secret("slack_bot_token") is None


def test_files_with_a_passphrase_are_as_before(laptop, tmp_path):
    """The encrypted file keeps the exact fields StreamSplitter 0.1.0 reads; it is never read without its passphrase."""
    laptop("a")
    set_up_first_laptop()
    path = teamkit.write(tmp_path / "k", teamkit.collect(), PASS)
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert set(doc) == {"format", "version", "kdf", "salt", "nonce", "data", "note"} and doc["version"] == 1
    assert teamkit.needs_passphrase(path)
    for missing in (None, ""):
        with pytest.raises(teamkit.TeamKitError, match="needs its passphrase"):
            teamkit.read(path, missing)
    assert teamkit.read(path, PASS)["slack"]["token"] == TOKEN


def test_refuses_short_passphrase_and_empty_setup(laptop, tmp_path):
    laptop("a")
    with pytest.raises(teamkit.TeamKitError, match="nothing to share"):
        teamkit.collect()
    set_up_first_laptop()
    with pytest.raises(teamkit.TeamKitError, match="at least"):
        teamkit.write(tmp_path / "k", teamkit.collect(), "short")


def test_summary_and_passphrase_never_show_secrets(laptop):
    laptop("a")
    set_up_first_laptop()
    lines = "\n".join(teamkit.summary(teamkit.collect(include_youtube_login=True)))
    assert "#errata" in lines and "Team 5026, logged in" in lines and "Blue Alliance" in lines
    for secret in (TOKEN, "GOCSPX-notreal", "1//notreal", "tba-notreal"):
        assert secret not in lines
    p = teamkit.suggest_passphrase()
    assert len(p) == 23 and len(teamkit.suggest_passphrase()) == 23 and p != teamkit.suggest_passphrase()


DRIVE = {"enabled": True, "version": "score", "height": 540, "name_format": "{code} {match_title}",
         "folder_id": "fo1", "folder_url": "https://drive.google.com/drive/folders/fo1",
         "folder_title": "FRC match videos", "event_folder_id": "fo2", "event_folder_event": "2026mttd",
         "event_folder_title": "2026 MadTown Throwdown", "event_folder_url": "https://drive.google.com/drive/folders/fo2"}
DRIVE_LOGIN = json.dumps({"refresh_token": "1//drive-notreal", "token": "ya29.drive-notreal"})


def test_google_drive_travels_with_its_folder_not_the_events(laptop, tmp_path):
    """The top folder and the choices go to the next laptop; the event's folder does not (each event finds its own);
    the Drive login only when asked; the sign-in app with Drive when YouTube does not carry it."""
    laptop("a")
    set_up_first_laptop()
    config.save_sections({"drive": DRIVE})
    config.set_secret("drive_token", DRIVE_LOGIN)
    kit = teamkit.collect(include_youtube_login=True)
    d = kit["drive"]
    assert "login" not in d and "client" not in d                       # YouTube carries the sign-in app
    assert not {k for k in d if k.startswith("event_folder")} and d["folder_id"] == "fo1" and d["height"] == 540
    lines = "\n".join(teamkit.summary(kit))
    assert "Google Drive: every match to the folder 'FRC match videos', each laptop logs in once, 540p" in lines
    path = teamkit.write(tmp_path / "k", teamkit.collect(include_drive_login=True), PASS)
    assert "drive-notreal" not in path.read_text(encoding="utf-8")

    laptop("b")
    config.save_sections({"drive": {"event_folder_id": "old", "event_folder_event": "2025mttd"}})
    done = teamkit.apply(teamkit.read(path, PASS))
    assert "Google Drive (logged in)" in done and config.get_secret("drive_token") == DRIVE_LOGIN
    dr = config.section("drive")
    assert dr == {"enabled": True, **{k: DRIVE[k] for k in teamkit.DRIVE_KEYS if k in DRIVE}}
    assert "drive-notreal" not in (tmp_path / "b" / "config.toml").read_text(encoding="utf-8")


def test_google_drive_alone_carries_the_sign_in_app(laptop, tmp_path):
    laptop("a")
    config.save_sections({"drive": DRIVE, "youtube": {"enabled": False}})
    config.set_secret("youtube_client", json.dumps(CLIENT))
    config.set_secret("drive_token", DRIVE_LOGIN)
    kit = teamkit.collect()                            # Drive alone is enough to share
    assert set(kit) >= {"drive"} and "youtube" not in kit and kit["drive"]["client"] == CLIENT
    assert "login" not in kit["drive"]
    path = teamkit.write(tmp_path / "k", kit, PASS)
    laptop("b")
    assert teamkit.apply(teamkit.read(path, PASS)) == ["Google Drive (log in once with setup --section drive)"]
    assert json.loads(config.get_secret("youtube_client")) == CLIENT and config.get_secret("drive_token") is None


def test_the_team_wiki_and_its_bot_password_travel_too(laptop, tmp_path):
    """The wiki's address, bot user, bot password and choices go to the next laptop; its page (an event's) does not."""
    laptop("a")
    config.save_sections({"event": {"key": "2026mttd"},
                          "wiki": {"enabled": True, "api_url": "https://www.team5026.com/api.php",
                                   "username": "Ian@StreamSplitter", "private": True, "version": "score",
                                   "embed_format": "<video>File:{file}</video>",
                                   "page_format": "{event_name} ({year}) match videos",
                                   "page_title": "Madtown Throwdown (2026) match videos", "page_event": "2026mttd",
                                   "event_page": "Madtown Throwdown (2026)"}})
    config.set_secret("wiki_bot_password", "wikibotpassword-notreal")
    kit = teamkit.collect()                            # the wiki alone is enough to share
    assert "slack" not in kit and kit["wiki"]["password"] == "wikibotpassword-notreal"
    assert not {"page_title", "page_event", "event_page"} & set(kit["wiki"])
    lines = "\n".join(teamkit.summary(kit))
    assert "Team wiki: every match to www.team5026.com as Ian@StreamSplitter, members-only" in lines
    assert "wikibotpassword-notreal" not in lines
    path = teamkit.write(tmp_path / "k", kit, PASS)
    assert "wikibotpassword-notreal" not in path.read_text(encoding="utf-8")

    laptop("b")
    config.save_sections({"wiki": {"page_title": "Old page", "page_event": "2025mttd"}})
    assert teamkit.apply(teamkit.read(path, PASS)) == ["the team wiki"]
    assert config.get_secret("wiki_bot_password") == "wikibotpassword-notreal"
    wk = config.section("wiki")
    assert wk == {"enabled": True, "api_url": "https://www.team5026.com/api.php", "username": "Ian@StreamSplitter",
                  "version": "score", "private": True, "embed_format": "<video>File:{file}</video>",
                  "page_format": "{event_name} ({year}) match videos"}
    assert "wikibotpassword-notreal" not in (tmp_path / "b" / "config.toml").read_text(encoding="utf-8")
