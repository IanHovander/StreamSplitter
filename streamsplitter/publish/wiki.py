"""Team wiki (MediaWiki): every match video uploaded as a file to the team's own wiki, on one members-only page per
event with one == section == per match, so the wiki's contents list jumps to each match (user, 2026-11-04: every
match, the team's own and others, at offseason and official FIRST events alike).

Plain HTTPS to the standard Action API (api.php) with requests; no MediaWiki library is needed for these calls:

    login    action=query&meta=tokens&type=login, then action=login with a bot password (lgname "User@botname"; a
             person makes it at Special:BotPasswords and types it into the setup), then meta=tokens for the CSRF
             token every write carries. Writes also send assert=user, so a session that lapsed fails
             ("assertuserfailed") instead of editing anonymously; that and "badtoken" log in again once.
    upload   chunked, because Cloudflare (in front of team5026.com, free plan) refuses request bodies over 100 MB:
             action=upload&stash=1 with filesize, offset and one ~10 MB piece (multipart "chunk"), and the filekey of
             the first answer with every later piece. The last piece makes the wiki put the file together ("Success",
             with a new filekey); one more action=upload with filekey + filename + comment + text publishes it.
    pages    action=edit&createonly=1 makes the event's page once ("articleexists" is fine: someone made it first);
             action=edit&section=new&sectiontitle=<match>&nocreate=1 adds each match at its end, which never touches
             what people wrote on the page (nocreate: a page deleted meanwhile is made again with {{Private}}, never
             by a bare section edit).

Resuming. job.state keeps the stash's filekey and offset; the outbox saves it after every piece through
WikiDestination's on_state hook (as for YouTube). The next attempt, also after a restart, asks the wiki where the
stash got to (action=upload&checkstatus=1): "Continue" goes on from the wiki's offset (it wins over ours: a piece whose
answer was lost may have arrived), "Success" goes straight to publishing, and a filekey the wiki no longer knows
(stashes expire after hours; "missingresult", "stashnosuchfilekey", ...) starts over from byte 0 - unless the user's
stash (list=mystashedfiles) holds the assembled file, which is what a lost answer to the last piece leaves (the wiki
gives the assembled file a new filekey and forgets the old one). A piece refused with "stashfailed" and the wiki's
own offset continues from that offset.

Idempotency. Before uploading, and before publishing a stash that may have been published already, the clip's SHA-1
is compared with the wiki's file of that name: the same bytes already there count as done. Publishing goes without
ignorewarnings, so the wiki's warnings decide: the same content under another name ("duplicate") is used instead of
a second copy; a different file of the same name uploaded by a person ("exists") is never overwritten
(PermanentError), one uploaded by this bot account (our own earlier cut of the same match) gets a new version, and
the harmless warnings (was-deleted, duplicate-archive, large-file, ...) are passed with ignorewarnings. A match's
section is added only when the page's wikitext does not name its file yet, so a retry after a lost answer adds
nothing; the new revision id is kept in job.state too.

Pacing and errors. Cloudflare allows 300 requests per 10 s per address; requests here are at least MIN_INTERVAL_S
apart (2-3 a second). No network, timeouts, HTTP 429/5xx, maxlag, ratelimited and read-only mode are TransientError
(the outbox tries again later); a refused login, missing rights (the bot password's grants), uploads switched off,
MP4 not allowed, a file too large for the wiki or Cloudflare's browser check are PermanentError with the fix.

The video markup and the page title are formats in config.toml ([wiki] embed_format with {file} and {title},
page_format with {event_name}, {year}, {event_key}), filled in by plain replacement so wiki markup with braces
({{...}}) can be typed as is: team5026's wiki is adding in-page MP4 playback and a members-only file gate
(2026-11-04), so its final markup is not known yet.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import urllib.parse

import requests

from . import templates
from .base import Job, Match, PermanentError, TransientError

CHUNK_SIZE = 10 * 1024 * 1024       # well under Cloudflare's 100 MB body limit; PHP and the web server must allow it
MIN_CHUNK = 1024 * 1024             # a piece refused as too large is halved down to this
MIN_INTERVAL_S = 0.4                # between requests: Cloudflare's free plan allows 300 per 10 s per address
API_TIMEOUT = (15, 60)              # (connect, read) seconds
CHUNK_TIMEOUT = (15, 300)           # a 10 MB piece on a slow venue uplink; the last one also waits for the assembly
MAXLAG_S = 5                        # writes wait while the wiki's database replicas lag more than this

PAGE_FORMAT = "{event_name} ({year}) match videos"      # team5026's event pages are "Name (year)"
EMBED_FORMAT = "[[File:{file}|800px]]"                  # a stock MediaWiki with TimedMediaHandler plays this
PRIVATE_MARKUP = "{{Private}}"                          # team5026's members-only template
INTRO = "Match videos from the {event}, recorded from the event webcast by StreamSplitter: one section per match."
TEST_PAGE = "StreamSplitter setup test"
TEST_FILE = "StreamSplitter_setup_test.mp4"
BOT_NAME = "StreamSplitter"                             # the bot name the setup asks for at Special:BotPasswords
# the grants (Special:BotPasswords check boxes) the setup asks for, and the rights that must come out of them
GRANTS = ("Edit existing pages", "Create, edit, and move pages", "Upload new files", "Upload, replace, and move files")
NEEDED_RIGHTS = {"edit": "Edit existing pages", "createpage": "Create, edit, and move pages",
                 "upload": "Upload new files"}
PAGE_FIELDS = ("event_name", "year", "event_key")
EMBED_FIELDS = ("file", "title")
SETUP_AGAIN = "streamsplitter setup --section wiki"
WIKI_DOC = "docs/setup-wiki.md"

try:
    from .. import __version__ as _VERSION
except Exception:  # noqa: BLE001 - a version in the User-Agent is a courtesy only
    _VERSION = "0"
USER_AGENT = f"StreamSplitter/{_VERSION} (FRC match video uploader; python-requests)"

_BAD_TITLE = re.compile(r"[#<>\[\]|{}]")       # characters MediaWiki never allows in a page title
_RELOGIN = {"badtoken", "assertuserfailed", "notloggedin", "assertnameduserfailed"}
_TRANSIENT = {"maxlag": 5.0, "ratelimited": 60.0, "readonly": 300.0, "editconflict": 30.0, "stashfilestorage": 60.0,
              "backend-fail-internal": 60.0, "lockmanager-fail-acquirelock": 30.0, "fileexists-concurrent": 30.0,
              "databaseerror": 60.0}
_STASH_GONE = {"missingresult", "stashnosuchfilekey", "stashedfilenotfound", "stashpathinvalid", "stashwrongowner",
               "stashzerolength", "stashnotloggedin", "invalid-file-key"}
_TOO_BIG = {"http413", "file-too-large"}

_CF_FIX = ("Cloudflare in front of the wiki asks StreamSplitter for a browser check, which a program cannot pass: the "
           "wiki's admin must let api.php through (Cloudflare: Security > WAF, a rule that skips the checks for "
           f"/api.php, or Bot Fight Mode off; {WIKI_DOC})")
_GRANTS_FIX = ("the bot password lacks a permission it needs: on the wiki open Special:BotPasswords, click "
               f"{BOT_NAME}, tick {', '.join(GRANTS)}, and click Update (if they are ticked already, the account "
               "itself may not do this: ask the wiki admin)")
_MP4_FIX = f"the wiki does not accept .mp4 files: its admin must add 'mp4' to $wgFileExtensions ({WIKI_DOC})"
_PROTECTED = f"the page is protected: ask a wiki admin, or pick another page title with {SETUP_AGAIN}"
_FIXES = {
    "permissiondenied": _GRANTS_FIX,
    "mustbeloggedin": f"the wiki does not accept the bot login for this; {SETUP_AGAIN}",
    "assertbotfailed": "the wiki account is not in the wiki's bot group: ask the wiki admin to add it",
    "blocked": "the bot's wiki account is blocked: ask a wiki admin to unblock it",
    "autoblocked": "the bot's wiki account or address is blocked: ask a wiki admin to lift the block",
    "uploaddisabled": f"uploads are switched off on the wiki: its admin must set $wgEnableUploads = true ({WIKI_DOC})",
    "filetype-banned": _MP4_FIX,
    "filetype-unwanted-type": _MP4_FIX,
    "verification-error": f"the wiki could not check the video as an MP4: its admin must allow video/mp4 uploads "
                          f"({WIKI_DOC})",
    "protectedpage": _PROTECTED,
    "cascadeprotected": _PROTECTED,
    "protectedtitle": _PROTECTED,
    "protectednamespace": _PROTECTED,
    "invalidtitle": f"the page title is not allowed on the wiki; pick another with {SETUP_AGAIN}",
    "spamblacklist": "the wiki's spam filter blocked the edit: ask the wiki admin",
    "abusefilter-disallowed": "the wiki's abuse filter blocked the edit: ask the wiki admin",
    "readapidenied": "the bot's wiki account may not read the wiki: ask the wiki admin to give it member access",
    "fileexists-forbidden": "the wiki does not let the bot account replace that file: ask the wiki admin",
    "fileexists-shared-forbidden": "a file of that name is in a shared file store the wiki uses: ask the wiki admin",
}


class ApiError(Exception):
    """An error answer from api.php: `code` ("badtoken", "stashfailed", ...), `info` (the wiki's words) and `data`
    (the whole error object, e.g. a stash's "offset"). WikiClient's methods turn the ones they do not expect into
    TransientError / PermanentError (WikiClient._mapped); the caller never sees this class."""

    def __init__(self, code: str, info: str = "", data: dict | None = None, retry_after: float | None = None):
        super().__init__(f"{code}: {info}" if info else code)
        self.code, self.info, self.data, self.retry_after = code, info, data or {}, retry_after


class PageMissing(Exception):
    """A section edit with nocreate found no page (deleted since it was made): make it again, then add the section."""


# ---------------------------------------------------------------------------------------------------------------------
# Formats and texts

def render_page_title(fmt: str, event_name: str | None, year, event_key: str | None) -> str:
    """The event's page title from `fmt` (plain replacement of {event_name}, {year}, {event_key}); characters a
    title cannot hold are dropped, and an empty "()" (no year known) too."""
    t = (fmt or PAGE_FORMAT).replace("{event_name}", event_name or event_key or "")
    t = t.replace("{year}", str(year or "")).replace("{event_key}", event_key or "")
    t = re.sub(r"\(\s*\)", "", t)
    t = " ".join(_BAD_TITLE.sub("", t).split())
    return t or "StreamSplitter match videos"


def title_problem(title: str) -> str | None:
    """What makes `title` unusable as a wiki page title, or None."""
    t = (title or "").strip()
    if not t:
        return "type a page title"
    if _BAD_TITLE.search(t):
        return "a wiki page title cannot contain # < > [ ] | { }"
    if len(t.encode("utf-8")) > 255:
        return "that title is too long for the wiki (255 bytes at most)"
    if t.startswith(":") or t.lower().startswith(("special:", "file:", "image:", "media:")):
        return "pick an ordinary page title (not a Special: or File: page)"
    return None


def page_format_problem(fmt: str) -> str | None:
    unknown = [f for f in re.findall(r"\{(\w*)\}", fmt or "") if f not in PAGE_FIELDS]
    if unknown:
        return f"unknown field {{{unknown[0]}}}; the fields are " + ", ".join(f"{{{f}}}" for f in PAGE_FIELDS)
    return title_problem(render_page_title(fmt, "Madtown Throwdown", 2026, "2026mttd"))


def embed_problem(fmt: str) -> str | None:
    """What makes the video markup unusable, or None: it must name the file ({file})."""
    if not (fmt or "").strip():
        return "the video markup is empty"
    if "{file}" not in fmt:
        return "the video markup must contain {file} where the file's name goes, e.g. " + EMBED_FORMAT
    return None


def render_embed(fmt: str, file: str, title: str) -> str:
    return fmt.replace("{file}", file).replace("{title}", title)


def event_text(m: Match) -> str:
    """"2026 Madtown Throwdown" (year and TBA's name), or what is known of it."""
    name = m.event_name or m.event_key or ""
    year = str(m.year) if m.year else ""
    return f"{year} {name}".strip() if name and year and not name.startswith(year) else name or year


def teams_line(m: Match) -> str:
    """"Red 1323, 6619, 254B - 81 | Blue 5026, 5499, 114 - 63": team numbers only (never names), the scores once TBA
    has both; "" when the teams are not known (no schedule)."""
    red = [str(t).removeprefix("frc") for t in m.red or [] if t]
    blue = [str(t).removeprefix("frc") for t in m.blue or [] if t]
    if not red and not blue:
        return ""
    scored = (m.red_score is not None and m.blue_score is not None and m.red_score >= 0 and m.blue_score >= 0)
    r = "Red " + (", ".join(red) or "?") + (f" - {m.red_score}" if scored else "")
    b = "Blue " + (", ".join(blue) or "?") + (f" - {m.blue_score}" if scored else "")
    return f"{r} | {b}"


def names_file(text: str, file: str) -> bool:
    """Whether wikitext names the file (spaces and underscores alike, any case): the section for it is there. The
    extension keeps qm1.mp4 from matching qm12.mp4."""
    norm = lambda s: (s or "").replace("_", " ").casefold()      # noqa: E731
    return bool(file) and norm(file) in norm(text)


def event_page_candidates(titles: list[str], event_name: str | None, year) -> list[str]:
    """Wiki pages that look like the event's own page ("Madtown Throwdown (2026)" for TBA's "MadTown Throwdown",
    2026), best first; other years' pages and match-video pages are left out."""
    def norm(s):
        return re.sub(r"[^0-9a-z]+", "", (s or "").casefold())
    want = norm(event_name)
    words = [w for w in re.findall(r"[0-9a-z]+", (event_name or "").casefold()) if len(w) > 2]
    scored = []
    for t in titles:
        low = t.casefold()
        if low.endswith(" match videos") or ":" in t:
            continue
        hit = re.match(r"^(.*?)\s*\((\d{4})\)\s*$", t)
        base = hit.group(1) if hit else t
        score = 3 if want and norm(base) == want else 1 if words and all(w in low for w in words) else 0
        if not score:
            continue                            # another event's page
        if hit and year:
            score += 2 if int(hit.group(2)) == int(year) else -2
        if score >= 2:
            scored.append((-score, len(scored), t))
    return [t for *_, t in sorted(scored)]


def wiki_event_name(event_page: str | None) -> str | None:
    """The wiki's own spelling of the event from its page title: "Madtown Throwdown (2026)" -> "Madtown Throwdown"."""
    if not event_page:
        return None
    return re.sub(r"\s*\(\d{4}\)\s*$", "", event_page).strip() or None


def api_candidates(text: str) -> list[str]:
    """api.php addresses to try for what a person typed: the address itself when it ends in api.php, else the wiki's
    usual places (<site>/api.php, <site>/w/api.php, beside an index.php in the address)."""
    t = (text or "").strip().strip("\"'")
    if not t:
        return []
    if not re.match(r"^https?://", t, re.I):
        t = "https://" + t
    u = urllib.parse.urlsplit(t)
    base = f"{u.scheme.lower()}://{u.netloc}"
    path = u.path or "/"
    out = []
    if path.endswith("api.php"):
        out.append(base + path)
    elif "index.php" in path:
        out.append(base + path.split("index.php", 1)[0] + "api.php")
    out += [base + "/api.php", base + "/w/api.php"]
    seen = []
    for x in out:
        if x not in seen:
            seen.append(x)
    return seen


def missing_rights(rights) -> list[str]:
    """The grants (as Special:BotPasswords names them) whose rights the login lacks."""
    have = set(rights or ())
    return [label for right, label in NEEDED_RIGHTS.items() if right not in have]


def _text(v) -> str:
    if isinstance(v, dict):
        return str(v.get("text") or v.get("*") or v.get("code") or "")
    return str(v or "")


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _retry_after(resp) -> float | None:
    try:
        return max(0.0, float(resp.headers.get("Retry-After")))
    except (TypeError, ValueError):
        return None


def _sha1(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _ii(ii) -> dict:
    ii = ii if isinstance(ii, dict) else {}
    return {k: ii.get(k) for k in ("sha1", "size", "user", "url", "descriptionurl")}


# ---------------------------------------------------------------------------------------------------------------------
# Client

class WikiClient:
    """The MediaWiki calls StreamSplitter needs, each mapped to TransientError / PermanentError. Logs in when a call
    needs it (and again once when the session lapsed). The password is never logged, shown or put in an error."""

    def __init__(self, api_url: str, username: str | None = None, password: str | None = None, *, session=None,
                 chunk_size: int = CHUNK_SIZE, min_interval: float = MIN_INTERVAL_S, clock=time.monotonic,
                 sleep=time.sleep):
        self.api_url = (api_url or "").strip()
        self.username = username
        self._password = password
        self.session = session or requests.Session()
        self.chunk_size = int(chunk_size)
        self.min_interval = float(min_interval)
        self.clock, self.sleep = clock, sleep
        self.user_name: str | None = None       # the account the wiki logged in ("Ian" for Ian@StreamSplitter)
        self._csrf: str | None = None
        self._site: dict | None = None
        self._lock = threading.RLock()
        self._next_at = 0.0

    def __repr__(self) -> str:
        return f"WikiClient({self.api_url!r}, {self.username!r})"

    # -- plumbing ------------------------------------------------------------------------------------------------------

    def _pace(self) -> None:
        with self._lock:
            now = self.clock()
            if self._next_at > now:
                self.sleep(self._next_at - now)
                now = self._next_at
            self._next_at = now + self.min_interval

    def _request(self, method: str, params: dict, *, files=None, what: str, timeout=API_TIMEOUT) -> dict:
        """One HTTP request; the wiki's JSON answer, or the mapped error. An API error answer is raised as ApiError
        for the caller to handle or map."""
        self._pace()
        headers = {"User-Agent": USER_AGENT}
        try:
            if method == "GET":
                resp = self.session.get(self.api_url, params=params, headers=headers, timeout=timeout)
            else:
                resp = self.session.post(self.api_url, data=params, files=files, headers=headers, timeout=timeout)
        except requests.exceptions.RequestException as e:
            raise TransientError(f"wiki {what}: no connection ({type(e).__name__})") from None
        status = resp.status_code
        if str(resp.headers.get("cf-mitigated") or "").lower() == "challenge":
            raise PermanentError(f"wiki {what}: {_CF_FIX}")
        if status == 413:
            raise ApiError("http413", "the web server refused a request this large")
        if status == 429 or status >= 500:
            wait = _retry_after(resp)
            raise TransientError(f"wiki {what}: HTTP {status}" + (" (too many requests)" if status == 429 else ""),
                                 retry_after=wait if wait is not None else (60.0 if status == 429 else None))
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            if status in (401, 403):
                raise PermanentError(f"wiki {what}: the web server refused StreamSplitter (HTTP {status}) before "
                                     f"the wiki saw the request; if Cloudflare protects the wiki, {_CF_FIX}")
            if status == 404:
                raise PermanentError(f"wiki {what}: there is no api.php at {self.api_url} (HTTP 404); fix the "
                                     f"address with {SETUP_AGAIN}")
            raise TransientError(f"wiki {what}: the answer did not come from the wiki (HTTP {status}; a Wi-Fi "
                                 "login page?)")
        err = data.get("error")
        if isinstance(err, dict):
            raise ApiError(str(err.get("code") or "unknown"), _text(err.get("info") or err.get("*")), err,
                           _retry_after(resp))
        if status >= 400:
            raise PermanentError(f"wiki {what}: HTTP {status}")
        return data

    def _call(self, params: dict, *, what: str, write: bool = False, post: bool = False, files=None,
              timeout=API_TIMEOUT) -> dict:
        """One API call. A write carries the CSRF token (in the POST body, as MediaWiki requires) and assert=user;
        a lapsed session (badtoken, assertuserfailed) logs in again and tries once more. Raises ApiError."""
        for attempt in (1, 2):
            p = {"format": "json", "formatversion": "2", **params}
            if write:
                p["token"] = self._token(refresh=attempt == 2)
                p["assert"] = "user"
                p["maxlag"] = MAXLAG_S
            p = {k: str(v) for k, v in p.items() if v is not None}
            try:
                return self._request("POST" if write or post else "GET", p, files=files, what=what,
                                     timeout=timeout)
            except ApiError as e:
                if write and attempt == 1 and e.code in _RELOGIN:
                    self._forget_login()
                    continue
                raise
        raise AssertionError("unreachable")

    def _mapped(self, e: ApiError, what: str) -> Exception:
        """TransientError / PermanentError for an API error the calling method does not handle itself."""
        code = e.code or "unknown"
        if code in _TRANSIENT or code.startswith("internal_api_error"):
            wait = e.retry_after if e.retry_after is not None else _TRANSIENT.get(code, 60.0)
            return TransientError(f"wiki {what}: the wiki asks to wait ({code})", retry_after=wait)
        if code in _RELOGIN:          # still after logging in again
            return TransientError(f"wiki {what}: the wiki keeps losing the login ({code}); trying again later",
                                  retry_after=300.0)
        if code in _TOO_BIG:
            return PermanentError(f"wiki {what}: {self._too_big_text(None)}")
        if code in _FIXES:
            return PermanentError(f"wiki {what}: {_FIXES[code]}")
        info = f": {e.info}" if e.info else ""
        return PermanentError(f"the wiki refused the {what} ({code}{info})")

    @staticmethod
    def _too_big_text(size: int | None) -> str:
        clip = f" of the {size / 1e6:.0f} MB clip" if size else ""
        return (f"the wiki refuses even {MIN_CHUNK >> 20} MB pieces{clip} as too large: its admin must raise "
                "$wgMaxUploadSize above the clip size, and PHP's upload_max_filesize and post_max_size (and the web "
                f"server's request size limit) above {CHUNK_SIZE >> 20} MB ({WIKI_DOC})")

    # -- login -------------------------------------------------------------------------------------------------------

    def _forget_login(self) -> None:
        with self._lock:
            self.user_name, self._csrf = None, None

    def _tokens(self, kind: str) -> str | None:
        try:
            r = self._call({"action": "query", "meta": "tokens", "type": kind}, what="login")
        except ApiError as e:
            raise self._mapped(e, "login") from None
        return ((r.get("query") or {}).get("tokens") or {}).get(f"{kind}token")

    def login(self) -> dict:
        """Log in with the bot password: {"user": the account name}. PermanentError when the wiki refuses it."""
        if not (self.username and self._password):
            raise PermanentError(f"no wiki bot login is stored; {SETUP_AGAIN}")
        with self._lock:
            for attempt in (1, 2):
                token = self._tokens("login")
                try:
                    r = self._call({"action": "login", "lgname": self.username, "lgpassword": self._password,
                                    "lgtoken": token}, post=True, what="login")
                except ApiError as e:
                    raise self._mapped(e, "login") from None
                lg = r.get("login") or {}
                result = str(lg.get("result") or "")
                if result == "Success":
                    self.user_name = str(lg.get("lgusername") or self.username.split("@")[0])
                    self._csrf = None
                    return {"user": self.user_name}
                if result in ("NeedToken", "WrongToken"):
                    if attempt == 1:
                        continue            # the session cookie was lost between the two requests
                    raise TransientError("the wiki's login session does not stick (cookies?); trying again later",
                                         retry_after=300.0)
                reason = _text(lg.get("reason")) or result or "no reason given"
                low = reason.lower()
                if "too many" in low or "throttl" in low:
                    raise TransientError(f"the wiki paused logins after too many tries ({reason}); trying again "
                                         "later", retry_after=300.0)
                if result == "Aborted" or "main password" in low or "clientlogin" in low:
                    raise PermanentError(f"the wiki wants a bot password, not the account's own password ({reason}): "
                                         "make one at Special:BotPasswords; the user name then looks like "
                                         f"Name@{BOT_NAME} ({SETUP_AGAIN})")
                raise PermanentError(f"the wiki refused the bot login ({reason}): check the user name (like "
                                     f"Name@{BOT_NAME}) and make a new bot password at Special:BotPasswords; then "
                                     f"{SETUP_AGAIN}")
        raise AssertionError("unreachable")

    def _token(self, refresh: bool = False) -> str:
        """The CSRF token for writes, logging in first when needed."""
        with self._lock:
            if refresh:
                self._csrf = None
            if self._csrf is None:
                if self.user_name is None:
                    self.login()
                token = self._tokens("csrf")
                if not token or token == "+\\":         # "+\" is the anonymous token: the wiki did not keep the login
                    self.login()
                    token = self._tokens("csrf")
                    if not token or token == "+\\":
                        raise TransientError("the wiki forgets the login at once (cookies blocked?); trying again "
                                             "later", retry_after=300.0)
                self._csrf = token
            return self._csrf

    def ensure_login(self) -> None:
        self._token()

    # -- the wiki and the account --------------------------------------------------------------------------------------

    def site_info(self) -> dict:
        """What the setup checks before asking for a login: {"sitename", "generator", "server", "articlepath",
        "maxuploadsize", "fileextensions", "private"}. A wiki that lets only members read still answers (private)."""
        try:
            r = self._call({"action": "query", "meta": "siteinfo", "siprop": "general|fileextensions"},
                           what="address check")
        except ApiError as e:
            if e.code == "readapidenied":           # it is a MediaWiki api.php; it only lets members read
                self._site = {"sitename": "", "generator": "MediaWiki", "private": True, "fileextensions": []}
                return dict(self._site)
            raise self._mapped(e, "address check") from None
        q = r.get("query") or {}
        g = q.get("general") or {}
        if not g:
            raise PermanentError(f"{self.api_url} answered, but not like a MediaWiki api.php")
        exts = [str(x.get("ext") if isinstance(x, dict) else x).lower() for x in q.get("fileextensions") or []]
        self._site = {"sitename": g.get("sitename") or "", "generator": g.get("generator") or "",
                      "server": g.get("server") or "", "articlepath": g.get("articlepath") or "",
                      "maxuploadsize": _int(g.get("maxuploadsize")), "fileextensions": exts, "private": False}
        return dict(self._site)

    def page_url(self, title: str) -> str:
        """The page's address for people (from the wiki's article path; asked once)."""
        if self._site is None:
            try:
                self.site_info()
            except Exception:  # noqa: BLE001 - an address of the index.php form works too
                self._site = {}
        t = urllib.parse.quote(title.replace(" ", "_"), safe="/:()!,;@$*'")
        server, path = self._site.get("server") or "", self._site.get("articlepath") or ""
        if server and "$1" in path:
            if server.startswith("//"):
                server = "https:" + server
            return server + path.replace("$1", t)
        return f"{self.api_url.rsplit('/', 1)[0]}/index.php?title={t}"

    def user_info(self) -> dict:
        """The logged-in account: {"name", "rights", "groups"} (rights as the bot password's grants allow them)."""
        self.ensure_login()
        try:
            r = self._call({"action": "query", "meta": "userinfo", "uiprop": "rights|groups"}, what="account check")
        except ApiError as e:
            raise self._mapped(e, "account check") from None
        u = (r.get("query") or {}).get("userinfo") or {}
        return {"name": u.get("name"), "rights": list(u.get("rights") or []), "groups": list(u.get("groups") or []),
                "anon": bool(u.get("anon"))}

    # -- pages -------------------------------------------------------------------------------------------------------

    def page_text(self, title: str) -> str | None:
        """The page's wikitext, None when there is no such page."""
        self.ensure_login()
        try:
            r = self._call({"action": "query", "prop": "revisions", "titles": title, "rvprop": "content",
                            "rvslots": "main"}, what="page read")
        except ApiError as e:
            raise self._mapped(e, "page read") from None
        pages = (r.get("query") or {}).get("pages") or []
        if not pages:
            return None
        p = pages[0]
        if p.get("invalid"):
            raise PermanentError(f"wiki page read: '{title}' is not a valid page title on the wiki; pick another "
                                 f"with {SETUP_AGAIN}")
        if p.get("missing"):
            return None
        revs = p.get("revisions") or []
        if not revs:
            return ""
        slot = (revs[0].get("slots") or {}).get("main") or {}
        return str(slot.get("content") if "content" in slot else revs[0].get("content") or "")

    def sections(self, title: str) -> list[dict] | None:
        """The page's sections ({"line", "anchor", "index", "level"} each), None when there is no such page."""
        self.ensure_login()
        try:
            r = self._call({"action": "parse", "page": title, "prop": "sections"}, what="page read")
        except ApiError as e:
            if e.code == "missingtitle":
                return None
            raise self._mapped(e, "page read") from None
        return list((r.get("parse") or {}).get("sections") or [])

    @staticmethod
    def _edited(r: dict, what: str) -> dict:
        edit = r.get("edit") or {}
        if edit.get("result") == "Success":
            return edit
        if "captcha" in edit:
            raise PermanentError(f"wiki {what}: the wiki asks the bot for a CAPTCHA: its admin must give the account "
                                 "the skipcaptcha right (for example by putting it in the bot group)")
        why = ", ".join(k for k in edit if k not in ("result", "title", "pageid")) or edit.get("result") or "unknown"
        raise PermanentError(f"wiki {what}: the wiki did not save it ({why}); ask the wiki admin")

    def create_page(self, title: str, text: str, summary: str) -> bool:
        """Make the page unless it exists (createonly): True when made, False when it was there already."""
        try:
            r = self._call({"action": "edit", "title": title, "text": text, "summary": summary, "createonly": 1,
                            "bot": 1}, write=True, what="page creation")
        except ApiError as e:
            if e.code == "articleexists":
                return False
            raise self._mapped(e, "page creation") from None
        self._edited(r, "page creation")
        return True

    def add_section(self, title: str, heading: str, text: str, summary: str) -> dict:
        """Add "== heading ==" with `text` at the end of an existing page (nocreate): {"newrevid"}. PageMissing
        when the page is gone."""
        try:
            r = self._call({"action": "edit", "title": title, "section": "new", "sectiontitle": heading,
                            "text": text, "summary": summary, "nocreate": 1, "bot": 1}, write=True,
                           what="match section")
        except ApiError as e:
            if e.code in ("missingtitle", "nocreate-missing"):
                raise PageMissing(title) from None
            raise self._mapped(e, "match section") from None
        edit = self._edited(r, "match section")
        return {"newrevid": edit.get("newrevid")}

    def delete(self, title: str, reason: str) -> bool:
        """Delete a page or file page (the setup test, when the bot may); False when it is gone already."""
        try:
            self._call({"action": "delete", "title": title, "reason": reason}, write=True, what="delete")
        except ApiError as e:
            if e.code == "missingtitle":
                return False
            raise self._mapped(e, "delete") from None
        return True

    def search_pages(self, query: str, limit: int = 20) -> list[str]:
        """Page titles for `query`: the wiki's search (case does not matter), then a title prefix search."""
        self.ensure_login()
        words = (query or "").split()
        out: list[str] = []
        for params in ({"list": "search", "srsearch": query, "srnamespace": 0, "srlimit": limit, "srprop": ""},
                       {"list": "prefixsearch", "pssearch": words[0] if words else query, "pslimit": limit}):
            try:
                r = self._call({"action": "query", **params}, what="search")
            except ApiError:
                continue                        # search switched off: the person types the title
            q = r.get("query") or {}
            for hit in (q.get("search") or []) + (q.get("prefixsearch") or []):
                t = hit.get("title") if isinstance(hit, dict) else None
                if t and t not in out:
                    out.append(t)
        return out

    # -- files -------------------------------------------------------------------------------------------------------

    def file_info(self, name: str) -> dict | None:
        """The wiki's file of that name: {"name", "sha1", "size", "user", "url", "descriptionurl"}, None if none."""
        self.ensure_login()
        title = name if name.lower().startswith(("file:", "image:")) else f"File:{name}"
        try:
            r = self._call({"action": "query", "titles": title, "prop": "imageinfo", "iiprop": "sha1|size|user|url"},
                           what="file lookup")
        except ApiError as e:
            raise self._mapped(e, "file lookup") from None
        pages = (r.get("query") or {}).get("pages") or []
        if not pages:
            return None
        p = pages[0]
        ii = (p.get("imageinfo") or [None])[0]
        if not ii:
            return None                         # no file (at most a description page)
        t = str(p.get("title") or title)
        return {"name": (t.split(":", 1)[1] if ":" in t else t).replace(" ", "_"), **_ii(ii)}

    def upload(self, path: str, filename: str, comment: str, text: str, state: dict, *, on_state=None,
               progress=None) -> dict:
        """Upload (or finish uploading) `path` as File:`filename`: {"file", "url", "page_url", "existed"}.

        `state` is the job's resume state, changed in place: target, size, sha1, filekey, offset, stage, then
        uploaded + file (the name it has on the wiki; another file's when the same content was there already).
        `on_state()` is called after each change so the caller can save it; `progress(sent, total)` after each
        piece. One call is one attempt: a drop raises TransientError with `state` ready for the next one."""
        if state.get("uploaded") and state.get("file"):
            return self._result(state)
        try:
            size = os.path.getsize(path)
        except OSError:
            raise PermanentError(f"the clip to upload is missing: {path}") from None
        if size == 0:
            raise PermanentError(f"the clip to upload is empty: {path}")

        def changed():
            if on_state:
                on_state()

        if state.get("size") != size or state.get("target") != filename:
            for k in ("filekey", "offset", "stage", "sha1"):       # another file than the stash was made for
                state.pop(k, None)
            state.update(size=size, target=filename)
        if not state.get("sha1"):
            state["sha1"] = _sha1(path)
            changed()
        self.ensure_login()
        if not state.get("filekey") or state.get("stage") == "assembled":
            # the same bytes under this name already: an earlier attempt published it and its answer was lost
            info = self.file_info(filename)
            if info and info.get("sha1") == state["sha1"]:
                return self._done(state, info, changed, existed=True)
        if state.get("stage") != "assembled":
            self._send_pieces(path, filename, size, state, changed, progress)
        return self._publish(filename, comment, text, state, changed)

    def _send_pieces(self, path: str, filename: str, size: int, state: dict, changed, progress) -> None:
        """Stash the file piece by piece until the wiki has put it together (state["stage"] = "assembled")."""
        offset = int(state.get("offset") or 0) if state.get("filekey") else 0    # an offset means nothing alone
        if state.get("filekey"):
            kind, value = self._stash_status(state, size)
            if kind == "done":
                return self._assembled(state, value, size, changed, progress)
            if kind == "poll":
                raise TransientError("the wiki is still putting the uploaded pieces together; asking again soon",
                                     retry_after=15.0)
            if kind == "continue":
                offset = value
            else:
                self._forget_stash(state)
                offset = 0
                changed()
        chunk = int(state.get("chunk_size") or self.chunk_size)
        restarted = False
        tries: dict[int, int] = {}
        with open(path, "rb") as f:
            while True:
                if offset >= size:
                    # every byte went, but no "Success" was heard (its answer was lost): ask the stash
                    kind, value = self._stash_status(state, size) if state.get("filekey") else ("gone", None)
                    if kind == "done":
                        return self._assembled(state, value, size, changed, progress)
                    if kind == "poll":
                        raise TransientError("the wiki is still putting the uploaded pieces together; asking again "
                                             "soon", retry_after=15.0)
                    if kind == "continue" and value < size:
                        offset = value
                        continue
                    if restarted:
                        raise TransientError("the wiki lost the uploaded pieces twice; trying again later",
                                             retry_after=60.0)
                    restarted, offset = True, 0
                    self._forget_stash(state)
                    changed()
                    continue
                tries[offset] = tries.get(offset, 0) + 1
                if tries[offset] > 3:
                    raise TransientError("the wiki upload is not advancing; trying again later", retry_after=60.0)
                f.seek(offset)
                piece = f.read(chunk)
                params = {"action": "upload", "stash": 1, "filename": filename, "filesize": size, "offset": offset,
                          "ignorewarnings": 1}      # warnings are about the whole file: they are read at publishing
                if offset and state.get("filekey"):
                    params["filekey"] = state["filekey"]
                try:
                    r = self._call(params, write=True, what="upload", timeout=CHUNK_TIMEOUT,
                                   files={"chunk": (filename, piece, "application/octet-stream")})
                except ApiError as e:
                    if e.code in _TOO_BIG:
                        if chunk <= MIN_CHUNK:
                            raise PermanentError(f"wiki upload: {self._too_big_text(size)}") from None
                        chunk = max(MIN_CHUNK, chunk // 2)      # PHP or the web server allow less: smaller pieces
                        state["chunk_size"] = chunk
                        tries[offset] -= 1
                        changed()
                        continue
                    theirs = _int(e.data.get("offset"))
                    if e.code == "stashfailed" and theirs is not None and offset and state.get("filekey"):
                        offset = max(0, min(theirs, size))      # the wiki says where it got to: go on from there
                        state["offset"] = offset
                        changed()
                        continue
                    if e.code in _STASH_GONE or e.code == "stashfailed":
                        if restarted:
                            raise TransientError(f"the wiki could not keep the uploaded pieces ({e.code}); trying "
                                                 "again later", retry_after=60.0) from None
                        restarted, offset = True, 0
                        self._forget_stash(state)
                        changed()
                        continue
                    raise self._mapped(e, "upload") from None
                u = r.get("upload") or {}
                if u.get("filekey"):
                    state["filekey"] = u["filekey"]
                result = u.get("result")
                if result == "Success":
                    return self._assembled(state, state.get("filekey"), size, changed, progress)
                if result == "Continue":
                    new = _int(u.get("offset"))
                    offset = offset + len(piece) if new is None else new
                    state["offset"] = offset
                    changed()
                    if progress:
                        progress(offset, size)
                    continue
                if result == "Poll":                # put together in the background: checkstatus says when done
                    state["offset"] = size
                    changed()
                    raise TransientError("the wiki is putting the uploaded pieces together; asking again soon",
                                         retry_after=15.0)
                raise TransientError(f"the wiki answered a piece of the upload with {result or 'nothing'}; trying "
                                     "again later")

    @staticmethod
    def _assembled(state: dict, filekey, size: int, changed, progress) -> None:
        state.update(filekey=filekey, stage="assembled", offset=size)
        changed()
        if progress:
            progress(size, size)

    def _stash_status(self, state: dict, size: int) -> tuple[str, object]:
        """Where the stash of state["filekey"] got to: ("continue", offset), ("done", final filekey), ("poll", None)
        or ("gone", None)."""
        filekey = state["filekey"]
        try:
            r = self._call({"action": "upload", "checkstatus": 1, "filekey": filekey}, write=True,
                           what="upload check")
        except ApiError as e:
            if e.code in _STASH_GONE or e.code == "stashfailed":
                found = self._find_stash(size, state.get("sha1"))
                return ("done", found) if found else ("gone", None)
            raise self._mapped(e, "upload check") from None
        u = r.get("upload") or {}
        result = u.get("result")
        if result == "Continue":
            return "continue", max(0, min(_int(u.get("offset")) or 0, size))
        if result == "Success":
            return "done", u.get("filekey") or filekey
        if result == "Poll":
            return "poll", None
        return "gone", None

    def _find_stash(self, size: int, sha1: str | None) -> str | None:
        """The filekey of an assembled file of ours in the wiki's stash with these exact bytes: a lost answer to the
        last piece leaves one (under a filekey we never heard). None when there is none or the wiki cannot list it."""
        if not sha1:
            return None
        try:
            r = self._call({"action": "query", "list": "mystashedfiles", "msfprop": "size", "msflimit": 50},
                           what="upload check")
            for f in (r.get("query") or {}).get("mystashedfiles") or []:
                if _int(f.get("size")) != size or not f.get("filekey"):
                    continue
                info = self._call({"action": "query", "prop": "stashimageinfo", "siifilekey": f["filekey"],
                                   "siiprop": "sha1"}, what="upload check")
                got = ((info.get("query") or {}).get("stashimageinfo") or [{}])[0].get("sha1")
                if got == sha1:
                    return str(f["filekey"])
        except ApiError:
            return None                         # an older wiki: the upload starts over
        return None

    def _publish(self, filename: str, comment: str, text: str, state: dict, changed, ignore: bool = False) -> dict:
        """Publish the assembled stash as File:`filename`; the warnings decide what a clash means (module
        docstring)."""
        params = {"action": "upload", "filename": filename, "filekey": state["filekey"], "comment": comment,
                  "text": text}
        if ignore:
            params["ignorewarnings"] = 1
        try:
            r = self._call(params, write=True, what="upload", timeout=CHUNK_TIMEOUT)
        except ApiError as e:
            if e.code == "fileexists-no-change" or e.code in _STASH_GONE or e.code == "stashfailed":
                info = self.file_info(filename)
                if info and info.get("sha1") == state.get("sha1"):
                    return self._done(state, info, changed, existed=True)
                if e.code != "fileexists-no-change":
                    self._forget_stash(state)
                    changed()
                    raise TransientError("the wiki lost the uploaded pieces before publishing them; uploading again",
                                         retry_after=15.0) from None
            raise self._mapped(e, "upload") from None
        u = r.get("upload") or {}
        result = u.get("result")
        if result == "Success":
            return self._done(state, {"name": u.get("filename") or filename, **_ii(u.get("imageinfo"))}, changed)
        if result == "Warning" and not ignore:
            return self._warned(filename, comment, text, state, changed, u.get("warnings") or {})
        if result == "Poll":
            raise TransientError("the wiki is publishing the file in the background; checking again soon",
                                 retry_after=15.0)
        raise TransientError(f"the wiki answered the upload with {result or 'nothing'}; trying again later")

    def _warned(self, filename: str, comment: str, text: str, state: dict, changed, warnings: dict) -> dict:
        sha1 = state.get("sha1")
        if "nochange" in warnings:
            info = self.file_info(filename)
            if info:
                return self._done(state, info, changed, existed=True)
        exists = warnings.get("exists")
        if exists:
            info = self.file_info(str(exists))
            if info and info.get("sha1") == sha1:
                return self._done(state, info, changed, existed=True)
            if info and not self._is_me(info.get("user")):
                raise PermanentError(f"the wiki already has a different file called {exists}, uploaded by "
                                     f"{info.get('user') or 'someone else'}; StreamSplitter never overwrites a "
                                     "person's file. Rename or delete that file on the wiki, then restart "
                                     "StreamSplitter")
            # else our own earlier upload of the same match (a re-cut): it gets a new version below
        dups = warnings.get("duplicate")
        for name in (dups if isinstance(dups, list) else [dups] if dups else []):
            info = self.file_info(str(name))
            if info and info.get("sha1") == sha1:
                return self._done(state, info, changed, existed=True)   # the same video is there: no second copy
        if warnings.get("badfilename"):
            filename = str(warnings["badfilename"])                     # the name the wiki can use
        return self._publish(filename, comment, text, state, changed, ignore=True)

    def _is_me(self, user) -> bool:
        norm = lambda s: str(s or "").replace("_", " ").strip().casefold()     # noqa: E731
        return bool(self.user_name) and norm(user) == norm(self.user_name)

    @staticmethod
    def _forget_stash(state: dict) -> None:
        for k in ("filekey", "offset", "stage"):
            state.pop(k, None)

    def _done(self, state: dict, info: dict, changed, existed: bool = False) -> dict:
        name = str(info.get("name") or state.get("target") or "").replace(" ", "_")
        state.update(uploaded=True, file=name, file_url=info.get("url"), file_page=info.get("descriptionurl"),
                     existed=existed)
        self._forget_stash(state)
        changed()
        return self._result(state)

    @staticmethod
    def _result(state: dict) -> dict:
        return {"file": state["file"], "url": state.get("file_url"), "page_url": state.get("file_page"),
                "existed": bool(state.get("existed"))}


# ---------------------------------------------------------------------------------------------------------------------
# Destination

class WikiDestination:
    """Destination "wiki": upload the clip as a file, make the event's page if it is not there, add the match's
    section. Every step is recorded in job.state and skipped on a retry once done, and the section is looked for in
    the page before it is added, so sending a job again never uploads twice or adds a second section."""

    name = "wiki"

    def __init__(self, client: WikiClient, *, page_title: str | None = None, page_event: str | None = None,
                 page_format: str = PAGE_FORMAT, embed_format: str = EMBED_FORMAT, private: bool = True,
                 private_markup: str = PRIVATE_MARKUP, on_state=None, progress=None, warn=None,
                 playoff_alliances: int = 8):
        """`page_title`: the event's page as confirmed in the setup (for `page_event`; other events' matches get
        `page_format`). `private`: `private_markup` at the top of the page and of every file page (members-only on
        team5026's wiki). `on_state(job)`: save job.state now (the outbox passes save_state). `warn(text)`: tell the
        contact something a person should look at (a page without the members-only markup)."""
        for problem in (page_format_problem(page_format), embed_problem(embed_format),
                        title_problem(page_title) if page_title else None):
            if problem:
                raise PermanentError(f"the wiki settings are not usable ({problem}); fix them with {SETUP_AGAIN}")
        self.client = client
        self.page_title, self.page_event = page_title, page_event
        self.page_format, self.embed_format = page_format, embed_format
        self.private, self.private_markup = bool(private), (private_markup or "").strip()
        self.on_state, self.progress, self.warn = on_state, progress, warn
        self.playoff_alliances = playoff_alliances
        self._ready: set[str] = set()           # pages made or found in this run
        self._checked: set[str] = set()         # pages whose members-only markup was checked in this run

    # -- names and texts -----------------------------------------------------------------------------------------------

    def page_for(self, job: Job) -> str:
        if job.kind == "test":
            return TEST_PAGE
        m = job.match
        if self.page_title and (not self.page_event or not m.event_key or m.event_key == self.page_event):
            return self.page_title
        return render_page_title(self.page_format, m.event_name, m.year, m.event_key)

    @staticmethod
    def file_name(job: Job) -> str:
        """"2026mttd_qm12.mp4" from the clip's name (replays "2026mttd_qm12_r2.mp4"); the event key is put in front
        of a name without it."""
        if job.kind == "test":
            return TEST_FILE
        m = job.match
        name = m.name or m.key or "match"
        if m.event_key and name != m.event_key and not name.startswith(m.event_key + "_"):
            name = f"{m.event_key}_{name}"
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "match"
        return f"{name}.mp4"

    def heading(self, job: Job) -> str:
        """The match's section title as FIRST names it: "Qualification 12", "Match 7 (R3)", "Final 2", "Practice 3",
        with "(Replay)" / "(Partial)" (templates.match_title, as YouTube and Slack name it)."""
        title = templates.match_title(job.match, self.playoff_alliances)[0]
        if job.kind == "test":
            title = f"{title} - {event_text(job.match)} (sample)"
        return " ".join(title.replace("=", "-").split()) or job.match.name or "Match"

    def _private_top(self) -> list[str]:
        return [self.private_markup] if self.private and self.private_markup else []

    def new_page_text(self, job: Job) -> str:
        if job.kind == "test":
            # not naming the file: a page that names it counts as having its section (names_file)
            body = "A test page from the StreamSplitter setup. It is safe to delete, with the test video's file."
        else:
            body = INTRO.format(event=event_text(job.match) or "event")
        return "\n".join(self._private_top() + [body]) + "\n"

    def section_text(self, job: Job, file: str, heading: str) -> str:
        line = teams_line(job.match)
        return render_embed(self.embed_format, file, heading) + (f"\n\n{line}" if line else "")

    def file_text(self, job: Job, page: str, heading: str) -> str:
        """The file's description page (used when the file is new)."""
        lines = self._private_top() + [f"{heading}, {event_text(job.match) or 'match video'}."]
        if teams_line(job.match):
            lines.append(teams_line(job.match))
        lines.append(f"Match videos: [[{page}]]. Recorded from the event webcast by StreamSplitter.")
        return "\n\n".join(lines)

    # -- sending -------------------------------------------------------------------------------------------------------

    def _saved(self, job: Job) -> None:
        if self.on_state:
            self.on_state(job)

    def send(self, job: Job) -> dict:
        st = job.state
        c = self.client
        heading = self.heading(job)
        page = st["page"] if st.get("section_done") and st.get("page") else self.page_for(job)
        progress = (lambda sent, total: self.progress(job, sent, total)) if self.progress else None
        what = f"{heading}, {event_text(job.match)}".rstrip(", ")
        up = c.upload(job.path, st.get("target") or self.file_name(job), f"StreamSplitter: {what}",
                      self.file_text(job, page, heading), st, on_state=lambda: self._saved(job), progress=progress)
        file = up["file"]
        if not st.get("section_done"):
            if st.get("page") != page:
                st["page"] = page
                st.pop("page_done", None)
            if not st.get("page_done"):
                self._ensure_page(page, job)
                st["page_done"] = True
                self._saved(job)
            text = c.page_text(page)
            if text is None:                    # deleted since: made again (members-only), never by the section edit
                self._ready.discard(page)
                self._ensure_page(page, job)
                text = ""
            self._check_private(page, text)
            if names_file(text, file):
                st["section_done"] = True       # a retry after the answer to the section edit was lost
            else:
                body = self.section_text(job, file, heading)
                try:
                    rev = c.add_section(page, heading, body, f"StreamSplitter: {heading}")
                except PageMissing:
                    self._ready.discard(page)
                    self._ensure_page(page, job)
                    rev = c.add_section(page, heading, body, f"StreamSplitter: {heading}")
                st.update(section_done=True, section_rev=rev.get("newrevid"))
            self._saved(job)
        out = {"url": c.page_url(page), "page": page, "file": file, "file_url": up.get("url"),
               "file_page": up.get("page_url"), "section_rev": st.get("section_rev")}
        if job.kind == "test":
            out["test"] = True
        return out

    def _ensure_page(self, page: str, job: Job) -> None:
        if page in self._ready:
            return
        self.client.create_page(page, self.new_page_text(job), "StreamSplitter: page for the match videos")
        self._ready.add(page)

    def _check_private(self, page: str, text: str) -> None:
        """Once per page and run: a page meant to be members-only that lacks the markup (a person made it first, or
        removed it) is reported, never changed."""
        if page in self._checked or not (self.private and self.private_markup):
            return
        self._checked.add(page)
        if self.private_markup.casefold() not in (text or "").casefold() and self.warn is not None:
            self.warn(f"The wiki page '{page}' has no {self.private_markup} at the top, so its match videos may be "
                      f"visible to everyone. Add {self.private_markup} on the first line if it should be "
                      "members-only.")
