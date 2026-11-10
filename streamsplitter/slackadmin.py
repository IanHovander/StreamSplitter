"""Settings and people from Slack: the owner's commands (user, 2026-11-05: "All settings should be changable within
slack ... make it so you can add people within slack/message them from the splitter").

    settings [area]               every setting, its value and the exact command that changes it
    set <area> <name> <value>     change one (SETTINGS; `help set` shows them all in Slack)
    set owner @name               who gets the alerts (asks first; the old owner stays an admin)
    set event <code>              save another event without starting it (listener.py; asks first)
    people                        who may use the app, and who the admins are (anyone allowed may ask)
    add @name                     let someone start and stop recordings: a Slack @mention (it arrives as <@U...>), or
                                  part of a name or an email address, searched with users:read / users:read.email;
                                  several matches are listed by number and the admin replies with one
    add admin @name               the same, as an admin (or make a person an admin)
    remove @name                  take that away (remove admin @name: no longer an admin, still a person)
    help <command or topic>       the details (help_topic)

Roles (user, 2026-11-09: after "set owner @Jamie Lee" the person who installed it, who also got the alerts until then,
could no longer change a setting during the event). Admins change settings, add and remove people, and change a
running recording (set stream, set teams, pause, resume: publish/commands.py). The owner is the admin who gets the
alerts ([notify] slack_user); the other admins are [listen] admins, ids that are also in [listen] users. Handing the
alerts over (set owner) keeps the old owner as an admin, so nobody locks themselves out, and there is always at least
one admin (the owner, who cannot be removed, only replaced). The people ([listen] users / user_names, kept as
before, so entries made by an earlier setup still work) may use help, status, split, yes, no and stop. Anything else
gets a no that names the admins and says what to do (refuse). Nobody is set up in advance: the setup no longer asks
for people.

Nobody is messaged unasked. Adding a person sends them nothing. Then the owner is asked "Send them a short how-to
note? Reply yes or no" (no default), and only a yes sends it. The only other message to someone who did not write
is the one to a new owner (set owner), who gets every alert from then on anyway.

Questions. A risky change asks first: YouTube public, The Blue Alliance video links (tbalink.WARNING; only with the
write key already saved), YouTube at an official FIRST event, a new owner, turning the listener off; so do the how-to
note and picking one of several people. They reuse the listener's pending questions (listener.py: one per person, a
new one replaces the old, only the asker's yes counts, QUESTION_S by Slack's message times): Admin.handle returns the
question and the caller keeps it; Admin.confirm answers it. With no listener alive, the recording's own poller keeps
it (publish/commands.py), in memory: a restart forgets it, and the admin asks again.

Who answers (publish/commands.py). While a Slack listener of this version is alive on the computer it answers these
in every DM. With no listener alive, the recording's poller answers them in its owner's DM. With neither, nothing
reads Slack.

When a change applies. A recording reads its settings when it starts (live.build_publishing), so most changes apply
from the next recording start; each reply says so, and says when a recording runs now with the old setting. stream,
teams, pause and resume change a running recording at once (LiveControls), and while nothing records they are saved
for the next start. Owner, admin and people changes apply at once in the listener (it reloads its pollers); a
running recording and the listener keep their alerts in the old owner's DM until they restart.

Secrets stay on the computer. Slack keeps every message, so the Slack token, the Google logins (YouTube, Drive), the
Blue Alliance read and write keys, the wiki's password and the team settings file are never typed into Slack: those
settings answer with the exact command to run on the computer (COMPUTER). A message that looks like a token or key
(notify._SECRET, or a long key-like word) is not used or repeated: the reply says to revoke it and make a new one
(secret_reply). Also computer-only, with the reason in COMPUTER: how the listener runs (background or window) and
turning it on, the test upload, and the data folders.
"""

from __future__ import annotations

import json
import re
import socket
import time
from dataclasses import dataclass

from . import config, streams

QUESTION_S = 900.0         # a question waits this long for its answer (by Slack's message times)
PICK_MAX = 9               # people listed when a name matches several
NOW, LIVE, START, LISTENER = "now", "live", "start", "listener"       # when a setting applies

RUN = "`run.bat --section {s}` (Mac/Linux: `sh run.sh --section {s}`)"
ON_WORDS = {"on", "yes", "true", "1", "enable", "enabled"}
OFF_WORDS = {"off", "no", "false", "0", "disable", "disabled"}
YES_WORDS = ("yes", "y")
NO_WORDS = ("no", "n", "nope", "nah", "cancel", ":-1:", ":thumbsdown:", ":x:")
HEADS = ("settings", "setting", "set", "people", "admins", "add", "remove")   # what Admin answers (and stop listener)


def run_cmd(section: str) -> str:
    """"`run.bat --section youtube` (Mac/Linux: `sh run.sh --section youtube`)"."""
    return RUN.format(s=section)


# ---------------------------------------------------------------------------------------------------------------
# The settings, as `settings` shows them and `help set` lists them
# ---------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Setting:
    area: str
    name: str            # "" for the area's on / off
    usage: str           # what to type
    what: str            # what it does, one line
    when: str            # NOW, LIVE (a running recording at once; else the next start), START, LISTENER


AREAS = (("event", "Event and stream"), ("slack", "Slack"), ("youtube", "YouTube"), ("drive", "Google Drive"),
         ("wiki", "Team wiki"), ("owner", "Owner and people"), ("listener", "Slack listener"))
AREA_WORDS = {"event": "event", "stream": "event", "slack": "slack", "youtube": "youtube", "yt": "youtube",
              "drive": "drive", "gdrive": "drive", "google": "drive", "wiki": "wiki", "website": "wiki",
              "owner": "owner", "contact": "owner", "alerts": "owner", "people": "owner", "listener": "listener",
              "listen": "listener", "listening": "listener"}

VERSION_WHAT = ("score: the match plus its results screen, about 2-3 min after the end (up to 5); clip: the match "
                "alone, about 1-1.5 min after the end")      # user 2026-11-07: Drive within 4 min

SETTINGS = (
    Setting("event", "event", "split <event>  or  set event <code>",
            "the event: split starts recording it, set event only saves it (both ask first)", START),
    Setting("event", "stream", "set stream <link>  or  set stream auto",
            "the stream to record; auto follows the link The Blue Alliance lists", LIVE),
    Setting("slack", "", "set slack on | off",
            "post your team's matches to the channel (alerts and commands work either way)", START),
    Setting("slack", "teams", "set teams 5026, 254", "whose matches go to the channel (set teams none: nobody's)",
            LIVE),
    Setting("slack", "channel", "set slack channel #name", "the channel for the matches", START),
    Setting("slack", "size", "set slack size 720 | full",
            "720p (about 40 MB, quicker on venue Wi-Fi) or as cut (1080p)", START),
    Setting("youtube", "", "set youtube on | off", "upload matches to YouTube (the login is made on the computer)",
            START),
    Setting("youtube", "privacy", "set youtube privacy unlisted | public | private",
            "who can see the playlist and the videos (public asks first)", START),
    Setting("youtube", "matches", "set youtube matches all | others",
            "every match, or only the matches without your teams (yours go to Slack)", START),
    Setting("youtube", "playlist", "set youtube playlist <name>", "this event's playlist (found or made on YouTube)",
            START),
    Setting("youtube", "title", "set youtube title <format>",
            "video titles, with {fields}: help youtube lists them", START),
    Setting("youtube", "description", "set youtube description <format>",
            "video descriptions (several lines are fine)", START),
    Setting("youtube", "tbalinks", "set youtube tbalinks on | off",
            "add each video to its match on The Blue Alliance (event organizers; asks first)", START),
    Setting("youtube", "version", "set youtube version score | clip", VERSION_WHAT, START),
    Setting("youtube", "pause", "pause youtube  /  resume youtube", "hold the uploads, or send them", LIVE),
    Setting("drive", "", "set drive on | off", "upload every match to the Google Drive folder", START),
    Setting("drive", "size", "set drive size 720 | 540 | full", "video size in Drive (720p is about 40 MB)", START),
    Setting("drive", "names", "set drive names <format>", "file names, with {fields}: help drive lists them", START),
    Setting("drive", "version", "set drive version score | clip", VERSION_WHAT, START),
    Setting("drive", "pause", "pause drive  /  resume drive", "hold the uploads, or send them", LIVE),
    Setting("wiki", "", "set wiki on | off", "upload every match to the team wiki", START),
    Setting("wiki", "version", "set wiki version score | clip", VERSION_WHAT, START),
    Setting("wiki", "private", "set wiki private on | off", "members-only page and videos", START),
    Setting("wiki", "page", "set wiki page <title>", "the wiki page for this event's videos", START),
    Setting("wiki", "embed", "set wiki embed <markup>", "how each video shows on the page, with {file}", START),
    Setting("wiki", "pause", "pause wiki  /  resume wiki", "hold the uploads, or send them", LIVE),
    Setting("owner", "owner", "set owner @name", "the admin who gets the alerts (asks first; the old one stays an "
                                                 "admin)", NOW),
    Setting("owner", "admins", "add admin @name  /  remove admin @name",
            "who else may change settings, people and the running recording", NOW),
    Setting("owner", "people", "add @name  /  remove @name", "who else may start and stop recordings", NOW),
    Setting("listener", "alarm", "set listener alarm on | off", "a DM to the owner if the listener stops", LISTENER),
    Setting("listener", "awake", "set listener awake on | off", "keep this computer from sleeping while it listens",
            LISTENER),
    Setting("listener", "off", "set listener off",
            "stop listening and no longer start at log-in (asks first; it is turned on again on the computer)", NOW),
)

# (what, setup step, why it stays on the computer)
COMPUTER = (
    ("the Slack app's token", "slack", "it is a password, and Slack keeps every message"),
    ("the YouTube login and YouTube's Terms of Service", "youtube",
     "it is a Google sign-in in the browser there, and the terms need a person's own OK"),
    ("the Google Drive login and its folder", "drive",
     "it is a Google sign-in in the browser there, and a new folder must also be shared with the team in Drive"),
    ("The Blue Alliance read key", "tba", "it is a password"),
    ("The Blue Alliance write key (for the video links)", "tbalinks", "it is a password"),
    ("the wiki's address, bot name and password", "wiki",
     "it is a password, checked against the wiki as it is typed"),
    ("the team settings file", "team", "it carries every password, locked with a passphrase"),
    ("how the listener runs (in the background or in a window), and turning it on", "listen",
     "it adds or removes the log-in start and starts a program there"),
    ("the test upload", "test", "a person checks the sample video on each site and deletes it"),
    ("the folders for recordings and clips (data_root, work_root, deliver_dir in config.toml)", None,
     "they are folders on that computer; moving them during an event loses track of the recordings"),
)
# set <area> <name> words that mean a computer-only thing: the setup step to run
COMPUTER_WORDS = {("slack", "token"): "slack", ("slack", "app"): "slack", ("youtube", "login"): "youtube",
                  ("youtube", "account"): "youtube", ("youtube", "channel"): "youtube", ("youtube", "terms"): "youtube",
                  ("drive", "login"): "drive", ("drive", "account"): "drive", ("drive", "folder"): "drive",
                  ("wiki", "password"): "wiki", ("wiki", "login"): "wiki", ("wiki", "address"): "wiki",
                  ("wiki", "url"): "wiki", ("wiki", "username"): "wiki", ("wiki", "user"): "wiki",
                  ("listener", "mode"): "listen", ("listener", "on"): "listen", ("tba", ""): "tba",
                  ("tba", "key"): "tba", ("key", ""): "tba", ("token", ""): "slack", ("password", ""): "wiki",
                  ("team", ""): "team", ("test", ""): "test", ("youtube", "key"): "tbalinks",
                  ("tbalinks", "key"): "tbalinks", ("data", ""): None, ("folder", ""): None}


# ---------------------------------------------------------------------------------------------------------------
# Secrets typed into Slack
# ---------------------------------------------------------------------------------------------------------------

# a TBA key (64 letters and digits), a wiki bot password (32 lowercase letters and digits) or another long key: letters
# and digits mixed, not a word inside a link, not a long hyphenated name
_KEYLIKE = re.compile(r"(?<![\w/.:=?&%-])(?:(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{40,}"
                      r"|(?=[a-z0-9]*\d)(?=[a-z0-9]*[a-z])[a-z0-9]{32})(?![\w/.-])")


def looks_secret(text: str) -> str | None:
    """"slack", "google" or "key" when `text` holds something shaped like a token or key; else None."""
    from .publish.notify import _SECRET
    s = streams.unwrap_slack(text or "")
    m = _SECRET.search(s)
    if m:
        return "slack" if m.group(0).startswith(("xox", "xapp")) else "google"
    return "key" if _KEYLIKE.search(s) else None


def secret_reply(text: str) -> str | None:
    """The reply to a message that looks like it holds a token or key (None otherwise). Nothing of it is used or
    repeated: it is in Slack's history now, so it should be revoked and replaced, and entered on the computer."""
    kind = looks_secret(text)
    if kind is None:
        return None
    how = {"slack": f"For the Slack app's token: api.slack.com/apps, StreamSplitter, OAuth & Permissions, Revoke; then "
                    f"reinstall the app and enter the new token on the computer with {run_cmd('slack')}.",
           "google": f"For a Google login: remove StreamSplitter at myaccount.google.com/permissions, then log in "
                     f"again on the computer with {run_cmd('youtube')}.",
           "key": f"For a Blue Alliance key: delete it at thebluealliance.com/account, make a new one and enter it on "
                  f"the computer with {run_cmd('tba')}. For a wiki password: make a new one and enter it with "
                  f"{run_cmd('wiki')}."}[kind]
    return ("That looks like a password or key, so I did not use it or repeat it. Slack keeps every message, so treat "
            f"it as seen: revoke it and make a new one. {how} Then delete your message. Passwords and keys are only "
            f"ever entered on the computer.")


# ---------------------------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------------------------

def _on_off(value: str) -> bool | None:
    v = (value or "").strip().lower().rstrip(".!")
    return True if v in ON_WORDS else False if v in OFF_WORDS else None


def _yes(b) -> str:
    return "on" if b else "off"


def _and(items: list[str]) -> str:
    items = [str(x) for x in items]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


def _err(e: Exception) -> str:
    from .publish.notify import redact
    return redact(str(e) or type(e).__name__)


def _person_name(p: dict) -> str:
    return p.get("real_name") or p.get("display_name") or p.get("name") or p["id"]


def _person_line(p: dict) -> str:
    extra = ", ".join(x for x in (p.get("name") and "@" + p["name"], p.get("email")) if x)
    return f"{_person_name(p)} ({extra})" if extra else _person_name(p)


_MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
_CHANNEL = re.compile(r"<#([CG][A-Z0-9]+)(?:\|([^>]*))?>")
_MAILTO = re.compile(r"<mailto:([^|>]+)(?:\|[^>]*)?>", re.I)


def people() -> tuple[list[str], list[str]]:
    """([listen] users, their names in the same order; a missing name is the id)."""
    cfg = config.section("listen")
    users = [u.strip() for u in cfg.get("users") or [] if isinstance(u, str) and u.strip()]
    names = list(cfg.get("user_names") or [])
    return users, [names[i] if i < len(names) and names[i] else u for i, u in enumerate(users)]


def owner() -> tuple[str | None, str]:
    """(the owner's Slack id, their name): the alerts contact, [notify] slack_user."""
    nt = config.section("notify")
    return nt.get("slack_user"), nt.get("slack_user_name") or nt.get("slack_user") or "the alerts contact"


def admin_ids() -> list[str]:
    """The admins: the owner first, then [listen] admins that are among the people (module docstring: roles)."""
    o, _ = owner()
    users, _ = people()
    extra = [a for a in dict.fromkeys(config.section("listen").get("admins") or [])
             if isinstance(a, str) and a in users and a != o]
    return ([o] if o else []) + extra


def admin_names() -> list[str]:
    o, oname = owner()
    users, names = people()
    return [oname if a == o else names[users.index(a)] for a in admin_ids()]


def is_admin_user(user: str | None) -> bool:
    return bool(user) and user in admin_ids()


def is_admin(text: str) -> bool:
    """Whether a message is one of Admin's commands (not stream / teams / pause / resume, which a running recording
    answers itself, written `set stream` / `set teams` too)."""
    words = streams.unwrap_slack(text or "").strip().split()
    if not words:
        return False
    head = words[0].lower().lstrip("/!").rstrip(":")
    if head == "stop" and len(words) > 1 and words[1].lower() in ("listener", "listening", "the"):
        return " ".join(words[1:]).lower() in ("listener", "listening", "the listener")
    if head not in HEADS:
        return False
    if head == "set" and len(words) > 1:
        sub = [w.lower() for w in words[1:3]]
        if sub[0] in ("stream", "link", "teams", "team") or sub[:2] == ["slack", "teams"] or \
                sub[:2] == ["slack", "team"]:
            return False
    return True


# ---------------------------------------------------------------------------------------------------------------
# YouTube playlists (also listener.py's split)
# ---------------------------------------------------------------------------------------------------------------

def make_playlist(client, title: str, ev: dict, privacy: str) -> dict:
    """The channel's playlist named `title` (its privacy changed to `privacy` when it differs), else a new one."""
    pl = client.find_playlist(title)
    if pl and pl.get("privacy") != privacy:
        pl = {**pl, **(client.set_playlist_privacy(pl["id"], privacy) or {}), "privacy": privacy}
    if not pl:
        year, name = ev.get("year") or "", ev.get("name") or ""
        pl = client.create_playlist(title, f"Matches from the {year} {name}, recorded and cut by "
                                           "StreamSplitter.".replace("  ", " "), privacy)
    return pl


def _youtube_client():
    from . import listener
    return listener._youtube_client()


def _logged_in(*names: str) -> bool:
    return all(config.get_secret(n) for n in names)


# ---------------------------------------------------------------------------------------------------------------
# The commands
# ---------------------------------------------------------------------------------------------------------------

class Admin:
    """The owner's settings and people commands (module docstring), for the Slack listener and, with no listener
    alive, the recording's poller. handle() answers a command, confirm() a question it asked. Everything outside
    (Slack, YouTube, the recordings, the listener) is passed in, so tests use fakes.

    `recording()`: whether a recording runs on this computer (replies say when a change applies). `listener`: the
    listener.Listener answering (it reloads its people after a change, and stops for `set listener off`); None in
    the recording's poller. `youtube()`: a YouTube client for this computer's login."""

    def __init__(self, client, *, host: str | None = None, log=print, clock=time.time, recording=lambda: False,
                 listener=None, youtube=None, login_off=None, tba_check=None):
        self.client = client
        self.host = host or socket.gethostname()
        self.log, self.clock = log, clock
        self.recording = recording
        self.listener = listener
        self.youtube = youtube or _youtube_client
        self.login_off = login_off              # removes the listener's log-in start (default: listener's)
        self.tba_check = tba_check              # (auth_id, secret, event key) -> problem or None (default: tbalink)
        self._user: str | None = None           # who sent the command being answered (handle, confirm)

    # roles ----------------------------------------------------------------------------------------------------------
    def is_admin_user(self, user: str | None) -> bool:
        return is_admin_user(user)

    def refuse(self, user: str | None = None, what: str = "change settings and people") -> str:
        """The no for someone who is not an admin: who the admins are, and what to do next."""
        names = admin_names() or [owner()[1]]
        you = self._name(user) if user else "your name"
        return (f"Only admins can {what}: {_and(names)}. Ask {'one of them' if len(names) > 1 else 'them'} to do it, "
                f"or to make you an admin (they reply add admin @{you}). Reply help for what you can do.")

    # one command ----------------------------------------------------------------------------------------------------
    def handle(self, user: str, text: str) -> tuple[str, dict | None]:
        """(the reply, a question to keep for its answer or None) for one of the admins' commands (people: anyone's)."""
        self._user = user
        raw = (text or "").strip()
        plain = streams.unwrap_slack(raw).strip()
        words = plain.split(None, 1)
        head = words[0].lower().lstrip("/!").rstrip(":") if words else ""
        rest = words[1].strip() if len(words) > 1 else ""
        raw_rest = raw.split(None, 1)[1].strip() if len(raw.split(None, 1)) > 1 else ""
        if head in ("people", "admins"):
            return self.people_text(), None
        if not is_admin_user(user):
            return self.refuse(user), None
        if head == "stop":                                      # stop listener
            return self.ask_listener_off()
        if head in ("settings", "setting"):
            return self.settings_text(rest), None
        if head == "add":
            return self.add(raw_rest)
        if head == "remove":
            return self.remove(raw_rest)
        if head == "set":
            return self.set(rest, raw_rest)
        return self.unknown(), None

    def unknown(self) -> str:
        return "I did not understand that. Reply settings for every setting and the command that changes it."

    # settings -------------------------------------------------------------------------------------------------------
    def settings_text(self, area: str = "") -> str:
        """Every setting with its value and the command that changes it, grouped by area (one area: `area`)."""
        want = AREA_WORDS.get(area.strip().lower().split()[0]) if area.strip() else None
        if area.strip() and want is None:
            return ("There is no such area. Reply settings for all of them, or settings with one of: "
                    + ", ".join(a for a, _ in AREAS) + ".")
        rec = self._recording()
        lines = [f"StreamSplitter settings on {self.host}" + (" (a recording runs now)" if rec else "") + ":"]
        for a, title in AREAS:
            if want and a != want:
                continue
            lines.append(f"*{title}*")
            for s in SETTINGS:
                if s.area == a:
                    lines.append(f"• {self.value(s)}  ·  `{s.usage}`")
            more = [w for w, step, _ in COMPUTER if step in _AREA_STEPS.get(a, ())]
            if more:
                lines.append(f"   On the computer only: {_and(more)}. help computer says how.")
        lines.append("Change one with the command shown; help set explains each. help computer: what is set only on "
                     "the computer.")
        return "\n".join(lines)

    def value(self, s: Setting) -> str:
        """"stream: https://..." for one setting, in words."""
        ev, sl, yt = config.section("event"), config.section("slack"), config.section("youtube")
        dr, wk, ls = config.section("drive"), config.section("wiki"), config.section("listen")
        k = (s.area, s.name)
        if k == ("event", "event"):
            if not ev.get("key"):
                return "event: none set"
            from .setup_wizard import _event_line
            return f"event: {_event_line(ev)}"
        if k == ("event", "stream"):
            return f"stream: {ev.get('stream_url') or 'none set (follows the link The Blue Alliance lists)'}"
        if k == ("slack", ""):
            where = f", to #{sl['channel_name']}" if sl.get("channel_name") else " (no channel chosen)"
            return f"matches to Slack: {_yes(sl.get('enabled'))}{where if sl.get('enabled') else ''}"
        if k == ("slack", "teams"):
            return f"teams: {', '.join(map(str, sl.get('teams') or [])) or 'none'}"
        if k == ("slack", "channel"):
            return f"channel: #{sl['channel_name']}" if sl.get("channel_name") else "channel: none chosen"
        if k == ("slack", "size"):
            return "video size: " + ("720p" if int(sl.get("clip_height") or 0) else "as cut (1080p)")
        if k == ("youtube", ""):
            off_for = ls.get("youtube_off_for")
            state = _yes(yt.get("enabled"))
            if not yt.get("enabled") and off_for and off_for == ev.get("key"):
                state = "off for this official event (split turns it back on for the next one)"
            login = "logged in" if _logged_in("youtube_token", "youtube_client") else "not logged in on this computer"
            return f"YouTube: {state} ({login})"
        if k == ("youtube", "privacy"):
            return f"privacy: {yt.get('privacy') or 'unlisted'}"
        if k == ("youtube", "matches"):
            return "matches: " + ("only the matches without your teams" if yt.get("route") == "others"
                                  else "every match")
        if k == ("youtube", "playlist"):
            if yt.get("playlist_title") and yt.get("playlist_event") == ev.get("key"):
                return f"playlist: {yt['playlist_title']}"
            return "playlist: none for this event yet"
        if k == ("youtube", "title"):
            from .publish import templates
            return f"title: {templates.example(yt.get('title_format') or templates.TITLE_FORMAT)} (for a sample match)"
        if k == ("youtube", "description"):
            fmt = yt.get("description_format") or ""
            n = len(fmt.strip("\n").splitlines())
            return f"description: {n} line{'s' if n != 1 else ''}" if fmt else "description: the standard one"
        if k == ("youtube", "tbalinks"):
            key = "write key saved" if _logged_in("tba_write_id", "tba_write_secret") else "no write key saved"
            return f"Blue Alliance video links: {_yes(yt.get('tba_links'))} ({key})"
        if k[1] == "pause":
            return f"uploads: {'paused' if config.section(s.area).get('paused') else 'not paused'}"
        if k[1] == "version":
            v = config.section(s.area).get("version") or "score"
            return "version: " + ("the match with its results screen (score)" if v == "score" else "the match alone (clip)")
        if k == ("drive", ""):
            login = "logged in" if _logged_in("drive_token", "youtube_client") else "not logged in on this computer"
            folder = f", folder \"{dr['folder_title']}\"" if dr.get("folder_title") else ""
            return f"Google Drive: {_yes(dr.get('enabled'))}{folder} ({login})"
        if k == ("drive", "size"):
            h = 720 if dr.get("height") is None else int(dr["height"])
            return "video size: " + (f"{h}p" if h else "as recorded (1080p)")
        if k == ("drive", "names"):
            from .publish import drive, templates
            try:
                return f"file names: {drive.render_name(dr.get('name_format') or drive.NAME_FORMAT, templates.SAMPLE)}"\
                       ".mp4 (for a sample match)"
            except Exception:  # noqa: BLE001 - only shown
                return "file names: (could not show them)"
        if k == ("wiki", ""):
            login = "logged in" if (wk.get("api_url") and wk.get("username") and
                                    _logged_in("wiki_bot_password")) else "not logged in on this computer"
            return f"team wiki: {_yes(wk.get('enabled'))} ({login})"
        if k == ("wiki", "private"):
            return f"members-only: {_yes(wk.get('private', True))}"
        if k == ("wiki", "page"):
            if wk.get("page_title") and wk.get("page_event") == ev.get("key"):
                return f"page: {wk['page_title']}"
            return "page: one named after the event"
        if k == ("wiki", "embed"):
            from .publish import wiki
            return f"video markup: {wk.get('embed_format') or wiki.EMBED_FORMAT}"
        if k == ("owner", "owner"):
            return f"owner (gets the alerts): {owner()[1]}"
        if k == ("owner", "admins"):
            others = admin_names()[1:]
            return f"other admins: {_and(others) if others else 'none'}"
        if k == ("owner", "people"):
            users, names = people()
            admins = set(admin_ids())
            plain = [n for u, n in zip(users, names) if u not in admins]
            return f"people: {_and(plain) if plain else 'nobody else yet'}"
        if k == ("listener", "alarm"):
            from .listener import WINDOW, mode_of
            return f"alarm: {_yes(ls.get('alarm', mode_of(ls) != WINDOW))}"
        if k == ("listener", "awake"):
            return f"keep awake: {_yes(ls.get('keep_awake', True))}"
        if k == ("listener", "off"):
            from .listener import WINDOW, mode_of
            how = ("off" if not ls.get("enabled") else "only while its window is open" if mode_of(ls) == WINDOW
                   else "in the background from each log-in")
            return f"Start from Slack: {how}"
        return s.name

    def _recording(self) -> bool:
        try:
            return bool(self.recording())
        except Exception:  # noqa: BLE001 - only changes the wording
            return False

    def when(self, kind: str) -> str:
        """When a change of `kind` (START, LISTENER) applies, as the end of a reply."""
        if kind == LISTENER:
            return " It applies the next time the Slack listener starts."
        if self._recording():
            return " It applies when the next recording starts; the one running now keeps the old setting."
        return " It applies when the next recording starts."

    # set ------------------------------------------------------------------------------------------------------------
    def set(self, rest: str, raw_rest: str = "") -> tuple[str, dict | None]:
        """set <area> <name> <value>: check the value as the setup does, save it, say when it applies."""
        words = rest.split(None, 2)
        if not words:
            return "set needs what to change, e.g. set youtube privacy unlisted. Reply settings to see them all.", None
        a = words[0].lower()
        area = AREA_WORDS.get(a)
        name = words[1].lower().rstrip(":") if len(words) > 1 else ""
        value = words[2].strip() if len(words) > 2 else ""
        step = COMPUTER_WORDS.get((area or a, name), COMPUTER_WORDS.get((a, ""), "?"))
        if step != "?":
            return self.computer_only(step), None
        if a in ("owner", "contact", "alerts"):
            return self.ask_owner(raw_rest.split(None, 1)[1] if len(raw_rest.split(None, 1)) > 1 else "")
        if area == "owner":
            return ("Reply add @name to let someone in, add admin @name for an admin, or remove @name. people lists "
                    "them."), None
        if area is None:
            return (f"There is no setting called that. Areas: {', '.join(x for x, _ in AREAS)}. Reply settings to see "
                    f"every setting with the command that changes it.", None)
        if a == "event":
            # the listener answers set event itself (listener.py); here a recording runs
            return (f"A recording runs on {self.host}, so the event changes once it is stopped: stop it on the "
                    f"computer, then change the event there with {run_cmd('event')}.", None)
        if name in ("pause", "paused", "resume") and area in ("youtube", "drive", "wiki"):
            return f"Reply pause {area} to hold the uploads, or resume {area} to send them.", None
        if name in ("on", "off", "enabled", "disabled") and not value:
            value, name = name, ""
        elif name in ("enabled", "enable") and value:
            name = ""
        fn = getattr(self, f"_set_{area}", None)
        if fn is None:
            return self.unknown(), None
        # formats keep their own spacing and capitals: the value as typed, with Slack's escapes undone
        full = streams.unwrap_slack(raw_rest).split(None, 2)
        typed = full[2].strip() if len(full) > 2 and name else value
        return fn(name, value, typed)

    def computer_only(self, step: str | None) -> str:
        if step is None:
            return ("The folders are set in config.toml on the computer (data_root, work_root, deliver_dir); change "
                    "them between events, never while a recording runs.")
        what = next((w for w, s, _ in COMPUTER if s == step), "that")
        why = next((y for _, s, y in COMPUTER if s == step), "")
        return (f"{what[:1].upper()}{what[1:]} can only be changed on the computer, since {why}. There, run "
                f"{run_cmd(step)}. Never type passwords or keys into Slack.")

    def _bad(self, usage: str) -> tuple[str, None]:
        return f"Reply {usage}.", None

    def _set_version(self, area: str, value: str) -> tuple[str, None]:
        """[youtube|drive|wiki] version: "score" waits for the results screen, "clip" sends the match alone at once
        (publisher.on_score)."""
        v = value.lower().rstrip(".")
        if v not in ("score", "clip"):
            return self._bad(f"set {area} version score (the match with its results screen, about 2-3 min after the "
                             f"end), or set {area} version clip (the match alone, about 1-1.5 min after the end)")
        config.save_sections({area: {"version": v}})
        where = {"youtube": "YouTube", "drive": "Google Drive", "wiki": "the team wiki"}[area]
        what = ("each match with its results screen (about 2-3 min after the end; the match alone when no results "
                "screen comes)" if v == "score" else "each match alone, about 1-1.5 min after the end")
        return f"OK: {where} gets {what}." + self.when(START), None

    # slack
    def _set_slack(self, name: str, value: str, typed: str) -> tuple[str, dict | None]:
        if name == "":
            on = _on_off(value)
            if on is None:
                return self._bad("set slack on, or set slack off")
            sl = config.section("slack")
            if on and not sl.get("channel_id"):
                return "Choose the channel first: set slack channel #name (then set slack on).", None
            config.save_sections({"slack": {"enabled": on}})
            what = (f"OK: your team's matches go to #{sl.get('channel_name')}." if on else
                    "OK: no match goes to the Slack channel. Alerts and these commands keep working.")
            return what + self.when(START), None
        if name == "channel":
            return self._slack_channel(typed)
        if name in ("teams", "team"):                # parse() sends set slack teams to `teams`; this is a fallback
            return "Reply teams 5026, 254 (teams alone shows them).", None
        if name in ("size", "height", "quality"):
            v = value.lower().rstrip("p")
            if v not in ("720", "full", "1080", "0", "as cut"):
                return self._bad("set slack size 720 (about 40 MB a match), or set slack size full (as cut, 1080p)")
            h = 720 if v == "720" else 0
            config.save_sections({"slack": {"clip_height": h}})
            return (f"OK: matches go to Slack {'at 720p (about 40 MB)' if h else 'as cut (1080p)'}." + self.when(START),
                    None)
        return self._no_such("slack", name)

    def _slack_channel(self, typed: str) -> tuple[str, None]:
        m = _CHANNEL.search(typed)
        name = m.group(1) if m else typed.strip().lstrip("#").split()[0] if typed.strip() else ""
        if not name:
            return self._bad("set slack channel #name")
        try:
            ch = self.client.find_channel(name)
        except Exception as e:  # noqa: BLE001
            return f"Could not look the channel up ({_err(e)}). Try again in a minute.", None
        if ch is None:
            return ("No such channel was found. A private channel shows up only once the app is in it: type /invite "
                    "@StreamSplitter in it, then try again."), None
        if not ch.get("is_member"):
            if ch.get("is_private"):
                return f"Invite the app first: type /invite @StreamSplitter in #{ch['name']}, then try again.", None
            try:
                self.client.join_channel(ch["id"])
            except Exception as e:  # noqa: BLE001
                return (f"The app could not join #{ch['name']} ({_err(e)}). Type /invite @StreamSplitter there, then "
                        f"try again."), None
        config.save_sections({"slack": {"channel_id": ch["id"], "channel_name": ch["name"]}})
        on = "" if config.section("slack").get("enabled") else " Slack posting is off: set slack on to post there."
        return f"OK: your team's matches go to #{ch['name']}.{on}" + self.when(START), None

    # youtube
    def _set_youtube(self, name: str, value: str, typed: str, confirmed: bool = False) -> tuple[str, dict | None]:
        yt, ev = config.section("youtube"), config.section("event")
        if name == "":
            on = _on_off(value)
            if on is None:
                return self._bad("set youtube on, or set youtube off")
            if not on:
                config.save_sections({"youtube": {"enabled": False}, "listen": {"youtube_off_for": None}})
                return "OK: nothing is uploaded to YouTube." + self.when(START), None
            return self._youtube_on(confirmed)
        if name == "privacy":
            v = value.lower().rstrip(".")
            if v not in ("unlisted", "public", "private"):
                return self._bad("set youtube privacy unlisted (only people with the link), public (anyone, and it "
                                 "shows in search) or private (only you)")
            if v == "public" and not confirmed and yt.get("privacy") != "public":
                return ("Public means every match video can be found by anyone searching YouTube. Make the playlist "
                        "and the new videos public? Reply yes or no.",
                        {"action": "set", "area": "youtube", "name": "privacy", "value": "public",
                         "again": "set youtube privacy public"})
            return self._youtube_privacy(v), None
        if name == "version":
            return self._set_version("youtube", value)
        if name in ("matches", "route"):
            v = value.lower().rstrip(".")
            if v not in ("all", "every", "others", "other"):
                return self._bad("set youtube matches all (every match), or set youtube matches others (only the "
                                 "matches without your teams)")
            route = "all" if v in ("all", "every") else "others"
            config.save_sections({"youtube": {"route": route}})
            what = "every match" if route == "all" else "only the matches without your teams (yours go to Slack)"
            return f"OK: YouTube gets {what}." + self.when(START), None
        if name == "playlist":
            return self._youtube_playlist(typed), None
        if name in ("title", "description"):
            from .publish import templates
            fmt = typed.strip("\n") if name == "description" else typed.strip()
            try:
                templates.validate(fmt, name)
            except ValueError as e:
                return f"That {name} cannot be used: {e}.", None
            config.save_sections({"youtube": {f"{name}_format": fmt}})
            sample = templates.example(fmt)
            shown = sample if name == "title" else "\n".join(sample.splitlines()[:6])
            return f"OK: the video {name}, for a sample match:\n{shown}\n" + self.when(START).strip(), None
        if name in ("tbalinks", "tba", "links", "tbalink"):
            return self._youtube_tbalinks(value, confirmed)
        return self._no_such("youtube", name)

    def _youtube_on(self, confirmed: bool) -> tuple[str, dict | None]:
        from .setup_wizard import is_official_event
        yt, ev = config.section("youtube"), config.section("event")
        if not _logged_in("youtube_token", "youtube_client"):
            return (f"This computer is not logged in to YouTube. That is done on the computer: {run_cmd('youtube')}.",
                    None)
        if not yt.get("terms_accepted"):
            return (f"YouTube's Terms of Service must be agreed to first, on the computer: {run_cmd('youtube')}.", None)
        if not ev.get("key"):
            return "Set the event first (split <event>, or set event <code>), then set youtube on.", None
        if is_official_event(ev) and not confirmed:
            return ("This is an official FIRST event: FIRST posts every match on its own YouTube channel, and copies "
                    "of its webcast can be claimed or blocked by YouTube. Upload only if FIRST has said teams may. "
                    "Turn YouTube on anyway? Reply yes or no.",
                    {"action": "set", "area": "youtube", "name": "", "value": "on", "again": "set youtube on"})
        note = ""
        if not (yt.get("playlist_event") == ev["key"] and yt.get("playlist_id")):
            from .publish import templates
            title = templates.render_playlist(templates.PLAYLIST_FORMAT, ev["key"], ev.get("name"), ev.get("year"))
            privacy = yt.get("privacy") or "unlisted"
            try:
                pl = make_playlist(self.youtube(), title, ev, privacy)
            except Exception as e:  # noqa: BLE001
                return f"Could not find or make the playlist ({_err(e)}), so YouTube stays off.", None
            config.save_sections({"youtube": {"playlist_id": pl["id"], "playlist_title": pl.get("title") or title,
                                              "playlist_event": ev["key"]}})
            note = f" Playlist: {pl.get('url')}" if pl.get("url") else ""
        config.save_sections({"youtube": {"enabled": True}, "listen": {"youtube_off_for": None}})
        yt = config.section("youtube")
        return (f"OK: matches upload to YouTube, {yt.get('privacy') or 'unlisted'}, to the playlist "
                f"\"{yt.get('playlist_title')}\".{note}" + self.when(START)), None

    def _youtube_privacy(self, v: str) -> str:
        yt, ev = config.section("youtube"), config.section("event")
        values = {"privacy": v}
        note = ""
        if v == "private" and yt.get("tba_links"):
            values["tba_links"] = False
            note = " The Blue Alliance video links are off now: a private video plays for nobody there."
        config.save_sections({"youtube": values})
        if yt.get("playlist_id") and yt.get("playlist_event") == ev.get("key") and \
                _logged_in("youtube_token", "youtube_client"):
            try:
                self.youtube().set_playlist_privacy(yt["playlist_id"], v)
                note = f" The playlist is {v} now." + note
            except Exception as e:  # noqa: BLE001 - the videos still follow the setting
                note = f" The playlist itself could not be changed ({_err(e)}); change it in YouTube Studio." + note
        return f"OK: new videos are {v}.{note}" + self.when(START)

    def _youtube_playlist(self, typed: str) -> str:
        from .publish import templates
        ev, yt = config.section("event"), config.section("youtube")
        fmt = typed.strip()
        try:
            templates.validate(fmt, "playlist")
        except ValueError as e:
            return f"That playlist name cannot be used: {e}."
        if not ev.get("key"):
            return "Set the event first (split <event>, or set event <code>)."
        if not _logged_in("youtube_token", "youtube_client"):
            return f"This computer is not logged in to YouTube. That is done on the computer: {run_cmd('youtube')}."
        title = templates.render_playlist(fmt, ev["key"], ev.get("name"), ev.get("year"))
        try:
            pl = make_playlist(self.youtube(), title, ev, yt.get("privacy") or "unlisted")
        except Exception as e:  # noqa: BLE001
            return f"Could not find or make that playlist ({_err(e)}). Nothing changed."
        config.save_sections({"youtube": {"playlist_id": pl["id"], "playlist_title": pl.get("title") or title,
                                          "playlist_event": ev["key"]}})
        url = f": {pl['url']}" if pl.get("url") else ""
        return f"OK: this event's matches go to the playlist \"{pl.get('title') or title}\"{url}." + self.when(START)

    def _youtube_tbalinks(self, value: str, confirmed: bool) -> tuple[str, dict | None]:
        from .publish import tbalink
        on = _on_off(value)
        if on is None:
            return self._bad("set youtube tbalinks on, or set youtube tbalinks off")
        yt, ev = config.section("youtube"), config.section("event")
        if not on:
            config.save_sections({"youtube": {"tba_links": False}})
            return "OK: no video is added to The Blue Alliance." + self.when(START), None
        if not yt.get("enabled"):
            return "YouTube is off, so there is nothing to link: set youtube on first.", None
        if yt.get("privacy") == "private":
            return "The videos are private, so a link on The Blue Alliance would play for nobody.", None
        if not _logged_in("tba_write_id", "tba_write_secret"):
            return (f"This needs The Blue Alliance write key for the event, entered on the computer: "
                    f"{run_cmd('tbalinks')}. (Organizers request one at {tbalink.REQUEST_URL}.)"), None
        if not ev.get("key"):
            return "Set the event first (split <event>, or set event <code>).", None
        if not confirmed:
            return (f"{tbalink.WARNING} Add each YouTube video to its match on The Blue Alliance? Reply yes or no.",
                    {"action": "set", "area": "youtube", "name": "tbalinks", "value": "on",
                     "again": "set youtube tbalinks on"})
        check = self.tba_check or (lambda i, s, k: tbalink.TrustedClient(i, s).check(k))
        try:
            problem = check(config.get_secret("tba_write_id"), config.get_secret("tba_write_secret"), ev["key"])
        except Exception as e:  # noqa: BLE001
            problem = _err(e)
        if problem:
            return (f"The saved write key did not work for {ev['key']} ({problem}), so the links stay off. Check it on "
                    f"the computer: {run_cmd('tbalinks')}."), None
        config.save_sections({"youtube": {"tba_links": True}})
        return f"OK: each YouTube video is added to its match on The Blue Alliance ({ev['key']})." + self.when(START), \
            None

    # drive
    def _set_drive(self, name: str, value: str, typed: str) -> tuple[str, None]:
        dr = config.section("drive")
        if name == "":
            on = _on_off(value)
            if on is None:
                return self._bad("set drive on, or set drive off")
            if not on:
                config.save_sections({"drive": {"enabled": False}})
                return "OK: nothing is uploaded to Google Drive." + self.when(START), None
            if not (_logged_in("drive_token", "youtube_client") and dr.get("folder_id")):
                return (f"Google Drive is not set up on this computer (its login and folder). That is done on the "
                        f"computer: {run_cmd('drive')}."), None
            config.save_sections({"drive": {"enabled": True}})
            return (f"OK: every match goes to the Google Drive folder \"{dr.get('folder_title') or dr['folder_id']}\"."
                    + self.when(START)), None
        if name in ("size", "height", "quality"):
            v = value.lower().rstrip("p")
            if v not in ("720", "540", "full", "1080", "0"):
                return self._bad("set drive size 720 (about 40 MB a match), 540 (about 27 MB) or full (as recorded, "
                                 "about 130 MB)")
            h = int(v) if v in ("720", "540") else 0
            config.save_sections({"drive": {"height": h}})
            return f"OK: Drive gets the matches {f'at {h}p' if h else 'as recorded (1080p)'}." + self.when(START), None
        if name == "version":
            return self._set_version("drive", value)
        if name in ("names", "name", "filenames", "files"):
            from .publish import drive, templates
            fmt = typed.strip()
            problem = drive.name_format_problem(fmt)
            if problem:
                return f"Those file names cannot be used: {problem}.", None
            config.save_sections({"drive": {"name_format": fmt}})
            return (f"OK: the files are named like this, for a sample match:\n{drive.render_name(fmt, templates.SAMPLE)}"
                    f".mp4\n" + self.when(START).strip()), None
        return self._no_such("drive", name)

    # wiki
    def _set_wiki(self, name: str, value: str, typed: str) -> tuple[str, None]:
        wk, ev = config.section("wiki"), config.section("event")
        if name == "":
            on = _on_off(value)
            if on is None:
                return self._bad("set wiki on, or set wiki off")
            if not on:
                config.save_sections({"wiki": {"enabled": False}})
                return "OK: nothing is uploaded to the team wiki." + self.when(START), None
            if not (wk.get("api_url") and wk.get("username") and _logged_in("wiki_bot_password")):
                return (f"The team wiki is not set up on this computer (its address and bot password). That is done "
                        f"on the computer: {run_cmd('wiki')}."), None
            config.save_sections({"wiki": {"enabled": True}})
            return "OK: every match goes to the team wiki." + self.when(START), None
        if name == "version":
            return self._set_version("wiki", value)
        if name in ("private", "members", "membersonly"):
            on = _on_off(value)
            if on is None:
                return self._bad("set wiki private on (members only), or set wiki private off")
            config.save_sections({"wiki": {"private": on}})
            return ("OK: the page and its videos are " + ("members-only." if on else "for every reader.")
                    + self.when(START)), None
        if name == "page":
            from .publish import wiki
            title = typed.strip()
            problem = wiki.title_problem(title)
            if problem:
                return f"That page title cannot be used: {problem}.", None
            if not ev.get("key"):
                return "Set the event first (split <event>, or set event <code>).", None
            config.save_sections({"wiki": {"page_title": title, "page_event": ev["key"]}})
            return f"OK: this event's matches go to the wiki page \"{title}\"." + self.when(START), None
        if name in ("embed", "markup"):
            from .publish import wiki
            fmt = typed.strip()
            problem = wiki.embed_problem(fmt)
            if problem:
                return f"That markup cannot be used: {problem}.", None
            config.save_sections({"wiki": {"embed_format": fmt}})
            return "OK: the videos show on the page with the new markup." + self.when(START), None
        return self._no_such("wiki", name)

    # listener
    def _set_listener(self, name: str, value: str, typed: str) -> tuple[str, dict | None]:
        if name in ("off", "stop") or (name == "" and _on_off(value) is False):
            return self.ask_listener_off()
        if name in ("alarm", "awake", "keep_awake", "keepawake", "sleep"):
            on = _on_off(value)
            if on is None:
                return self._bad(f"set listener {'alarm' if name == 'alarm' else 'awake'} on, or off")
            if name == "alarm":
                config.save_sections({"listen": {"alarm": on}})
                return ("OK: " + ("the owner gets a DM if the listener stops." if on else
                                  "no DM if the listener stops.") + self.when(LISTENER)), None
            if name == "sleep":
                on = not on
            config.save_sections({"listen": {"keep_awake": on}})
            return ("OK: " + ("the computer stays awake while it listens." if on else
                              "the computer may sleep while it listens (it then hears nothing).")
                    + self.when(LISTENER)), None
        return self._no_such("listener", name)

    def _no_such(self, area: str, name: str = "") -> tuple[str, None]:
        import difflib
        names = [s.name for s in SETTINGS if s.area == area and s.name]
        close = difflib.get_close_matches(name.lower(), names, n=1, cutoff=0.7) if name else []
        usage = next((s.usage for s in SETTINGS if s.area == area and close and s.name == close[0]), None)
        hint = f" Did you mean `{usage}`?" if usage else ""
        return (f"There is no such {area} setting.{hint} Reply settings {area} to see them with their commands."), None

    def ask_listener_off(self) -> tuple[str, dict]:
        return (f"Turn Start from Slack off on {self.host}? It stops listening now and no longer starts at log-in, so "
                f"nobody can start or stop recordings from Slack until it is turned on again on the computer with "
                f"{run_cmd('listen')}. A recording that runs keeps going. Reply yes or no.",
                {"action": "listener_off", "again": "set listener off"})

    def _listener_off(self, user: str) -> str:
        config.save_sections({"listen": {"enabled": False}})
        try:
            if self.login_off is not None:
                self.login_off()
            else:
                from . import listener
                listener.remove_login_start()
        except Exception as e:  # noqa: BLE001
            self.log(f"could not remove the listener's log-in start ({_err(e)})")
        if self.listener is not None:
            self.listener.quit = f"turned off from Slack by {self._name(user)}"
            return (f"OK: Start from Slack is off on {self.host}. It stops listening now; a recording that runs keeps "
                    f"going. To turn it on again, on {self.host}: {run_cmd('listen')}.")
        return (f"OK: Start from Slack is off on {self.host}: it no longer starts at log-in. To turn it on again: "
                f"{run_cmd('listen')}.")

    def _name(self, user: str) -> str:
        o, name = owner()
        if user == o:
            return name
        users, names = people()
        return names[users.index(user)] if user in users else user

    # people ---------------------------------------------------------------------------------------------------------
    def people_text(self) -> str:
        o, oname = owner()
        users, names = people()
        admins = admin_ids()
        lines = [f"Who may use the StreamSplitter app on {self.host}:",
                 "*Admins*: settings, people and the running recording (set stream, set teams, pause, resume), and "
                 "everything people can do",
                 f"• {oname} (the owner: gets the alerts)"]
        lines += [f"• {n}" for u, n in zip(users, names) if u in admins]
        plain = [n for u, n in zip(users, names) if u not in admins]
        lines.append("*People*: split, stop, status, help")
        lines += [f"• {n}" for n in plain] or ["• nobody else yet"]
        lines.append("Admins add someone with add @name (or part of their name, or their email), an admin with add "
                     "admin @name, and take it away with remove @name. Adding sends them nothing unless you say so. "
                     "set owner @name sends the alerts to another admin.")
        if names and self.listener is not None and hasattr(self.listener, "read_every"):
            every = self.listener.read_every()
            lines.append(("Messages are read within about 20 s" if every <= 20 else
                          f"Admins' messages are read within about 20 s, everyone else's within about {every:.0f} s")
                         + "; after someone writes, every few seconds.")
        return "\n".join(lines)

    def find_person(self, raw: str) -> list[dict] | str:
        """The workspace members `raw` means (an @mention, an email, or part of a name), or the reply why not."""
        raw = (raw or "").strip()
        m = _MENTION.search(raw)
        mail = _MAILTO.search(raw)
        if m:
            query = m.group(1)
        elif mail:
            query = mail.group(1).strip()
        else:
            query = streams.unwrap_slack(raw).strip().strip("\"'`").removeprefix("mailto:")
            if re.fullmatch(r"@?[UW][A-Z0-9]{8,}", query):
                query = query.lstrip("@")
        if not query:
            return "Say who: add @name (type @ and pick them), or part of their name, or their email."
        try:
            found = self.client.find_users(query)
        except Exception as e:  # noqa: BLE001
            return f"Could not search Slack ({_err(e)}). Try again in a minute."
        if not found:
            return ("Nobody in the workspace matches that. Try part of their name, their email address, or an "
                    "@mention (type @ and pick them).")
        if len(found) > PICK_MAX:
            return f"{len(found)} people match that. Type more of their name, or @mention them."
        return found

    def pick(self, found: list[dict], purpose: str) -> tuple[str, dict]:
        lines = ["Several people match. Reply with the number of the one you mean, or no:"]
        lines += [f"  {i + 1}. {_person_line(p)}" for i, p in enumerate(found)]
        return "\n".join(lines), {"action": "pick", "purpose": purpose,
                                  "people": [{"id": p["id"], "name": _person_name(p)} for p in found]}

    @staticmethod
    def _as_admin(raw: str) -> tuple[str, bool]:
        """("@Sam", True) for "admin @Sam" / "@Sam as admin"; (raw, False) otherwise."""
        raw = (raw or "").strip()
        m = re.match(r"^(?:an\s+)?admins?\b[\s:]*(.*)$", raw, re.I | re.S)
        if m:
            return m.group(1).strip(), True
        m = re.match(r"^(.*?)\s+as\s+(?:an\s+)?admin$", raw, re.I | re.S)
        if m:
            return m.group(1).strip(), True
        return raw, False

    def add(self, raw: str) -> tuple[str, dict | None]:
        raw, admin = self._as_admin(raw)
        found = self.find_person(raw)
        if isinstance(found, str):
            return (found.replace("add @name", "add admin @name") if admin else found), None
        if len(found) > 1:
            return self.pick(found, "add_admin" if admin else "add")
        return self._add(found[0]["id"], _person_name(found[0]), admin=admin)

    def _add(self, pid: str, name: str, admin: bool = False) -> tuple[str, dict | None]:
        o, oname = owner()
        users, names = people()
        admins = [a for a in config.section("listen").get("admins") or [] if a in users and a != o]
        if pid == o:
            return f"{name} is the owner already (an admin, and gets the alerts).", None
        if pid in users:
            if admin and pid not in admins:
                config.save_sections({"listen": {"admins": admins + [pid]}})
                self._people_changed()
                self.log(f"made {name} ({pid}) an admin")
                return (f"OK: {name} is an admin now: they can change settings, people and the running recording. "
                        f"They got no message."), None
            if pid in admins:
                return f"{name} is an admin already. Reply people for the list.", None
            return (f"{name} may use the app already. To make them an admin: add admin @{name}. Reply people for the "
                    f"list."), None
        values = {"users": users + [pid], "user_names": names + [name]}
        if admin:
            values["admins"] = admins + [pid]
        config.save_sections({"listen": values})
        self._people_changed()
        self.log(f"added {name} ({pid}) to the {'admins' if admin else 'people who may use the app'}")
        idle = "" if self.listener is not None else (f" (Start from Slack is not running on {self.host}, so their "
                                                     f"messages are read only once it runs.)")
        what = (" as an admin: they may now change settings, people and the running recording, and start and stop "
                "recordings," if admin else ": they may now start and stop recordings")
        return (f"Added {name}{what} by messaging the StreamSplitter app (help shows them how). They got no message "
                f"from it.{idle} Send them a short how-to note? Reply yes or no.",
                {"action": "note", "id": pid, "name": name})

    def remove(self, raw: str) -> tuple[str, dict | None]:
        o, oname = owner()
        users, names = people()
        raw, admin_only = self._as_admin(raw)
        raw = (raw or "").strip()
        m = _MENTION.search(raw)
        mail = _MAILTO.search(raw)
        q = streams.unwrap_slack(raw).strip().strip("\"'`").lstrip("@").casefold()
        if m:
            hits = [m.group(1)]
        elif mail or ("@" in q and "." in q):
            try:
                hits = [p["id"] for p in self.client.find_users(mail.group(1) if mail else q)]
            except Exception as e:  # noqa: BLE001
                return f"Could not search Slack ({_err(e)}). Try again in a minute.", None
        elif q:
            hits = [u for u, n in zip(users, names) if q in n.casefold() or q == u.casefold()] or \
                ([o] if o and q in oname.casefold() else [])
        else:
            return "Say who: remove @name, or part of their name. Reply people for the list.", None
        if o in hits and len(hits) == 1:
            return (f"{oname} is the owner (gets the alerts), so they stay an admin. To send the alerts to someone "
                    f"else first: set owner @name."), None
        hits = [h for h in hits if h in users]
        if not hits:
            return "Nobody of that name may use the app. Reply people for the list.", None
        if len(hits) > 1:
            return self.pick([{"id": h, "real_name": names[users.index(h)]} for h in hits],
                             "unadmin" if admin_only else "remove")
        return (self._unadmin(hits[0]) if admin_only else self._remove(hits[0])), None

    def _unadmin(self, pid: str) -> str:
        users, names = people()
        admins = [a for a in config.section("listen").get("admins") or [] if a in users]
        name = names[users.index(pid)] if pid in users else pid
        if pid not in admins:
            return f"{name} is not an admin. (remove @{name} takes away their use of the app.)"
        config.save_sections({"listen": {"admins": [a for a in admins if a != pid]}})
        self._people_changed()
        self.log(f"{name} ({pid}) is no longer an admin")
        return f"OK: {name} is no longer an admin; they may still start and stop recordings. They got no message."

    def _remove(self, pid: str) -> str:
        users, names = people()
        if pid not in users:
            return "They may not use the app any more already."
        i = users.index(pid)
        name = names[i]
        admins = [a for a in config.section("listen").get("admins") or [] if a != pid]
        config.save_sections({"listen": {"users": users[:i] + users[i + 1:], "user_names": names[:i] + names[i + 1:],
                                         "admins": admins or None}})
        self._people_changed(removed=pid)
        self.log(f"removed {name} ({pid}) from the people who may use the app")
        return f"Removed {name}: the app no longer answers them. They got no message."

    def _people_changed(self, removed: str | None = None) -> None:
        if self.listener is not None:
            try:
                self.listener.reload_people(removed=removed)
            except Exception as e:  # noqa: BLE001 - its next check picks it up
                self.log(f"could not reload the people ({_err(e)})")

    def note_text(self, name: str) -> str:
        _, oname = owner()
        key = config.section("event").get("key") or "2026mttd"
        return (f"Hi {name}! {oname} added you to StreamSplitter, the match video recorder on {self.host}. To start "
                f"recording an event, message this app split {key}; stop stops it, status shows how it is going. "
                f"Replies come within about 20 s. Reply help for what you can do.")

    def _send_note(self, pid: str, name: str) -> str:
        try:
            self.client.dm(pid, self.note_text(name).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        except Exception as e:  # noqa: BLE001
            return f"Could not message {name} ({_err(e)})."
        self.log(f"sent {name} the how-to note")
        return f"Sent {name} a short how-to note."

    # owner ----------------------------------------------------------------------------------------------------------
    def ask_owner(self, raw: str) -> tuple[str, dict | None]:
        found = self.find_person(raw)
        if isinstance(found, str):
            return found.replace("add @name", "set owner @name"), None
        if len(found) > 1:
            return self.pick(found, "owner")
        return self._ask_owner(found[0]["id"], _person_name(found[0]))

    def _ask_owner(self, pid: str, name: str) -> tuple[str, dict | None]:
        o, oname = owner()
        if pid == o:
            return f"{name} is the owner already.", None
        stays = ("You stay an admin, so you can still change settings from this chat." if self._user == o else
                 f"{oname} stays an admin.")
        return (f"Make {name} the owner? The alerts (the stopped-running alarm too) go to them from now on, and they "
                f"become an admin. {stays} Reply yes or no.", {"action": "owner", "id": pid, "name": name})

    def _make_owner(self, user: str, pid: str, name: str) -> str:
        o, oname = owner()
        users, names = people()
        admins = [a for a in config.section("listen").get("admins") or [] if a in users and a not in (pid, o)]
        keep = [(u, n) for u, n in zip(users, names) if u != pid]
        if o and o not in [u for u, _ in keep]:
            keep.append((o, oname))
        if o and o not in admins:
            admins.append(o)                     # the old owner keeps the admin rights (module docstring: roles)
        config.save_sections({"notify": {"slack_user": pid, "slack_user_name": name},
                              "listen": {"users": [u for u, _ in keep], "user_names": [n for _, n in keep],
                                         "admins": admins or None}})
        self._people_changed()
        self.log(f"{self._name(user) if user != pid else oname} made {name} ({pid}) the owner; {oname} stays an admin")
        note = ""
        try:
            self.client.dm(pid, (f"{self._name(user)} made you the owner of StreamSplitter on {self.host}: its alerts "
                                 f"come to you here, and you are an admin: you can change its settings and add "
                                 f"people. Reply help for what you can do.")
                           .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        except Exception as e:  # noqa: BLE001
            note = f" (Could not message them: {_err(e)}.)"
        if self.listener is not None:
            later = (f" The listener and a running recording keep sending their alerts to {oname} until they "
                     f"restart.")
        elif self._recording():
            later = (f" The running recording keeps sending its alerts to {oname}, and reads only {oname}'s chat, "
                     f"until it restarts.")
        else:
            later = ""
        stays = ("You stay an admin, so you can still change settings." if user == o else
                 f"{oname} stays an admin (remove admin @{oname} takes that away).")
        return f"OK: {name} is the owner now and was told so.{note} {stays}{later}"

    # answers to questions -------------------------------------------------------------------------------------------
    def confirm(self, user: str, q: dict, word: str) -> tuple[str, dict | None]:
        """(the reply, a follow-up question or None) to `word` ("yes", "no" or a number) for the question `q` that
        handle() asked `user`."""
        self._user = user
        a = q.get("action")
        if word in NO_WORDS:
            return {"note": f"OK, no message sent to {q.get('name')}.", "listener_off": "OK, it keeps listening.",
                    "pick": "OK."}.get(a, "OK, nothing changed."), None
        if not is_admin_user(user):
            return self.refuse(user), None
        if a == "pick":
            n = len(q.get("people") or [])
            if not word.isdigit() or not 1 <= int(word) <= n:
                return f"Reply with the number of the one you mean (1 to {n}), or no.", q
            p = q["people"][int(word) - 1]
            if q.get("purpose") == "owner":
                return self._ask_owner(p["id"], p["name"])
            if q.get("purpose") == "remove":
                return self._remove(p["id"]), None
            if q.get("purpose") == "unadmin":
                return self._unadmin(p["id"]), None
            return self._add(p["id"], p["name"], admin=q.get("purpose") == "add_admin")
        if word.isdigit():
            return "Reply yes or no.", q
        if a == "note":
            return self._send_note(q["id"], q["name"]), None
        if a == "owner":
            return self._make_owner(user, q["id"], q["name"]), None
        if a == "listener_off":
            return self._listener_off(user), None
        if a == "set" and q.get("area") == "youtube":
            return self._set_youtube(q.get("name") or "", q.get("value") or "", q.get("value") or "", confirmed=True)
        return "That question cannot be answered any more. Ask again.", None

    # while nothing records: stream, teams, pause, resume saved for the next start ------------------------------------
    def offline(self, cmd) -> str:
        """A running recording's own commands (commands.Command) while nothing records: saved for the next start."""
        from .publish import commands
        if cmd.error:
            return cmd.error
        nxt = " It applies when the next recording starts."
        if cmd.name == "stream":
            if cmd.arg is None:
                url = config.section("event").get("stream_url")
                return ((f"Stream: {url}." if url else "Stream: none set; the next recording follows the link The Blue "
                         "Alliance lists.") + " To change it, reply set stream <link>, or set stream auto.")
            config.save_sections({"event": {"stream_url": cmd.arg}})
            return f"OK: the next recording records {cmd.arg} (saved in config.toml)."
        if cmd.name == "stream_auto":
            config.save_sections({"event": {"stream_url": None}})
            return "OK: the next recording follows the link The Blue Alliance lists for the event."
        sl = config.section("slack")
        where = f"#{sl['channel_name']}" if sl.get("channel_name") else "the team's Slack channel"
        if cmd.name == "teams":
            if cmd.arg is None:
                teams = ", ".join(map(str, sl.get("teams") or []))
                return (f"Matches of {teams} go to {where}." if teams else f"No team's matches go to {where}.") + \
                    " To change them, reply for example: set teams 5026, 254"
            config.save_sections({"slack": {"teams": list(cmd.arg)}})
            if not cmd.arg:
                return f"OK: no team's matches go to {where}." + nxt
            return f"OK: matches of {', '.join(cmd.arg)} go to {where}." + nxt
        if cmd.name in ("pause", "resume"):
            from .publish.outbox import label
            dest = cmd.arg or "youtube"
            if not config.section(dest).get("enabled"):
                return f"{label(dest)} uploads are off, so there is nothing to {cmd.name}."
            was = bool(config.section(dest).get("paused"))
            if cmd.name == "pause":
                config.save_sections({dest: {"paused": True}})
                return (f"OK: {label(dest)} uploads stay paused when the next recording starts (they wait; nothing is "
                        f"lost). Reply resume {dest} to send them.")
            config.save_sections({dest: {"paused": None}})
            return (f"OK: {label(dest)} uploads go out when the next recording starts." if was else
                    f"{label(dest)} uploads are not paused.")
        return commands.UNKNOWN


_AREA_STEPS = {"slack": ("slack",), "youtube": ("youtube", "tbalinks"), "drive": ("drive",), "wiki": ("wiki",),
               "event": ("tba",), "listener": ("listen",)}


# ---------------------------------------------------------------------------------------------------------------
# help <topic>
# ---------------------------------------------------------------------------------------------------------------

def set_help(area: str | None = None) -> str:
    """Every change command, grouped by area (one area: `area`), with when it applies."""
    when = {NOW: "now", LIVE: "now while recording, else at the next start", START: "at the next recording start",
            LISTENER: "at the listener's next start"}
    lines = ["Change a setting by replying one of these (admins only). settings shows the current values."
             if area is None else ""]
    for a, title in AREAS:
        if area and a != area:
            continue
        lines.append(f"*{title}*")
        lines += [f"• `{s.usage}`: {s.what}; applies {when[s.when]}" for s in SETTINGS if s.area == a]
        more = [w for w, step, _ in COMPUTER if step in _AREA_STEPS.get(a, ())]
        if more:
            lines.append(f"   On the computer only: {_and(more)}. help computer says how.")
    if area == "youtube":
        from .publish import templates
        lines.append("Fields for title and description: " + ", ".join(f"{{{k}}}" for k in templates.FIELDS))
    if area == "drive":
        from .publish import drive
        lines.append("Fields for file names: " + ", ".join(f"{{{k}}}" for k in drive.NAME_FIELDS)
                     + "; a [part in brackets] is left out until its values are known.")
    return "\n".join(x for x in lines if x)


def computer_help() -> str:
    lines = ["These are set only on the computer StreamSplitter runs on, never in Slack:"]
    for what, step, why in COMPUTER:
        how = run_cmd(step) if step else "edit config.toml"
        lines.append(f"• {what}: {how}. Why: {why}.")
    lines.append("Never type a password or key into Slack: Slack keeps every message. If one was sent, revoke it and "
                 "make a new one.")
    return "\n".join(lines)


QUESTION_MIN = int(QUESTION_S // 60)
TOPICS = {
    "split": "split <event> starts recording an event on this computer, e.g. split 2026mttd. The event: its code from "
             "thebluealliance.com, its thebluealliance.com/event/... link, or part of its name or city (split "
             "madtown; several hits are listed with their codes). With the code of an event that is on today or "
             "tomorrow it starts at once and the reply sums up the stream and where the clips go (stop stops it). "
             "Found by name, or not on now, it asks first: reply yes (within "
             f"{QUESTION_MIN} minutes; it reminds you once), or no; split 2026mttd yes starts without asking. "
             "start <event> and record <event> work too. One recording per computer: stop the running one first. It "
             "needs Start from Slack on the computer.",
    "yes": "yes or no answers the question asked last: start this event, stop this recording, change this setting, "
           f"send this note. Only the person who was asked can answer, within {QUESTION_MIN} minutes. A new question "
           "replaces the old one. ok, sure and y work as yes; cancel and n as no.",
    "status": "status says what is going on: whether a recording runs (its stream, the last video, the clips cut "
              "today and the uploads) and whether Start from Slack (the listener) runs, and how quickly your messages "
              "are read. list and info work too.",
    "stop": "stop asks \"Stop recording ...? Reply yes\"; after yes the recording finishes what it is doing (an upload "
            "can take a minute) and the app says when it has stopped. stop yes (or stop 2026mttd, the code of the "
            "event recording) stops it without asking. Without Start from Slack on the computer, it can only be "
            "stopped there.",
    "stream": "set stream <link> records this YouTube or Twitch link instead (admins), e.g. set stream "
              "https://www.youtube.com/@ChannelName/live; the channel's /live page is best, it follows each day's "
              "stream. set stream auto goes back to the link The Blue Alliance lists. set stream alone shows the "
              "link. While recording it applies at once; otherwise it is saved for the next start. stream <link> "
              "works too.",
    "teams": "set teams <numbers> sets whose matches are posted to the team's Slack channel (admins), e.g. set teams "
             "5026, 254; set teams none posts nobody's. set teams alone shows them. While recording it applies from "
             "the next match; otherwise at the next start. teams 5026 works too.",
    "pause": "pause youtube holds the YouTube uploads (they wait on the computer; nothing is lost, also across a "
             "restart); resume youtube sends them. The same for drive and wiki. Admins only. While nothing records "
             "it is saved for the next start.",
    "settings": "settings lists every setting with its value and the command that changes it; settings youtube shows "
                "one area (event, slack, youtube, drive, wiki, owner, listener). Admins only.",
    "people": "people lists who may use the app and who the admins are (anyone may ask). Admins add someone with "
              "add @name: type @ and pick them, or part of their name, or their email; when several match, they are "
              "listed by number and you reply with one. add admin @name adds an admin (or makes a person one). The "
              "person gets no message; you are then asked whether to send them a short how-to note (yes or no). "
              "remove @name takes it away, also without a message; remove admin @name keeps them as a person. People "
              "may use split, stop, status, yes, no and help.",
    "admin": "Admins change settings, add and remove people, and change the running recording (set stream, set teams, "
             "pause, resume). The owner is the admin who gets the alerts. add admin @name makes someone an admin; "
             "remove admin @name takes that away. set owner @name sends the alerts to someone else, and the old "
             "owner stays an admin, so nobody locks themselves out. An admin who cannot reach Slack can still change "
             "everything on the computer (run.bat).",
    "owner": "The owner is the admin who gets the alerts: every warning and the stopped-running alarm. set owner "
             "@name hands that over (it asks first); the new owner is told, and the old owner stays an admin. A "
             "recording that runs and the listener keep sending alerts to the old owner until they restart.",
    "listener": "The Slack listener (Start from Slack) reads these messages on the computer and starts and stops "
                "recordings. It reads each chat every 20 s, and every 3 s for 2 minutes after someone writes. set "
                "listener off stops it and keeps it from starting at log-in (it asks first); turning it on again, "
                "and choosing between the background and a window, is done on the computer. set listener alarm "
                "on|off and set listener awake on|off apply at its next start.",
    "help": "help shows what you can do now; help all lists every command; help <command> explains one, e.g. help "
            "set, help add, help stop. help computer lists what is set only on the computer. Capitals, formatting "
            "and a full stop at the end do not matter.",
}
TOPIC_WORDS = {"no": "yes", "y": "yes", "n": "yes", "ok": "yes", "start": "split", "record": "split",
               "event": "split", "resume": "pause", "add": "people", "remove": "people", "person": "people",
               "admins": "admin", "contact": "owner", "alerts": "owner", "listen": "listener", "setting": "settings",
               "link": "stream", "team": "teams", "commands": "help", "list": "status", "info": "status",
               "secret": "computer", "secrets": "computer", "password": "computer", "passwords": "computer",
               "token": "computer", "key": "computer", "keys": "computer", "login": "computer"}
TOPIC_LIST = "split, stop, status, stream, teams, pause, settings, set, people, admin, owner, listener, computer"


def help_topic(topic: str) -> str:
    """The details for `help <topic>`: a command, an area of settings, set, or computer."""
    t = topic.strip().lower().lstrip("/!").split()[0] if topic.strip() else ""
    if t == "set":
        rest = topic.strip().split(None, 2)
        if len(rest) > 1 and rest[1].lower() in ("stream", "link"):
            return TOPICS["stream"]
        if len(rest) > 1 and rest[1].lower() in ("teams", "team"):
            return TOPICS["teams"]
        if len(rest) > 1 and AREA_WORDS.get(rest[1].lower()):
            return set_help(AREA_WORDS[rest[1].lower()])
        return set_help()
    if t == "computer":
        return computer_help()
    t = TOPIC_WORDS.get(t, t)
    if t == "computer":
        return computer_help()
    if t in TOPICS:
        return TOPICS[t]
    if t in AREA_WORDS and t not in ("owner", "listener", "people"):
        return set_help(AREA_WORDS[t])
    return f"There is no help on that. Reply help for what you can do now, or help with one of: {TOPIC_LIST}."
