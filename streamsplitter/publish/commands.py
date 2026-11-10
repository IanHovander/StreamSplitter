"""Slack commands: what people type to the StreamSplitter app in Slack, and which program answers.

People drive StreamSplitter by direct-messaging its Slack app: `split 2026mttd`, `status`, `pause youtube` ... This
module holds what both programs that read those DMs share: cleaning and parsing a message (clean, parse), the reply to
a message that is not understood (unknown_reply: "Did you mean `pause youtube`?", else the few commands that matter
now), the help (first_help: a short first screen for the asker's state; help_text: every command; help <topic>:
slackadmin.help_topic), the running recording's own commands (dispatch), the recording's DM reader (CommandPoller)
and its mailbox (Mailbox).

Who answers (user, 2026-11-09: "Resume youtube" got both "OK: ... resumed" and a refusal, and "TBA" got "I did not
understand that" twice, 8 s apart: the Slack listener and the running recording both read the same DM and disagreed
about who answers what). Exactly one program reads a DM at any time:

- A Slack listener of this version is alive (listener.py; its lock held and "protocol": PROTOCOL in its status.json):
  it reads every allowed person's DM and answers every message. A command for the running recording (status, set
  stream, set teams, pause, resume) goes to the recording through its mailbox (Mailbox): the listener writes the
  request into <work_root>/live/<tag>/slack_in/, the recording does it and writes the reply into
  <data_root>/listen/replies/, and the listener posts it. The recording reads no Slack then (CommandPoller is off).
- No listener alive: the recording's CommandPoller reads its owner's DM (the alerts contact when it started) and
  answers everything there: its own commands, help, status, settings and people (slackadmin.Admin); split and stop
  say that Start from Slack is not running.
- A listener of an older version is alive (no "protocol" in its status): the recording answers in its owner's DM
  everything but what that listener answers (legacy_listener_command), as before this version, and listener.py does
  the mirror image for a recording of an older version (no mailbox): one answer per message while versions are mixed.
Both use the same saved read position per person (<data_root>/listen/commands_<user>.json; the recording also keeps
its own, <data_root>/outbox/<tag>.commands.json), so a message answered by one is not answered again by the other
when one takes over from the other.

Parsing (clean, parse). Slack delivers what was typed with its own markup: `code` and ```blocks```, *bold*,
_italics_, ~strike~, <https://...|label> links, <@U123> mentions, &amp; &lt; &gt;; phones add smart quotes. A leading
@mention of the app, a leading / or !, a leading > (a quote), formatting, wrapping quotes and trailing punctuation are
dropped, and capitals do not matter. The value of a free-text setting (a YouTube title or description, Drive file
names, a wiki page or markup, a playlist name) keeps its own spacing, capitals and characters. The words a person
would guess work too: start or record for split, list or info for status, ok or sure for yes, cancel for no, and
`stream <link>` / `teams 5026` for `set stream <link>` / `set teams 5026`.

Responsiveness (user, 2026-11-09: no sign a message was seen, replies 10-20 s). A DM is read every POLL_S, and every
FAST_S for HOT_S after its person writes (the listener shares that within Slack's limit, listener.py). A message the
program answers gets a :eyes: reaction as soon as it is read, when the app has the optional reactions:write
permission (apps made before 2026-11-10 lack it: then nothing happens and nothing is said about it). help, status and
the first messages say how long a reply normally takes.

Scopes. Reading a DM needs im:history (apps made before 2026-11-04 lack it): CommandPoller checks the token's scopes
(auth.test) and, when it is missing, DMs once how to add it (SCOPE_FIX) and asks again every SCOPE_RETRY_S. Rate limits
(Slack's docs, 2026-11-04): conversations.history and .replies are Tier 3 (about 50 calls a minute per app and
method); a `ratelimited` answer is waited out.

Safety. Only the person's own messages in their DM are read (another person, a bot, an edit or a join is ignored).
Messages sent before StreamSplitter started are not acted on, except those in the RESTART_GRACE_S before a restart;
the older ones get one note that they were not done (missed_note). The recording cannot be stopped from its own
reader (only through the listener, which asks first); `set stream` accepts only a YouTube or Twitch link. Replies never
repeat what was typed, except a command they correct ("Did you mean ...?"); a message that looks like a token or key
is not used or repeated (slackadmin.secret_reply).

help_text() (Slack mrkdwn, ready to post) is also what the setup wizard puts in the contact's hello DM, and plain
text for its summary. short_help() (SHORT_LIST) is the one-line list at the end of the setup test post
(publish/slack.py TEST_FORMAT). A long reply is posted as a few messages (split_message).
"""

from __future__ import annotations

import difflib
import json
import os
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import slackadmin, streams
from .base import PermanentError, TransientError

POLL_S = 20.0              # how often a DM is read
FAST_S = 3.0               # ... and this often for HOT_S after its person wrote
HOT_S = 120.0
ERROR_WAIT_S = 60.0        # after Slack could not be reached
SCOPE_RETRY_S = 600.0      # while im:history is missing (a person may add it and reinstall meanwhile)
HISTORY_LIMIT = 15         # messages per read (Slack's cap for distributed non-Marketplace apps; plenty here)
MAX_PER_POLL = 5           # commands handled per read; more wait for the next one
RESTART_GRACE_S = 900.0    # after a restart, commands sent this recently are still handled
MESSAGE_MAX = 3500         # a longer reply goes out as several messages (Slack truncates at 40,000; ~4,000 reads well)
NEEDED_SCOPE = "im:history"
REACT_SCOPE = "reactions:write"      # optional (slack.OPTIONAL_SCOPES): the :eyes: on a message that was read
SEEN, THANKS = "eyes", "+1"
PROTOCOL = 2               # this version's listener <-> recording protocol (the mailbox)
INBOX_DIR = "slack_in"     # in the recording's folder: the listener's requests
ADVERT_FILE = "commands.json"        # in the recording's folder: {"protocol", "pid"} while its mailbox is open
REPLIES_DIR = "replies"    # in the listener's folder: the recording's replies
MAILBOX_EVERY_S = 0.5
SOLO, LEGACY, OFF, LISTENER = "solo", "legacy", "off", "listener"     # CommandPoller.mode()
# Slack's answers to reactions.add that mean this app may not react at all (remembered: it stops trying)
_NO_REACTIONS = {"missing_scope", "not_allowed_token_type", "no_permission", "restricted_action", "access_denied",
                 "invalid_auth", "not_authed", "token_revoked"}

SCOPE_FIX = ("Your Slack app needs one more permission to read your replies (the commands, such as status): "
             "api.slack.com/apps > StreamSplitter > OAuth & Permissions > Scopes > Add im:history > Reinstall to "
             "Workspace. Then on the app's App Home page, under Show Tabs, tick \"Allow users to send Slash commands "
             "and messages from the messages tab\". Everything else keeps working meanwhile.")
SCOPE_OK = "StreamSplitter can read your replies now. Reply help for the commands."
STOP_HOW = ("Start from Slack (the Slack listener) is not running on the recording's computer, so it cannot be stopped "
            "from Slack. To stop it there: double-click run.bat (Mac/Linux: sh run.sh) and choose Stop, or run "
            "`streamsplitter stop`.")
SPLIT_NEEDS = ("split needs Start from Slack (the Slack listener), which is not running on this computer now. To "
               "start it there: double-click run.bat (Mac/Linux: sh run.sh) and choose Start listening for Slack "
               "commands.")
UNKNOWN = "I did not understand that."
NOTHING_TO_CONFIRM = "There is no question waiting for a yes or no. Reply help for the commands."
NOTHING_TO_PICK = "There is no list waiting for a number. Reply help for the commands."
EXAMPLE_LINK = "https://www.youtube.com/@ChannelName/live"
QUESTION_MIN = int(slackadmin.QUESTION_S // 60)

# what to type, what it does. <channel> is filled in by help_text; "e.g. " starts the example
LISTEN_COMMANDS = (
    ("split <event>", "start recording an event (start <event> works too): its code, its thebluealliance.com link or "
                      "part of its name, e.g. split 2026mttd"),
    ("stop", "stop the recording (it asks first; stop yes stops at once)"),
)
STATUS_ROW = ("status", "what is recording and how it is going (list works too)")
YES_NO = ("yes / no", f"answer the question just asked (it waits {QUESTION_MIN} minutes)")
# the running recording's own commands (admins)
COMMANDS = (
    ("set stream <link>", "record this YouTube or Twitch link instead, e.g. set stream " + EXAMPLE_LINK),
    ("set stream auto", "go back to the stream link The Blue Alliance lists for the event"),
    ("set teams <numbers>", "whose matches are posted to <channel>, e.g. set teams 5026, 254"),
    ("pause youtube", "hold the YouTube uploads (they wait; nothing is lost)"),
    ("resume youtube", "send the held YouTube uploads"),
)
DRIVE_COMMANDS = (
    ("pause drive", "hold the Google Drive uploads (they wait; nothing is lost)"),
    ("resume drive", "send the held Google Drive uploads (also after the Drive was full and has room again)"),
)
WIKI_COMMANDS = (
    ("pause wiki", "hold the team wiki uploads (they wait; nothing is lost)"),
    ("resume wiki", "send the held wiki uploads"),
)
SETTINGS_COMMANDS = (
    ("settings", "every setting, its value and the command that changes it, e.g. settings youtube"),
    ("set <area> <name> <value>", "change one (help set lists them all), e.g. set youtube privacy unlisted"),
)
PEOPLE_COMMANDS = (
    ("add @name", "let someone start and stop recordings; they get no message unless you say so, e.g. add @Sam"),
    ("add admin @name", "make someone an admin: settings, people and the running recording, e.g. add admin @Sam"),
    ("remove @name", "take that away again (remove admin @name keeps them as a person)"),
    ("set owner @name", "who gets the alerts (it asks first; you stay an admin)"),
)
PEOPLE_ROW = ("people", "who may use this app, and who the admins are")
HELP_COMMANDS = (
    ("help", "what you can do now, in short"),
    ("help all", "this list"),
    ("help <command>", "one command explained, e.g. help split"),
)
SHORT_LIST = ("help", "status", "split 2026mttd", "stop", "pause youtube", "resume youtube", "pause drive",
              "resume drive", "set stream <link>", "set stream auto", "settings", "add @name")
RECORDING_COMMANDS = ("stream", "stream_auto", "teams", "pause", "resume")


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _clock(t) -> str:
    return time.strftime("%a %H:%M", time.localtime(float(t))) if t else "?"


def _and(items) -> str:
    items = [str(x) for x in items]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


# ---------------------------------------------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------------------------------------------

def help_text(channel_name: str | None = None, *, slack: bool = True, wiki: bool = False, drive: bool = False,
              listen: bool = False, owner: bool = True, owner_name: str | None = None, escape: bool = True) -> str:
    """Every command the asker may use, grouped, with the exact text to type (`help all`). slack=True (default):
    Slack mrkdwn ready for chat.postMessage (the text to type in `code`, & < > escaped; escape=False leaves the
    escaping to the poster); slack=False: plain text (a console). drive / wiki: the Drive and wiki commands are listed
    (a team without them is not shown them). listen: the Slack listener runs on this computer (split and stop are
    listed; without it, how to stop a recording on its computer). owner=False: someone who is not an admin: only what
    they may use, and who does the rest (`owner_name`: the admins' names)."""
    where = f"#{channel_name.lstrip('#')}" if channel_name else "the team's Slack channel"
    recording = (list(LISTEN_COMMANDS) if listen else []) + [STATUS_ROW, YES_NO]
    groups = [("Recording", recording)]
    if owner:
        groups.append(("The running recording (admins)",
                       list(COMMANDS + (DRIVE_COMMANDS if drive else ()) + (WIKI_COMMANDS if wiki else ()))))
        groups.append(("Settings (admins)", list(SETTINGS_COMMANDS)))
        groups.append(("People (adding, removing and the owner: admins)", [PEOPLE_ROW] + list(PEOPLE_COMMANDS)))
    else:
        groups.append(("People", [PEOPLE_ROW]))
    groups.append(("Help", list(HELP_COMMANDS)))
    esc = _esc if escape else str
    lines = ["StreamSplitter commands: reply in this chat with one of these, one per message (capitals and "
             "formatting do not matter):"]
    for title, rows in groups:
        lines.append(f"*{title}*" if slack else f"{title}:")
        for cmd, what in rows:
            what = what.replace("<channel>", where)
            if slack:
                desc, _, example = what.partition("e.g. ")
                shown = " / ".join(f"`{esc(c.strip())}`" for c in cmd.split(" / "))
                lines.append(f"• {shown}: {esc(desc)}" + (f"e.g. `{esc(example)}`" if example else ""))
            else:
                lines.append(f"  {cmd:<16} {what}")
    tail = []
    if not owner:
        tail.append(f"Admins ({owner_name or 'the alerts contact'}) change the stream, the uploads, the settings and "
                    f"the people.")
    if not listen:
        tail.append(STOP_HOW)
    lines += [esc(t) if slack else t for t in tail]
    return "\n".join(lines)


def help_reply(topic: str | None, **kw) -> str:
    """help all (help_text, Slack markup, escaped when posted) or help <topic> (slackadmin.help_topic)."""
    if topic and topic.split()[0] not in ALL_WORDS:
        return slackadmin.help_topic(topic)
    return help_text(slack=True, escape=False, **kw)


ALL_WORDS = ("all", "everything", "more", "full", "commands", "list")


def short_help(*, slack: bool = True) -> str:
    """One line for the setup test post: who can send the commands, and where."""
    say = "Message the StreamSplitter app in Slack to control it (help lists what you can do): "
    if slack:
        return say + " · ".join(f"`{_esc(c)}`" for c in SHORT_LIST)
    return say + ", ".join(SHORT_LIST)


@dataclass
class HelpContext:
    """What first_help and unknown_reply need to know about the asker and this computer."""
    host: str = ""
    admin: bool = True               # may change settings, people and the running recording
    listener: bool = True            # Start from Slack answers here (split and stop work)
    recording: str | None = None     # "MadTown Throwdown (2026mttd)" while one records
    since: float | None = None
    event: str | None = None         # the event set up (its code) while nothing records
    event_name: str | None = None
    drive: bool = False
    wiki: bool = False
    admins: tuple = ()               # their names, for someone who is not one
    every_s: float = POLL_S          # how often the asker's chat is read
    react: bool = False              # the app can put :eyes: on a message it read


def reply_time(every_s: float = POLL_S, react: bool = False) -> str:
    """The normal response time, in words (help, status, the first messages)."""
    return (f"Replies come within about {every_s:.0f} s, and within a few seconds while you keep writing."
            + (" :eyes: on your message means it was read." if react else ""))


def top_commands(ctx: HelpContext) -> list[tuple[str, str]]:
    """The few commands that matter in this state, most useful first: (what to type, what it does); several to type
    are joined by " / "."""
    rows: list[tuple[str, str]] = []
    if ctx.recording:
        rows.append(("status", "how it is going (the stream, the clips, the uploads)"))
        if ctx.admin:
            rows.append(("pause youtube / resume youtube", "hold or send the YouTube uploads"
                         + (" (pause drive works the same)" if ctx.drive else "")))
            rows.append(("set stream <link>", "record another YouTube or Twitch link"))
        if ctx.listener:
            rows.append(("stop", "stop recording (it asks first)"))
        elif ctx.admin:
            rows.append(("settings", "see and change the settings"))
        if not ctx.admin:
            rows.append(PEOPLE_ROW)
        return rows
    if ctx.event:
        rows.append((f"split {ctx.event}", f"start recording {ctx.event_name or ctx.event}, the event set up"))
        rows.append(("split <event>", "record another event: its code from thebluealliance.com (e.g. 2026mttd), its "
                                      "link, or part of its name"))
    else:
        rows.append(("split <event>", "start recording an event: its code from thebluealliance.com, e.g. split "
                                      "2026mttd, its link, or part of its name"))
    rows.append(("status", "what is going on"))
    rows.append(("settings", "see and change the settings") if ctx.admin else PEOPLE_ROW)
    return rows


def _rows(rows) -> list[str]:
    return [f"• {' / '.join(f'`{c.strip()}`' for c in cmd.split(' / '))}: {what}" for cmd, what in rows]


def state_line(ctx: HelpContext) -> str:
    if ctx.recording:
        line = f"Recording {ctx.recording} on {ctx.host} since {_clock(ctx.since)}."
    else:
        line = f"Nothing is recording on {ctx.host}." + (f" The event set up is {ctx.event_name or ctx.event} "
                                                        f"({ctx.event})." if ctx.event else "")
    if not ctx.listener:
        line += (f" Start from Slack is not running on {ctx.host}, so recordings cannot be started or stopped from "
                 f"Slack now.")
    return line


def first_help(ctx: HelpContext) -> str:
    """`help`: what StreamSplitter is doing, the few commands that matter now, where the rest is, and how long a reply
    takes (plain text with Slack markup; escaped when posted)."""
    lines = [state_line(ctx)] + _rows(top_commands(ctx))
    if not ctx.admin and ctx.admins:
        lines.append(f"Admins ({_and(ctx.admins)}) change the stream, the uploads and the settings.")
    lines.append("`help all` lists every command; `help <command>` explains one, e.g. `help split`.")
    lines.append(reply_time(ctx.every_s, ctx.react))
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------------------
# Cleaning and parsing
# ---------------------------------------------------------------------------------------------------------------

_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)
_TYPO = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'", "“": '"',
                       "”": '"', "„": '"', "‟": '"', "″": '"', " ": " ", " ": " ",
                       " ": " ", " ": " ", "　": " "})
_LEAD_MENTION = re.compile(r"^<@[UWB][A-Z0-9]+(?:\|[^>]*)?>[\s,:]*(?=\S)")
_WRAPPED = re.compile(r"(```+|`+|\*+|_+|~+|\"|')\s*(.+?)\s*\1", re.S)
_FREE_AREAS = {"youtube", "yt", "drive", "gdrive", "google", "wiki", "website"}
_FREE_NAMES = {"title", "description", "names", "name", "filenames", "files", "embed", "markup", "page", "playlist"}
_TRAILING = ".!?,;:…"
_TOKEN_MARKS = "*_~\"'"


def _unwrap_whole(s: str) -> str:
    """Formatting or quotes around the whole message: `status`, *pause youtube*, ```help```, "status"."""
    while True:
        m = _WRAPPED.fullmatch(s)
        if not m or not m.group(2).strip():
            return s
        s = m.group(2).strip()


def _token(t: str) -> str:
    t = t.replace("`", "")                      # code marks mean nothing inside a command
    if t.startswith("<") or t.endswith(">"):    # Slack's own markup (<https://...|...>, <@U...>): as sent
        return t.strip(_TOKEN_MARKS) if not (t.startswith("<") and t.endswith(">")) else t
    return t.strip(_TOKEN_MARKS)


def clean(text: str) -> str:
    """A message as a command: Slack's markup for links and mentions kept (parse and slackadmin undo it once), its
    formatting dropped (module docstring). A free-text setting's value is kept as typed."""
    s = (text or "").translate(_INVISIBLE).replace("\r\n", "\n").replace("\r", "\n").strip()
    s = _LEAD_MENTION.sub("", s, count=1)
    s = re.sub(r"^(?:&gt;|>)[ \t]*(?=\S)", "", s)          # a quote: "> status"
    s = _unwrap_whole(s)
    if len(s) > 1 and s[0] in "/!" and s[1] not in "/!":
        s = s[1:].lstrip()
    if re.fullmatch(r"\?+", s):
        return "help"
    m = re.match(r"\s*(\S+)\s+(\S+)\s+(\S+)", s)
    if m and _token(m.group(1)).lower() == "set" and _token(m.group(2)).lower() in _FREE_AREAS and \
            _token(m.group(3)).lower().rstrip(":") in _FREE_NAMES:
        return " ".join(_token(m.group(i)) for i in (1, 2, 3)) + s[m.end():]
    s = s.translate(_TYPO)
    toks = [t for t in (_token(t) for t in s.split()) if t]
    s = " ".join(toks)
    if not s.endswith(">"):
        s = s.rstrip(_TRAILING).rstrip()
    return s


@dataclass
class Command:
    name: str                       # help status split set_event yes no number ack stop stream stream_auto teams
    #                                 pause resume admin unknown
    arg: object = None              # help: the topic; split / set_event: the event typed; stream: the link (None:
    #                                 show it); teams: list | None; pause / resume: the destination; number: the int;
    #                                 yes: the word; stop: an event code named with it
    error: str | None = None        # a reply saying what was wrong with the command
    raw: str = field(default="", repr=False)      # the cleaned message, Slack's markup undone
    slack: str = field(default="", repr=False)    # the cleaned message as Slack sent it (mentions as <@U...>)
    confirm: bool = False           # split ... yes, stop yes: no question first


_TEAM = re.compile(r"^(?:frc)?(\d{1,5}[a-z]?)$", re.I)
_KEY = re.compile(r"\d{4}[a-z0-9]{1,16}", re.I)
_TBA_LINK = re.compile(r"(?:https?://)?(?:www\.)?thebluealliance\.com/event/(\d{4}[a-z0-9]+)", re.I)

GREETINGS = {"hi", "hello", "hey", "howdy", "yo"}
HELP_WORDS = {"help", "commands", "command", "menu", "options"} | GREETINGS
STATUS_WORDS = {"status", "list", "info", "state", "stats"}
SPLIT_WORDS = {"split", "start", "record"}
STOP_WORDS = {"stop", "quit", "exit", "end", "halt", "shutdown", "kill"}
YES_WORDS = {"yes", "y", "yep", "yeah", "yup", "ya", "ok", "okay", "sure", "confirm", "go", ":+1:", ":thumbsup:",
             ":white_check_mark:", ":heavy_check_mark:", ":ok:"}
SOFT_YES = {"ok", "okay", "sure", ":+1:", ":thumbsup:", ":ok:"}      # with no question waiting: thanks, not a yes
NO_WORDS = {"no", "n", "nope", "nah", "cancel", ":-1:", ":thumbsdown:", ":x:"}
ACK_WORDS = {"thanks", "thank", "thx", "ty", "cool", "great", "nice", "awesome", "perfect", "k", "kk", "got",
             ":pray:", ":tada:"}
CONFIRM_WORDS = {"yes", "y", "now", "confirm", "go"}
FILLER = {"recording", "the", "it", "please", "pls", "again", "event", "now", "this", "a", "an"}
PAUSE_WORDS = {"pause": "pause", "hold": "pause", "resume": "resume", "unpause": "resume", "continue": "resume"}
DESTS = {"youtube": "youtube", "yt": "youtube", "youtube uploads": "youtube", "uploads": "youtube",
         "wiki": "wiki", "the wiki": "wiki", "wiki uploads": "wiki", "team wiki": "wiki", "website": "wiki",
         "drive": "drive", "google drive": "drive", "gdrive": "drive", "the drive": "drive", "drive uploads": "drive",
         "google drive uploads": "drive", "the youtube uploads": "youtube"}


def parse_teams(text: str) -> list[str] | None:
    """"5026, 254" / "5026 254" / "frc5026" -> ["5026", "254"]; "none" -> []; None when something is not a team."""
    words = [w for w in re.split(r"[\s,;&+]+", text.strip()) if w and w.lower() != "and"]
    if [w.lower() for w in words] in (["none"], ["nobody"]):
        return []
    out = []
    for w in words:
        m = _TEAM.match(w)
        if not m:
            return None
        t = m.group(1).upper()
        if t not in out:
            out.append(t)
    return out or None


def _stream(rest: str, raw: str, slack: str) -> Command:
    low = rest.lower()
    if not rest:
        return Command("stream", None, raw=raw, slack=slack)
    if low in ("auto", "tba", "automatic", "default", "the blue alliance"):
        return Command("stream_auto", raw=raw, slack=slack)
    url = streams.normalize_stream_url(rest)
    if url is None:
        return Command("stream", error="That does not look like a YouTube or Twitch stream link. Reply set stream "
                                       f"<link>, for example: set stream {EXAMPLE_LINK}", raw=raw, slack=slack)
    return Command("stream", url, raw=raw, slack=slack)


def _teams(rest: str, raw: str, slack: str) -> Command:
    if not rest:
        return Command("teams", None, raw=raw, slack=slack)
    teams = parse_teams(rest)
    if teams is None:
        return Command("teams", error="set teams needs team numbers, for example: set teams 5026, 254 (or set teams "
                                      "none to post no matches to Slack)", raw=raw, slack=slack)
    return Command("teams", teams, raw=raw, slack=slack)


def parse(text: str) -> Command:
    """One DM as a Command (module docstring: what is cleaned, the other words that work)."""
    slack = clean(text)
    raw = streams.unwrap_slack(slack).strip()
    words = raw.split(None, 1)
    if not words:
        return Command("unknown", raw=raw, slack=slack)
    head = words[0].lower().rstrip(_TRAILING)
    rest = words[1].strip() if len(words) > 1 else ""
    low = rest.lower()
    n = len(raw.split())

    def cmd(name, arg=None, **kw):
        return Command(name, arg, raw=raw, slack=slack, **kw)
    if head in HELP_WORDS:
        topic = " ".join(w for w in low.split() if w not in ("me", "please", "pls", "with", "on", "for", "about"))
        return cmd("help", None if head in GREETINGS else topic or None)
    if head in STATUS_WORDS and len(rest) < 30:
        return cmd("status")
    if head in YES_WORDS and n <= 3:
        return cmd("yes", head)
    if head in NO_WORDS and n <= 3:
        return cmd("no", head)
    if n == 1 and head.isdigit():
        return cmd("number", int(head))
    if head in ACK_WORDS and n <= 4:
        return cmd("ack")
    if head in SPLIT_WORDS:
        toks = rest.split()
        confirm = bool(toks) and toks[-1].lower() in CONFIRM_WORDS
        toks = toks[:-1] if confirm else toks
        if all(t.lower() in FILLER for t in toks):
            toks = []
        return cmd("split", " ".join(toks), confirm=confirm)
    if head in STOP_WORDS:
        if slackadmin.is_admin(raw):                        # stop listener
            return cmd("admin")
        if len(rest) < 40:
            toks = [t.lower() for t in rest.split()]
            code = next((t for t in toks if _KEY.fullmatch(t)), None)
            return cmd("stop", code, confirm=any(t in CONFIRM_WORDS for t in toks))
    if head == "set" and rest:
        sub = rest.split(None, 1)
        s0, more = sub[0].lower(), (sub[1].strip() if len(sub) > 1 else "")
        if s0 in ("stream", "link"):
            return _stream(more, raw, slack)
        if s0 in ("teams", "team"):
            return _teams(more, raw, slack)
        if s0 == "slack" and more and more.split()[0].lower() in ("teams", "team"):
            return _teams(more.split(None, 1)[1] if len(more.split(None, 1)) > 1 else "", raw, slack)
        if s0 == "event":
            toks = more.split()
            confirm = bool(toks) and toks[-1].lower() in CONFIRM_WORDS
            return cmd("set_event", " ".join(toks[:-1] if confirm else toks), confirm=confirm)
    if slackadmin.is_admin(raw):
        return cmd("admin")
    if head in ("stream", "link"):
        return _stream(rest, raw, slack)
    if head in ("teams", "team"):
        return _teams(rest, raw, slack)
    if head in PAUSE_WORDS:
        name = PAUSE_WORDS[head]
        if low in DESTS:
            return cmd(name, DESTS[low])
        if low == "slack":
            return cmd(name, error="Only YouTube, Google Drive and wiki uploads can be paused: the team's matches go "
                                   "to Slack at once (drive team needs them).")
        close = difflib.get_close_matches(low, ["youtube", "drive", "wiki"], n=1, cutoff=0.6) if low else []
        if close:
            return cmd(name, error=f"Did you mean `{name} {close[0]}`?")
        return cmd(name, error=f"Reply {name} youtube or {name} drive (or {name} wiki).")
    return cmd("unknown")


# ---------------------------------------------------------------------------------------------------------------
# A message not understood
# ---------------------------------------------------------------------------------------------------------------

KNOWN_HEADS = ("help", "status", "list", "split", "start", "stop", "stream", "teams", "pause", "resume", "settings",
               "set", "people", "add", "remove", "yes", "no")
ADMIN_HEADS = {"set", "stream", "teams", "pause", "resume", "settings", "add", "remove"}
# a word that hints at what was meant: (the command, what it does)
KEYWORDS = {
    "tba": ("set stream auto", "record the stream link The Blue Alliance lists"),
    "bluealliance": ("set stream auto", "record the stream link The Blue Alliance lists"),
    "link": ("set stream <link>", "record another YouTube or Twitch link"),
    "twitch": ("set stream <link>", "record another YouTube or Twitch link"),
    "url": ("set stream <link>", "record another YouTube or Twitch link"),
    "upload": ("status", "how the uploads are doing"),
    "uploads": ("status", "how the uploads are doing"),
    "youtube": ("pause youtube / resume youtube", "hold or send the YouTube uploads"),
    "drive": ("pause drive / resume drive", "hold or send the Google Drive uploads"),
    "admin": ("people", "who may use this app, and who the admins are"),
    "admins": ("people", "who may use this app, and who the admins are"),
    "owner": ("people", "who may use this app, and who the admins are"),
    "who": ("people", "who may use this app, and who the admins are"),
    "setting": ("settings", "every setting and the command that changes it"),
    "event": ("split <event>", "start recording an event"),
    "recording": ("status", "what is recording and how it is going"),
}
_ADMIN_HINTS = {"set stream auto", "set stream <link>", "pause youtube / resume youtube", "pause drive / resume drive",
                "settings"}


def suggestion(text: str, admin: bool = True) -> tuple[str, str | None] | None:
    """(the command a message probably meant, what it does or None), or None: a misspelt command word ("puase
    youtube"), a link on its own, or a word that hints at one ("TBA"). Admin-only commands only for an admin."""
    raw = streams.unwrap_slack(clean(text)).strip()
    words = raw.split()
    if not words:
        return None
    low = [w.lower() for w in words]
    if len(words) == 1:
        m = _TBA_LINK.search(raw)
        if m:
            return f"split {m.group(1).lower()}", "start recording that event"
        url = streams.normalize_stream_url(raw) if "." in raw else None
        if url and admin:
            return f"set stream {url}", "record that link"
    close = difflib.get_close_matches(low[0], KNOWN_HEADS, n=1, cutoff=0.75)
    if close and close[0] != low[0] and (admin or close[0] not in ADMIN_HEADS):
        fixed = " ".join([close[0]] + words[1:])
        if len(fixed) <= 60 and parse(fixed).name != "unknown":
            return fixed, None
    for w in low:
        hit = KEYWORDS.get(w.strip(_TRAILING))
        if hit and (admin or hit[0] not in _ADMIN_HINTS):
            return hit
    return None


def unknown_reply(text: str, ctx: HelpContext) -> str:
    """The one reply to a message not understood: what it probably meant, else the few commands for this state."""
    hint = suggestion(text, admin=ctx.admin)
    if hint:
        shown = " / ".join(f"`{c.strip()}`" for c in hint[0].split(" / "))
        return f"{UNKNOWN} Did you mean {shown}" + (f" ({hint[1]})?" if hint[1] else "?")
    return "\n".join([f"{UNKNOWN} What you can do now:"] + _rows(top_commands(ctx)[:4])
                     + ["`help all` lists every command."])


# ---------------------------------------------------------------------------------------------------------------
# The listener of an older version (before 2026-11-10): what it answered, so a recording does not answer it too
# ---------------------------------------------------------------------------------------------------------------

def _legacy_is_admin(raw: str) -> bool:
    words = raw.split()
    if not words:
        return False
    head = words[0].lower().lstrip("/!").rstrip(":")
    if head == "stop" and len(words) > 1 and words[1].lower() in ("listener", "listening", "the"):
        return " ".join(words[1:]).lower() in ("listener", "listening", "the listener")
    if head not in ("settings", "setting", "set", "people", "add", "remove"):
        return False
    if head == "set" and len(words) > 1:
        sub = [w.lower() for w in words[1:3]]
        if sub[0] in ("stream", "link", "teams", "team") or sub[:2] in (["slack", "teams"], ["slack", "team"]):
            return False
    return True


def legacy_listener_command(text: str) -> bool:
    """Whether a listener of an older version answers this DM itself (its split, stop, list, yes, no, numbers,
    settings and people), as its own listener_command said: then a recording leaves it to that listener, and the rest
    is the recording's to answer (and the other way round in listener.py, for a recording of an older version)."""
    raw = streams.unwrap_slack(text or "").strip()
    words = raw.split(None, 1)
    if not words:
        return False
    head = words[0].lower().lstrip("/!").rstrip(":")
    rest = words[1].strip() if len(words) > 1 else ""
    if head == "split":
        return True
    if len(words) == 1 and (head.rstrip(".!?") in ("list", "yes", "y", "no", "n") or head.isdigit()):
        return True
    if head in ("help", "?", "commands", "command") or (head == "status" and not rest):
        return False
    if head == "set" and rest:
        sub = rest.split(None, 1)
        if sub[0].lower() in ("stream", "link", "teams", "team"):
            return False
        if sub[0].lower() == "slack" and len(sub) > 1 and sub[1].split()[0].lower() in ("teams", "team"):
            return False
    if _legacy_is_admin(raw):
        return True
    if head in ("stream", "link", "teams", "team", "pause", "hold", "resume", "unpause", "continue"):
        return False
    return head in ("stop", "quit", "exit", "shutdown", "kill") and len(rest) < 40


listener_command = legacy_listener_command      # the old name


def split_message(text: str, limit: int = MESSAGE_MAX) -> list[str]:
    """`text` as one message, or several of at most `limit` characters cut at line ends (a longer line is cut)."""
    out, cur = [], ""
    for line in str(text).split("\n"):
        while len(line) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur or not out:
        out.append(cur)
    return out


def post_reply(client, channel: str, text: str, thread: str | None = None, *, notifier=None, log=print) -> dict | None:
    """Post `text` (plain text with Slack markup; & < > are escaped here) in `channel`, in `thread` when given, as a
    few messages when it is long. When Slack refuses now, it is queued as a DM through `notifier` (given only when
    that DM goes to the asker)."""
    parts = split_message(text)
    out = None
    for i, part in enumerate(parts):
        try:
            out = client.post_message(channel, _esc(part), thread_ts=thread)
        except Exception as e:  # noqa: BLE001 - the notifier keeps it and retries
            log(f"Slack commands: could not reply ({type(e).__name__})" + ("; queued as a DM" if notifier else ""))
            if notifier is not None:
                for rest in parts[i:]:
                    notifier.info(rest, dm=True)
            return None
    return out


# ---------------------------------------------------------------------------------------------------------------
# The running recording's own commands
# ---------------------------------------------------------------------------------------------------------------

def dispatch(cmd: Command, controls, channel_name: str | None = None, listen: bool = False) -> str:
    """The reply (plain text) to one of the running recording's commands, after doing it. `controls` does the work
    (live.LiveControls): status_text(), stream_text(), set_stream(url), stream_auto(), teams_text(), set_teams(list),
    pause(dest), resume(dest); `controls.drive_on` / `controls.wiki_on` (optional) list the Drive / wiki commands in
    help. Settings, people and questions are CommandPoller's (slackadmin.Admin); split and stop the listener's."""
    if cmd.error:
        return cmd.error
    if cmd.name == "help":
        return help_reply(cmd.arg, channel_name=channel_name, wiki=bool(getattr(controls, "wiki_on", False)),
                          drive=bool(getattr(controls, "drive_on", False)), listen=listen)
    if cmd.name in ("yes", "no"):
        return NOTHING_TO_CONFIRM
    if cmd.name == "status":
        return controls.status_text()
    if cmd.name == "stream":
        if cmd.arg is None:
            return (controls.stream_text() + "\nTo record another link, reply: set stream <link>. To use the link The "
                                              "Blue Alliance lists, reply: set stream auto")
        return controls.set_stream(cmd.arg)
    if cmd.name == "stream_auto":
        return controls.stream_auto()
    if cmd.name == "teams":
        if cmd.arg is None:
            return controls.teams_text() + "\nTo change them, reply for example: set teams 5026, 254"
        return controls.set_teams(cmd.arg)
    if cmd.name == "pause":
        return controls.pause(cmd.arg)
    if cmd.name == "resume":
        return controls.resume(cmd.arg)
    if cmd.name == "stop":
        return STOP_HOW
    return UNKNOWN


# ---------------------------------------------------------------------------------------------------------------
# Which listener is alive
# ---------------------------------------------------------------------------------------------------------------

def _ts(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def listener_protocol() -> int:
    """0: no Slack listener alive on this computer (its lock free); else the protocol its status.json names (1: a
    listener of an older version, which names none). An unreadable status of a live listener counts as 1, which
    keeps the answers of the older versions."""
    try:
        from .. import listener
        if not listener.is_running():
            return 0
        path = listener.listen_folder() / listener.STATUS_FILE
        for _ in range(3):                       # it is replaced atomically; Windows can refuse an instant
            try:
                st = json.loads(path.read_text(encoding="utf-8"))
                return int(st.get("protocol") or 1) if isinstance(st, dict) else 1
            except (OSError, ValueError):
                time.sleep(0.05)
        return 1
    except Exception:  # noqa: BLE001 - an unreadable lock: answer as without the listener
        return 0


def listener_on() -> bool:
    """Whether a Slack listener is alive on this computer (its lock is held, in the background or in a window)."""
    return listener_protocol() > 0


def shared_position(user: str) -> Path:
    """The read position of `user`'s DM that the listener and a recording share."""
    from .. import listener
    return listener.listen_folder() / f"commands_{user}.json"


def _read_position(path: Path | None) -> float | None:
    if path is None:
        return None
    try:
        return float(json.loads(Path(path).read_text(encoding="utf-8"))["last_ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_position(path: Path, ts: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"last_ts": f"{ts:.6f}"}), encoding="utf-8")
    os.replace(tmp, path)


def missed_note(msgs: list[dict]) -> str:
    """The one note about messages sent while nothing read Slack (older than the restart grace): not done."""
    times = sorted(_ts(m.get("ts")) for m in msgs)
    n = len(times)
    when = _clock(times[0]) if n == 1 else f"{_clock(times[0])} to {_clock(times[-1])}"
    return (f"StreamSplitter was not reading Slack when you sent {'a message' if n == 1 else f'{n} messages'} "
            f"({when}), so {'it was' if n == 1 else 'they were'} not done. Send again what you still want.")


# ---------------------------------------------------------------------------------------------------------------
# Reading a DM
# ---------------------------------------------------------------------------------------------------------------

class CommandPoller:
    """Reads one person's DM with the app and answers each message (module docstring). poll() is one round, called
    by live.py's watcher thread (or the listener's loop) when its return value (seconds) has passed. `listen` (for
    tests): the protocol of the Slack listener alive (0 / False: none; 1 / True: an older version; 2: this one);
    default listener_protocol(), asked every round. `admin` (for tests): the slackadmin.Admin answering settings and
    people with no listener alive (made when first needed). `limited`: seconds Slack asked to wait after a
    `ratelimited` answer (the listener reads it, then sets 0). `shared_path`: the read position shared with the
    listener (shared_position), read when this poller takes over and written while it reads alone. `reads`: the
    conversations.history calls made (the listener's budget)."""

    def __init__(self, client, user_id: str, controls, *, notifier=None, channel_name: str | None = None,
                 state_path: Path | None = None, shared_path: Path | None = None, log=print, clock=time.time,
                 listen=None, admin=None, host: str | None = None):
        self.client, self.user, self.controls = client, user_id, controls
        self.notifier = notifier
        self.channel_name = channel_name
        self.state_path = Path(state_path) if state_path else None
        self.shared_path = Path(shared_path) if shared_path else None
        self.log, self.clock = log, clock
        self.listen = listen if listen is not None else listener_protocol
        self.admin = admin
        self.host = host or socket.gethostname()
        self.pending: dict | None = None        # the settings question waiting for its answer (no listener alive)
        self.limited = 0.0
        self.scope_ok: bool | None = None       # None: not known yet
        self.can_react: bool | None = None      # reactions:write: None until known
        self.reads = 0
        self.heard: float | None = None         # when this person's last message was answered (this clock)
        self._scope_warned = False
        self._channel: str | None = None
        self._mode: str | None = None
        self._gap_from: float | None = None     # the position saved before a gap longer than RESTART_GRACE_S
        self.last_ts: float | None = self._load()
        self._lock = threading.Lock()

    # state across restarts ----------------------------------------------------------------------------------------
    def _load(self) -> float | None:
        saved = [t for t in (_read_position(self.state_path), _read_position(self.shared_path)) if t is not None]
        if not saved:
            return None
        floor = self.clock() - RESTART_GRACE_S
        if max(saved) < floor:
            self._gap_from = max(saved)
        return max(max(saved), floor)

    def _save(self) -> None:
        if self.last_ts is None:
            return
        paths = [self.state_path] + ([self.shared_path] if self._mode == SOLO else [])
        for path in paths:
            if path is None:
                continue
            try:
                _write_position(path, self.last_ts)
            except OSError as e:
                self.log(f"could not save {path} ({e})")

    def _catch_up(self) -> None:
        """Taking over from the listener: start after what it answered (and nothing before was missed: it read)."""
        self._gap_from = None
        shared = _read_position(self.shared_path)
        if shared is not None:
            self.last_ts = max(self.last_ts or 0.0, shared)

    # who reads ------------------------------------------------------------------------------------------------------
    def mode(self) -> str:
        """SOLO (no listener: answer everything), LEGACY (a listener of an older version: answer what it does not)
        or OFF (this version's listener reads this DM: read nothing)."""
        try:
            p = int(self.listen() or 0)
        except Exception:  # noqa: BLE001 - as without the listener
            p = 0
        return SOLO if p <= 0 else LEGACY if p < PROTOCOL else OFF

    # scopes ---------------------------------------------------------------------------------------------------------
    def check_scope(self) -> bool | None:
        """Whether the token has im:history, from auth.test (None when Slack does not say or cannot be reached)."""
        try:
            info = self.client.auth_test()
        except Exception as e:  # noqa: BLE001 - reading the DM says it too
            self.log(f"Slack commands: could not check the app's permissions ({type(e).__name__})")
            return None
        scopes = info.get("scopes")
        if scopes is None:
            return None
        if self.can_react is None:
            self.can_react = REACT_SCOPE in scopes
        ok = NEEDED_SCOPE in scopes and NEEDED_SCOPE not in (info.get("missing_scopes") or [])
        if not ok:
            self._missing()
        else:
            self.scope_ok = True
        return ok

    def _missing(self) -> None:
        self.scope_ok = False
        if not self._scope_warned:
            self._scope_warned = True
            self.log("Slack commands are off: the app lacks the im:history permission")
            if self.notifier is not None:
                self.notifier.warn(SCOPE_FIX, key="slack_im_history")

    def react(self, m: dict, name: str = SEEN) -> bool:
        """Put `name` on the message `m` (reactions.add). Nothing is said when the app may not: the permission is
        optional, and a missing one is remembered."""
        if self.can_react is False or not hasattr(self.client, "add_reaction") or not self._channel:
            return False
        try:
            self.client.add_reaction(self._channel, str(m.get("ts")), name)
        except PermanentError as e:
            code = getattr(e, "code", "")
            if code == "already_reacted":
                return True
            if code in _NO_REACTIONS:
                self.can_react = False
                self.log(f"Slack commands: no reactions ({code}: the optional reactions:write); replies only")
            return False
        except Exception as e:  # noqa: BLE001 - only a nicety
            self.log(f"Slack commands: could not react ({type(e).__name__})")
            return False
        self.can_react = True
        return True

    # one round ------------------------------------------------------------------------------------------------------
    def poll(self) -> float:
        with self._lock:
            return self._poll()

    def next_wait(self) -> float:
        """FAST_S for HOT_S after this person wrote, else POLL_S."""
        return FAST_S if self.heard is not None and self.clock() - self.heard < HOT_S else POLL_S

    def _poll(self) -> float:
        mode = self.mode()
        before, self._mode = self._mode, mode
        if mode == OFF:
            self._gap_from = None                # the listener reads this DM: whatever it saw was not missed
            return POLL_S
        if before in (OFF, LEGACY) and mode == SOLO:
            self._catch_up()
        if self.scope_ok is None and self.check_scope() is False:
            return SCOPE_RETRY_S
        try:
            if self._channel is None:
                self._channel = self.client.open_dm(self.user)
            self.reads += 1
            msgs = self.client.history(self._channel, limit=HISTORY_LIMIT)
        except PermanentError as e:
            if getattr(e, "code", "") == "missing_scope":
                self._missing()
                return SCOPE_RETRY_S
            self.log(f"Slack commands: cannot read the DM ({e}); trying again in {SCOPE_RETRY_S / 60:.0f} min")
            return SCOPE_RETRY_S
        except TransientError as e:
            wait = max(float(e.retry_after or 0.0), ERROR_WAIT_S)
            if getattr(e, "code", "") == "ratelimited":
                self.limited = wait              # Slack's limit is per app: the listener holds every read back
            return wait
        except Exception as e:  # noqa: BLE001 - never stop the watcher thread
            self.log(f"Slack commands: {type(e).__name__}: {e}")
            return ERROR_WAIT_S
        if self.scope_ok is not True:
            if self.scope_ok is False and self.notifier is not None:
                self.notifier.info(SCOPE_OK)
            self.scope_ok = True
        newest = max([_ts(m.get("ts")) for m in msgs] + [_ts(m.get("latest_reply")) for m in msgs] + [0.0])
        if self.last_ts is None:                 # the first look: what is there already is not a command
            self.last_ts = newest                # (Slack's clock; 0 for an empty DM, which has nothing old)
            self._save()
            return self.next_wait()
        last = self.last_ts
        if self._gap_from is not None:
            gap, self._gap_from = self._gap_from, None
            missed = [m for m in msgs if gap < _ts(m.get("ts")) <= last and self._is_command(m)]
            if missed and mode != LEGACY:
                self._note(missed)
        todo = [m for m in msgs if _ts(m.get("ts")) > last and self._is_command(m)]
        for parent in msgs:
            if _ts(parent.get("latest_reply")) > last and parent.get("reply_count"):
                try:
                    replies = self.client.replies(self._channel, parent["ts"], oldest=f"{last:.6f}",
                                                  limit=HISTORY_LIMIT)
                except Exception as e:  # noqa: BLE001 - the thread is read again next time
                    self.log(f"Slack commands: could not read a thread ({type(e).__name__})")
                    newest = min(newest, _ts(parent.get("ts")))
                    continue
                todo += [r for r in replies if _ts(r.get("ts")) > last and r.get("ts") != parent.get("ts")
                         and self._is_command(r)]
        todo.sort(key=lambda m: _ts(m.get("ts")))
        for m in todo[:MAX_PER_POLL]:
            self._answer(m)
            self.last_ts = max(self.last_ts, _ts(m.get("ts")))
            self._save()
        if len(todo) <= MAX_PER_POLL:
            self.last_ts = max(self.last_ts, newest)
            self._save()
            return self.next_wait()
        return 1.0                               # more waiting: the next ones soon

    def _note(self, missed: list[dict]) -> None:
        self.log(f"Slack commands: {len(missed)} message(s) from before a gap not done (said so)")
        post_reply(self.client, self._channel, missed_note(missed), notifier=self._fallback(), log=self.log)

    def _is_command(self, m: dict) -> bool:
        return (m.get("user") == self.user and not m.get("bot_id") and not m.get("app_id")
                and m.get("subtype") in (None, "thread_broadcast") and bool(str(m.get("text") or "").strip()))

    # answering ------------------------------------------------------------------------------------------------------
    def _answer(self, m: dict) -> None:
        text = str(m.get("text") or "")
        secret = slackadmin.secret_reply(text)
        if self._mode == LEGACY and (secret is not None or legacy_listener_command(text)):
            self.log("Slack command for the Slack listener (an older version): left to it")
            return
        self.heard = self.clock()
        if secret is not None:
            self.react(m)
            self.log("Slack message from the contact looked like a password or key: not used")
            self._reply(m, secret)
            return
        cmd = parse(text)
        ack = cmd.name == "ack" or (cmd.name == "yes" and cmd.arg in SOFT_YES and self.pending is None)
        reacted = self.react(m, THANKS if ack else SEEN)
        self.log(f"Slack command from the contact: {cmd.name}" + (" (not understood)" if cmd.name == "unknown" else ""))
        try:
            reply = (None if reacted else ":+1:") if ack else self.reply_to(cmd, m)
        except Exception as e:  # noqa: BLE001 - say so rather than nothing
            from .notify import redact
            self.log(f"Slack command {cmd.name} failed: {type(e).__name__}: {redact(e)}")
            reply = (f"That did not work ({type(e).__name__}: {redact(e)}). The log on the StreamSplitter computer "
                     f"says more.")
        if reply:
            self._reply(m, reply)

    def reply_to(self, cmd: Command, m: dict) -> str | None:
        """The reply to one message of this DM, after doing what it asks (no listener alive, or an older one)."""
        name, legacy = cmd.name, self._mode == LEGACY
        if name == "help":
            if not cmd.arg:
                return first_help(self.help_context())
            return help_reply(cmd.arg, channel_name=self.channel_name, owner=True, listen=legacy,
                              wiki=bool(getattr(self.controls, "wiki_on", False)),
                              drive=bool(getattr(self.controls, "drive_on", False)))
        if name == "status":
            text = self.controls.status_text()
            return text if legacy else f"{text}\n{self.listener_line()}"
        if name in ("admin", "yes", "no", "number", "set_event"):
            return self._admin_answer(m, cmd)
        if name == "split":                     # an older listener left it (start, record): not a word it knew
            return f"Did you mean `split {cmd.arg or '<event>'}`?" if legacy else SPLIT_NEEDS
        if name == "stop":
            return "Did you mean `stop`?" if legacy else STOP_HOW
        if name in RECORDING_COMMANDS:
            return dispatch(cmd, self.controls, self.channel_name)
        return unknown_reply(str(m.get("text") or ""), self.help_context())

    def listener_line(self) -> str:
        return (f"Slack listener: not running on {self.host}, so split and stop do not work from Slack now. This "
                f"recording answers here; it reads this chat every {POLL_S:.0f} s (every {FAST_S:.0f} s for "
                f"{HOT_S / 60:.0f} minutes after you write).")

    def help_context(self) -> HelpContext:
        facts = getattr(self.controls, "help_facts", None)
        try:
            facts = facts() if callable(facts) else {}
        except Exception:  # noqa: BLE001 - only the help's first line
            facts = {}
        facts = facts if isinstance(facts, dict) else {}
        return HelpContext(host=self.host, admin=True, listener=self._mode == LEGACY,
                           recording=facts.get("recording") or "the event", since=facts.get("since"),
                           drive=bool(facts.get("drive")), wiki=bool(facts.get("wiki")), every_s=POLL_S,
                           react=bool(self.can_react))

    def _admin(self):
        if self.admin is None:
            self.admin = slackadmin.Admin(self.client, log=self.log, clock=self.clock, recording=lambda: True)
        return self.admin

    def _admin_answer(self, m: dict, cmd: Command) -> str:
        """Settings and people (slackadmin.Admin) with no listener alive, and the answers to their questions (one
        waiting at a time, in memory, QUESTION_S long by Slack's message times)."""
        ts = str(m.get("ts") or "")
        if cmd.name in ("admin", "set_event"):
            reply, q = self._admin().handle(self.user, cmd.slack or str(m.get("text") or ""))
            if q is not None:
                self.pending = {"ts": ts, "q": q}
            return reply
        p, self.pending = self.pending, None
        if p is None:
            return NOTHING_TO_CONFIRM if cmd.name in ("yes", "no") else NOTHING_TO_PICK
        if _ts(ts) - _ts(p["ts"]) > slackadmin.QUESTION_S:
            again = p["q"].get("again")
            return (f"That question expired after {QUESTION_MIN} minutes, so nothing was done."
                    + (f" Reply {again} to ask again." if again else ""))
        word = str(cmd.arg) if cmd.name == "number" else cmd.name
        reply, q = self._admin().confirm(self.user, p["q"], word)
        if q is not None:
            self.pending = {"ts": ts, "q": q}
        return reply

    def _fallback(self):
        """The notifier, when its DM goes to this person (a reply Slack refused now is queued there)."""
        if self.notifier is None:
            return None
        return self.notifier if getattr(self.notifier, "user", self.user) == self.user else None

    def _reply(self, m: dict, reply: str) -> dict | None:
        """Post `reply` (plain text) in the DM, in the thread `m` was in, as a few messages when it is long
        (split_message); queued as a DM when Slack refuses now."""
        thread = m.get("thread_ts") if m.get("thread_ts") and m.get("thread_ts") != m.get("ts") else None
        return post_reply(self.client, self._channel, reply, thread, notifier=self._fallback(), log=self.log)


# ---------------------------------------------------------------------------------------------------------------
# The recording's mailbox (module docstring: who answers)
# ---------------------------------------------------------------------------------------------------------------

def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def advertised(folder: Path) -> dict | None:
    """The recording's advert in its folder ({"protocol", "pid", "since"}), if any."""
    try:
        data = json.loads((Path(folder) / ADVERT_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def claim(path: Path, suffix: str) -> Path | None:
    """Rename `path` to the same name with `suffix`; None when someone else renamed it first (the one who renames
    first decides: the recording taking a request, or the listener giving up on it)."""
    target = path.with_suffix(suffix)
    try:
        os.rename(path, target)
    except OSError:
        return None
    return target


class Mailbox:
    """The running recording's end of the listener's mailbox: every MAILBOX_EVERY_S it takes the requests in
    <its folder>/slack_in/ (a rename to .taken; the listener may rename an untaken one to .expired first), does each
    with dispatch() and LiveControls, and writes the reply into the listener's replies folder, for the listener to post
    (the request says where: channel and thread). start() advertises it (ADVERT_FILE, with this process's id, which
    listener.Runner.mailbox checks against the recording's status); stop() withdraws it. `lock` is held while a
    command runs (live.py's Watcher holds it for EventWatch and its own reads), so they never run at the same time."""

    def __init__(self, folder: Path, controls, *, channel_name: str | None = None, replies: Path | None = None,
                 log=print, every_s: float = MAILBOX_EVERY_S, clock=time.time, lock=None):
        self.folder = Path(folder)
        self.inbox = self.folder / INBOX_DIR
        self.controls, self.channel_name = controls, channel_name
        self._replies = Path(replies) if replies else None
        self.log, self.every_s, self.clock = log, every_s, clock
        self.lock = lock if lock is not None else threading.RLock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="slack-mailbox", daemon=True)

    @property
    def replies(self) -> Path:
        if self._replies is None:
            from .. import listener
            self._replies = listener.listen_folder() / REPLIES_DIR
        return self._replies

    def advertise(self) -> None:
        self.inbox.mkdir(parents=True, exist_ok=True)
        _write_json(self.folder / ADVERT_FILE, {"protocol": PROTOCOL, "pid": os.getpid(), "since": self.clock()})

    def withdraw(self) -> None:
        try:
            (self.folder / ADVERT_FILE).unlink(missing_ok=True)
        except OSError:
            pass

    def start(self) -> None:
        try:
            self.advertise()
        except OSError as e:
            self.log(f"Slack mailbox: could not open it ({e}); the Slack listener cannot reach this recording")
            return
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.withdraw()
        if self._thread.is_alive():
            self._thread.join(5.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.process()
            except Exception as e:  # noqa: BLE001 - never let the mailbox thread die
                self.log(f"Slack mailbox: {type(e).__name__}: {e}")
            self._stop.wait(self.every_s)

    def process(self) -> int:
        """Answer every request waiting; how many."""
        try:
            waiting = sorted(self.inbox.glob("*.json"))
        except OSError:
            return 0
        done = 0
        for path in waiting:
            taken = claim(path, ".taken")
            if taken is None:
                continue                          # the listener gave up on it first
            try:
                req = json.loads(taken.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                taken.unlink(missing_ok=True)
                continue
            try:
                cmd = parse(str(req.get("text") or ""))
                with self.lock:
                    reply = dispatch(cmd, self.controls, self.channel_name)
            except Exception as e:  # noqa: BLE001 - say so rather than nothing
                from .notify import redact
                self.log(f"Slack mailbox: {type(e).__name__}: {redact(e)}")
                reply = (f"That did not work ({type(e).__name__}: {redact(e)}). The recording's log on its computer "
                         f"says more.")
            if req.get("append"):
                reply = f"{reply}\n{req['append']}"
            finished = claim(taken, ".done")
            if finished is None:
                continue                          # the listener stopped waiting meanwhile: it said so
            try:
                _write_json(self.replies / f"{req.get('id') or taken.stem}.json",
                            {"id": req.get("id"), "user": req.get("user"), "channel": req.get("channel"),
                             "thread": req.get("thread"), "text": reply})
            except OSError as e:
                self.log(f"Slack mailbox: could not hand the reply over ({e})")
            finished.unlink(missing_ok=True)
            done += 1
        return done
