"""A YouTube stream of the event that nobody listed (user, 2026-11-10).

MadTown Throwdown 2026: the links The Blue Alliance had were official YouTube videos that never ran for more than six
minutes and the event's Twitch channel, which was recorded for ten hours of Saturday and glitched. All that day Team
1671 streamed the whole event on YouTube ("Madtown Throwdown 2026 Day 1"); TBA got that link only on Sunday.

    candidates(event)      live YouTube videos whose title names the event (YouTube's own search with its "Live"
                           filter, read by yt-dlp: no API key, about 1 s)
    roster(event_key)      the event's team numbers (TBA)
    vet(url, roster)       is this the event? One picture of the stream is read; VET_TEAMS or more of the event's
                           team numbers on it (the match overlay, a rankings or queueing screen) says yes. Between
                           matches there may be none: the next round looks again.
    Scout                  runs a round of the above on its own thread (a round takes 5-40 s; EventWatch's thread
                           also answers Slack commands and must not wait for it)

What `live` does with a round (live.EventWatch): a vetted stream joins the recording's list of links, where the
recorder takes the best one that delivers (record.py); one that could not be vetted is only suggested to the contact,
once. A stream found here is never saved in config.toml.

A title names the event when it has every word of TBA's name or short name for it (two words or more; "MadTown
Throwdown" matches "Madtown Throwdown 2026 Day 1" and "MAD TOWN THROWDOWN - Finals") and no other year than the
event's ("2022 Madtown Throwdown" is a replay).
"""

from __future__ import annotations

import re
import subprocess
import threading
import urllib.parse
from pathlib import Path

from . import ffmpeg, record, tba

SEARCH = "https://www.youtube.com/results?search_query={q}&sp=EgJAAQ%3D%3D"      # sp: YouTube's "Live" filter
SEARCH_N = 12
VET_TEAMS = 3                # this many of the event's team numbers on one picture: it is the event
VET_MAX = 3                  # streams looked at per round (each: resolve + one picture + one read)
FRAME_TIMEOUT_S = 45.0
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d\d(?!\d)")
_NUMBER = re.compile(r"(?<!\d)\d{3,5}(?!\d)")       # one- and two-digit numbers are scores and timers far more often
_SMALL_WORDS = {"the", "of", "at", "and", "a", "in"}


# ---------------------------------------------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------------------------------------------

def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(s or "").lower())


def event_names(event: dict) -> list[str]:
    """TBA's name and short name for the event, without "presented by ..."; only names of two words or more (one
    word, "Sacramento", is in too many titles)."""
    out: list[str] = []
    for k in ("name", "short_name"):
        n = re.split(r"\bpresented by\b", str(event.get(k) or ""), flags=re.I)[0].strip(" -,")
        if len([w for w in _words(n) if w not in _SMALL_WORDS]) >= 2 and n.lower() not in [x.lower() for x in out]:
            out.append(n)
    return out


def title_matches(title: str, event: dict) -> bool:
    """Whether a video title names this event (module docstring)."""
    year = str(event.get("year") or "")
    years = set(_YEAR.findall(str(title or "")))
    if years and year and year not in years:
        return False
    squashed = "".join(_words(title))
    for name in event_names(event):
        words = [w for w in _words(name) if w not in _SMALL_WORDS and not _YEAR.fullmatch(w)]
        if words and all(w in squashed for w in words):
            return True
    return False


def youtube_live_search(query: str, limit: int = SEARCH_N) -> list[dict]:
    """YouTube's live videos for `query`, as it ranks them: [{"video", "url", "title", "channel", "viewers"}]."""
    import yt_dlp
    params = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True, "playlistend": limit,
              "socket_timeout": 20}
    with yt_dlp.YoutubeDL(params) as ydl:
        info = ydl.extract_info(SEARCH.format(q=urllib.parse.quote_plus(query)), download=False) or {}
    out = []
    for e in info.get("entries") or []:
        vid = str((e or {}).get("id") or "")
        if len(vid) != 11 or e.get("live_status") not in (None, "is_live"):
            continue
        out.append({"video": vid, "url": f"https://www.youtube.com/watch?v={vid}", "title": str(e.get("title") or ""),
                    "channel": str(e.get("channel") or e.get("uploader") or ""),
                    "viewers": int(e.get("concurrent_view_count") or 0)})
    return out


def candidates(event: dict, search=youtube_live_search, known=()) -> list[dict]:
    """Live YouTube videos whose title names the event, most watched first; `known` video ids are left out."""
    found: dict[str, dict] = {}
    for name in event_names(event):
        for v in search(f"{name} {event.get('year') or ''}".strip()):
            if v["video"] not in found and v["video"] not in known and title_matches(v["title"], event):
                found[v["video"]] = v
    return sorted(found.values(), key=lambda v: -v["viewers"])


# ---------------------------------------------------------------------------------------------------------------
# Is it the event?
# ---------------------------------------------------------------------------------------------------------------

def roster(event_key: str, tba_get=None) -> set[str]:
    """The event's team numbers, as text ("254")."""
    keys = (tba_get or tba.get)(f"/event/{event_key}/teams/keys") or []
    return {str(k)[3:] for k in keys if str(k).startswith("frc") and str(k)[3:].isdigit()}


def grab_frame(src, ff: str | None = None, timeout: float = FRAME_TIMEOUT_S, at_s: float | None = None):
    """One picture of the stream as OpenCV reads it (BGR), or None. `src`: a record.Source (its HLS playlist; ffmpeg
    starts near the live edge), or a video file's path (`at_s`: this far in)."""
    import cv2
    import numpy as np
    cmd = [ff or ffmpeg.find("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error"]
    if isinstance(src, (str, Path)):
        cmd += [*(["-ss", f"{at_s:.3f}"] if at_s else []), "-i", str(src)]
    else:
        cmd += ["-rw_timeout", "20000000", *record._http_args(src.headers), "-i", src.video_url]
    cmd += ["-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not out:
        return None
    return cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_COLOR)


_ENGINE = None


def numbers_on(frame) -> set[str]:
    """Every number of 3 to 5 digits read on the picture (a whole-frame read, as detect/overlay.py does; its own
    engine, made at the first call: the follower's is in use on another thread)."""
    global _ENGINE
    if _ENGINE is None:
        from rapidocr import RapidOCR
        _ENGINE = RapidOCR(params={"Global.use_cls": False, "Global.log_level": "warning"})
    r = _ENGINE(frame)
    out: set[str] = set()
    for txt in r.txts or []:
        out.update(_NUMBER.findall(str(txt)))
    return out


def vet(url: str, teams: set[str], *, resolver=record.resolve, grab=grab_frame, read=numbers_on) -> dict:
    """{"live", "ok", "seen" (the event's team numbers on the picture), "why"} for one stream link."""
    try:
        src = resolver(url, 1080)
    except record.NotLive as e:
        return {"live": False, "ok": False, "seen": [], "why": str(e)}
    except Exception as e:  # noqa: BLE001 - yt-dlp, the network
        return {"live": False, "ok": False, "seen": [], "why": f"could not be opened: {record._short(e)}"}
    if not teams:
        return {"live": True, "ok": False, "seen": [], "why": "the event's team list is not known"}
    frame = grab(src)
    if frame is None:
        return {"live": True, "ok": False, "seen": [], "why": "its picture could not be read"}
    seen = sorted(read(frame) & teams, key=int)
    if len(seen) >= VET_TEAMS:
        return {"live": True, "ok": True, "seen": seen, "why": f"teams {', '.join(seen)} are on its picture"}
    return {"live": True, "ok": False, "seen": seen,
            "why": ("none of the event's team numbers are on its picture right now" if not seen else
                    f"only {len(seen)} of the event's team numbers {'is' if len(seen) == 1 else 'are'} on its picture "
                    f"right now")}


# ---------------------------------------------------------------------------------------------------------------
# A round, on its own thread
# ---------------------------------------------------------------------------------------------------------------

class Scout:
    """start(...) begins a round unless one is running; take() hands a finished round over, once:
    {"streams": [{"video", "url", "title", "channel", "viewers", "live", "ok", "seen", "why"}], "error": str | None}.
    `refresh()` is called first on the round's thread (EventWatch.fetch_event: TBA may list a new webcast)."""

    def __init__(self, event_key: str, *, search=youtube_live_search, vet=vet, roster=roster, log=print):
        self.event_key = event_key
        self._search, self._vet, self._roster, self.log = search, vet, roster, log
        self.teams: set[str] = set()
        self._thread: threading.Thread | None = None
        self._result: dict | None = None

    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def round(self, event: dict | None, known=(), refresh=None) -> dict:
        try:
            if refresh is not None:
                event = refresh() or event
            if not event:
                return {"streams": [], "error": "the event's record is not known (The Blue Alliance)"}
            found = candidates(event, self._search, known)[:VET_MAX]
            if found and not self.teams:
                try:
                    self.teams = set(self._roster(self.event_key))
                except Exception as e:  # noqa: BLE001 - no TBA key, offline: nothing is vetted, all are suggested
                    self.log(f"stream search: the event's team list could not be read ({type(e).__name__}: {e})",
                             flush=True)
            return {"streams": [{**v, **self._vet(v["url"], self.teams)} for v in found], "error": None}
        except Exception as e:  # noqa: BLE001 - a search that failed is tried again next round
            return {"streams": [], "error": f"{type(e).__name__}: {record._short(e)}"}

    def start(self, event: dict | None, known=(), refresh=None) -> bool:
        if self.busy():
            return False

        def run():
            self._result = self.round(event, known, refresh)
        self._thread = threading.Thread(target=run, name="stream-scout", daemon=True)
        self._thread.start()
        return True

    def take(self) -> dict | None:
        if self.busy():
            return None
        out, self._result = self._result, None
        return out
