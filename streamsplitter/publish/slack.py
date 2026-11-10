"""Slack: the team's own match clips in a channel, the score version as a reply in the clip's thread, and direct
messages to a contact (info, warnings, and the stopped-running alarm: a scheduled DM that notify.py keeps pushing
back, so it only fires when the script dies).

Uploads use Slack's external upload flow (files.upload was switched off on 2025-11-12): files.getUploadURLExternal
gives an upload URL and a file id, the bytes are POSTed there, and files.completeUploadExternal shares the file to the
channel with the comment. None of the three returns the ts of the message the file lands in, which the score reply
needs as its thread. The share shows up in files.info's `shares` a moment later (Slack scans the file first, longer
for bigger files), so upload_file polls files.info for up to SHARE_WAIT_S. When it has not shown by then, the result
carries ts None and the file id; a later score job resolves it with find_share() (SlackDestination does so from
job.state["thread_file_id"]) and otherwise posts to the channel.

A video plays in Slack only after Slack has processed it (its own copies for playback), which starts once the file is
shared: a person who opens it at once may see it stall on a frame for a while (user's setup test, 2026-11-04). Our
files are not the cause: every posted file is an MP4 with its index first (+faststart: cutter.cut, cut_score,
trim_start and small_copy). Nor can it be hidden until ready (Slack's docs, checked 2026-11-04): files.info has no
processing state (only mp4 / hls / thumb_video fields, undocumented as to when they appear), and a file uploaded
without a channel stays private: files.completeUploadExternal can be called only once per file, and no public method
shares an uploaded file to a channel afterwards (files.share is not part of the Web API; files.sharedPublicURL needs a
user token and makes a public link). So the flow stays: upload, then share with the comment.

Every call is one attempt (slack_sdk's own retries are off): failures become TransientError (no network, HTTP
429/5xx, Slack's server-side codes) or PermanentError saying what a person should do; the outbox retries.

Reading the contact's replies (publish/commands.py): history() and replies() read the DM with the app
(conversations.history / conversations.replies, Tier 3 for an app made in its own workspace). They need the bot scope
im:history (REQUIRED_SCOPES, from the manifest; apps made before 2026-11-04 lack it until it is added and the app
reinstalled), and the App Home messages tab must accept messages (the manifest's messages_tab_read_only_enabled is
false since then).

Scheduled messages (Slack's docs for chat.scheduleMessage and chat.deleteScheduledMessage, checked 2026-11-04):
post_at must be in the future and at most 120 days ahead; at most 30 messages may be due in one channel within any
5 minutes; a message due within 60 s can no longer be deleted (invalid_scheduled_message_id); both are Tier 3
(50+ calls a minute); DM channel ids (D...) and user ids are accepted. schedule_message keeps post_at at least
MIN_LEAD_S ahead (so the alarm can always still be cancelled), corrects for this computer's clock from Slack's Date
header, and always schedules to the DM channel id (a delete must name the same channel); delete_scheduled returns
False instead of raising when Slack says the message is gone or too close to its time.
"""

from __future__ import annotations

import email.utils
import http.client
import json
import logging
import re
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError, SlackClientError

from .. import ffmpeg
from .base import Job, Match, PermanentError, TransientError
from .commands import short_help

log = logging.getLogger(__name__)

MANIFEST = Path(__file__).with_name("slack_manifest.json")
SAMPLE_CLIP = Path(__file__).resolve().parent.parent / "assets" / "sample_match_2024mttd_qm6.mp4"
APPS_URL = "https://api.slack.com/apps"

SHARE_WAIT_S = 30.0          # how long upload_file waits for the message ts to show in files.info
RETRY_SHARE_WAIT_S = 10.0    # the same on a retry, for an upload whose completion may have gone through
UPLOAD_TIMEOUT_S = 120.0     # socket timeout for POSTing the video (the API calls use the client's timeout)
COPY_TIMEOUT_S = 300.0       # a smaller copy takes ~7 s; a hung ffmpeg must not hold the Slack queue forever
MIN_LEAD_S = 90              # Slack refuses to delete a scheduled message due within 60 s; keep the alarm cancellable
MAX_AHEAD_S = 120 * 86400 - 3600   # Slack's limit is 120 days

# The message text. Slack mrkdwn: *bold*; the values are escaped. {red}/{blue} are the team numbers, the teams in
# SlackDestination's `teams` in bold. A clip with no teams known gets CLIP_FORMAT_NO_TEAMS.
CLIP_FORMAT = "*{title}*{event}\nRed {red}  |  Blue {blue}"
CLIP_FORMAT_NO_TEAMS = "*{title}*{event}"
SCORE_FORMAT = "*{title}* with the results screen{score}"        # {score}: ": Red 112 - Blue 98" once TBA has it
TEST_FORMAT = ("StreamSplitter setup test: if you can see this sample match video, the team's match videos will be "
               "posted in this channel.\n" + short_help(slack=True))   # ends with the Slack commands (commands.py)

_CHANNEL_ID = re.compile(r"^[CG][A-Z0-9]{8,}$")
_USER_ID = re.compile(r"^[UW][A-Z0-9]{8,}$")
_NOT_JSON = "Received a response in a non-JSON format"   # slack_sdk's error text for an HTML page (Wi-Fi login)

# Slack error codes that a later try can get past: server trouble, workspace migrations, send-rate windows, and a
# network that is not allowed (venue Wi-Fi; another network works)
_TRANSIENT = {"ratelimited", "rate_limited", "fatal_error", "internal_error", "service_unavailable", "request_timeout",
              "team_added_to_org", "org_login_required", "message_limit_exceeded", "restricted_too_many",
              "accesslimited"}

_FIX_TOKEN = ("Slack no longer accepts StreamSplitter's token ({code}): the app may have been removed from the "
              "workspace, or the token was copied wrong. Open https://api.slack.com/apps -> StreamSplitter -> OAuth & "
              "Permissions, copy the Bot User OAuth Token (it starts with xoxb-) and run `streamsplitter setup` again "
              "(docs/setup-slack.md).")
_FIX_SCOPE = ("The StreamSplitter Slack app lacks the permission {needed}. Open https://api.slack.com/apps -> "
              "StreamSplitter -> OAuth & Permissions, add {needed} under Bot Token Scopes, click Reinstall to "
              "Workspace, then run `streamsplitter setup` again if the token changed.")
_FIX_INVITE = "Invite the StreamSplitter app to {where}: type /invite @StreamSplitter there."
_FIX_GONE = ("Slack cannot find {where}: it was deleted, or it is private and the StreamSplitter app is not in it. "
             "Type /invite @StreamSplitter in the channel, or pick another channel with `streamsplitter setup`.")
_FIX_PRIVATE = ("{where} is private, so the app cannot join it by itself: type /invite @StreamSplitter in it.")
_FIX_JOIN = ("The StreamSplitter app cannot join {where} ({code}). Type /invite @StreamSplitter in it, or pick "
             "another channel with `streamsplitter setup`.")
_FIX_ARCHIVED = "{where} is archived. Unarchive it in Slack, or pick another channel with `streamsplitter setup`."
_FIX_DENIED = ("Slack does not let the StreamSplitter app post in {where} ({code}). Ask a workspace admin to allow "
               "it, or pick another channel with `streamsplitter setup`.")
_FIX_UPLOADS = ("This Slack workspace does not let the app upload videos ({code}). Ask a workspace admin to allow "
                "file uploads, or turn Slack off with `streamsplitter setup`.")
_FIX_SIZE = ("This Slack workspace refuses large uploads. Run `streamsplitter setup --section slack` and choose the "
             "smaller 720p videos, or ask a workspace admin.")
_FIX_STORAGE = ("The Slack workspace's file storage is full. Delete old files in Slack or ask a workspace admin; "
                "videos cannot be posted until there is room.")
_FIX_PERSON = "Slack cannot message that person ({code}). Pick the contact again with `streamsplitter setup`."

_ADVICE = {
    **dict.fromkeys(("invalid_auth", "not_authed", "token_revoked", "token_expired", "account_inactive", "bad_token",
                     "invalid_token", "not_allowed_token_type", "team_access_not_granted", "enterprise_is_restricted",
                     "two_factor_setup_required"), _FIX_TOKEN),
    "missing_scope": _FIX_SCOPE,
    "not_in_channel": _FIX_INVITE,
    **dict.fromkeys(("channel_not_found", "invalid_channel"), _FIX_GONE),
    "method_not_supported_for_channel_type": _FIX_PRIVATE,
    **dict.fromkeys(("app_cannot_join_channel", "too_many_members", "channel_is_limited_access"), _FIX_JOIN),
    "is_archived": _FIX_ARCHIVED,
    **dict.fromkeys(("posting_to_channel_denied", "restricted_action", "restricted_action_read_only_channel",
                     "restricted_action_thread_only_channel", "restricted_action_non_threadable_channel",
                     "ekm_access_denied", "no_permission", "access_denied"), _FIX_DENIED),
    **dict.fromkeys(("file_uploads_disabled", "file_uploads_except_images_disabled", "file_type_not_allowed",
                     "user_is_external_guest", "slack_connect_file_link_sharing_blocked"), _FIX_UPLOADS),
    "file_upload_size_restricted": _FIX_SIZE,
    "storage_limit_reached": _FIX_STORAGE,
    **dict.fromkeys(("user_not_found", "users_not_found", "user_disabled", "cannot_dm_bot"), _FIX_PERSON),
}
_THREAD_GONE = {"thread_not_found", "invalid_thread_ts", "message_not_found", "restricted_action_non_threadable_channel"}


def _code(e: Exception) -> str:
    if isinstance(e, SlackApiError) and isinstance(e.response.data, dict):
        return str(e.response.data.get("error") or "")
    return ""


def _header(headers, name: str) -> str | None:
    for k, v in (headers or {}).items():
        if k.lower() == name:
            return v[0] if isinstance(v, list) else v
    return None


def _retry_after(headers) -> float | None:
    try:
        v = _header(headers, "retry-after")
        return None if v is None else max(0.0, float(v))
    except (TypeError, ValueError):
        return None


def _server_time(headers) -> float | None:
    try:
        return email.utils.parsedate_to_datetime(_header(headers, "date")).timestamp()
    except (TypeError, ValueError, IndexError):
        return None


def _esc(s) -> str:
    """Slack treats &, < and > as markup in message text."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _permanent(message: str, code: str = "") -> PermanentError:
    err = PermanentError(message)
    err.code = code
    return err


def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


# the bot scopes StreamSplitter asks for (auth_test reports the ones a token lacks as missing_scopes); im:history,
# added 2026-11-04 for the Slack commands, is the one older installs lack. OPTIONAL_SCOPES are in the manifest (a new
# app gets them) but nothing needs them: reactions:write (2026-11-10) puts :eyes: on a command as soon as it is read,
# and an app without it just answers (publish/commands.py), so the setup does not ask an existing app to add it.
OPTIONAL_SCOPES: tuple[str, ...] = ("reactions:write",)
REQUIRED_SCOPES: tuple[str, ...] = tuple(s for s in manifest()["oauth_config"]["scopes"]["bot"]
                                         if s not in OPTIONAL_SCOPES)


def create_app_url() -> str:
    """Slack's "create an app from a manifest" link with StreamSplitter's manifest filled in (Slack documents
    https://api.slack.com/apps?new_app=1&manifest_json=<URL-encoded JSON>). The person picks the workspace, reviews
    the summary and clicks Create; the link holds nothing secret."""
    body = json.dumps(manifest(), separators=(",", ":"))
    return f"{APPS_URL}?new_app=1&manifest_json={urllib.parse.quote(body, safe='')}"


def match_title(m: Match) -> str:
    """The match as FIRST names it ("Qualification 12"), from templates.py so Slack and YouTube agree; the clip name
    when that cannot be had (templates.py belongs to another module and must not stop a clip reaching drive team)."""
    try:
        from .templates import match_title as title_of
        t = title_of(m)
        t = t if isinstance(t, str) else t[0]
        return t or m.name
    except Exception:
        return m.name


def small_copy(src: Path, height: int, cache_dir: Path | None = None) -> Path:
    """A `height`p copy of `src` for phones, made once and reused on retry. It goes in a slack_<h>p folder beside the
    clip, not beside the clip itself, so the follower's *.mp4 scan of the clip folder does not count it as a clip."""
    src = Path(src)
    folder = Path(cache_dir) if cache_dir else src.parent / f"slack_{int(height)}p"
    dst = folder / src.name
    if dst.is_file() and dst.stat().st_size > 0 and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".{src.name}.part"
    # the same re-encode as pipeline._deliver (libx264 veryfast, ~7 s and 3x smaller for a 170 s 1080p clip)
    subprocess.run([ffmpeg.find(), "-v", "error", "-y", "-i", str(src), "-vf", f"scale=-2:{int(height)},fps=30",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-c:a", "copy", "-movflags", "+faststart",
                    "-f", "mp4", str(tmp)], check=True, timeout=COPY_TIMEOUT_S,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    tmp.replace(dst)
    return dst


class SlackClient:
    """A thin wrapper over slack_sdk's WebClient: one attempt per call, errors mapped to base.py's two kinds with
    advice a person can follow. Never prints or logs the token."""

    def __init__(self, token: str, timeout: float = 30.0, *, share_wait_s: float = SHARE_WAIT_S):
        self.web = WebClient(token=token, timeout=timeout, retry_handlers=[])
        self.timeout = timeout
        self.share_wait_s = share_wait_s
        self.sleep = time.sleep
        self.channel_names: dict[str, str] = {}    # id -> name, for messages like "invite the app to #errata"
        self._ims: dict[str, str] = {}             # user id -> DM channel id
        self._skew = 0.0                           # Slack's clock minus this computer's, from the Date header

    # plumbing ------------------------------------------------------------------------------------------------------
    def _call(self, method: str, what: str, where: str | None = None, /, **kw):
        """One Web API call (slack_sdk method name, then what it does in words, then the channel it concerns for
        the error message; positional so they never clash with Slack's own `channel` argument)."""
        try:
            return getattr(self.web, method)(**kw)
        except Exception as e:
            raise self._error(e, what, where) from None

    def _where(self, channel: str | None) -> str:
        if channel and channel in self.channel_names:
            return f"#{self.channel_names[channel]}"
        if channel and channel[:1] == "D":
            return "the direct message"
        return "the Slack channel"

    def _error(self, e: Exception, what: str, channel: str | None = None) -> Exception:
        """TransientError or PermanentError for a failed call; anything unexpected (a bug) is returned unchanged."""
        if isinstance(e, (TransientError, PermanentError)):
            return e
        if isinstance(e, SlackApiError):
            r = e.response
            code = _code(e)
            wait = _retry_after(r.headers)
            if r.status_code == 429 or code in ("ratelimited", "rate_limited"):
                err = TransientError(f"Slack asked StreamSplitter to slow down ({what}); trying again later",
                                     retry_after=wait)
                err.code = "ratelimited"           # the Slack listener then holds back all its reads (listener.py)
                return err
            if r.status_code >= 500 or code in _TRANSIENT or code.startswith(_NOT_JSON):
                why = "a page that is not Slack (a Wi-Fi login?)" if code.startswith(_NOT_JSON) else \
                    (code or f"HTTP {r.status_code}")
                return TransientError(f"Slack did not answer properly ({what}: {why}); trying again later",
                                      retry_after=wait)
            data = r.data if isinstance(r.data, dict) else {}
            fix = _ADVICE.get(code)
            if fix is None:
                return _permanent(f"Slack refused {what} ({code or r.status_code}); trying again will not help. "
                                  f"Tell whoever looks after StreamSplitter.", code)
            return _permanent(fix.format(code=code, where=self._where(channel),
                                         needed=data.get("needed") or "the missing permission"), code)
        if isinstance(e, (OSError, http.client.HTTPException)):     # URLError, timeouts, resets, TLS, cut-off replies
            return TransientError(f"no connection to Slack ({what}: {type(e).__name__}); trying again later")
        if isinstance(e, SlackClientError):
            return _permanent(f"StreamSplitter built a bad Slack request ({what}): {e}")
        return e

    def _conversation(self, channel_id: str) -> str:
        """A user id becomes the DM channel with that user, so a scheduled message is deleted from where it went."""
        return self.open_dm(channel_id) if _USER_ID.match(channel_id or "") else channel_id

    def _learn_clock(self, headers) -> None:
        server = _server_time(headers)
        if server is not None:          # the Date header has 1 s steps and the reply took a moment: ignore <= 10 s
            skew = server - time.time()
            self._skew = skew if abs(skew) > 10.0 else 0.0

    # account, channels, people -------------------------------------------------------------------------------------
    def auth_test(self) -> dict:
        """Who the token is, plus the scopes Slack says it has and the ones the manifest wants but it lacks."""
        r = self._call("auth_test", "checking the token")
        scopes = _header(r.headers, "x-oauth-scopes")
        out = {"team": r.get("team"), "team_id": r.get("team_id"), "bot_user_id": r.get("user_id"),
               "url": r.get("url"), "bot_name": r.get("user")}
        if scopes is not None:
            have = {s.strip() for s in scopes.split(",") if s.strip()}
            out["scopes"] = sorted(have)
            out["missing_scopes"] = [s for s in REQUIRED_SCOPES if s not in have]
        return out

    def _channel(self, c: dict) -> dict:
        self.channel_names[c["id"]] = c.get("name") or c["id"]
        return {"id": c["id"], "name": c.get("name"), "is_private": bool(c.get("is_private")),
                "is_member": bool(c.get("is_member"))}

    def find_channel(self, name: str) -> dict | None:
        """A channel by name ("#errata" or "errata") or id. Private channels are only visible once the app is in
        them (/invite); archived ones are skipped."""
        raw = (name or "").strip()
        if _CHANNEL_ID.match(raw):
            try:
                return self._channel(self.web.conversations_info(channel=raw)["channel"])
            except SlackApiError as e:
                if _code(e) == "channel_not_found":
                    return None
                raise self._error(e, "looking up the channel", raw) from None
            except Exception as e:
                raise self._error(e, "looking up the channel", raw) from None
        want = raw.lstrip("#").strip().lower()
        if not want:
            return None
        cursor = None
        while True:
            r = self._call("conversations_list", "listing channels", types="public_channel,private_channel",
                           exclude_archived=True, limit=999, cursor=cursor)
            for c in r.get("channels") or []:
                if (c.get("name") or "").lower() == want:
                    return self._channel(c)
            cursor = (r.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return None

    def join_channel(self, channel_id: str) -> None:
        """Public channels only; a private one answers with what to type (/invite)."""
        self._call("conversations_join", "joining the channel", channel_id, channel=channel_id)

    @staticmethod
    def _user(u: dict) -> dict:
        p = u.get("profile") or {}
        return {"id": u["id"], "name": u.get("name"), "real_name": p.get("real_name") or u.get("real_name"),
                "display_name": p.get("display_name") or None, "email": p.get("email")}

    def find_users(self, query: str) -> list[dict]:
        """People by email (exact), id, or name (contains, any case: handle, real name or display name); exact name
        matches first. Bots, Slackbot and deactivated people are left out."""
        q = (query or "").strip()
        if not q:
            return []
        if "@" in q.lstrip("@"):
            try:
                u = self.web.users_lookupByEmail(email=q)["user"]
            except SlackApiError as e:
                if _code(e) == "users_not_found":
                    return []
                raise self._error(e, "looking up the email address") from None
            except Exception as e:
                raise self._error(e, "looking up the email address") from None
            return [] if u.get("deleted") else [self._user(u)]
        if _USER_ID.match(q):
            try:
                return [self._user(self.web.users_info(user=q)["user"])]
            except SlackApiError as e:
                if _code(e) == "user_not_found":
                    return []
                raise self._error(e, "looking up the person") from None
            except Exception as e:
                raise self._error(e, "looking up the person") from None
        needle = q.lstrip("@").casefold()
        found, cursor = [], None
        while True:
            r = self._call("users_list", "listing people", limit=200, cursor=cursor)
            for u in r.get("members") or []:
                if u.get("deleted") or u.get("is_bot") or u.get("id") == "USLACKBOT":
                    continue
                d = self._user(u)
                if any(needle in (d[k] or "").casefold() for k in ("name", "real_name", "display_name")):
                    found.append(d)
            cursor = (r.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        def rank(d: dict) -> tuple:
            exact = needle in {(d[k] or "").casefold() for k in ("name", "real_name", "display_name")}
            return not exact, (d["real_name"] or d["name"] or "").casefold()
        return sorted(found, key=rank)

    # messages --------------------------------------------------------------------------------------------------------
    def post_message(self, channel_id: str, text: str, thread_ts: str | None = None) -> dict:
        """`text` is Slack mrkdwn (escape &, < and > in values that are not links). No link previews: Slack unfurls
        YouTube links in a bot's messages by default, so help's example link (youtube.com/@ChannelName/live, a real
        stranger's channel) came with that channel's card under it (user, 2026-11-07)."""
        ch = self._conversation(channel_id)
        r = self._call("chat_postMessage", "posting a message", ch, channel=ch, text=text, thread_ts=thread_ts,
                       unfurl_links=False, unfurl_media=False)
        return {"ts": r.get("ts"), "channel": r.get("channel") or ch}

    def open_dm(self, user_id: str) -> str:
        """The DM channel id with a person (conversations.open; the same channel every time, so it is cached)."""
        if user_id not in self._ims:
            r = self._call("conversations_open", "opening a direct message", users=user_id)
            self._ims[user_id] = r["channel"]["id"]
        return self._ims[user_id]

    def dm(self, user_id: str, text: str) -> dict:
        return self.post_message(self.open_dm(user_id), text)

    def schedule_message(self, channel_id: str, text: str, post_at: int) -> str:
        """Schedule `text` for the unix time `post_at` (this computer's clock) and return the scheduled_message_id.
        post_at is kept MIN_LEAD_S..120 days ahead; a user id is turned into the DM channel id first."""
        ch = self._conversation(channel_id)
        for attempt in range(2):
            now = time.time() + self._skew
            at = int(min(max(float(post_at) + self._skew, now + MIN_LEAD_S), now + MAX_AHEAD_S))
            try:
                r = self.web.chat_scheduleMessage(channel=ch, text=text, post_at=at)
            except SlackApiError as e:
                if _code(e) != "time_in_past":
                    raise self._error(e, "scheduling the alarm message", ch) from None
                server = _server_time(e.response.headers)
                if attempt == 0 and server is not None:
                    self._skew = server - time.time()     # this computer's clock is behind Slack's: retry once
                    continue
                raise _permanent("Slack says the alarm time is in the past: this computer's clock is wrong. Set it "
                                 "right (Windows: Settings -> Time & language -> Date & time -> Sync now).",
                                 "time_in_past") from None
            except Exception as e:
                raise self._error(e, "scheduling the alarm message", ch) from None
            self._learn_clock(r.headers)
            return r["scheduled_message_id"]

    def delete_scheduled(self, channel_id: str, scheduled_message_id: str) -> bool:
        """Cancel a scheduled message. False when Slack says it is gone: already posted, already deleted, or due
        within 60 s (Slack refuses to delete those, so it will still post)."""
        ch = self._conversation(channel_id)
        try:
            self.web.chat_deleteScheduledMessage(channel=ch, scheduled_message_id=scheduled_message_id)
            return True
        except SlackApiError as e:
            if _code(e) == "invalid_scheduled_message_id":
                return False
            raise self._error(e, "cancelling the alarm message", ch) from None
        except Exception as e:
            raise self._error(e, "cancelling the alarm message", ch) from None

    def list_scheduled(self, channel_id: str | None = None) -> list[dict]:
        """This app's scheduled messages not yet posted ({"id", "channel_id", "post_at", "text"}): after a crash,
        the alarm of the run before can be found and cancelled."""
        ch = self._conversation(channel_id) if channel_id else None
        out, cursor = [], None
        while True:
            r = self._call("chat_scheduledMessages_list", "listing scheduled messages", ch, channel=ch, limit=100,
                           cursor=cursor)
            out += [{k: m.get(k) for k in ("id", "channel_id", "post_at", "text")}
                    for m in r.get("scheduled_messages") or []]
            cursor = (r.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return out

    def add_reaction(self, channel_id: str, ts: str, name: str) -> None:
        """Put the emoji `name` (e.g. "eyes") on the message `ts` (reactions.add). Needs the optional reactions:write
        (OPTIONAL_SCOPES): without it Slack answers missing_scope, a PermanentError whose .code says so;
        already_reacted is one too."""
        ch = self._conversation(channel_id)
        self._call("reactions_add", "reacting to a message", ch, channel=ch, timestamp=ts, name=name)

    def update_message(self, channel_id: str, ts: str, text: str) -> None:
        """Replace the text of a message this app posted (chat.update; no new notification): a stopped-running alarm
        Slack posted although it had been cancelled becomes a false-alarm note (notify.py)."""
        ch = self._conversation(channel_id)
        self._call("chat_update", "editing a message", ch, channel=ch, ts=ts, text=text)

    # reading the contact's replies (publish/commands.py) -----------------------------------------------------------
    _MSG_KEYS = ("ts", "user", "bot_id", "app_id", "subtype", "text", "thread_ts", "reply_count", "latest_reply")

    def history(self, channel_id: str, limit: int = 15, oldest: str | None = None) -> list[dict]:
        """The latest `limit` messages of a conversation (newest first; thread replies are not among them, only their
        parent with reply_count / latest_reply). A user id is turned into the DM with that person. Needs im:history
        for a DM; without it Slack answers missing_scope (a PermanentError whose .code says so)."""
        ch = self._conversation(channel_id)
        kw = {"channel": ch, "limit": int(limit)}
        if oldest:
            kw["oldest"] = oldest
        r = self._call("conversations_history", "reading the direct message", ch, **kw)
        return [{k: m.get(k) for k in self._MSG_KEYS} for m in r.get("messages") or []]

    def replies(self, channel_id: str, ts: str, oldest: str | None = None, limit: int = 15) -> list[dict]:
        """The thread of the message `ts` (the parent first, then its replies, oldest first)."""
        ch = self._conversation(channel_id)
        kw = {"channel": ch, "ts": ts, "limit": int(limit)}
        if oldest:
            kw["oldest"] = oldest
        r = self._call("conversations_replies", "reading a thread of the direct message", ch, **kw)
        return [{k: m.get(k) for k in self._MSG_KEYS} for m in r.get("messages") or []]

    # files -----------------------------------------------------------------------------------------------------------
    def permalink(self, channel_id: str, ts: str) -> str | None:
        try:
            return self.web.chat_getPermalink(channel=channel_id, message_ts=ts).get("permalink")
        except Exception:        # only a nicety for messages to people; the post itself went through
            return None

    def find_share(self, file_id: str, channel_id: str, wait_s: float = 0.0) -> dict | None:
        """The message that shows `file_id` in the channel ({"file_id", "ts", "thread_ts", "permalink"}), or None.
        Slack fills a file's shares a moment after files.completeUploadExternal, so this polls files.info for up to
        `wait_s` seconds (but not for a file Slack does not know)."""
        waited, delay = 0.0, 1.0
        while True:
            try:
                f = self.web.files_info(file=file_id).get("file") or {}
            except SlackApiError as e:
                if _code(e) in ("file_not_found", "file_deleted"):
                    return None
                raise self._error(e, "looking up the posted video", channel_id) from None
            except Exception as e:
                raise self._error(e, "looking up the posted video", channel_id) from None
            shares = f.get("shares") or {}
            for kind in ("public", "private"):
                for s in (shares.get(kind) or {}).get(channel_id) or []:
                    if s.get("ts"):
                        return {"file_id": file_id, "ts": s["ts"], "thread_ts": s.get("thread_ts"),
                                "permalink": self.permalink(channel_id, s["ts"]) or f.get("permalink")}
            if waited + delay > wait_s:
                return None
            self.sleep(delay)
            waited += delay
            delay = min(delay * 1.5, 5.0)

    def _post_bytes(self, url: str, data: bytes, name: str) -> None:
        req = Request(url, data=data, method="POST")      # raw bytes, as slack_sdk's files_upload_v2 sends them
        try:
            with urlopen(req, timeout=max(self.timeout, UPLOAD_TIMEOUT_S)) as resp:
                resp.read()
                status = resp.status
        except HTTPError as e:
            if e.code == 413:
                raise _permanent(f"Slack refused {name} as too large. " + _FIX_SIZE, "too_large") from None
            raise TransientError(f"Slack's file server refused the upload of {name} (HTTP {e.code}); trying again "
                                 f"later", retry_after=_retry_after(dict(e.headers.items()) if e.headers else None)) \
                from None
        except (OSError, http.client.HTTPException) as e:
            raise TransientError(f"the upload of {name} broke off ({type(e).__name__}); trying again later") from None
        if status != 200:
            raise TransientError(f"Slack's file server answered HTTP {status} to the upload of {name}; trying again "
                                 f"later")

    def upload_file(self, channel_id: str, path: str, title: str, comment: str, thread_ts: str | None = None, *,
                    state: dict | None = None, on_state=None) -> dict:
        """Post the video at `path` with `comment` to the channel, or as a reply in the thread `thread_ts`.
        Returns {"file_id", "permalink", "ts"}; ts (the message's, for threading) is None when the share did not show
        within share_wait_s (find_share can get it later).

        `state` (the job's) keeps the file id of an upload whose completion may have gone through although its reply
        was lost; a retry looks for that file's message first instead of posting the video twice. `on_state()` is
        called once the file id is in `state`, before the video is shared, so the caller can save it: a crash during
        the share or the wait for its message then finds the post instead of posting again."""
        state = state if state is not None else {}
        prev = state.get("slack_file_id")
        if prev:
            found = self.find_share(prev, channel_id, wait_s=min(RETRY_SHARE_WAIT_S, self.share_wait_s))
            if found:
                state.pop("slack_file_id", None)
                return found
            state.pop("slack_file_id", None)      # never shared: upload again (a duplicate beats a lost clip)
        p = Path(path)
        try:
            # the whole file in memory (~90 MB) so it is open for well under a second: the pipeline renames a clip
            # when the results screen corrects its name, and Windows refuses to rename a file that is open
            data = p.read_bytes()
        except FileNotFoundError:
            raise _permanent(f"the video {p.name} is gone (deleted or renamed), so it cannot be posted",
                             "file_missing") from None
        except OSError as e:
            raise TransientError(f"could not read {p.name} ({e.strerror or type(e).__name__}); trying again later") \
                from None
        if not data:
            raise _permanent(f"the video {p.name} is empty, so it cannot be posted", "file_empty")
        r = self._call("files_getUploadURLExternal", "starting the upload", channel_id, filename=p.name,
                       length=len(data))
        fid = r["file_id"]
        self._post_bytes(r["upload_url"], data, p.name)
        state["slack_file_id"] = fid               # from here on a retry first looks for this file's message
        if on_state is not None:
            try:
                on_state()                         # on disk before the share (and the up-to-30 s wait for it)
            except Exception as e:  # noqa: BLE001 - saving is a safeguard; the post goes on without it
                log.warning("slack: could not save the upload state (%s)", type(e).__name__)
        try:
            self._call("files_completeUploadExternal", "sharing the video", channel_id,
                       files=[{"id": fid, "title": title}], channel_id=channel_id, initial_comment=comment,
                       thread_ts=thread_ts)
        except PermanentError:
            state.pop("slack_file_id", None)
            raise
        found = self.find_share(fid, channel_id, wait_s=self.share_wait_s)
        state.pop("slack_file_id", None)
        return found or {"file_id": fid, "ts": None, "thread_ts": thread_ts, "permalink": None}


class SlackDestination:
    """Destination "slack": a clip as a new message in the channel, its score version as a reply in the clip's thread
    (job.state["thread_ts"], set by the publisher from the clip's result; or job.state["thread_file_id"], the clip's
    file id, when the clip's ts was not known yet), a setup test as the bundled sample clip."""

    name = "slack"

    def __init__(self, client: SlackClient, channel_id: str, *, clip_height: int = 0, channel_name: str | None = None,
                 teams=(), on_state=None):
        """`on_state(job)`: saves the job's state mid-send (live.build_publishing wires the outbox's save_state)."""
        self.client = client
        self.channel_id = channel_id
        self.clip_height = int(clip_height or 0)
        self.teams = {str(t) for t in teams}       # shown in bold in the message
        self.on_state = on_state
        if channel_name:
            client.channel_names[channel_id] = channel_name.lstrip("#")

    def _teams(self, teams: list[str]) -> str:
        return " ".join(f"*{_esc(t)}*" if str(t) in self.teams else _esc(t) for t in teams) or "?"

    def comment(self, m: Match, kind: str) -> str:
        title = _esc(match_title(m))
        if kind == "test":
            return TEST_FORMAT
        if kind == "score":
            known = m.red_score is not None and m.blue_score is not None
            return SCORE_FORMAT.format(title=title, score=f": Red {m.red_score} - Blue {m.blue_score}" if known else "")
        event = f" - {_esc(m.event_name)}" if m.event_name else ""
        if not m.red and not m.blue:
            return CLIP_FORMAT_NO_TEAMS.format(title=title, event=event)
        return CLIP_FORMAT.format(title=title, event=event, red=self._teams(m.red), blue=self._teams(m.blue))

    def _sized(self, src: Path, cache_dir: Path | None = None) -> tuple[Path, str | None]:
        """The file to send: the smaller copy when clip_height is set; the clip as cut when the copy fails, so a
        broken ffmpeg never keeps a clip from drive team."""
        if not self.clip_height or not src.is_file():
            return src, None
        try:
            return small_copy(src, self.clip_height, cache_dir), None
        except (OSError, subprocess.SubprocessError) as e:        # no ffmpeg, a failed or hung encode, a full disk
            log.warning("slack: %sp copy of %s failed (%s); sending the clip as cut", self.clip_height, src.name,
                        type(e).__name__)
            return src, f"the {self.clip_height}p copy failed ({type(e).__name__}); sent the clip as cut"

    def _thread(self, job: Job) -> str | None:
        ts = job.state.get("thread_ts")
        if ts or not job.state.get("thread_file_id"):
            return ts
        found = self.client.find_share(job.state["thread_file_id"], self.channel_id)
        return found["ts"] if found else None

    def send(self, job: Job) -> dict:
        if job.kind not in ("clip", "score", "test"):
            raise _permanent(f"StreamSplitter does not know how to post a {job.kind!r} job to Slack", "bad_kind")
        m = job.match
        cache = None
        if job.kind == "test":
            path = Path(job.path) if job.path and Path(job.path).is_file() else SAMPLE_CLIP
            title = "StreamSplitter setup test"
            cache = Path(tempfile.gettempdir()) / "streamsplitter" / f"slack_{self.clip_height}p"  # not the package
        else:
            path = Path(job.path)
            title = match_title(m) + (" (results)" if job.kind == "score" else "")
        thread_ts = self._thread(job) if job.kind == "score" else None
        src, note = self._sized(path, cache)
        comment = self.comment(m, job.kind)
        save = (lambda: self.on_state(job)) if self.on_state is not None else None
        try:
            res = self.client.upload_file(self.channel_id, str(src), title, comment, thread_ts, state=job.state,
                                          on_state=save)
        except PermanentError as e:
            if not thread_ts or getattr(e, "code", "") not in _THREAD_GONE:
                raise
            thread_ts = None                       # the clip's message was deleted: post the score on its own
            res = self.client.upload_file(self.channel_id, str(src), title, comment, None, state=job.state,
                                          on_state=save)
        out = {"ts": res.get("ts"), "permalink": res.get("permalink"), "file_id": res.get("file_id"),
               "channel": self.channel_id}
        if thread_ts:
            out["thread_ts"] = thread_ts
        if note:
            out["note"] = note
        return out
