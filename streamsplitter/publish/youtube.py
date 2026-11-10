"""YouTube: log in once in the browser, then upload every match clip unlisted and add it to the event's playlist.

Plain HTTPS to the YouTube Data API v3 (requests) with google-auth's OAuth credentials: the Google API client library
is not needed for the handful of calls used here.

Uploads use YouTube's resumable protocol. One POST opens an upload session (title, description, privacy) and returns
its URI, kept in the job's state; PUTs send the file in 8 MiB pieces. After a network drop or a restart, an empty PUT
("Content-Range: bytes */<size>") asks YouTube how much it has (308 + Range) and the upload continues from there. A
session YouTube no longer knows (404/410) is opened again from the start. An upload that finished but whose answer was
lost is found by the same question (YouTube answers with the video), so a retry never makes a second copy, as long as
the caller keeps job.state (the outbox saves it every few seconds; YouTubeDestination's on_state hook can save it
after every step).

Errors map to base.TransientError (try later: no network, timeouts, HTTP 429/5xx, rate limits, the daily quota until
midnight US Pacific) or base.PermanentError (a person must act: log in again, enable the API, give the account a
channel), with a message that says what to do.

Quota (docs/research/youtube.md, A.3): a Google Cloud project may make 100 uploads a day (videos.insert's own bucket)
and spend 10,000 units a day on everything else (a playlist add or a description update is 50). QuotaTracker keeps a
local estimate of both per Pacific day that the notifier can read (quota_status()).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from . import templates
from .base import Job, PermanentError, TransientError

# One scope covers everything used here (upload, playlists, channel lookup, video update/delete). youtube.upload alone
# cannot create playlists or add to them, and asking for one scope means Google's consent page has no box to untick.
SCOPES = ["https://www.googleapis.com/auth/youtube"]

API = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
TOKEN_URI = "https://oauth2.googleapis.com/token"
CHUNK_SIZE = 8 * 1024 * 1024        # YouTube wants pieces in multiples of 256 KiB (except the last)
CATEGORY_ID = "28"                  # Science & Technology (FIRST is a STEM programme); 17 is Sports. IDs unconfirmed
PRIVACY = ("unlisted", "public", "private")
TEST_TITLE = "StreamSplitter setup test"
TEST_DESCRIPTION = "A test upload from the StreamSplitter setup. It is safe to delete."
LOGIN_AGAIN = "log in to YouTube again: streamsplitter setup --section youtube"

API_TIMEOUT = (15, 60)              # (connect, read) seconds
CHUNK_TIMEOUT = (15, 300)           # an 8 MiB piece on a slow home uplink

# Quota per call, from Google's Quota Calculator as read 2026-11-04 (docs/research/youtube.md, A.3). Since 2026-06-01
# videos.insert has its own bucket: 1 per call, 100 calls a day per Google Cloud project. Everything else shares the
# general bucket of 10,000 units a day. Both reset at midnight US Pacific. The upload session's opening POST is the
# videos.insert call; the PUTs that carry the file are assumed not to be charged (unconfirmed).
QUOTA_COSTS = {
    "videos.insert": 1,             # upload bucket
    "videos.update": 50,
    "videos.delete": 50,
    "videos.list": 1,
    "playlists.insert": 50,
    "playlists.update": 50,
    "playlists.list": 1,
    "playlistItems.insert": 50,
    "playlistItems.list": 1,
    "channels.list": 1,
}
UPLOAD_BUCKET = {"videos.insert"}
QUOTA_COSTS_CONFIRMED = True        # Quota Calculator, 2026-11-03 revision (research report A.3)
DAILY_QUOTA = 10_000                # general bucket, units per day (default allocation)
DAILY_UPLOADS = 100                 # upload bucket, videos.insert calls per day (default allocation)
UNITS_PER_UPLOAD = QUOTA_COSTS["playlistItems.insert"] + QUOTA_COSTS["videos.update"]   # playlist add + score update
QUOTA_RESET_MARGIN_S = 120          # wait a little past midnight Pacific before trying again
QUOTA_KEEP_DAYS = 14

# ---------------------------------------------------------------------------------------------------------------------
# Pacific time: YouTube's quota day starts at midnight US Pacific. Windows Pythons often have no time zone database
# (the tzdata package), so the US rule (since 2007) is the fallback.

try:
    from zoneinfo import ZoneInfo
    _PACIFIC = ZoneInfo("America/Los_Angeles")
except Exception:
    _PACIFIC = None


def _us_pacific_offset(utc: datetime) -> timedelta:
    """UTC offset of US Pacific time at `utc`: daylight time from the second Sunday of March 02:00 PST to the first
    Sunday of November 02:00 PDT."""
    def sunday(month: int, nth: int) -> datetime:
        first = datetime(utc.year, month, 1, tzinfo=timezone.utc)
        return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (nth - 1))
    start = sunday(3, 2) + timedelta(hours=10)
    end = sunday(11, 1) + timedelta(hours=9)
    return timedelta(hours=-7) if start <= utc < end else timedelta(hours=-8)


def _pacific_offset(utc: datetime) -> timedelta:
    if _PACIFIC is not None:
        return utc.astimezone(_PACIFIC).utcoffset()
    return _us_pacific_offset(utc)


def pacific_day(now: float | None = None) -> str:
    """The quota day ("2026-11-06") at wall time `now` (epoch seconds; default now)."""
    utc = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
    return (utc + _pacific_offset(utc)).date().isoformat()


def seconds_to_pacific_midnight(now: float | None = None) -> float:
    """Seconds from `now` until the next midnight US Pacific, when YouTube's daily quota resets."""
    utc = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
    local = utc + _pacific_offset(utc)
    midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), timezone.utc)
    guess = midnight - _pacific_offset(utc)
    guess = midnight - _pacific_offset(guess)       # the offset at midnight itself (DST may change during the day)
    return (guess - utc).total_seconds()


# ---------------------------------------------------------------------------------------------------------------------
# Quota estimate

_quota_lock = threading.Lock()


def default_quota_path() -> Path:
    from .. import config
    return config.data_root() / "outbox" / "youtube_quota.json"


class QuotaTracker:
    """Local estimate of the quota this computer spent per Pacific day, in both buckets (general units, upload calls).
    Google charges a call even when it fails, so every call that reached YouTube is counted (not one that never got
    there: YouTubeClient._send). It is an estimate: other computers using
    the same Google Cloud project are not seen, and Google's own count (Cloud console, APIs & Services, Quotas) is
    the truth."""

    def __init__(self, path: Path | str | None = None, daily_limit: int = DAILY_QUOTA,
                 daily_uploads: int = DAILY_UPLOADS, clock=time.time):
        self._path = Path(path) if path else None
        self.daily_limit = daily_limit
        self.daily_uploads = daily_uploads
        self.clock = clock

    @property
    def path(self) -> Path:
        if self._path is None:
            self._path = default_quota_path()
        return self._path

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data.get("days"), dict) else {"days": {}}
        except (OSError, ValueError, AttributeError):
            return {"days": {}}

    def _save(self, data: dict) -> None:
        days = data["days"]
        for old in sorted(days)[:-QUOTA_KEEP_DAYS]:
            del days[old]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    @staticmethod
    def _day(day: dict | None) -> dict:
        """A day's record with every key present (tolerates a hand-edited or older file)."""
        day = day if isinstance(day, dict) else {}
        day.setdefault("units", 0)
        day.setdefault("uploads", 0)
        if not isinstance(day.get("calls"), dict):
            day["calls"] = {}
        if not isinstance(day.get("exhausted"), list):
            day["exhausted"] = []
        return day

    def _update(self, fn) -> None:
        with _quota_lock:
            data = self._load()
            key = pacific_day(self.clock())
            day = data["days"][key] = self._day(data["days"].get(key))
            fn(day)
            try:
                self._save(data)
            except OSError:
                pass        # an estimate is not worth failing an upload for

    def add(self, call: str) -> None:
        """Count one API call by its quota name ("videos.insert")."""
        cost = QUOTA_COSTS.get(call, 1)

        def f(day):
            day["uploads" if call in UPLOAD_BUCKET else "units"] += cost
            day["calls"][call] = day["calls"].get(call, 0) + 1
        self._update(f)

    def mark_exhausted(self, bucket: str) -> None:
        """YouTube said a daily limit ran out: "units" (general quota), "uploads" (videos.insert bucket) or "channel"
        (the channel's own upload limit). Today is used up whatever the estimate says."""
        def f(day):
            if bucket not in day["exhausted"]:
                day["exhausted"].append(bucket)
        self._update(f)

    def status(self) -> dict:
        """Today's estimate: {"day", "units", "limit", "remaining", "uploads", "upload_limit", "uploads_remaining",
        "uploads_left" (uploads the day still allows, counting a playlist add and a score update for each),
        "exhausted" (buckets YouTube said ran out), "blocked" (no upload possible until the reset), "resets_in_s",
        "calls", "costs_confirmed"}."""
        now = self.clock()
        day_key = pacific_day(now)
        with _quota_lock:
            day = self._day(self._load()["days"].get(day_key))
        out = set(day["exhausted"])
        remaining = 0 if "units" in out else max(0, self.daily_limit - day["units"])
        uploads_remaining = 0 if out & {"uploads", "channel"} else max(0, self.daily_uploads - day["uploads"])
        uploads_left = min(uploads_remaining, remaining // UNITS_PER_UPLOAD)
        return {"day": day_key, "units": day["units"], "limit": self.daily_limit, "remaining": remaining,
                "uploads": day["uploads"], "upload_limit": self.daily_uploads, "uploads_remaining": uploads_remaining,
                "uploads_left": uploads_left, "exhausted": sorted(out), "blocked": uploads_left == 0,
                "resets_in_s": round(seconds_to_pacific_midnight(now)), "calls": dict(day["calls"]),
                "costs_confirmed": QUOTA_COSTS_CONFIRMED}


def quota_status(path: Path | str | None = None, daily_limit: int = DAILY_QUOTA,
                 daily_uploads: int = DAILY_UPLOADS) -> dict:
    """Today's quota estimate (QuotaTracker.status) for the notifier and `streamsplitter status`."""
    return QuotaTracker(path, daily_limit, daily_uploads).status()


# ---------------------------------------------------------------------------------------------------------------------
# Login

class LoginError(PermanentError):
    """The browser login did not give a usable YouTube sign-in; the message says why."""


def check_client_config(cfg: dict) -> dict:
    """The OAuth client JSON downloaded from Google Cloud, checked to be a Desktop app client. Returns it."""
    if not isinstance(cfg, dict):
        raise LoginError("the OAuth client file is not JSON from Google Cloud (Credentials, OAuth client ID)")
    if "web" in cfg and "installed" not in cfg:
        raise LoginError("this OAuth client is a 'Web application'; create one of type 'Desktop app' instead "
                         "(docs/setup-youtube.md)")
    inst = cfg.get("installed")
    if not isinstance(inst, dict) or not inst.get("client_id") or not inst.get("client_secret"):
        raise LoginError("this is not an OAuth client file: in Google Cloud open APIs & Services, Credentials, your "
                         "Desktop app client, and use Download JSON")
    return cfg


def read_client_config(path: str | Path) -> dict:
    """Load and check the downloaded client_secret_....json."""
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise LoginError(f"cannot read the OAuth client file {path}: {type(e).__name__}") from None
    return check_client_config(cfg)


def login(client_config: dict, *, open_browser: bool = True, timeout_s: float = 600) -> str:
    """Log in to YouTube in the browser (installed-app OAuth, redirect to a random loopback port, PKCE) and return the
    token JSON (with the refresh token) for config.set_secret("youtube_token", ...). Google's account chooser decides
    the channel: pick the team's channel (a Brand Account) there, not the personal one; then confirm it with
    YouTubeClient.channel()."""
    from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError
    from oauthlib.oauth2.rfc6749.errors import OAuth2Error

    flow = InstalledAppFlow.from_client_config(check_client_config(client_config), SCOPES)
    try:
        creds = flow.run_local_server(
            # Google's desktop-app guide asks for the loopback IP rather than "localhost"
            host="127.0.0.1", port=0, open_browser=open_browser, timeout_seconds=timeout_s,
            authorization_prompt_message="If no browser window opened, open this link to log in to YouTube:\n{url}",
            success_message="StreamSplitter is logged in to YouTube. You can close this tab.",
            # consent: Google sends a refresh token every time; select_account: the channel chooser always shows
            prompt="consent select_account", access_type="offline")
    except WSGITimeoutError:
        raise LoginError("the YouTube login was not finished in time; run it again") from None
    except OAuth2Error as e:
        raise LoginError(f"the YouTube login was cancelled or refused ({e.error})") from None
    except Warning:             # oauthlib: "Scope has changed" when the YouTube permission was not granted
        raise LoginError("Google did not grant permission to manage YouTube: log in again and allow it") from None
    if not creds.refresh_token:
        raise LoginError("Google sent no refresh token; remove StreamSplitter at myaccount.google.com/permissions "
                         "and log in again")
    return creds.to_json()


# ---------------------------------------------------------------------------------------------------------------------
# Errors

_TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}
_RATE = {"rateLimitExceeded", "userRateLimitExceeded", "backendError", "internalError", "RATE_LIMIT_EXCEEDED"}
_QUOTA = {"quotaExceeded", "dailyLimitExceeded"}
_FIXES = {
    "forbidden": "the logged-in account may not do this on the channel; log in with the channel's owner or a "
                 f"manager ({LOGIN_AGAIN})",
    "insufficientPermissions": f"the login does not allow managing YouTube; {LOGIN_AGAIN} and allow everything asked",
    "ACCESS_TOKEN_SCOPE_INSUFFICIENT": f"the login does not allow managing YouTube; {LOGIN_AGAIN} and allow "
                                       "everything asked",
    "youtubeSignupRequired": "the Google account that logged in has no YouTube channel; pick the team's channel in "
                             f"the account chooser ({LOGIN_AGAIN})",
    "accessNotConfigured": "the YouTube Data API v3 is not enabled in the Google Cloud project; enable it "
                           "(docs/setup-youtube.md, step 2) and try again",
    "SERVICE_DISABLED": "the YouTube Data API v3 is not enabled in the Google Cloud project; enable it "
                        "(docs/setup-youtube.md, step 2) and try again",
    "invalidTitle": "YouTube refused the title; change the title format (streamsplitter setup --section youtube)",
    "invalidDescription": "YouTube refused the description; change the description format (streamsplitter setup "
                          "--section youtube)",
    "invalidCategoryId": "YouTube refused the video category",
    "forbiddenPrivacySetting": "YouTube refused the privacy setting; choose unlisted or private in the setup",
    "invalidPrivacyStatus": "YouTube refused the privacy setting; choose unlisted or private in the setup",
    "playlistNotFound": "the event playlist is gone (deleted?); make a new one with streamsplitter setup --section "
                        "youtube",
    "channelNotFound": f"YouTube cannot find the channel; {LOGIN_AGAIN}",
    "authError": f"YouTube refused the login; {LOGIN_AGAIN}",
}


class NotListedYet(TransientError):
    """YouTube does not list a video that was just uploaded (it can take a moment)."""


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


def clean_title(title: str, fallback: str = "Match video", limit: int = 100) -> str:
    """A title YouTube accepts: no < or >, one line, at most `limit` characters (100 for a video, 150 for a
    playlist), never empty. A title that ends in the match's TBA id ("... (qm12)", templates.TBA_TITLE_FORMAT) keeps
    it when cut: TBA's playlist page reads the match from it."""
    t = " ".join((title or "").replace("<", "").replace(">", "").split())
    tail = _TBA_TAIL.search(t) if len(t) > limit else None
    if tail:
        return t[:limit - len(tail.group(0))].rstrip() + tail.group(0)
    return t[:limit].rstrip() or fallback[:limit]


_TBA_TAIL = re.compile(r" \((?:qm\d+|(?:ef|qf|sf|f)\d+m\d+)\)$")


def clean_description(text: str) -> str:
    """A description YouTube accepts: no < or >, at most 5000 bytes."""
    d = (text or "").replace("<", "").replace(">", "")
    b = d.encode("utf-8")
    return d if len(b) <= 5000 else b[:5000].decode("utf-8", "ignore")


def _range_end(resp) -> int:
    """The last byte YouTube has, from a 308's "Range: bytes=0-N" (-1 when it has none)."""
    r = resp.headers.get("Range") or ""
    try:
        return int(r.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return -1


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def playlist_url(playlist_id: str) -> str:
    return f"https://www.youtube.com/playlist?list={playlist_id}"


# ---------------------------------------------------------------------------------------------------------------------
# Client

class YouTubeClient:
    """The YouTube calls StreamSplitter needs, each mapped to TransientError / PermanentError. The access token is
    renewed when it expires (and once more if YouTube answers 401); `on_token(token_json)` is handed the renewed token
    so the caller can store it (config.set_secret("youtube_token", ...)). Never logs or shows a token."""

    def __init__(self, token_json: str, client_config: dict | None = None, on_token=None, *, session=None,
                 quota: QuotaTracker | None = None, chunk_size: int = CHUNK_SIZE, clock=time.time):
        from google.oauth2.credentials import Credentials
        try:
            info = json.loads(token_json)
            client = (client_config or {}).get("installed") or (client_config or {}).get("web") or {}
            for k in ("client_id", "client_secret"):
                if not info.get(k) and client.get(k):
                    info[k] = client[k]
            self.creds = Credentials.from_authorized_user_info(info)
        except (TypeError, ValueError, AttributeError):
            raise PermanentError(f"the stored YouTube login is unusable; {LOGIN_AGAIN}") from None
        if chunk_size <= 0 or chunk_size % (256 * 1024):
            raise ValueError("chunk_size must be a multiple of 256 KiB")
        self.on_token = on_token
        self.session = session or requests.Session()
        self.quota = quota or QuotaTracker(clock=clock)
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
                    # one for the client's life: google-auth closes the session when its Request is dropped
                    self._auth_request = Request(self.session)
                try:
                    self.creds.refresh(self._auth_request)
                except RefreshError as e:
                    if getattr(e, "retryable", False):
                        raise TransientError("Google could not renew the YouTube login just now") from None
                    raise PermanentError(f"the YouTube login expired or was revoked; {LOGIN_AGAIN}") from None
                except TransportError:
                    raise TransientError("no connection to Google to renew the YouTube login") from None
                if self.on_token:
                    self.on_token(self.creds.to_json())
            return self.creds.token

    def _send(self, method: str, url: str, *, what: str, cost: str | None = None, ok=(), headers=None,
              timeout=API_TIMEOUT, **kw) -> requests.Response:
        """One call with the access token. Returns the response when it is 2xx or in `ok`, else raises the mapped
        error. A 401 renews the token and tries once more; a second 401 means the login is gone.

        The call counts towards the quota estimate once YouTube answered (whatever the status: Google charges failed
        calls too) or the answer was lost on the way back (a read timeout: the request got there). A call that never
        reached YouTube (offline, no DNS, refused) costs nothing, so retries during an outage do not use up the
        estimate and send a false "daily limit used up" warning."""
        counted = False
        for attempt in (1, 2):
            token = self._token(force=attempt == 2)
            h = {**(headers or {}), "Authorization": f"Bearer {token}"}
            try:
                resp = self.session.request(method, url, headers=h, timeout=timeout, **kw)
            except requests.exceptions.RequestException as e:
                if cost and not counted and isinstance(e, requests.exceptions.ReadTimeout):
                    self.quota.add(cost)
                # the exception text can hold the upload session URI: name the kind of failure only
                raise TransientError(f"YouTube {what}: no connection ({type(e).__name__})") from None
            if cost and not counted:
                self.quota.add(cost)
                counted = True
            if resp.status_code == 401 and attempt == 1:
                continue
            if 200 <= resp.status_code < 300 or resp.status_code in ok:
                return resp
            raise self._error(resp, what, cost)
        raise AssertionError("unreachable")

    def _error(self, resp, what: str, cost: str | None = None) -> Exception:
        status = resp.status_code
        reason, message = _reason(resp)
        if reason in _QUOTA or reason == "uploadLimitExceeded":
            # daily limits: waiting for the reset helps, so the outbox keeps the clip and tries after midnight Pacific
            wait = seconds_to_pacific_midnight(self.clock()) + QUOTA_RESET_MARGIN_S
            when = f"uploads continue after midnight US Pacific, in {wait / 3600:.1f} h"
            if reason == "uploadLimitExceeded":
                self.quota.mark_exhausted("channel")
                return TransientError(f"YouTube {what}: the channel reached its daily upload limit (YouTube's own "
                                      f"limit per channel, not the API quota); {when}. A phone-verified channel gets "
                                      "a higher limit: youtube.com/verify", retry_after=wait)
            bucket = "uploads" if cost in UPLOAD_BUCKET else "units"
            self.quota.mark_exhausted(bucket)
            which = (f"{DAILY_UPLOADS} uploads a day" if bucket == "uploads"
                     else f"{DAILY_QUOTA:,} units a day")
            return TransientError(f"YouTube's daily API quota ran out ({which}, during the {what}); {when}",
                                  retry_after=wait)
        if status in _TRANSIENT_STATUS or reason in _RATE:
            return TransientError(f"YouTube {what}: HTTP {status} {reason}".rstrip(), retry_after=_retry_after(resp))
        if status == 401:
            return PermanentError(f"YouTube refused the login ({what}); {LOGIN_AGAIN}")
        if reason in _FIXES:
            return PermanentError(f"YouTube {what}: {_FIXES[reason]}")
        detail = f" {reason}" if reason else ""
        msg = f": {message}" if message else ""
        if status == 403:
            return PermanentError(f"YouTube refused the {what} (HTTP 403{detail}){msg}; check that the logged-in "
                                  f"account manages the channel ({LOGIN_AGAIN})")
        return PermanentError(f"YouTube rejected the {what} (HTTP {status}{detail}){msg}")

    # -- channel and playlists -------------------------------------------------------------------------------------------

    def channel(self) -> dict:
        """The channel the login belongs to: {"id", "title", "url"}."""
        r = self._send("GET", f"{API}/channels", what="channel lookup", cost="channels.list",
                       params={"part": "snippet", "mine": "true"})
        items = r.json().get("items") or []
        if not items:
            raise PermanentError(f"YouTube {_FIXES['youtubeSignupRequired']}")
        c = items[0]
        custom = (c.get("snippet") or {}).get("customUrl")
        return {"id": c["id"], "title": (c.get("snippet") or {}).get("title", ""),
                "url": f"https://www.youtube.com/{custom}" if custom else f"https://www.youtube.com/channel/{c['id']}"}

    @staticmethod
    def _playlist(p: dict) -> dict:
        return {"id": p["id"], "title": (p.get("snippet") or {}).get("title", ""), "url": playlist_url(p["id"]),
                "privacy": (p.get("status") or {}).get("privacyStatus")}

    def find_playlist(self, title: str) -> dict | None:
        """The channel's playlist with this title (case and outer spaces ignored), or None."""
        want = title.strip().casefold()
        page = None
        while True:
            params = {"part": "snippet,status", "mine": "true", "maxResults": 50}
            if page:
                params["pageToken"] = page
            data = self._send("GET", f"{API}/playlists", what="playlist lookup", cost="playlists.list",
                              params=params).json()
            for p in data.get("items") or []:
                if (p.get("snippet") or {}).get("title", "").strip().casefold() == want:
                    return self._playlist(p)
            page = data.get("nextPageToken")
            if not page:
                return None

    def create_playlist(self, title: str, description: str, privacy: str = "unlisted") -> dict:
        body = {"snippet": {"title": clean_title(title, "Matches", 150), "description": clean_description(description)},
                "status": {"privacyStatus": privacy}}
        r = self._send("POST", f"{API}/playlists", what="playlist creation", cost="playlists.insert",
                       params={"part": "snippet,status"}, json=body)
        return self._playlist(r.json())

    def set_playlist_privacy(self, playlist_id: str, privacy: str) -> dict:
        """Make an existing playlist unlisted, public or private (the setup's choice, for a playlist made earlier).
        YouTube's update replaces the whole snippet, so the playlist's own title and description are read first and
        sent back unchanged (1 + 50 units)."""
        if privacy not in PRIVACY:
            raise PermanentError(f"YouTube privacy must be one of {', '.join(PRIVACY)}, not {privacy!r}")
        r = self._send("GET", f"{API}/playlists", what="playlist lookup", cost="playlists.list",
                       params={"part": "snippet,status", "id": playlist_id})
        items = r.json().get("items") or []
        if not items:
            raise PermanentError(f"YouTube playlist privacy change: {_FIXES['playlistNotFound']}")
        snip = items[0].get("snippet") or {}
        snippet = {"title": snip.get("title") or "Matches", "description": snip.get("description") or ""}
        if snip.get("defaultLanguage"):
            snippet["defaultLanguage"] = snip["defaultLanguage"]
        r = self._send("PUT", f"{API}/playlists", what="playlist privacy change", cost="playlists.update",
                       params={"part": "snippet,status"},
                       json={"id": playlist_id, "snippet": snippet, "status": {"privacyStatus": privacy}})
        return self._playlist(r.json())

    def add_to_playlist(self, playlist_id: str, video_id: str) -> None:
        body = {"snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}}
        r = self._send("POST", f"{API}/playlistItems", what="playlist add", cost="playlistItems.insert",
                       params={"part": "snippet"}, json=body, ok=(404,))
        if r.status_code == 404:
            if _reason(r)[0] == "videoNotFound":
                raise NotListedYet("YouTube does not list the new video yet; adding it to the playlist later")
            raise self._error(r, "playlist add")

    def playlist_has(self, playlist_id: str, video_id: str) -> bool:
        """Whether the video is in the playlist already (so a retried add does not list it twice)."""
        r = self._send("GET", f"{API}/playlistItems", what="playlist check", cost="playlistItems.list",
                       params={"part": "id", "playlistId": playlist_id, "videoId": video_id, "maxResults": 5})
        return bool(r.json().get("items"))

    # -- videos ----------------------------------------------------------------------------------------------------------

    def set_description(self, video_id: str, title: str, description: str, category_id: str = CATEGORY_ID) -> None:
        """Replace the title and description (e.g. once TBA posted the score). YouTube's update replaces the whole
        snippet, so title and category are sent too; tags are not used by StreamSplitter."""
        body = {"id": video_id, "snippet": {"title": clean_title(title), "description": clean_description(description),
                                            "categoryId": category_id}}
        self._send("PUT", f"{API}/videos", what="description update", cost="videos.update",
                   params={"part": "snippet"}, json=body)

    def delete_video(self, video_id: str) -> None:
        """Delete a video (the setup test); one that is already gone is fine."""
        self._send("DELETE", f"{API}/videos", what="video delete", cost="videos.delete", params={"id": video_id},
                   ok=(404,))

    def video_status(self, video_id: str) -> dict | None:
        """YouTube's status block for a video (privacyStatus, uploadStatus, ...), None when it is not found. The setup
        test uses it to see whether YouTube locked the upload as private."""
        r = self._send("GET", f"{API}/videos", what="video lookup", cost="videos.list",
                       params={"part": "status", "id": video_id})
        items = r.json().get("items") or []
        return items[0].get("status") if items else None

    def videos(self, video_ids: list[str]) -> dict[str, dict]:
        """{video id: YouTube's record with its snippet and status} for the videos that still exist, 50 per call (one
        quota unit a call). A deleted video is simply not in the answer."""
        ids, out = list(dict.fromkeys(video_ids)), {}
        for i in range(0, len(ids), 50):
            r = self._send("GET", f"{API}/videos", what="video lookup", cost="videos.list",
                           params={"part": "snippet,status", "id": ",".join(ids[i:i + 50])})
            for item in r.json().get("items") or []:
                out[item.get("id")] = item
        return out

    def set_title(self, video_id: str, title: str, snippet: dict) -> None:
        """Change only the title. YouTube's update replaces the whole snippet, so the rest of `snippet` (as videos()
        returned it) goes back unchanged."""
        keep = {k: snippet[k] for k in ("description", "categoryId", "tags", "defaultLanguage", "defaultAudioLanguage")
                if snippet.get(k) is not None}
        keep.setdefault("categoryId", CATEGORY_ID)
        self._send("PUT", f"{API}/videos", what="title update", cost="videos.update", params={"part": "snippet"},
                   json={"id": video_id, "snippet": {**keep, "title": clean_title(title)}})

    # -- resumable upload ----------------------------------------------------------------------------------------------

    def upload(self, path: str, title: str, description: str, privacy: str, state: dict, progress=None,
               on_state=None, *, category_id: str = CATEGORY_ID) -> dict:
        """Upload (or finish uploading) `path` and return {"video_id", "url", "privacy", "locked_private"}.

        `state` is the job's resume state, updated in place: upload_uri (the session), upload_size, upload_offset,
        then video_id once done. `on_state()` is called after each change so the caller can save it; `progress(sent,
        total)` after each piece. One call makes one attempt: a drop raises TransientError with `state` ready for the
        next attempt."""
        if state.get("video_id"):
            return self._result(state, privacy)
        if privacy not in PRIVACY:
            raise PermanentError(f"YouTube privacy must be one of {', '.join(PRIVACY)}, not {privacy!r}")
        try:
            size = os.path.getsize(path)
        except OSError:
            raise PermanentError(f"the clip to upload is missing: {path}") from None
        if size == 0:
            raise PermanentError(f"the clip to upload is empty: {path}")

        def changed():
            if on_state:
                on_state()

        if state.get("upload_uri") and state.get("upload_size") != size:
            self._forget_session(state)         # the file changed since the session was opened
        offset = None
        if state.get("upload_uri"):
            offset, video = self._query(state, size)
            if video is not None:
                return self._finish(state, video, privacy, size, progress, changed)
        restarted = False
        if offset is None:
            self._open_session(path, size, title, description, privacy, category_id, state)
            changed()
            offset = 0
        stalls = 0
        with open(path, "rb") as f:
            while True:
                if offset >= size:
                    # YouTube has every byte but has not answered with the video: ask once more, else later
                    got, video = self._query(state, size)
                    if video is not None:
                        return self._finish(state, video, privacy, size, progress, changed)
                    raise TransientError("YouTube has the whole clip but has not finished it; asking again later")
                f.seek(offset)
                chunk = f.read(self.chunk_size)
                end = offset + len(chunk) - 1
                r = self._send("PUT", state["upload_uri"], what="upload", data=chunk, ok=(308, 404, 410),
                               timeout=CHUNK_TIMEOUT, allow_redirects=False,
                               headers={"Content-Length": str(len(chunk)), "Content-Type": "video/mp4",
                                        "Content-Range": f"bytes {offset}-{end}/{size}"})
                if r.status_code in (200, 201):
                    return self._finish(state, self._json(r), privacy, size, progress, changed)
                if r.status_code in (404, 410):
                    # the session ended under us: open a new one once in this attempt
                    self._forget_session(state)
                    changed()
                    if restarted:
                        raise TransientError("YouTube ended the upload session twice; trying again later")
                    restarted = True
                    self._open_session(path, size, title, description, privacy, category_id, state)
                    changed()
                    offset = 0
                    continue
                new = _range_end(r) + 1
                stalls = stalls + 1 if new <= offset else 0
                if stalls >= 3:
                    raise TransientError("the YouTube upload is not advancing; trying again later")
                offset = new
                state["upload_offset"] = offset
                changed()
                if progress:
                    progress(offset, size)

    def _open_session(self, path, size, title, description, privacy, category_id, state) -> None:
        title = clean_title(title, Path(path).stem)
        description = clean_description(description)
        body = {"snippet": {"title": title, "description": description, "categoryId": category_id},
                "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False, "embeddable": True}}
        r = self._send("POST", UPLOAD_URL, what="upload start", cost="videos.insert", json=body,
                       params={"uploadType": "resumable", "part": "snippet,status", "notifySubscribers": "false"},
                       headers={"X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4",
                                "Content-Type": "application/json; charset=UTF-8"})
        uri = r.headers.get("Location")
        if not uri:
            raise TransientError("YouTube did not open an upload session; trying again later")
        state.update(upload_uri=uri, upload_size=size, upload_offset=0, upload_opened=self.clock(),
                     upload_sessions=state.get("upload_sessions", 0) + 1, title=title, description=description)

    def _query(self, state: dict, size: int) -> tuple[int | None, dict | None]:
        """Ask the session how much YouTube has: (next offset, None), (None, video) when it already finished, or
        (None, None) when the session is no longer usable (forgotten from `state`)."""
        r = self._send("PUT", state["upload_uri"], what="upload check", data=b"", ok=(308, 400, 403, 404, 410),
                       allow_redirects=False, headers={"Content-Length": "0", "Content-Range": f"bytes */{size}"})
        if r.status_code in (200, 201):
            return None, self._json(r)
        if r.status_code == 308:
            return _range_end(r) + 1, None
        if r.status_code == 403 and _reason(r)[0] in _QUOTA | _RATE:
            raise self._error(r, "upload check")
        self._forget_session(state)             # expired, or no longer accepted: a new session starts over
        return None, None

    @staticmethod
    def _json(r) -> dict:
        try:
            return r.json()
        except ValueError:
            raise TransientError("YouTube finished the upload but its answer was cut off; checking again later") \
                from None

    @staticmethod
    def _forget_session(state: dict) -> None:
        for k in ("upload_uri", "upload_offset", "upload_opened"):
            state.pop(k, None)

    def _finish(self, state, video: dict, privacy: str, size: int, progress, changed) -> dict:
        vid = video.get("id")
        if not vid:
            raise TransientError("YouTube finished the upload but sent no video id; checking again later")
        state["video_id"] = vid
        state["video_url"] = watch_url(vid)
        state["privacy"] = (video.get("status") or {}).get("privacyStatus") or privacy
        self._forget_session(state)
        changed()
        if progress:
            progress(size, size)
        return self._result(state, privacy)

    @staticmethod
    def _result(state: dict, privacy: str) -> dict:
        actual = state.get("privacy") or privacy
        return {"video_id": state["video_id"], "url": watch_url(state["video_id"]), "privacy": actual,
                "locked_private": actual == "private" and privacy != "private"}


def ensure_playlist(client: YouTubeClient, title: str, description: str = "", privacy: str = "unlisted") -> dict:
    """The channel's playlist with this title, made if there is none (the setup wizard calls this once)."""
    return client.find_playlist(title) or client.create_playlist(title, description, privacy)


# ---------------------------------------------------------------------------------------------------------------------
# Destination

class YouTubeDestination:
    """Destination "youtube": upload the clip, add it to the event playlist, and keep the description current. Every
    step is recorded in job.state and skipped on a retry once done, so sending a job again never uploads twice or
    lists a video twice. A job whose description has changed since upload (TBA posted the score) is brought up to
    date when sent again."""

    name = "youtube"

    def __init__(self, client: YouTubeClient, playlist_id: str | None, title_format: str, description_format: str,
                 privacy: str = "unlisted", *, on_state=None, progress=None, test_in_playlist: bool = True,
                 playoff_alliances: int = 8, max_playlist_tries: int = 10):
        """`on_state(job)`: save job.state now (the outbox should pass this so a restart mid-upload resumes the
        session instead of starting over). `progress(job, sent, total)`: optional. `test_in_playlist`: the wizard's
        test upload goes into the playlist too, proving the whole path; the wizard deletes it afterwards.
        `playoff_alliances`: 8, or 4 for an event with a 4-alliance bracket (sets the "(R2)" in playoff titles)."""
        try:
            # fields only: a long title is cut to YouTube's limit at upload, which is no reason to stop publishing
            templates.fields_used(title_format)
            templates.fields_used(description_format)
        except templates.TemplateError as e:
            raise PermanentError(f"the YouTube title or description format is not usable ({e}); fix it with "
                                 "streamsplitter setup --section youtube") from None
        if privacy not in PRIVACY:
            raise PermanentError(f"YouTube privacy must be one of {', '.join(PRIVACY)}, not {privacy!r}")
        self.client = client
        self.playlist_id = playlist_id
        self.title_format = title_format
        self.description_format = description_format
        self.privacy = privacy
        self.on_state = on_state
        self.progress = progress
        self.test_in_playlist = test_in_playlist
        self.playoff_alliances = playoff_alliances
        self.max_playlist_tries = max_playlist_tries

    def render(self, job: Job) -> tuple[str, str]:
        """(title, description) for the job, as YouTube will get them."""
        if job.kind == "test":
            return TEST_TITLE, TEST_DESCRIPTION
        title = templates.render(self.title_format, job.match, self.playoff_alliances)
        description = templates.render(self.description_format, job.match, self.playoff_alliances)
        return clean_title(title, job.match.name or "Match video"), clean_description(description)

    def _saved(self, job: Job) -> None:
        if self.on_state:
            self.on_state(job)

    def send(self, job: Job) -> dict:
        st = job.state
        title, description = self.render(job)
        progress = (lambda sent, total: self.progress(job, sent, total)) if self.progress else None
        res = self.client.upload(job.path, title, description, self.privacy, st, progress=progress,
                                 on_state=lambda: self._saved(job))
        vid = res["video_id"]
        if self.playlist_id and (job.kind != "test" or self.test_in_playlist) and not st.get("playlist_done"):
            self._add_to_playlist(job, vid)
        if st.get("description") is not None and (st.get("title"), st["description"]) != (title, description):
            # scores arrived (or the match was renamed) since the video went up; no record = nothing to compare
            self.client.set_description(vid, title, description)
            st["title"], st["description"] = title, description
            self._saved(job)
        out = {**res, "playlist_id": self.playlist_id if st.get("playlist_done") else None}
        if job.kind == "test":
            out["test"] = True
        return out

    def _add_to_playlist(self, job: Job, vid: str) -> None:
        st = job.state
        tries = st.get("playlist_tries", 0)
        # an earlier add may have landed with its answer lost: look before adding again (1 unit against 50)
        if not (tries and self.client.playlist_has(self.playlist_id, vid)):
            st["playlist_tries"] = tries + 1
            self._saved(job)
            try:
                self.client.add_to_playlist(self.playlist_id, vid)
            except NotListedYet:
                if tries + 1 >= self.max_playlist_tries:
                    raise PermanentError(f"YouTube no longer lists video {vid} (deleted?), so it cannot go in the "
                                         "playlist; upload it again by hand if it is wanted") from None
                raise
        st["playlist_done"] = True
        st["playlist_id"] = self.playlist_id
        self._saved(job)
