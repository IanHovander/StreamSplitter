"""streamsplitter tba: an event's YouTube videos onto their match pages on The Blue Alliance, after the fact.

During an event, [youtube] tba_links adds each video as it is uploaded, which needs an organizer's write key
(publish/tbalink.py). This command covers the rest: the videos already uploaded, and the people with no key.

    streamsplitter tba                    what TBA has, what this computer uploaded, and the way to add the rest
    streamsplitter tba --event 2026mttd   another event than the one set up
    streamsplitter tba --all              also the matches TBA already has a video for
    streamsplitter tba --retitle          put each match's TBA id in the title of its video on YouTube
    streamsplitter tba --yes              do not ask before adding links or renaming

With a write key saved (setup --section tbalinks) the links are added over TBA's Trusted API, one match per request
(TBA adds nothing from a request that names a match it lacks). Without a key it prints TBA's playlist page and the
playlist's link: paste, Submit Playlist, Add Suggestions; a TBA moderator reviews them. That page reads each video's
match from its title (tbalink.guess_partial) and skips the rows it leaves blank, so --retitle adds the id ("...
(qm12)") to the best upload of each match to add and to no other.

The uploads are read from the outboxes (<data_root>/outbox/*.sqlite, opened read-only): every finished YouTube upload
of a clip or its score version whose match belongs to the event. Per match the best one is the latest play (a replay
over the first play), then the newest upload (a re-run from a better stream over the first try); partial clips and
private videos are left out. When this computer is logged in to YouTube the videos are looked up there first (one
quota unit per 50), so a deleted video is never linked and the titles are the ones YouTube has now.
"""

from __future__ import annotations

import json
import re
import sqlite3
import textwrap
from pathlib import Path
from urllib.parse import quote

from . import config, tba
from .publish import tbalink
from .publish.base import Job, PermanentError, TransientError

_REPLAY = re.compile(r"_r(\d+)")
_LEVELS = {"qm": 0, "ef": 1, "qf": 2, "sf": 3, "f": 4}


def add_arguments(ap) -> None:
    ap.add_argument("--event", help="TBA's event key (default: the event set up on this computer)")
    ap.add_argument("--all", action="store_true",
                    help="also the matches The Blue Alliance already has a video for (default: only the others)")
    ap.add_argument("--retitle", action="store_true",
                    help="add each match's TBA id to the title of its video on YouTube (\"... (qm12)\"), so TBA's "
                         "playlist page fills in the matches by itself; 50 YouTube quota units per video")
    ap.add_argument("--yes", action="store_true", help="do not ask before adding links or renaming videos")


def run_from_args(a) -> int:
    return run(a.event, every=a.all, retitle=a.retitle, yes=a.yes)


def order(part: str) -> tuple:
    """Sort key that puts partial keys in playing order: qm1, qm2, ... sf1m1, ... f1m1."""
    level = re.match(r"[a-z]+", part).group(0)
    return (_LEVELS.get(level, 9), *(int(n) for n in re.findall(r"\d+", part)))


def uploaded(outbox_dir: Path, event_key: str, log=None) -> dict[str, list[dict]]:
    """TBA partial key ("qm12") -> this computer's YouTube uploads of that match, best first. Each upload is
    {"video_id", "title", "playlist_id", "done_at", "replay", "job"}; "title" is the one sent at upload."""
    found: dict[str, list[dict]] = {}
    for path in sorted(Path(outbox_dir).glob("*.sqlite")):
        try:
            db = sqlite3.connect(f"file:{quote(path.as_posix(), safe='/:')}?mode=ro", uri=True, timeout=10.0)
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT json, result, done_at FROM jobs WHERE destination='youtube' AND "
                              "status='done' AND kind IN ('clip', 'score') ORDER BY rowid").fetchall()
            db.close()
        except sqlite3.Error as e:
            if log:
                log(f"{path.name}: not read ({e})")
            continue
        for row in rows:
            try:
                job = Job.from_dict(json.loads(row["json"]))
                result = json.loads(row["result"]) if row["result"] else {}
            except (TypeError, ValueError, KeyError):
                continue                        # a job from a version with other fields
            m, st = job.match, job.state
            part = tbalink.partial_key(m.key, event_key)
            vid = st.get("video_id") or result.get("video_id")
            if part is None or not vid or "_partial" in (m.name or ""):
                continue
            if "private" in (st.get("privacy"), result.get("privacy")) or result.get("locked_private"):
                continue
            hit = _REPLAY.search(m.name or "")
            found.setdefault(part, []).append({
                "video_id": str(vid), "title": st.get("title") or "", "done_at": float(row["done_at"] or 0.0),
                "playlist_id": st.get("playlist_id") or result.get("playlist_id"),
                "replay": max(m.replay or 1, int(hit.group(1)) if hit else 1), "job": job})
    for ups in found.values():
        ups.sort(key=lambda u: (-u["replay"], -u["done_at"]))
    return found


def on_youtube(ours: dict[str, list[dict]], records: dict[str, dict]) -> tuple[dict[str, list[dict]], int]:
    """`ours` without the videos YouTube no longer has or shows only to their owner, each upload's "title" and
    "snippet" as YouTube has them now; and how many uploads were dropped. `records`: YouTubeClient.videos()."""
    kept, dropped = {}, 0
    for part, ups in ours.items():
        for u in ups:
            rec = records.get(u["video_id"])
            if rec is None or (rec.get("status") or {}).get("privacyStatus") == "private":
                dropped += 1
                continue
            snippet = rec.get("snippet") or {}
            kept.setdefault(part, []).append({**u, "title": snippet.get("title") or u["title"], "snippet": snippet})
    return kept, dropped


def plan(matches: list[dict], ours: dict[str, list[dict]], event_key: str, *, every: bool = False) -> dict:
    """What TBA has and what could be added: {"total": matches TBA lists, "with_video": ... that have a video,
    "ours": ... this computer has a video for, "linked": [partial keys whose video on TBA is one of ours], "todo":
    {partial key: the upload to add}}. `every`: also matches that have someone else's video."""
    total = with_video = n_ours = 0
    linked, todo = [], {}
    for mt in matches:
        part = tbalink.partial_key(mt.get("key"), event_key)
        if part is None:
            continue
        total += 1
        videos = mt.get("videos") or []
        with_video += bool(videos)
        mine = ours.get(part)
        if not mine:
            continue
        n_ours += 1
        on_tba = {v.get("key") for v in videos if v.get("type") == "youtube"}
        if any(u["video_id"] in on_tba for u in mine):
            linked.append(part)
        elif every or not videos:
            todo[part] = mine[0]
    return {"total": total, "with_video": with_video, "ours": n_ours, "linked": sorted(linked, key=order),
            "todo": dict(sorted(todo.items(), key=lambda kv: order(kv[0])))}


def titled(todo: dict[str, dict]) -> list[str]:
    """The partial keys in `todo` whose video title TBA's playlist page reads as that match."""
    return [part for part, u in todo.items() if tbalink.guess_partial(u["title"]) == part]


def _confirm(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _youtube_client():
    """The logged-in YouTube client, or None when this computer is not logged in to YouTube."""
    token, client_json = config.get_secret("youtube_token"), config.get_secret("youtube_client")
    if not token or not client_json:
        return None
    from .publish import youtube
    return youtube.YouTubeClient(token, json.loads(client_json),
                                 on_token=lambda j: config.set_secret("youtube_token", j))


def run(event: str | None = None, *, every: bool = False, retitle: bool = False, yes: bool = False, say=print,
        confirm=_confirm, get=None, trusted=None, yt=None) -> int:
    """The command. `get`, `trusted` and `yt` stand in for tba.get, the TrustedClient and the YouTubeClient in the
    tests; `yt=False` means not logged in."""
    cfg_event = config.section("event")
    event = event or cfg_event.get("key")
    if not event:
        say("No event: name one (streamsplitter tba --event 2026mttd) or run streamsplitter setup.")
        return 2
    try:
        matches = (get or tba.get)(f"/event/{event}/matches")
    except tba.TBAError as e:
        say(f"The Blue Alliance could not be read for {event}: {e}")
        return 1
    ours = uploaded(config.data_root() / "outbox", event, log=say)
    checked = failed = False
    try:
        yt = _youtube_client() if yt is None else (yt or None)
        if yt is not None and ours:
            ours, dropped = on_youtube(ours, yt.videos([u["video_id"] for ups in ours.values() for u in ups]))
            checked = True
            if dropped:
                say(f"{dropped} upload(s) are no longer on YouTube or are private; they are left out.")
    except (TransientError, PermanentError) as e:
        failed = True
        say(f"YouTube could not be asked about the videos ({e}); going by what was uploaded.")
    p = plan(matches or [], ours, event, every=every)
    todo = p["todo"]
    name = cfg_event.get("name") if cfg_event.get("key") == event else None
    say(f"{name + ' (' + event + ')' if name else event} on The Blue Alliance: {p['total']} matches, "
        f"{p['with_video']} with a video.")
    say(f"This computer uploaded a YouTube video for {p['ours']} of them; {len(p['linked'])} of those are on The "
        f"Blue Alliance already, {len(todo)} can be added"
        + ("." if every else " to matches that have no video there."))
    if ours and not checked and not failed:
        say("(This computer is not logged in to YouTube, so the videos were not checked there.)")
    if not todo:
        left = p["ours"] - len(p["linked"])
        say("Nothing to add." + (f" (--all would add {left} more to matches that already have a video.)"
                                 if left and not every else ""))
        return 0
    if retitle:
        return _retitle(say, confirm, yes, yt if checked else None, todo)
    auth_id, secret = config.get_secret("tba_write_id"), config.get_secret("tba_write_secret")
    if trusted is None and auth_id and secret:
        trusted = tbalink.TrustedClient(auth_id, secret)
    if trusted is not None:
        return _link(say, confirm, yes, trusted, event, todo)
    _suggest(say, event, todo)
    return 0


def _link(say, confirm, yes: bool, client, event: str, todo: dict[str, dict]) -> int:
    """Add the links with the write key, one match per request."""
    say()
    for line in textwrap.wrap(tbalink.WARNING, 100):
        say(line)
    if not yes and not confirm(f"Add {len(todo)} video link(s) to {event} on The Blue Alliance?"):
        say("Nothing was added.")
        return 0
    problem = client.check(event)
    if problem:
        say(f"The saved write key did not work for {event}: {problem}")
        say(f"Nothing was added. (The key: {tbalink.SETUP_AGAIN})")
        return 1
    done, missing = [], []
    for part, u in todo.items():
        try:
            client.add_videos(event, {part: u["video_id"]})
            done.append(part)
        except tbalink.NotOnTBA:
            missing.append(part)
        except (TransientError, PermanentError) as e:
            say(f"Stopped at {part}: {e}")
            say(f"{len(done)} of {len(todo)} were added. Run the command again for the rest.")
            return 1
    say(f"Added {len(done)} video link(s) to The Blue Alliance ({tbalink.BASE}/event/{event}); TBA's pages can take "
        "a few minutes to show them.")
    if missing:
        say(f"Not on The Blue Alliance yet: {', '.join(missing)}.")
    return 0


def _suggest(say, event: str, todo: dict[str, dict]) -> None:
    """No write key: the two ways to get the videos onto TBA, with the links to click."""
    from .publish import youtube
    playlists = list(dict.fromkeys(u["playlist_id"] for u in todo.values() if u.get("playlist_id")))
    have = titled(todo)
    say()
    say("No Blue Alliance write key is saved on this computer, so the links cannot be added from here. Two ways:")
    say()
    say("1. You run the event or its scoring table: ask TBA for a write key with match video access (log in first)")
    say(f"     {tbalink.REQUEST_URL}")
    say(f"   then run {tbalink.SETUP_AGAIN}, and this command again.")
    say()
    say("2. Anyone with a TBA account: suggest the playlist. A TBA moderator reviews the suggestions.")
    say(f"     open   {tbalink.suggest_url(event)}")
    for pl in playlists or [None]:
        say(f"     paste  {youtube.playlist_url(pl) if pl else 'the link of the playlist on YouTube'}")
    say("   and press Submit Playlist. TBA fills in the match of each video whose title has the match's id (qm12,")
    say("   sf3m1, f1m2) and skips the rows left blank. Check the list, then press Add Suggestions.")
    if len(have) == len(todo):
        say(f"   All {len(todo)} videos to add have the id in their title.")
    else:
        say(f"   {len(have)} of the {len(todo)} videos to add have the id in their title. To add it to the others:")
        say("     streamsplitter tba --retitle" + ("   (after logging in: streamsplitter setup --section youtube)"
                                                    if not any("snippet" in u for u in todo.values()) else ""))
        say("   and for new uploads: streamsplitter setup --section tbalinks")
    say("   TBA's rules for suggestions: no videos taken from an official FIRST webcast; a video that shows the")
    say("   score screen helps the moderators.")


def _retitle(say, confirm, yes: bool, yt, todo: dict[str, dict]) -> int:
    """Put the match's TBA id at the end of the title each video in `todo` has on YouTube now, where TBA's page does
    not read the match from it yet. Nothing else about the video changes."""
    from .publish import youtube
    if yt is None:
        say("Renaming needs the videos' titles from YouTube, and this computer could not ask YouTube for them. "
            "Log in with: streamsplitter setup --section youtube")
        return 1
    new = {}
    for part, u in todo.items():
        if tbalink.guess_partial(u["title"]) == part:
            continue
        title = youtube.clean_title(f"{u['title']} ({part})")
        if tbalink.guess_partial(title) == part:
            new[part] = title
        else:                                           # something earlier in the title reads as another match
            say(f"{part}: The Blue Alliance would read '{title}' as {tbalink.guess_partial(title)}; left as it is. "
                "Type its match on TBA's page instead.")
    if not new:
        say("No video to rename: the titles already carry their match's id.")
        return 0
    units = len(new) * youtube.QUOTA_COSTS["videos.update"]
    first = next(iter(new))
    say()
    say(f"{len(new)} video(s) get the match's id at the end of the title, for example:")
    say(f"    {todo[first]['title']}")
    say(f" -> {new[first]}")
    say(f"Only the title changes. It uses {units} of YouTube's {youtube.DAILY_QUOTA} quota units a day.")
    if not yes and not confirm(f"Rename {len(new)} video(s) on YouTube?"):
        say("Nothing was renamed.")
        return 0
    done = 0
    for part, title in new.items():
        try:
            yt.set_title(todo[part]["video_id"], title, todo[part].get("snippet") or {})
            done += 1
        except (TransientError, PermanentError) as e:
            say(f"Stopped at {part}: {e}")
            say(f"{done} of {len(new)} were renamed. Run the command again for the rest.")
            return 1
    say(f"Renamed {done} video(s). Now run streamsplitter tba for the links to click.")
    return 0
