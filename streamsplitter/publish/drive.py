"""Google Drive: every match uploaded into a folder the team shares (user, 2026-11-05: the wiki is no place to keep
videos; a shared Drive folder is free, members-only, and plays in the browser and the Drive phone app).

Plain HTTPS to the Drive API v3 (requests) with google-auth's OAuth credentials, as youtube.py does. The OAuth client
is the team's Google Cloud "Desktop app" client already stored for YouTube (secret youtube_client: one Cloud project,
with the Google Drive API enabled too). The Drive login is its own token (secret drive_token), so the account whose
storage holds the videos can differ from the YouTube channel's.

Scope: drive.file only. It is non-sensitive and lets StreamSplitter see and change only the files and folders it made
itself; nothing else in the account's Drive. So StreamSplitter makes its own top folder ("FRC match videos", named in
the setup) and never uploads into a folder a person made, and an account that did not make the folder cannot see it
(the setup then offers a new one). Inside the top folder there is one folder per event, "<year> <event name>" like the
YouTube playlist, looked for by its appProperties (then its name) before one is made, so a restart or a second laptop
logged in to the same account reuses it. Files go to the logged-in account's My Drive (no shared drives).

Files: "Q07 Qualification 7 - 5026 254 1678 vs 971 8 649.mp4" (name_format, render_name): a code that sorts in play
order within each stage, the match title as YouTube names it, then red vs blue. modifiedTime is set to when the match
was played (the match clip's cut time, a few seconds after its end), so Drive's "Last modified" sort is play order too.
The description is the YouTube description format; a video that went up before TBA posted the score gets it later by
files.update (a "describe" job, as for YouTube). Each file carries appProperties ss_match (the clip's name) and ss_job:
before a new upload the event folder is searched for ss_match, so a retry whose answer was lost, or a second laptop on
the same account, keeps the file that is there instead of making a duplicate.

Size: [drive] height 720 (default; 540; 0 = as cut). The smaller copy is made inside the job (the outbox worker, never
the follower) by ffmpeg at below-normal priority, cached next to the clip in drive_<h>p/ (written as .part, then
renamed), reused by a retry and deleted once uploaded. Measured: a 130 MB 1080p score clip becomes 39.7 MB at 720p in
8.5 s, 27 MB at 540p. If ffmpeg fails, the clip goes up as cut (the contact is told once).

Upload: Drive's resumable protocol. POST /upload/drive/v3/files?uploadType=resumable with the metadata as JSON returns
the session URI (Location); PUTs carry 8 MiB pieces (multiples of 256 KiB) with Content-Range; 308 + Range says what
Drive has; an empty PUT "bytes */<size>" asks after a drop or a restart; 404 (or 410) means the session is gone (it
lasts a week) and the upload starts over, as does a piece refused with another 400/403 (Drive's guide; the third
refusal is reported). job.state keeps the session URI and offset; on_state saves them.

Errors: no network, timeouts, 429, 5xx, rateLimitExceeded and userRateLimitExceeded are TransientError (the outbox
backs off). storageQuotaExceeded (the account's Drive is full) is DriveFull, also transient: the destination calls
on_full once (live.py pauses the Drive queue and DMs "free space ..., then reply resume drive"). A refused or revoked
login, the API not enabled (accessNotConfigured, SERVICE_DISABLED) and the top folder gone (deleted, or this laptop
logged in to another account) are PermanentError saying what to do. Free space comes from about.get (storageQuota);
under about one event at the chosen height the contact gets a DM, and `status` shows it.

Checked in developers.google.com/drive/api on 2026-11-05: the resumable protocol above (session URI in Location,
pieces in multiples of 256 KiB, 308 + Range, "bytes */size", 404 = session expired so restart, sessions expire after a
week); drive.file is listed as non-sensitive, and about.get and files.create accept it (about.get requires `fields`);
appProperties are private to the requesting app, at most 30 per app per file, 124 bytes per key + value, searched with
"appProperties has { key='k' and value='v' }"; quotes in a query are escaped as \\'; the reasons storageQuotaExceeded,
userRateLimitExceeded, rateLimitExceeded (403 and 429), notFound and authError; trashing is files.update
{"trashed": true}, by the owner only, and the trash is emptied after 30 days; webViewLink is the browser link; 750 GB
of uploads per user a day. Not confirmed there: that files.create accepts modifiedTime (the reference does not mark it
output-only and other Drive tools set it on create; if Drive ignored it, files would sort by upload time instead);
that a session can also answer 410 (handled like 404); the reason Google sends for a disabled API (accessNotConfigured
/ SERVICE_DISABLED, as for YouTube); whether drive.file access is per OAuth client or per Cloud project (every laptop
uses the same client, so it does not matter here); whether user.emailAddress comes back under drive.file (the
display name is shown when it does not).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests

from .. import ffmpeg
from . import templates
from .base import Job, Match, PermanentError, TransientError
from .youtube import check_client_config, read_client_config  # noqa: F401 - the same Desktop app client

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
API = "https://www.googleapis.com/drive/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
TOKEN_URI = "https://oauth2.googleapis.com/token"
FOLDER_MIME = "application/vnd.google-apps.folder"
CHUNK_SIZE = 8 * 1024 * 1024        # multiples of 256 KiB (except the last)
API_TIMEOUT = (15, 60)              # (connect, read) seconds
CHUNK_TIMEOUT = (15, 300)           # an 8 MiB piece on a slow venue uplink
COPY_TIMEOUT_S = 600.0              # the 720p copy takes ~9 s at below-normal priority; a hung ffmpeg must not block

TOP_TITLE = "FRC match videos"
DEFAULT_HEIGHT = 720
HEIGHTS = (720, 540, 0)             # 0 = upload the clip as cut (1080p)
MB_PER_MATCH = {720: 40, 540: 27, 0: 130}      # a score clip, measured (module docstring)
EVENT_MATCHES = 100                 # "about one event" for the low-space warning
FREE_GB = 15                        # a free Google account's storage, shared with Gmail and Photos
STORAGE_EVERY_S = 600.0             # about.get at most this often while uploading
NAME_FORMAT = "{code} {match_title}[ - {teams}]"
NAME_FIELDS = {"code": "the play-order code: Q07, P03, PF1, PR02 (practice)",
               "teams": "red vs blue: 5026 254 1678 vs 971 8 649", **templates.FIELDS}
TEST_PREFIX = "StreamSplitter setup test"
TEST_DESCRIPTION = "A test upload from the StreamSplitter setup. It is safe to delete."
SETUP_AGAIN = "streamsplitter setup --section drive"
LOGIN_AGAIN = f"log in to Google Drive again: {SETUP_AGAIN}"
API_PAGE = "https://console.cloud.google.com/apis/library/drive.googleapis.com"
DRIVE_DOC = "docs/setup-drive.md"
# appProperties (private to StreamSplitter): what a folder or file is, so it can be found again
PROP_KIND, PROP_EVENT, PROP_MATCH, PROP_JOB = "ss", "ss_event", "ss_match", "ss_job"
FILE_FIELDS = "id,name,mimeType,parents,trashed,webViewLink,appProperties,description,size,modifiedTime"

_BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')      # not allowed in a Windows file name (for downloads)
_OPTIONAL = re.compile(r"\[([^\[\]]*)\]")


# ---------------------------------------------------------------------------------------------------------------------
# Names

def match_code(m: Match, quals: int | None = None) -> str:
    """The code that sorts a stage in play order: Q07 (qualification 7, padded to the digits of the event's last
    qualification, at least 2), P03 (double-elimination playoff match 3), PF1-PF3 (its finals), PR02 (practice 2);
    before 2023 QF2-1, SF1-2, EF3-1 (set-match) and F1-F3. "" for a clip with no known level."""
    level, set_n, num, _replay = templates._parts(m)
    year = templates._year(m)
    double = year is None or year >= 2023
    if level == "qm" and num is not None:
        width = max(2, len(str(quals or 0)))
        return f"Q{num:0{width}d}"
    if level == "p" and num is not None:
        return f"PR{num:02d}"
    if (level == "playoff" or (level == "sf" and double)) and set_n is not None:
        return f"P{set_n:02d}"
    if level == "f":
        return ("PF" if double else "F") + ("" if num is None else str(num))
    if level in ("ef", "qf", "sf") and set_n is not None:
        return f"{level.upper()}{set_n}" + (f"-{num}" if num is not None else "")
    return ""


def teams_vs(m: Match) -> str:
    """"5026 254 1678 vs 971 8 649" (red vs blue), "" when neither alliance is known."""
    red = [str(t).removeprefix("frc") for t in m.red or [] if t]
    blue = [str(t).removeprefix("frc") for t in m.blue or [] if t]
    if not red and not blue:
        return ""
    return f"{' '.join(red) or '?'} vs {' '.join(blue) or '?'}"


def clean_name(text: str, fallback: str = "match") -> str:
    """A file name that also works when downloaded to Windows: no \\ / : * ? " < > |, one line, at most 180
    characters."""
    t = " ".join(_BAD_NAME.sub("_", text or "").split()).strip(" .")
    return t[:180].rstrip(" .") or fallback


def name_fields(fmt: str) -> list[str]:
    """The fields a name format uses. Raises templates.TemplateError for an unknown field or broken braces."""
    import string
    try:
        parsed = list(string.Formatter().parse(fmt))
    except ValueError as e:
        raise templates.TemplateError(f"{e}: write a value as {{field}}") from None
    out = []
    for _literal, name, spec, conv in parsed:
        if name is None:
            continue
        if name not in NAME_FIELDS:
            raise templates.TemplateError(f"unknown field {{{name}}}; the fields are "
                                          + ", ".join(f"{{{f}}}" for f in NAME_FIELDS))
        if spec or conv:
            raise templates.TemplateError(f"write {{{name}}} plainly, without ! or : after the name")
        out.append(name)
    return out


def render_name(fmt: str | None, m: Match, alliances: int = 8, quals: int | None = None) -> str:
    """The file name (without .mp4) for `m`. A [bracketed part] is left out while one of its fields is blank (no
    teams before the schedule is known); blanks elsewhere are tidied away. An unnamed clip keeps its clip name."""
    fmt = (fmt or NAME_FORMAT).replace("\r", " ").replace("\n", " ")
    name_fields(fmt)
    vals = {**templates.values(m, alliances), "code": match_code(m, quals), "teams": teams_vs(m)}

    def optional(hit: re.Match) -> str:
        used = name_fields(hit.group(1))
        if not used:
            return hit.group(0)
        return hit.group(1) if all(vals[f] for f in used) else ""
    line = _OPTIONAL.sub(optional, fmt).format_map(vals)
    line = re.sub(r"\s{2,}", " ", line)
    line = re.sub(r"(\s[-|:,])+\s*$", "", line)
    line = re.sub(r"^\s*([-|:,]\s)+", "", line)
    return clean_name(line, m.name or "match")


def name_format_problem(fmt: str) -> str | None:
    """What makes a name format unusable, or None."""
    if not (fmt or "").strip():
        return "the name format is empty"
    if "\n" in fmt.strip():
        return "a file name must be one line"
    try:
        name_fields(fmt)
        render_name(fmt, templates.SAMPLE)
    except (templates.TemplateError, ValueError) as e:
        return str(e)
    return None


def clean_description(text: str) -> str:
    b = (text or "").encode("utf-8")
    return text if len(b) <= 5000 else b[:5000].decode("utf-8", "ignore")


def event_title(event_key: str | None, event_name: str | None, year) -> str:
    """The event's folder, named like its YouTube playlist: "2026 MadTown Throwdown" (the key without a name)."""
    if not event_name:
        return event_key or "Matches"
    return clean_name(templates.render_playlist(templates.PLAYLIST_FORMAT, event_key, event_name, year), event_key or
                      "Matches")


def gb(n: int | None) -> str:
    return "?" if n is None else f"{n / 1e9:.1f}"


def matches_left(free: int | None, height: int) -> int | None:
    if free is None:
        return None
    return int(free // (MB_PER_MATCH.get(height, MB_PER_MATCH[0]) * 1e6))


def file_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


def folder_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


def _q(value: str) -> str:
    """A value for a Drive query, inside single quotes."""
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def _prop(key: str, value: str) -> str:
    """`value` cut to fit an appProperty (124 bytes for key and value together)."""
    b = str(value).encode("utf-8")[:124 - len(key.encode("utf-8"))]
    return b.decode("utf-8", "ignore")


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _rfc3339(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(t % 1 * 1000):03d}Z"


# ---------------------------------------------------------------------------------------------------------------------
# Login

class LoginError(PermanentError):
    """The browser login did not give a usable Google Drive sign-in; the message says why."""


def login(client_config: dict, *, open_browser: bool = True, timeout_s: float = 600) -> str:
    """Log in to Google Drive in the browser (installed-app OAuth, loopback, PKCE) and return the token JSON for
    config.set_secret("drive_token", ...). The account chosen in Google's account chooser holds the videos."""
    from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError
    from oauthlib.oauth2.rfc6749.errors import OAuth2Error

    try:
        cfg = check_client_config(client_config)
    except PermanentError as e:
        raise LoginError(str(e)) from None
    flow = InstalledAppFlow.from_client_config(cfg, SCOPES)
    try:
        creds = flow.run_local_server(
            host="127.0.0.1", port=0, open_browser=open_browser, timeout_seconds=timeout_s,
            authorization_prompt_message="If no browser window opened, open this link to log in to Google Drive:\n{url}",
            success_message="StreamSplitter is logged in to Google Drive. You can close this tab.",
            prompt="consent select_account", access_type="offline")
    except WSGITimeoutError:
        raise LoginError("the Google Drive login was not finished in time; run it again") from None
    except OAuth2Error as e:
        raise LoginError(f"the Google Drive login was cancelled or refused ({e.error})") from None
    except Warning:             # oauthlib: "Scope has changed" when the Drive permission was not granted
        raise LoginError("Google did not grant the Google Drive permission: log in again and allow it") from None
    if not creds.refresh_token:
        raise LoginError("Google sent no refresh token; remove StreamSplitter at myaccount.google.com/permissions "
                         "and log in again")
    return creds.to_json()


# ---------------------------------------------------------------------------------------------------------------------
# Errors

class DriveFull(TransientError):
    """The account's Google Drive is full (storageQuotaExceeded, or less free than the next video)."""

    def __init__(self, message: str, retry_after: float | None = 3600.0):
        super().__init__(message, retry_after=retry_after)


class NotFound(PermanentError):
    """Drive does not know a file or folder (deleted, or another account's)."""


_TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}
_RATE = {"rateLimitExceeded", "userRateLimitExceeded", "backendError", "internalError", "RATE_LIMIT_EXCEEDED",
         "sharingRateLimitExceeded"}
_DAILY = {"dailyLimitExceeded", "quotaExceeded"}
_FULL = {"storageQuotaExceeded"}
_NOT_ENABLED = (f"the Google Drive API is not enabled in the team's Google Cloud project: open {API_PAGE}, pick the "
                "project StreamSplitter uses at the top, click Enable, wait a minute and start StreamSplitter again")
_FIXES = {
    "accessNotConfigured": _NOT_ENABLED,
    "SERVICE_DISABLED": _NOT_ENABLED,
    "insufficientPermissions": f"the login does not allow Google Drive; {LOGIN_AGAIN} and allow it",
    "ACCESS_TOKEN_SCOPE_INSUFFICIENT": f"the login does not allow Google Drive; {LOGIN_AGAIN} and allow it",
    "authError": f"Google Drive refused the login; {LOGIN_AGAIN}",
    "domainPolicy": "the account's school or company admin does not allow Drive apps: log in with another Google "
                    f"account ({SETUP_AGAIN})",
    "appNotAuthorizedToFile": "StreamSplitter may only use folders it made with this Google account: make a new "
                              f"folder with {SETUP_AGAIN}",
    "insufficientFilePermissions": "StreamSplitter may only use folders it made with this Google account: make a new "
                                   f"folder with {SETUP_AGAIN}",
}


def _reason(resp) -> tuple[str, str]:
    """(reason, message) from a Google error body, ("", "") when there is none."""
    try:
        body = resp.json()
    except Exception:
        return "", ""
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        reasons = [e.get("reason") for e in err.get("errors") or [] if isinstance(e, dict) and e.get("reason")]
        reasons += [d.get("reason") for d in err.get("details") or [] if isinstance(d, dict) and d.get("reason")]
        return (reasons[0] if reasons else ""), str(err.get("message") or "")[:300]
    if isinstance(err, str):            # OAuth endpoint style: {"error": "invalid_grant", ...}
        return err, str(body.get("error_description") or "")[:300]
    return "", ""


def _retry_after(resp) -> float | None:
    try:
        return max(0.0, float(resp.headers.get("Retry-After")))
    except (TypeError, ValueError):
        return None


def _range_end(resp) -> int:
    """The last byte Drive has, from a 308's "Range: bytes=0-N" (-1 when it has none)."""
    r = resp.headers.get("Range") or ""
    try:
        return int(r.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return -1


def _file(d: dict) -> dict:
    """A Drive file as StreamSplitter uses it."""
    fid = d.get("id") or ""
    folder = d.get("mimeType") == FOLDER_MIME
    return {"id": fid, "name": d.get("name") or "", "url": d.get("webViewLink") or (folder_url(fid) if folder
                                                                                    else file_url(fid)),
            "trashed": bool(d.get("trashed")), "parents": list(d.get("parents") or []),
            "props": dict(d.get("appProperties") or {}), "description": d.get("description"),
            "size": _int(d.get("size")), "folder": folder}


# ---------------------------------------------------------------------------------------------------------------------
# Client

class DriveClient:
    """The Drive calls StreamSplitter needs, each mapped to TransientError / PermanentError. The access token is
    renewed when it expires (and once more on a 401); `on_token(token_json)` gets the renewed token so the caller can
    store it (config.set_secret("drive_token", ...)). Never logs or shows a token."""

    def __init__(self, token_json: str, client_config: dict | None = None, on_token=None, *, session=None,
                 chunk_size: int = CHUNK_SIZE, clock=time.time):
        from google.oauth2.credentials import Credentials
        try:
            info = json.loads(token_json)
            client = (client_config or {}).get("installed") or (client_config or {}).get("web") or {}
            for k in ("client_id", "client_secret"):
                if not info.get(k) and client.get(k):
                    info[k] = client[k]
            self.creds = Credentials.from_authorized_user_info(info)
        except (TypeError, ValueError, AttributeError):
            raise PermanentError(f"the stored Google Drive login is unusable; {LOGIN_AGAIN}") from None
        if chunk_size <= 0 or chunk_size % (256 * 1024):
            raise ValueError("chunk_size must be a multiple of 256 KiB")
        self.on_token = on_token
        self.session = session or requests.Session()
        self.chunk_size = chunk_size
        self.clock = clock
        self._lock = threading.Lock()
        self._auth_request = None

    # -- plumbing ------------------------------------------------------------------------------------------------------

    def _token(self, force: bool = False) -> str:
        from google.auth.exceptions import RefreshError, TransportError
        from google.auth.transport.requests import Request
        with self._lock:
            if force or not self.creds.valid:
                if self._auth_request is None:
                    self._auth_request = Request(self.session)
                try:
                    self.creds.refresh(self._auth_request)
                except RefreshError as e:
                    if getattr(e, "retryable", False):
                        raise TransientError("Google could not renew the Google Drive login just now") from None
                    raise PermanentError(f"the Google Drive login expired or was revoked; {LOGIN_AGAIN}") from None
                except TransportError:
                    raise TransientError("no connection to Google to renew the Google Drive login") from None
                if self.on_token:
                    self.on_token(self.creds.to_json())
            return self.creds.token

    def _send(self, method: str, url: str, *, what: str, ok=(), headers=None, timeout=API_TIMEOUT,
              **kw) -> requests.Response:
        """One call with the access token: the response when it is 2xx or in `ok`, else the mapped error. A 401 renews
        the token and tries once more."""
        for attempt in (1, 2):
            token = self._token(force=attempt == 2)
            h = {**(headers or {}), "Authorization": f"Bearer {token}"}
            try:
                resp = self.session.request(method, url, headers=h, timeout=timeout, **kw)
            except requests.exceptions.RequestException as e:
                # the exception text can hold the upload session URI: name the kind of failure only
                raise TransientError(f"Google Drive {what}: no connection ({type(e).__name__})") from None
            if resp.status_code == 401 and attempt == 1:
                continue
            if 200 <= resp.status_code < 300 or resp.status_code in ok:
                return resp
            raise self._error(resp, what)
        raise AssertionError("unreachable")

    def _error(self, resp, what: str) -> Exception:
        status = resp.status_code
        reason, message = _reason(resp)
        if reason in _FULL:
            return DriveFull(f"Google Drive is full (the {what} was refused: storageQuotaExceeded)")
        if reason in _DAILY:
            return TransientError(f"Google Drive {what}: a daily limit of the Google Cloud project ran out "
                                  f"({reason}); trying again in an hour", retry_after=3600.0)
        if status in _TRANSIENT_STATUS or reason in _RATE:
            return TransientError(f"Google Drive {what}: HTTP {status} {reason}".rstrip(),
                                  retry_after=_retry_after(resp))
        if status == 401:
            return PermanentError(f"Google Drive refused the login ({what}); {LOGIN_AGAIN}")
        if reason in _FIXES:
            return PermanentError(f"Google Drive {what}: {_FIXES[reason]}")
        if status == 404:
            return NotFound(f"Google Drive {what}: not found{': ' + message if message else ''}")
        detail = f" {reason}" if reason else ""
        msg = f": {message}" if message else ""
        return PermanentError(f"Google Drive rejected the {what} (HTTP {status}{detail}){msg}")

    @staticmethod
    def _json(r) -> dict:
        try:
            data = r.json()
        except ValueError:
            raise TransientError("Google Drive's answer was cut off; trying again later") from None
        return data if isinstance(data, dict) else {}

    # -- account, folders, files ---------------------------------------------------------------------------------------

    def about(self) -> dict:
        """The account and its storage: {"name", "email", "limit", "usage", "in_drive", "in_trash", "free",
        "checked"} (bytes; limit and free None for an account without a limit)."""
        r = self._send("GET", f"{API}/about", what="account check",
                       params={"fields": "user(displayName,emailAddress),"
                                         "storageQuota(limit,usage,usageInDrive,usageInDriveTrash)"})
        d = self._json(r)
        q, u = d.get("storageQuota") or {}, d.get("user") or {}
        limit, usage = _int(q.get("limit")), _int(q.get("usage"))
        return {"name": u.get("displayName"), "email": u.get("emailAddress"), "limit": limit, "usage": usage,
                "in_drive": _int(q.get("usageInDrive")), "in_trash": _int(q.get("usageInDriveTrash")),
                "free": None if limit is None or usage is None else max(0, limit - usage), "checked": self.clock()}

    def get(self, file_id: str, what: str = "folder check") -> dict | None:
        """A file or folder StreamSplitter made (_file), None when Drive does not know it (deleted for good, or made
        by another account)."""
        r = self._send("GET", f"{API}/files/{urllib.parse.quote(file_id, safe='')}", what=what, ok=(404,),
                       params={"fields": FILE_FIELDS})
        return None if r.status_code == 404 else _file(self._json(r))

    def find(self, query: str, what: str = "folder lookup") -> list[dict]:
        """The files StreamSplitter made that match a Drive query (drive.file sees no others)."""
        out, page = [], None
        while True:
            params = {"q": query, "spaces": "drive", "pageSize": 100, "fields": f"nextPageToken,files({FILE_FIELDS})"}
            if page:
                params["pageToken"] = page
            data = self._json(self._send("GET", f"{API}/files", what=what, params=params))
            out += [_file(f) for f in data.get("files") or []]
            page = data.get("nextPageToken")
            if not page:
                return out

    def create_folder(self, name: str, parent: str | None = None, props: dict | None = None) -> dict:
        body = {"name": clean_name(name, TOP_TITLE), "mimeType": FOLDER_MIME}
        if parent:
            body["parents"] = [parent]
        if props:
            body["appProperties"] = props
        r = self._send("POST", f"{API}/files", what="folder creation", params={"fields": FILE_FIELDS}, json=body)
        return _file(self._json(r))

    def update(self, file_id: str, body: dict, what: str = "update") -> dict:
        r = self._send("PATCH", f"{API}/files/{urllib.parse.quote(file_id, safe='')}", what=what,
                       params={"fields": FILE_FIELDS}, json=body)
        return _file(self._json(r))

    def trash(self, file_id: str) -> bool:
        """Move a file to the trash (never deleted for good: Drive empties the trash after 30 days). False when it
        is gone already."""
        r = self._send("PATCH", f"{API}/files/{urllib.parse.quote(file_id, safe='')}", what="move to trash",
                       ok=(404,), params={"fields": "id,trashed"}, json={"trashed": True})
        return r.status_code != 404

    # -- resumable upload ----------------------------------------------------------------------------------------------

    def upload(self, path: str, metadata: dict, state: dict, progress=None, on_state=None) -> dict:
        """Upload (or finish uploading) `path` with `metadata` (name, parents, description, appProperties, ...) and
        return {"file_id", "url"}.

        `state` is the job's resume state, changed in place: upload_uri (the session), upload_size, upload_offset,
        upload_src (size and time of the file the session is for), then file_id and url. `on_state()` is called after
        each change; `progress(sent, total)` after each piece. One call is one attempt: a drop raises TransientError
        with `state` ready for the next."""
        if state.get("file_id"):
            return self._result(state)
        try:
            st = os.stat(path)
        except OSError:
            raise PermanentError(f"the clip to upload is missing: {path}") from None
        size = st.st_size
        if size == 0:
            raise PermanentError(f"the clip to upload is empty: {path}")
        src = [size, int(st.st_mtime)]

        def changed():
            if on_state:
                on_state()

        if state.get("upload_uri") and (state.get("upload_size") != size or state.get("upload_src") != src):
            self.forget_session(state)          # another file than the session was opened for (a new copy)
        offset = None
        if state.get("upload_uri"):
            offset, done = self._query(state, size)
            if done is not None:
                return self._finish(state, done, size, progress, changed)
        restarted = False
        if offset is None:
            self._open_session(size, metadata, state, src)
            changed()
            offset = 0
        stalls = 0
        with open(path, "rb") as f:
            while True:
                if offset >= size:
                    _, done = self._query(state, size)
                    if done is not None:
                        return self._finish(state, done, size, progress, changed)
                    raise TransientError("Google Drive has the whole video but has not finished it; asking again later")
                f.seek(offset)
                chunk = f.read(self.chunk_size)
                end = offset + len(chunk) - 1
                r = self._send("PUT", state["upload_uri"], what="upload", data=chunk, ok=(308, 400, 403, 404, 410),
                               timeout=CHUNK_TIMEOUT, allow_redirects=False,
                               headers={"Content-Length": str(len(chunk)), "Content-Type": "video/mp4",
                                        "Content-Range": f"bytes {offset}-{end}/{size}"})
                if r.status_code in (200, 201):
                    return self._finish(state, self._json(r), size, progress, changed)
                if r.status_code in (400, 403):
                    # Drive's guide: other 4xx during a resumable upload, start over with a new session. Limits,
                    # a full Drive and known causes are said as they are; a third refusal in a row too
                    reason = _reason(r)[0]
                    state["refusals"] = state.get("refusals", 0) + 1
                    if reason in _RATE | _FULL | _DAILY | set(_FIXES) or state["refusals"] >= 3:
                        raise self._error(r, "upload")
                    self.forget_session(state)
                    changed()
                    raise TransientError(f"Google Drive refused a piece of the upload (HTTP {r.status_code} "
                                         f"{reason}); starting it again".replace(" )", ")"))
                if r.status_code in (404, 410):
                    # the session ended (they last a week): open a new one once in this attempt
                    self.forget_session(state)
                    changed()
                    if restarted:
                        raise TransientError("Google Drive ended the upload session twice; trying again later")
                    restarted = True
                    self._open_session(size, metadata, state, src)
                    changed()
                    offset = 0
                    continue
                new = _range_end(r) + 1
                stalls = stalls + 1 if new <= offset else 0
                if stalls >= 3:
                    raise TransientError("the Google Drive upload is not advancing; trying again later")
                offset = new
                state["upload_offset"] = offset
                changed()
                if progress:
                    progress(offset, size)

    def _open_session(self, size: int, metadata: dict, state: dict, src: list) -> None:
        r = self._send("POST", UPLOAD_URL, what="upload start", json=metadata,
                       params={"uploadType": "resumable", "fields": FILE_FIELDS},
                       headers={"X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4",
                                "Content-Type": "application/json; charset=UTF-8"})
        uri = r.headers.get("Location")
        if not uri:
            raise TransientError("Google Drive did not open an upload session; trying again later")
        state.update(upload_uri=uri, upload_size=size, upload_offset=0, upload_src=src, upload_opened=self.clock(),
                     upload_sessions=state.get("upload_sessions", 0) + 1)

    def _query(self, state: dict, size: int) -> tuple[int | None, dict | None]:
        """Ask the session how much Drive has: (next offset, None), (None, file) when it finished already, or
        (None, None) when the session is no longer usable (forgotten from `state`)."""
        r = self._send("PUT", state["upload_uri"], what="upload check", data=b"", ok=(308, 400, 403, 404, 410),
                       allow_redirects=False, headers={"Content-Length": "0", "Content-Range": f"bytes */{size}"})
        if r.status_code in (200, 201):
            return None, self._json(r)
        if r.status_code == 308:
            return _range_end(r) + 1, None
        if r.status_code == 403 and _reason(r)[0] in _RATE | _FULL | _DAILY:
            raise self._error(r, "upload check")
        self.forget_session(state)              # expired or refused: a new session starts over
        return None, None

    @staticmethod
    def forget_session(state: dict) -> None:
        for k in ("upload_uri", "upload_offset", "upload_opened", "upload_src"):
            state.pop(k, None)

    def _finish(self, state: dict, f: dict, size: int, progress, changed) -> dict:
        fid = f.get("id")
        if not fid:
            raise TransientError("Google Drive finished the upload but sent no file id; checking again later")
        state["file_id"] = fid
        state["url"] = f.get("webViewLink") or file_url(fid)
        state.pop("refusals", None)
        self.forget_session(state)
        changed()
        if progress:
            progress(size, size)
        return self._result(state)

    @staticmethod
    def _result(state: dict) -> dict:
        return {"file_id": state["file_id"], "url": state.get("url") or file_url(state["file_id"])}


# ---------------------------------------------------------------------------------------------------------------------
# Folders (the setup uses these too)

def find_top_folder(client: DriveClient, title: str) -> dict | None:
    """A top folder StreamSplitter made earlier with this login and this name (not in the trash), or None."""
    hits = client.find(f"mimeType = '{FOLDER_MIME}' and trashed = false and name = '{_q(title)}' and "
                       f"appProperties has {{ key='{PROP_KIND}' and value='top' }}")
    return hits[0] if hits else None


def create_top_folder(client: DriveClient, title: str) -> dict:
    """The top folder, in the logged-in account's My Drive."""
    return client.create_folder(title, None, {PROP_KIND: "top"})


def ensure_event_folder(client: DriveClient, top_id: str, event_key: str, title: str) -> dict:
    """The event's folder inside the top folder: the one StreamSplitter made for this event key (another laptop or an
    earlier run), else one of the same name it made, else a new one."""
    base = f"'{_q(top_id)}' in parents and mimeType = '{FOLDER_MIME}' and trashed = false"
    key = _q(_prop(PROP_EVENT, event_key))
    hits = client.find(f"{base} and appProperties has {{ key='{PROP_EVENT}' and value='{key}' }}")
    if not hits:
        hits = client.find(f"{base} and name = '{_q(title)}'")
    if hits:
        return hits[0]
    return client.create_folder(title, top_id, {PROP_KIND: "event", PROP_EVENT: _prop(PROP_EVENT, event_key)})


# ---------------------------------------------------------------------------------------------------------------------
# The smaller copy

def encode_small(src: Path, dst: Path, height: int, timeout: float = COPY_TIMEOUT_S) -> None:
    """The same re-encode as pipeline._deliver (libx264 veryfast, crf 26, 30 fps, audio copied, +faststart), at
    below-normal priority so the live pipeline stays responsive."""
    cmd = [ffmpeg.find(), "-v", "error", "-y", "-i", str(src), "-vf", f"scale=-2:{int(height)},fps=30", "-c:v",
           "libx264", "-preset", "veryfast", "-crf", "26", "-c:a", "copy", "-movflags", "+faststart", "-f", "mp4",
           str(dst)]
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
    elif shutil.which("nice"):
        cmd = ["nice", "-n", "10"] + cmd
    subprocess.run(cmd, check=True, timeout=timeout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **kw)


# ---------------------------------------------------------------------------------------------------------------------
# Destination

class DriveDestination:
    """Destination "drive": the clip (a smaller copy by default) into the event's folder, named to sort in play order,
    its description kept current. The upload is recorded in job.state and the folder searched before a new one, so
    sending a job again never makes a second copy."""

    name = "drive"

    def __init__(self, client: DriveClient, folder_id: str, *, folder_title: str | None = None,
                 event: dict | None = None, event_folder_id: str | None = None, event_folder_event: str | None = None,
                 height: int = DEFAULT_HEIGHT, name_format: str = NAME_FORMAT,
                 description_format: str = templates.DESCRIPTION_FORMAT, on_state=None, progress=None, on_folder=None,
                 on_full=None, warn=None, playoff_alliances: int = 8, encode=None, log=None, clock=time.time):
        """`folder_id`: the top folder the setup made. `event`: config's [event] (key, name, year): its folder holds
        clips with no event key. `on_folder(event_key, folder)`: an event folder was found or made (live saves it in
        config.toml). `on_full(text)`: the Drive is full (live pauses the queue and DMs `text`; without it, `text` is
        a warning). `warn(text, key)`: tell the contact (low space, copies failing). `on_state(job)`: save job.state
        now (the outbox's save_state). `encode(src, dst, height)`: makes the smaller copy (ffmpeg)."""
        problem = name_format_problem(name_format)
        if problem:
            raise PermanentError(f"the Google Drive name format is not usable ({problem}); fix it with {SETUP_AGAIN}")
        try:
            templates.fields_used(description_format)
            height = int(height)
        except (templates.TemplateError, TypeError, ValueError) as e:
            raise PermanentError(f"the Google Drive settings are not usable ({e}); fix them with {SETUP_AGAIN}") \
                from None
        if not folder_id:
            raise PermanentError(f"no Google Drive folder is set; make it with {SETUP_AGAIN}")
        self.client = client
        self.folder_id, self.folder_title = folder_id, folder_title or TOP_TITLE
        self.event = dict(event or {})
        self.event_folder_id, self.event_folder_event = event_folder_id, event_folder_event
        self.height = max(0, height)
        self.name_format, self.description_format = name_format, description_format
        self.on_state, self.progress, self.on_folder, self.on_full, self.warn = on_state, progress, on_folder, \
            on_full, warn
        self.playoff_alliances = playoff_alliances
        self.quals: int | None = None           # the event's last qualification number (Publisher's hook sets it)
        self.encode = encode or encode_small
        self.log = log or print
        self.clock = clock
        self.storage: dict | None = None        # the last about.get, with "matches_left"
        self._folders: dict[str, str] = {}      # event key -> folder id, checked in this run
        self._top_ok = False
        self._low_said = False
        self._copy_said = False

    # -- names and texts -----------------------------------------------------------------------------------------------

    def render(self, job: Job) -> tuple[str, str]:
        """(file name, description) for the job, as Drive gets them."""
        m = job.match
        description = templates.render(self.description_format, m, self.playoff_alliances)
        if job.kind == "test":
            name = f"{TEST_PREFIX} - {render_name(self.name_format, m, self.playoff_alliances, self.quals)}"
            return clean_name(name) + ".mp4", clean_description(f"{TEST_DESCRIPTION}\n\n{description}")
        name = render_name(self.name_format, m, self.playoff_alliances, self.quals)
        return name + ".mp4", clean_description(description)

    def _props(self, job: Job) -> dict:
        return {PROP_KIND: "video", PROP_MATCH: _prop(PROP_MATCH, job.match.name or ""),
                PROP_JOB: _prop(PROP_JOB, job.id)}

    def _played(self, job: Job) -> float | None:
        """When the match was played: the match clip's cut time (Publisher puts it in job.state), else the file's."""
        t = job.state.get("played")
        if isinstance(t, (int, float)) and t > 0:
            return float(t)
        if job.kind == "test":
            return None
        try:
            return os.path.getmtime(job.path)
        except (OSError, TypeError, ValueError):
            return None

    def _saved(self, job: Job) -> None:
        if self.on_state:
            self.on_state(job)

    def _warn(self, text: str, key: str) -> None:
        """Tell the contact (the notifier logs it too); the log only without one."""
        if self.warn is not None:
            try:
                self.warn(text, key)
                return
            except Exception:  # noqa: BLE001 - a warning is a nicety
                pass
        self.log(text)

    # -- sending -------------------------------------------------------------------------------------------------------

    def send(self, job: Job) -> dict:
        if job.kind not in ("clip", "score", "test", "describe"):
            raise PermanentError(f"StreamSplitter does not know how to send a {job.kind!r} job to Google Drive")
        st = job.state
        name, description = self.render(job)
        if not st.get("file_id"):
            if job.kind == "describe":
                return {"skipped": "nothing was uploaded"}
            self._upload(job, name, description)
        elif st.get("description") is not None and (st.get("name"), st["description"]) != (name, description):
            # TBA posted the score (or the teams) after the upload; no record = nothing to compare
            body = {"name": name, "description": description}
            played = self._played(job)
            if played:
                body["modifiedTime"] = _rfc3339(played)     # keep the play-order sort
            try:
                self.client.update(st["file_id"], body, what="description update")
            except NotFound:
                self.log(f"Google Drive: {name} is gone (deleted?); its description is not updated")
                return {"file_id": st["file_id"], "gone": True}
            st["name"], st["description"] = name, description
            self._saved(job)
        out = {"file_id": st["file_id"], "url": st.get("url"), "name": st.get("name"), "folder_id": st.get("folder_id")}
        if st.get("existed"):
            out["existed"] = True
        if job.kind == "test":
            out["test"] = True
        return out

    def _upload(self, job: Job, name: str, description: str) -> None:
        st = job.state
        progress = (lambda sent, total: self.progress(job, sent, total)) if self.progress else None
        try:
            if not st.get("upload_uri") or not st.get("folder_id"):
                st["folder_id"] = self.folder_for(job)
            if not st.get("upload_uri") and job.kind != "test":
                # looked for before the copy is made: a video already there costs no re-encode
                found = self._existing(st["folder_id"], job)
                if found is not None:
                    st.update(file_id=found["id"], url=found["url"], name=found["name"],
                              description=found.get("description") or "", existed=True)
                    self._saved(job)
                    self.log(f"Google Drive: {job.match.name} is in the folder already ({found['name']}); not "
                             "uploaded again")
                    self._drop_copy(self._copy_path(Path(job.path)), job)
                    return
            src = self._source(job)
            try:
                size = os.path.getsize(src)
            except OSError:
                raise PermanentError(f"the clip to upload is missing: {src}") from None
            self._check_space(size)
            for attempt in (1, 2):
                meta = {"name": name, "mimeType": "video/mp4", "parents": [st["folder_id"]],
                        "description": description, "appProperties": self._props(job)}
                played = self._played(job)
                if played:
                    meta["modifiedTime"] = _rfc3339(played)
                try:
                    self.client.upload(str(src), meta, st, progress=progress, on_state=lambda: self._saved(job))
                    break
                except NotFound:
                    # the folder went away (deleted while running): look again once; a top folder that is gone
                    # stops with what to do
                    self.client.forget_session(st)
                    self._folders.clear()
                    self._top_ok = False
                    if attempt == 2:
                        raise PermanentError(f"Google Drive keeps refusing the folder for {job.match.name}; run "
                                             f"{SETUP_AGAIN}") from None
                    st["folder_id"] = self.folder_for(job)
        except DriveFull as e:
            self._full(str(e))
            raise
        st.update(name=name, description=description)
        self._saved(job)
        self._used(size)
        self._drop_copy(src, job)

    def _existing(self, folder_id: str, job: Job) -> dict | None:
        """A video of this match StreamSplitter put in the folder before (a lost answer, a restart that lost the job's
        state, a second laptop on the same account)."""
        key = _q(_prop(PROP_MATCH, job.match.name or ""))
        if not key:
            return None
        hits = self.client.find(f"'{_q(folder_id)}' in parents and trashed = false and "
                                f"appProperties has {{ key='{PROP_MATCH}' and value='{key}' }}", what="video lookup")
        return hits[0] if hits else None

    # -- folders -------------------------------------------------------------------------------------------------------

    def top(self) -> str:
        """The top folder's id, checked once per run: PermanentError with what to do when it is gone or in the
        trash."""
        if self._top_ok:
            return self.folder_id
        f = self.client.get(self.folder_id)
        if f is None:
            raise PermanentError(f"Google Drive cannot find the folder '{self.folder_title}': it was deleted, or this "
                                 "laptop is logged in to another Google account than the one that made it. Run "
                                 f"{SETUP_AGAIN} to make a new folder (or log in with that account)")
        if f["trashed"]:
            raise PermanentError(f"the Google Drive folder '{self.folder_title}' is in the trash: restore it in "
                                 f"Google Drive (Trash, right-click it, Restore), or make a new one with "
                                 f"{SETUP_AGAIN}")
        self._top_ok = True
        return self.folder_id

    def folder_for(self, job: Job) -> str:
        """The folder a job's video goes in: its event's (found or made inside the top folder), the top folder for
        the setup test and for a clip with no event."""
        if job.kind == "test":
            return self.top()
        m = job.match
        key = m.event_key or self.event.get("key")
        if not key:
            return self.top()
        if key in self._folders:
            return self._folders[key]
        fid = None
        if key == self.event_folder_event and self.event_folder_id:
            f = self.client.get(self.event_folder_id)
            if f is not None and not f["trashed"] and (not f["parents"] or self.folder_id in f["parents"]):
                fid = f["id"]
        if fid is None:
            top = self.top()
            same = self.event.get("key") == key
            name = m.event_name if m.event_key == key and m.event_name else self.event.get("name") if same else None
            year = m.year if m.event_key == key and m.year else self.event.get("year") if same else None
            if year is None and key[:4].isdigit():
                year = int(key[:4])
            f = ensure_event_folder(self.client, top, key, event_title(key, name, year))
            fid = f["id"]
            self.event_folder_id, self.event_folder_event = fid, key
            if self.on_folder is not None:
                try:
                    self.on_folder(key, f)
                except Exception as e:  # noqa: BLE001 - it is found again next time anyway
                    self.log(f"Google Drive: could not save the event folder ({type(e).__name__})")
        self._folders[key] = fid
        return fid

    # -- the smaller copy ----------------------------------------------------------------------------------------------

    def _source(self, job: Job) -> Path:
        """The file to upload: the smaller copy (made now, or the one a retry made before), or the clip as cut for
        height 0, for the setup test, or when the copy fails (said once)."""
        src = Path(job.path)
        if job.kind == "test" or not self.height or job.state.get("as_cut") or not src.is_file():
            return src
        try:
            small = self._small(src)
            if small.stat().st_size >= src.stat().st_size:
                # a stream at this height already (a 720p webcast): the copy saves nothing
                job.state["as_cut"] = True
                self._saved(job)
                small.unlink(missing_ok=True)
                return src
            return small
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            job.state["as_cut"] = True
            self._saved(job)
            if not self._copy_said:
                self._copy_said = True
                self._warn(f"Google Drive: the {self.height}p copies cannot be made ({type(e).__name__}: "
                           f"{str(e)[:120]}), so the match videos go up as cut, about 3x larger. Check that ffmpeg "
                           "works (run the setup again: it installs it).", "drive_copy")
            return src

    def _copy_path(self, src: Path) -> Path:
        return src.parent / f"drive_{self.height}p" / src.name

    def _small(self, src: Path) -> Path:
        dst = self._copy_path(src)
        folder = dst.parent
        if dst.is_file() and dst.stat().st_size > 0 and dst.stat().st_mtime >= src.stat().st_mtime:
            return dst                          # a retry: the copy made before (the session is for its bytes)
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / f".{src.name}.part"
        try:
            self.encode(src, tmp, self.height)
            if not tmp.is_file() or tmp.stat().st_size == 0:
                raise OSError("ffmpeg made no file")
            os.replace(tmp, dst)
        finally:
            tmp.unlink(missing_ok=True)
        return dst

    def _drop_copy(self, src: Path, job: Job) -> None:
        """The smaller copy is not needed once Drive has the video."""
        if Path(src) == Path(job.path) or not Path(src).parent.name.startswith("drive_"):
            return
        try:
            Path(src).unlink(missing_ok=True)
            Path(src).parent.rmdir()            # only when empty
        except OSError:
            pass

    # -- storage -------------------------------------------------------------------------------------------------------

    def _refresh_storage(self) -> dict | None:
        try:
            s = self.client.about()
        except (PermanentError, TransientError) as e:
            self.log(f"Google Drive: free space not available ({e})")
            return self.storage
        s["matches_left"] = matches_left(s.get("free"), self.height)
        self.storage = s
        return s

    def storage_status(self) -> dict | None:
        """What `status` shows (no network): {"account", "free_gb", "limit_gb", "matches_left", "height",
        "checked"}; None before the first upload looked."""
        s = self.storage
        if not s:
            return None
        return {"account": s.get("email") or s.get("name"), "free_gb": None if s.get("free") is None
                else round(s["free"] / 1e9, 1), "limit_gb": None if s.get("limit") is None
                else round(s["limit"] / 1e9, 1), "matches_left": s.get("matches_left"), "height": self.height,
                "checked": s.get("checked")}

    def _check_space(self, size: int) -> None:
        """Before an upload: free space from about.get (asked at most every STORAGE_EVERY_S, again when it looks too
        small); DriveFull when the video cannot fit, a DM when less than about one event is left."""
        now = self.clock()
        s = self.storage
        if s is None or now - float(s.get("checked") or 0) >= STORAGE_EVERY_S:
            s = self._refresh_storage()
        if not s or s.get("limit") is None or s.get("free") is None:
            return
        if s["free"] < size and now - float(s.get("checked") or 0) > 60:
            s = self._refresh_storage() or s
        who = f" ({s.get('email') or s.get('name')})" if s.get("email") or s.get("name") else ""
        if s["free"] < size:
            raise DriveFull(f"Google Drive{who} is full: {gb(s['free'])} GB free of {gb(s['limit'])} GB, and the "
                            f"next video is {size / 1e6:.0f} MB")
        low = EVENT_MATCHES * MB_PER_MATCH.get(self.height, MB_PER_MATCH[0]) * 1e6
        if s["free"] < low and not self._low_said:
            self._low_said = True
            self._warn(f"Google Drive{who} has {gb(s['free'])} GB free: about {s.get('matches_left')} more match "
                       f"videos, less than one event. Free some space there (empty the Drive trash too), or log in "
                       f"with another account before the next event ({SETUP_AGAIN}).", "drive_space")
        elif s["free"] >= low * 1.25:
            self._low_said = False

    def _used(self, size: int) -> None:
        s = self.storage
        if s and s.get("free") is not None:
            s["free"] = max(0, s["free"] - size)
            if s.get("usage") is not None:
                s["usage"] += size
            s["matches_left"] = matches_left(s["free"], self.height)

    def _full(self, reason: str) -> None:
        """The Drive is full: on_full pauses the queue, so this is said once per pause (a `resume drive` while still
        full says it again)."""
        s = self.storage or {}
        who = f" ({s.get('email') or s.get('name')})" if s.get("email") or s.get("name") else ""
        text = (f"Google Drive{who} is full, so match videos are not uploaded there: they wait on this computer. "
                "Free space in that Drive (empty its trash too) or log in with another account "
                f"({SETUP_AGAIN}), then reply `resume drive`.")
        self.log(f"Google Drive: {reason}")
        if self.on_full is not None:
            try:
                self.on_full(text)
                return
            except Exception as e:  # noqa: BLE001
                self.log(f"Google Drive: could not pause the queue ({type(e).__name__})")
        self._warn(text, "drive_full")
