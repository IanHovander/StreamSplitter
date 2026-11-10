"""Which stream to record: the webcasts The Blue Alliance lists for an event, and stream links a person types.

    suggest_streams(event_key) -> [{"url", "label", "date", "recommended"}]     (the setup wizard and `live`)
    pick(event, today) -> the recommended suggestion, or None                  (`live`, each morning)
    normalize_stream_url(text) -> a YouTube / Twitch link, or None             (`stream <link>` from Slack)

TBA's /event/{key} has `webcasts`: [{"type": "youtube" | "twitch" | ..., "channel", "date" (yyyy-mm-dd or null),
"file"}]. For YouTube, `channel` is usually a video id (one video per day of the event, each dated), sometimes a
channel id (UC...) or handle; for Twitch it is the channel name, whose page always shows what is live.

The recommended link (`pick`, `suggest_streams`): a webcast dated today (in the event's own time zone); else a YouTube
channel's live page (listed by TBA, or found from the channel of TBA's videos when there are several, one per day:
the live page follows from day to day); else the one YouTube video when TBA lists just one (a channel that streams
several events may show another event on its live page); else a Twitch channel; else the first listed. Only YouTube
and Twitch are recordable (record.py); other webcast types are left out.

Stdlib only plus tba.py (and yt-dlp, lazily, to find a video's channel), so the setup wizard can use it before the
detection packages are installed. The event's time zone needs the `tzdata` package on Windows (no system tz database);
without it the computer's own time zone is used, which is right for a computer at the venue.
"""

from __future__ import annotations

import datetime as _dt
import html
import re
import urllib.parse

YOUTUBE_HOSTS = {"youtube.com", "youtu.be", "youtube-nocookie.com"}
TWITCH_HOSTS = {"twitch.tv"}
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_SLACK_LINK = re.compile(r"<([^<>|]+)(?:\|[^<>]*)?>")


# ---------------------------------------------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------------------------------------------

def unwrap_slack(text: str) -> str:
    """Slack's message text as typed: <https://x|x> and <https://x> become https://x, &amp; &lt; &gt; plain."""
    return html.unescape(_SLACK_LINK.sub(lambda m: m.group(1), text or ""))


def _host(u: urllib.parse.SplitResult) -> str:
    h = (u.hostname or "").lower()
    for p in ("www.", "m.", "music."):
        h = h.removeprefix(p)
    return h


def normalize_stream_url(text: str) -> str | None:
    """A YouTube or Twitch link from what a person typed (or Slack sent), else None. https:// is added; a YouTube
    channel page (youtube.com/@Name, /channel/ID, .../streams) becomes its /live page, which follows whatever the
    channel streams; a Twitch channel page loses its sub-page (twitch.tv/name/videos -> twitch.tv/name)."""
    s = unwrap_slack(text).strip().strip("\"'`<>()[],.;").strip()
    if not s or any(c.isspace() for c in s):
        return None
    if not re.match(r"^[a-z][a-z0-9+.-]*://", s, re.I):
        s = "https://" + s
    try:
        u = urllib.parse.urlsplit(s)
        port = u.port
    except ValueError:
        return None
    if u.scheme.lower() not in ("http", "https") or u.username or u.password or port not in (None, 80, 443):
        return None
    host = _host(u)
    parts = [p for p in u.path.split("/") if p]
    if host in YOUTUBE_HOSTS:
        if host == "youtu.be":
            return f"https://youtu.be/{parts[0]}" if parts and _VIDEO_ID.match(parts[0]) else None
        if parts and parts[0] == "watch":
            v = urllib.parse.parse_qs(u.query).get("v", [""])[0]
            return f"https://www.youtube.com/watch?v={v}" if _VIDEO_ID.match(v) else None
        if len(parts) >= 2 and parts[0] in ("live", "shorts", "embed") and _VIDEO_ID.match(parts[1]):
            return f"https://www.youtube.com/watch?v={parts[1]}"
        n = 1 if parts and parts[0].startswith("@") else 2 if parts and parts[0] in ("channel", "c", "user") else 0
        if n and len(parts) >= n:
            base = "/".join(parts[:n])
            if len(parts) == n or parts[n] in ("live", "streams", "videos", "featured", "home"):
                return f"https://www.youtube.com/{base}/live"
        return None
    if host in TWITCH_HOSTS:
        if parts and re.match(r"^[A-Za-z0-9_]{1,25}$", parts[0]) and parts[0].lower() not in ("videos", "directory"):
            return f"https://www.twitch.tv/{parts[0]}"
        return None
    return None


def webcast_url(w: dict) -> str | None:
    """The link for one TBA webcast record, or None for a type record.py cannot record."""
    kind, ch = str(w.get("type") or "").lower(), str(w.get("channel") or "").strip()
    if not ch:
        return None
    if kind == "youtube":
        if _VIDEO_ID.match(ch):
            return f"https://www.youtube.com/watch?v={ch}"
        if _CHANNEL_ID.match(ch):
            return f"https://www.youtube.com/channel/{ch}/live"
        if ch.startswith("@"):
            return f"https://www.youtube.com/{ch}/live"
        return normalize_stream_url(ch)
    if kind == "twitch":
        return normalize_stream_url(ch if "/" in ch else f"twitch.tv/{ch}")
    return None


def is_live_page(url: str) -> bool:
    """A link that follows whatever the channel streams (a YouTube channel's /live page, a Twitch channel)."""
    u = urllib.parse.urlsplit(url)
    return (_host(u) in TWITCH_HOSTS) or (_host(u) == "youtube.com" and u.path.rstrip("/").endswith("/live")
                                          and not u.path.startswith("/live/"))


def kind(url: str | None) -> str | None:
    """"youtube" or "twitch" for a link on one of them, else None."""
    try:
        h = _host(urllib.parse.urlsplit(url if "://" in (url or "") else "https://" + (url or "")))
    except ValueError:
        return None
    return "youtube" if h in YOUTUBE_HOSTS else "twitch" if h in TWITCH_HOSTS else None


def video_id(url: str | None) -> str | None:
    """The YouTube video id of a link to one video (watch?v=, youtu.be/, /live/), else None."""
    norm = normalize_stream_url(url or "") or ""
    u = urllib.parse.urlsplit(norm)
    if _host(u) == "youtu.be":
        return u.path.strip("/") or None
    return urllib.parse.parse_qs(u.query).get("v", [None])[0] if u.path == "/watch" else None


def channel_live_page(video_url: str) -> str | None:
    """The /live page of the YouTube channel that owns `video_url` (yt-dlp reads the video's page), or None."""
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                               "socket_timeout": 20}) as ydl:
            info = ydl.extract_info(video_url, download=False, process=False) or {}
    except Exception:  # noqa: BLE001 - a suggestion only
        return None
    cid = info.get("channel_id")
    if cid and _CHANNEL_ID.match(str(cid)):
        return f"https://www.youtube.com/channel/{cid}/live"
    handle = str(info.get("uploader_id") or "")
    return f"https://www.youtube.com/{handle}/live" if handle.startswith("@") else None


# ---------------------------------------------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------------------------------------------

def fetch_event(event_key: str, tba_get=None) -> dict:
    """TBA's /event/{key} (name, dates, timezone, webcasts). Raises when TBA cannot be asked (no key, offline)."""
    if tba_get is None:
        from . import tba
        tba_get = tba.get
    data = tba_get(f"/event/{event_key}")
    return data if isinstance(data, dict) else {}


def suggestions(event: dict, today: _dt.date | None = None, channel_live=channel_live_page) -> list[dict]:
    """Every recordable webcast of a TBA event record as {"url", "label", "date", "recommended"}, the recommended
    one first (module docstring). `today`: the event's local date (None: no webcast counts as today's).
    `channel_live(video_url)`: finds a channel's live page for several dated videos (None: not looked up)."""
    casts = [w for w in (event.get("webcasts") or []) if isinstance(w, dict)]
    rows, seen = [], set()
    for w in casts:
        url = webcast_url(w)
        if not url or url in seen:
            continue
        seen.add(url)
        date = str(w.get("date") or "") or None
        kind = str(w.get("type")).lower()
        if kind == "twitch":
            label = f"Twitch: {url.split('//', 1)[1]}"
        elif is_live_page(url):
            label = "The event's YouTube channel, live page (follows each day's stream)"
        else:
            label = f"The stream of {date}" if date else "The event's stream"
        rows.append({"url": url, "label": label, "date": date, "recommended": False, "kind": kind})
    if not rows:
        return []
    yt_videos = [r for r in rows if r["kind"] == "youtube" and not is_live_page(r["url"])]
    best = None
    if today is not None:
        best = next((r for r in rows if r["date"] == today.isoformat()), None)
        if best is not None:
            best["label"] = f"Today's stream ({best['date']})" + (f": {best['url'].split('//', 1)[1]}")
    if best is None:
        best = next((r for r in rows if r["kind"] == "youtube" and is_live_page(r["url"])), None)
    if best is None and len(yt_videos) > 1 and channel_live is not None:
        page = channel_live(yt_videos[0]["url"])
        if page and page not in seen:
            best = {"url": page, "date": None, "recommended": False, "kind": "youtube",
                    "label": f"The event's YouTube channel, live page (follows each day's stream; TBA lists "
                             f"{len(yt_videos)} day streams)"}
            rows.append(best)
    if best is None and len(yt_videos) == 1:
        best = yt_videos[0]
    if best is None:
        best = next((r for r in rows if r["kind"] == "twitch"), rows[0])
    best["recommended"] = True
    best["label"] += " (recommended)"
    out = [best] + [r for r in rows if r is not best]
    return [{k: r[k] for k in ("url", "label", "date", "recommended")} for r in out]


def pick(event: dict, today: _dt.date | None = None, channel_live=channel_live_page) -> dict | None:
    """The recommended suggestion for `event` on `today`, or None when TBA lists nothing recordable."""
    rows = suggestions(event, today, channel_live)
    return rows[0] if rows else None


def alternates(event: dict | None, today: _dt.date | None, main: str | None) -> list[str]:
    """Links the recorder tries when `main` is not live (record.py --also): the event's other TBA webcasts that show
    what is live today, in order: a webcast dated today, a YouTube channel's live page or a video TBA gives no date
    (the event's one video), a Twitch channel; never `main` (compared as normalize_stream_url gives them), never a
    video dated another day. User, 2026-11-06: MadTown 2026 streamed on Twitch only while the YouTube day videos TBA
    lists never went live."""
    ranked = []
    for w in (event or {}).get("webcasts") or []:
        url = webcast_url(w) if isinstance(w, dict) else None
        if not url:
            continue
        if today is not None and str(w.get("date") or "") == today.isoformat():
            rank = 0
        elif is_live_page(url):
            rank = 2 if _host(urllib.parse.urlsplit(url)) in TWITCH_HOSTS else 1
        elif not w.get("date"):
            rank = 1
        else:
            continue
        ranked.append((rank, url))
    skip = {main, normalize_stream_url(main or "")}
    out: list[str] = []
    for _, url in sorted(ranked, key=lambda r: r[0]):
        if url not in skip and url not in out:
            out.append(url)
    return out


def todays_webcast(event: dict, today: _dt.date) -> str | None:
    """The link of a webcast TBA dates `today`, or None."""
    for w in event.get("webcasts") or []:
        if isinstance(w, dict) and str(w.get("date") or "") == today.isoformat():
            url = webcast_url(w)
            if url:
                return url
    return None


def suggest_streams(event_key: str, *, tba_get=None, today: _dt.date | None = None,
                    channel_live=channel_live_page) -> list[dict]:
    """The setup wizard's list: {"url", "label", "date", "recommended"} for each stream TBA lists for `event_key`,
    the recommended one first; [] when TBA lists none or cannot be asked (no key, offline). `today` defaults to the
    event's local date when the event is on now, else None (days ahead, no webcast is "today's")."""
    try:
        event = fetch_event(event_key, tba_get)
    except Exception:  # noqa: BLE001 - the wizard then asks for a link
        return []
    if today is None:
        day = event_today(event)
        days = event_days(event)
        today = day if days and days[0] <= day <= days[1] else None
    return suggestions(event, today, channel_live)


# ---------------------------------------------------------------------------------------------------------------
# The event's days and time zone
# ---------------------------------------------------------------------------------------------------------------

def event_zone(event: dict | None):
    """(tzinfo, exact) for the event: its TBA `timezone` (zoneinfo; Windows needs the tzdata package), else this
    computer's own zone with exact False."""
    name = (event or {}).get("timezone")
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(str(name)), True
        except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError (no tzdata), a bad name
            pass
    return _dt.datetime.now().astimezone().tzinfo, False


def event_days(event: dict | None) -> tuple[_dt.date, _dt.date] | None:
    """(start_date, end_date) of a TBA event record, or None when it does not say."""
    try:
        a = _dt.date.fromisoformat(str((event or {})["start_date"]))
        b = _dt.date.fromisoformat(str((event or {}).get("end_date") or a))
        return a, b
    except (KeyError, TypeError, ValueError):
        return None


def event_now(event: dict | None, t: float, tz=None) -> _dt.datetime:
    """Unix time `t` as the event's local time (`tz` overrides the event's zone)."""
    return _dt.datetime.fromtimestamp(t, tz if tz is not None else event_zone(event)[0])


def event_today(event: dict | None, t: float | None = None, tz=None) -> _dt.date:
    import time
    return event_now(event, time.time() if t is None else t, tz).date()
