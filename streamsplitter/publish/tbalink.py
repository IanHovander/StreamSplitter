"""The Blue Alliance: each YouTube video's link added to its match on TBA. Off by default; for event organizers.

TBA's Trusted (write) API v1, read from TBA's source on GitHub on 2026-11-05 (the docs pages refused automated reads):
src/backend/api/trusted_api_main.py, handlers/trusted.py, trusted_api_auth_helper.py and
api_trusted_parsers/json_match_video_parser.py.

    POST /api/trusted/v1/event/<event key>/match_videos/add      body {"qm6": "<11-character YouTube id>"}

The body maps a partial match key (qm6, sf3m1, f1m2) to one YouTube id. It is signed with two headers: X-TBA-Auth-Id
(the key's id) and X-TBA-Auth-Sig, the MD5 hex of secret + request path (/api/trusted/v1/..., no host) + the body
exactly as sent. The key needs the match-video permission for the event. TBA gives write keys to event organizers for
their events; a match-video key can also cover this year's offseason events whose webcast channel is listed for it.
Answers: 200 {"Success": ...}; 401 for a wrong key or an event the key does not cover; 404 for a match TBA does not
have yet (playoff matches appear once scheduled, so the link is tried again for LINK_FOR_S); 400 for bad input.
.../match_videos/delete (same body) removes a link; StreamSplitter never calls it.

Why it is off by default (user, 2026-11-05): uploading an event's footage to YouTube before its organizer can take away
the organizer's control of how the footage is used (YouTube's copy detection may then flag the organizer's own
uploads), and a TBA link makes that copy the one everyone finds. Only the event's organizers, or someone with their
written OK, should turn it on (WARNING, shown by the setup).

Without a write key (read from TBA's source on 2026-11-10: suggestion_submission.py, playlist.html,
youtube_video_helper.py): anyone logged in to TBA can suggest a whole YouTube playlist on SUGGEST_URL; a TBA
moderator reviews it. That page fills in each video's match from its title with guess_partial below, so a title that
ends in the match's TBA id ("... (qm12)", templates.TBA_TITLE_FORMAT) needs no typing. Write keys are requested on
REQUEST_URL (offseason events, by the people who run the event or its scoring table); TBA answers one request with
several matches by adding none of them when it lacks any one (404), so tbavideos.py sends one match per request.
"""

from __future__ import annotations

import hashlib
import json
import re
import time

import requests

from .base import Job, PermanentError, TransientError

BASE = "https://www.thebluealliance.com"
PREFIX = "/api/trusted/v1"
TIMEOUT = (15, 60)                  # (connect, read) seconds
LINK_FOR_S = 12 * 3600.0            # a match TBA does not have yet is tried again for this long ...
NOT_YET_EVERY_S = 600.0             # ... this often
REQUEST_URL = "https://www.thebluealliance.com/request/apiwrite"       # log in first; /request/apikey is a 404
SUGGEST_URL = "https://www.thebluealliance.com/suggest/event/video?event_key={event}"
SETUP_AGAIN = "streamsplitter setup --section tbalinks"

WARNING = ("Only turn this on if you run the event, or the event's organizers said yes in writing. Uploading an "
           "event's footage to YouTube before its organizers do can take away their control of how it is used (for "
           "example, YouTube's copy detection may then flag their own uploads), and a link on The Blue Alliance makes "
           "your copy the one everyone finds. Not recommended otherwise.")

_PARTIAL = re.compile(r"^(qm\d+|(?:ef|qf|sf|f)\d+m\d+)$")
_VIDEO_ID = re.compile(r"^[0-9A-Za-z_-]{11}$")


def partial_key(match_key: str | None, event_key: str | None) -> str | None:
    """"qm6" from "2026mttd_qm6" (likewise sf3m1, f1m2); None for a key TBA has no match page for (practice, an
    unnamed clip, another event's key)."""
    if not match_key or not event_key or not match_key.startswith(f"{event_key}_"):
        return None
    part = match_key[len(event_key) + 1:]
    return part if _PARTIAL.match(part) else None


def suggest_url(event_key: str) -> str:
    """TBA's page that takes a YouTube playlist as video suggestions for an event (log in first)."""
    return SUGGEST_URL.format(event=event_key)


def guess_partial(title: str) -> str:
    """The match TBA's playlist page reads from a video's title ("qm12", "sf3m1"; "" when it finds none, and the
    person must type it): a copy of guessMatchPartial in TBA's youtube_video_helper.py."""
    qual = re.search(r"[^\w\d]*(q|qm)(\d+).*", title or "", re.IGNORECASE)
    if qual is not None:
        return f"qm{qual.group(2)}"
    elim = re.search(r"[^\w\d]*(ef\d+m\d+|qf\d+m\d+|sf\d+m\d+|f\d+m\d+).*", title or "", re.IGNORECASE)
    return elim.group(1).lower() if elim is not None else ""


def signature(secret: str, path: str, body: str) -> str:
    """X-TBA-Auth-Sig: md5(secret + path + body), hex."""
    return hashlib.md5(f"{secret}{path}{body}".encode("utf-8")).hexdigest()


def match_url(match_key: str) -> str:
    return f"{BASE}/match/{match_key}"


class NotOnTBA(TransientError):
    """TBA has no such match yet (404): a playoff match is added once it is scheduled."""

    def __init__(self, message: str):
        super().__init__(message, retry_after=NOT_YET_EVERY_S)


class Refused(PermanentError):
    """TBA refused the request as bad input (400): the key itself was accepted."""


class TrustedClient:
    """The one Trusted API call StreamSplitter makes. Never logs or shows the secret."""

    def __init__(self, auth_id: str, auth_secret: str, *, session=None, base: str = BASE):
        if not auth_id or not auth_secret:
            raise PermanentError(f"no TBA write key is set; add it with {SETUP_AGAIN}")
        self.auth_id, self._secret = str(auth_id).strip(), str(auth_secret).strip()
        self.session = session or requests.Session()
        self.base = base.rstrip("/")

    def add_videos(self, event_key: str, videos: dict[str, str]) -> dict:
        path = f"{PREFIX}/event/{event_key}/match_videos/add"
        body = json.dumps(videos, separators=(",", ":"))
        headers = {"X-TBA-Auth-Id": self.auth_id, "X-TBA-Auth-Sig": signature(self._secret, path, body),
                   "Content-Type": "application/json", "User-Agent": "streamsplitter"}
        try:
            r = self.session.post(self.base + path, data=body.encode("utf-8"), headers=headers, timeout=TIMEOUT)
        except requests.exceptions.RequestException as e:
            raise TransientError(f"The Blue Alliance: no connection ({type(e).__name__})") from None
        if r.status_code == 200:
            try:
                data = r.json()
            except ValueError:
                data = {}
            return data if isinstance(data, dict) else {}
        msg = _message(r)
        if r.status_code == 401:
            raise PermanentError(f"The Blue Alliance refused the write key{': ' + msg if msg else ''}. Check its ID "
                                 f"and secret, and that it has match-video access to {event_key} ({SETUP_AGAIN})")
        if r.status_code == 404:
            raise NotOnTBA(f"The Blue Alliance has no such match yet{': ' + msg if msg else ''}")
        if r.status_code == 429 or r.status_code >= 500:
            raise TransientError(f"The Blue Alliance: HTTP {r.status_code}", retry_after=_retry_after(r))
        if r.status_code == 400:
            raise Refused(f"The Blue Alliance refused the video link (HTTP 400){': ' + msg if msg else ''}")
        raise PermanentError(f"The Blue Alliance refused the video link (HTTP {r.status_code}){': ' + msg if msg else ''}")

    def check(self, event_key: str) -> str | None:
        """None when the key may add videos to the event, else why not. Sends an empty list of videos, which changes
        nothing: the key and the event are checked before the body is read."""
        try:
            self.add_videos(event_key, {})
        except Refused:
            return None                         # past the key check; TBA only disliked the empty list
        except PermanentError as e:
            return str(e)
        except TransientError as e:
            return f"could not reach The Blue Alliance ({e})"
        return None


def _message(r) -> str:
    try:
        data = r.json()
    except ValueError:
        return ""
    if isinstance(data, dict):
        for k in ("Error", "error", "message", "Success"):
            if data.get(k):
                return str(data[k])[:200]
    return ""


def _retry_after(r) -> float | None:
    try:
        return max(0.0, float(r.headers.get("Retry-After")))
    except (TypeError, ValueError):
        return None


class TBALinkDestination:
    """Destination "tba": a "link" job (queued by the Publisher once YouTube has the video) adds the video's id to its
    match on TBA. A match TBA does not list yet is tried again until the job's link_until; adding the same link twice
    does no harm."""

    name = "tba"

    def __init__(self, client: TrustedClient, *, clock=time.time, log=None):
        self.client = client
        self.clock = clock
        self.log = log or print

    def send(self, job: Job) -> dict:
        if job.kind != "link":
            raise PermanentError(f"StreamSplitter does not know how to send a {job.kind!r} job to The Blue Alliance")
        st, m = job.state, job.match
        vid = st.get("video_id")
        if not vid or not _VIDEO_ID.match(str(vid)):
            raise PermanentError(f"{m.name}: no YouTube video id to put on The Blue Alliance")
        part = partial_key(m.key, m.event_key)
        if part is None:
            return {"skipped": "The Blue Alliance has no match page for this clip"}
        key = f"{m.event_key}_{part}"
        if not st.get("linked"):
            try:
                self.client.add_videos(m.event_key, {part: str(vid)})
            except NotOnTBA:
                if self.clock() >= float(st.get("link_until") or 0):
                    raise PermanentError(f"The Blue Alliance still has no match {key}; its video was not linked") \
                        from None
                raise
            st["linked"] = key
        return {"linked": key, "video_id": vid, "url": match_url(key)}
