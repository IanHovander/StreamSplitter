"""Which clip goes where: the team's own matches to Slack (drive team needs them within minutes), every other match
to YouTube, and every match to the team's Google Drive folder and the team wiki when they are set up. Also turns a
pipeline row into the Match the destinations and templates are given.

A match counts as the team's own when one of [slack] teams is among its teams: the TBA schedule entry's six, plus the
numbers the overlay read during the match (`teams_read`, which the pipeline keeps to scheduled teams), plus, when
there is no schedule entry (no schedule yet, practice, an unnamed clip), every number the overlay's team panel showed
(`numbers_shown`, stand-in numbers like 9993 for 254B included). The union errs towards Slack on purpose: an extra
clip in the team's channel costs nothing, a missing one costs drive team their match.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..namer import teams_of
from .base import Match

_KEY = re.compile(r"_(qm|ef|qf|sf|f|p)(\d+)(?:m(\d+))?$")
_REPLAY = re.compile(r"_r(\d+)$")


def norm_team(t) -> str:
    """"frc5026", "5026", 5026 -> "5026"; offseason B teams keep their letter ("frc1323B" -> "1323B")."""
    s = str(t).strip()
    return (s[3:] if s.lower().startswith("frc") else s).upper()


def route(teams: set[str], cfg_slack: dict, cfg_youtube: dict, cfg_wiki: dict | None = None,
          cfg_drive: dict | None = None) -> list[str]:
    """Where a match goes: "slack", "youtube", "drive", "wiki", in that order, any or none. Slack: enabled and one of
    its teams plays. YouTube: enabled and either route = "all" (the default: the team's own matches go to Slack and
    YouTube, user 2026-11-04) or the match does not go to Slack (route = "others" keeps the team's own matches off
    YouTube). Google Drive and the team wiki: every match when enabled, whatever YouTube does and at official FIRST
    events too (user, 2026-11-04/05: the team's own members-only archive)."""
    own = {norm_team(t) for t in cfg_slack.get("teams") or []}
    to_slack = bool(cfg_slack.get("enabled")) and bool(own & {norm_team(t) for t in teams or ()})
    out = ["slack"] if to_slack else []
    if cfg_youtube.get("enabled") and (cfg_youtube.get("route", "all") == "all" or not to_slack):
        out.append("youtube")
    if (cfg_drive or {}).get("enabled"):
        out.append("drive")
    if (cfg_wiki or {}).get("enabled"):
        out.append("wiki")
    return out


def schedule_entry(key: str | None, schedule: list[dict] | None) -> dict | None:
    if not key:
        return None
    return next((e for e in schedule or [] if e.get("key") == key), None)


def row_teams(row: dict, entry: dict | None) -> set[str]:
    """The teams a row is routed by (see the module docstring)."""
    naming = row.get("naming") or {}
    teams = set(teams_of(entry)) if entry else set()
    teams |= {str(t) for t in naming.get("teams_read") or []}
    if entry is None:
        teams |= {str(t) for t in row.get("numbers_shown") or []}
    return {norm_team(t) for t in teams}


def _score(entry: dict, colour: str) -> int | None:
    s = ((entry.get("alliances") or {}).get(colour) or {}).get("score")
    return s if isinstance(s, int) and s >= 0 else None


def alliance_teams(entry: dict, colour: str) -> list[str]:
    return [norm_team(k) for k in ((entry.get("alliances") or {}).get(colour) or {}).get("team_keys") or []]


def match_from_row(row: dict, schedule: list[dict] | None = None, cfg_event: dict | None = None,
                   clip_key: str = "clip") -> Match:
    """The Match a pipeline row shows. Teams and scores come from the schedule entry; without one, red and blue stay
    empty (the overlay's numbers carry no alliance colour; they still route the clip). An unnamed clip keeps its
    file name as `name` and has no key."""
    cfg_event = cfg_event or {}
    naming = row.get("naming") or {}
    key = naming.get("key")
    path = (row.get(clip_key) or row.get("clip") or {}).get("path")
    name = row.get("name") or naming.get("name") or (Path(path).stem if path else "clip")
    entry = schedule_entry(key, schedule)
    event_key = ((entry or {}).get("event_key") or (key.split("_")[0] if key and "_" in key else None)
                 or cfg_event.get("key"))
    same_event = not cfg_event.get("key") or cfg_event.get("key") == event_key
    year = cfg_event.get("year") if same_event and cfg_event.get("year") else None
    if year is None and event_key and event_key[:4].isdigit():
        year = int(event_key[:4])
    level = set_n = num = None
    if entry is not None:
        level, set_n, num = entry.get("comp_level"), entry.get("set_number"), entry.get("match_number")
    elif key and (m := _KEY.search(key)):
        level = m.group(1)
        if level in ("qm", "p"):
            set_n, num = 1, int(m.group(2))
        else:
            set_n, num = int(m.group(2)), int(m.group(3) or 1)
    rep = _REPLAY.search(name)
    return Match(key=key, name=name, event_key=event_key, event_name=cfg_event.get("name") if same_event else None,
                 year=int(year) if year else None, comp_level=level, set_number=set_n, match_number=num,
                 replay=int(rep.group(1)) if rep else 1,
                 red=alliance_teams(entry, "red") if entry else [], blue=alliance_teams(entry, "blue") if entry else [],
                 red_score=_score(entry, "red") if entry else None, blue_score=_score(entry, "blue") if entry else None)
