"""Titles, descriptions and playlist names for the YouTube uploads, filled in from a Match.

A format is plain text with {field} placeholders ({{ and }} for a literal brace). Only the fields in FIELDS may be
used, as plain {name}: no {name!r}, {name:>5} or {red.x}, so a format typed into the setup wizard cannot fail at
upload time in some new way. validate() says what is wrong in words a person can act on; example() shows a format
filled in for a sample match.

Values that are not known yet (no score posted, a clip the namer could not name) are blank, never "None":

- a [bracketed part] that holds a field is left out while any of its fields is blank: "Red (Teams {red})[ -
  {red_score}]" reads "Red (Teams 254, 1678, 5026)" until TBA posts the score. Brackets without a field are ordinary
  text;
- a line that uses a blank field outside brackets is left out entirely (a practice match has no TBA link line);
- a one-line format (a title) is never dropped: its blanks are left empty and the spaces and dashes around them tidied.

Defaults follow the FIRST Robotics Competition channel (docs/research/youtube.md, part B): playlists "<year> <event>",
titles "<match> - <year> <event>" with FIRST's match labels (Qualification 12, Match 7 (R2) for double-elimination
playoffs, Final 1, Final 2, Final Tiebreaker), and the description's first four lines in FIRST's shape, linking TBA
instead of FRC Events. FIRST's "Uploaded by MatchLIVE" and copyright lines are deliberately not copied.
"""

from __future__ import annotations

import re
import string

from .base import Match

PLAYLIST_FORMAT = "{year} {event_name}"
TITLE_FORMAT = "{match_title} - {year} {event_name}"
# the same with the match's TBA id at the end ("... (qm12)"): TBA's playlist page then knows each video's match by
# itself (publish/tbalink.py guess_partial); the brackets leave it off practice matches and unnamed clips
TBA_TITLE_SUFFIX = "[ ({tba_id})]"
TBA_TITLE_FORMAT = TITLE_FORMAT + TBA_TITLE_SUFFIX
CREDIT = "Recorded from the event webcast with StreamSplitter."
DESCRIPTION_FORMAT = (
    "{match_title} - {year} {event_name}\n"
    "Red (Teams {red})[ - {red_score}]\n"
    "Blue (Teams {blue})[ - {blue_score}]\n"
    "{tba_url}\n"
    "\n"
    + CREDIT
)

FIELDS = {
    "year": "the event's year: 2026",
    "event_name": "TBA's name for the event: MadTown Throwdown",
    "event_key": "TBA's event code: 2026mttd",
    "match_title": "FIRST's label: Qualification 12, Practice 3, Match 7 (R2), Final 2, Final Tiebreaker; a replay "
                   "adds (Replay), an aborted attempt the pipeline kept (<key>_partial) adds (Partial)",
    "level": "Qualification, Practice, Playoff, Final (Quarterfinal / Semifinal before 2023)",
    "number": "the match number: 12 for Qualification 12, 7 for Match 7 (R2)",
    "replay": "Replay for the second play of a match, Replay 2 for the third; blank otherwise",
    "red": "red alliance teams: 254, 1678, 5026",
    "blue": "blue alliance teams",
    "red_score": "red's final score once TBA posts it",
    "blue_score": "blue's final score once TBA posts it",
    "match_key": "TBA's match key: 2026mttd_qm12",
    "tba_id": "the match's short id on The Blue Alliance: qm12, sf3m1, f1m2 (not for practice matches or partial "
              "clips); in a title it lets TBA's playlist page match the video by itself",
    "tba_url": "the match's page on The Blue Alliance (not for practice matches)",
    "event_url": "the event's page on The Blue Alliance",
}

# YouTube's own limits (characters for titles, bytes for descriptions); < and > are refused in both
LIMITS = {"title": 100, "playlist": 150, "description": 5000}

SAMPLE = Match(key="2026mttd_qm12", name="2026mttd_qm12", event_key="2026mttd", event_name="MadTown Throwdown",
               year=2026, comp_level="qm", set_number=1, match_number=12,
               red=["5026", "254", "1678"], blue=["604", "8033", "199"], red_score=112, blue_score=98)

# Double-elimination round of each playoff match, as FIRST titles them ("Match 10 (R3)"). The 8-alliance bracket's
# rounds are from FIRST's own titles (research B.2); the 4-alliance bracket's are from the game manual's bracket and
# unconfirmed against FIRST's titles.
PLAYOFF_ROUNDS = {
    8: {1: 1, 2: 1, 3: 1, 4: 1, 5: 2, 6: 2, 7: 2, 8: 2, 9: 3, 10: 3, 11: 4, 12: 4, 13: 5},
    4: {1: 1, 2: 1, 3: 2, 4: 2, 5: 3},
}

_LEVEL_WORD = {"qm": "Qualification", "p": "Practice", "ef": "Octofinal", "qf": "Quarterfinal", "sf": "Semifinal",
               "f": "Final"}
_KEY = re.compile(r"_(qm|ef|qf|sf|f|p|playoff)(\d+)(?:m(\d+))?(?:_r(\d+))?(?:_partial(\d*))?$")
_TBA_ID = re.compile(r"^(qm\d+|(?:ef|qf|sf|f)\d+m\d+)$")
_OPTIONAL = re.compile(r"\[([^\[\]]*)\]")
_DOUBLE_ELIM_FROM = 2023     # TBA: sf<N>m1 is playoff match N, f1m<N> the finals


class TemplateError(ValueError):
    """A title, description or playlist format that cannot be used; the message says how to fix it."""


def fields_used(fmt: str) -> list[str]:
    """The fields `fmt` uses, in order. Raises TemplateError for an unknown field or broken braces."""
    try:
        parsed = list(string.Formatter().parse(fmt))
    except ValueError as e:
        raise TemplateError(f"{e}: write a value as {{field}} and a literal brace as {{{{ or }}}}") from None
    names = []
    for _literal, name, spec, conv in parsed:
        if name is None:
            continue
        if name not in FIELDS:
            shown = "{}" if name == "" else f"{{{name}}}"
            raise TemplateError(f"unknown field {shown}; the fields you can use are: "
                                + ", ".join(f"{{{f}}}" for f in FIELDS))
        if conv or spec:
            raise TemplateError(f"write {{{name}}} plainly, without ! or : after the name")
        names.append(name)
    return names


def validate(fmt: str, kind: str = "description") -> None:
    """Check a format before it is saved: fields, braces, and YouTube's rules for `kind` ("title", "playlist",
    "description"): one line for a title or playlist, no < or >, and the sample within YouTube's length limit."""
    if not fmt or not fmt.strip():
        raise TemplateError(f"the {kind} format is empty")
    fields_used(fmt)
    if "<" in fmt or ">" in fmt:
        raise TemplateError("YouTube does not allow < or > in titles and descriptions")
    if kind in ("title", "playlist") and ("\n" in fmt.strip() or "\r" in fmt.strip()):
        raise TemplateError(f"a {kind} must be one line")
    limit = LIMITS.get(kind)
    sample = example(fmt)
    size = len(sample.encode("utf-8")) if kind == "description" else len(sample)
    if limit and size > limit:
        unit = "bytes" if kind == "description" else "characters"
        raise TemplateError(f"the sample {kind} is {size} {unit}; YouTube allows {limit}")


def default_description(team: str | None = None) -> str:
    """DESCRIPTION_FORMAT with the credit naming the uploading team, as the research suggests ("Recorded from the
    event webcast by FRC Team 5026"); the wizard offers this as the default."""
    if not team:
        return DESCRIPTION_FORMAT
    return DESCRIPTION_FORMAT.replace(CREDIT, f"Recorded from the event webcast by FRC Team {team}.")


def _parts(m: Match) -> tuple[str | None, int | None, int | None, int]:
    """(comp_level, set_number, match_number, replay): the Match's own fields, with what is missing read from its
    clip name or key ("2026mttd_sf3m1", "2026mttd_qm12_r2", the namer's "2026mttd_playoff5" for an event TBA does
    not list)."""
    p_level = p_set = p_num = None
    p_replay = 1
    for text in (m.name, m.key):
        hit = _KEY.search(text or "")
        if hit:
            p_level, a, b = hit.group(1), int(hit.group(2)), hit.group(3)
            if p_level in ("qm", "p", "playoff"):
                p_set, p_num = 1, a
            else:
                p_set, p_num = a, int(b) if b else 1
            p_replay = int(hit.group(4) or 1)
            break
    level = m.comp_level or p_level
    same = level == p_level
    set_n = m.set_number if m.set_number is not None else (p_set if same else None)
    num = m.match_number if m.match_number is not None else (p_num if same else None)
    replay = m.replay if (m.replay or 1) > 1 else p_replay
    return level, set_n, num, replay


def _year(m: Match) -> int | None:
    if m.year:
        return int(m.year)
    key = m.event_key or m.key or m.name or ""
    return int(key[:4]) if key[:4].isdigit() else None


def _numbered(word: str, n: int | None) -> str:
    return word if n is None else f"{word} {n}"


def match_title(m: Match, alliances: int = 8) -> tuple[str, str, str]:
    """(match_title, level, number) as FIRST titles them, replays marked. From 2023 (double elimination) TBA's
    sf<N>m1 is "Match N (R<round>)" and f1m1-f1m3 are Final 1, Final 2, Final Tiebreaker; before 2023, "Quarterfinal 2
    Match 1" and so on. `alliances`: the playoff bracket (8, or 4 for small events) that sets the round. A clip with
    no known level is titled by its clip name."""
    level, set_n, num, replay = _parts(m)
    year = _year(m)
    double_elim = year is None or year >= _DOUBLE_ELIM_FROM
    if level == "playoff" or (level == "sf" and double_elim):
        word, n = "Playoff", set_n
        rnd = PLAYOFF_ROUNDS.get(alliances, {}).get(n) if n is not None else None
        title = _numbered("Match", n) + (f" (R{rnd})" if rnd else "")
    elif level == "f":
        word, n = "Final", num
        if double_elim and n is not None and n >= 3:
            title = "Final Tiebreaker" + (f" {n - 2}" if n > 3 else "")
        else:
            title = _numbered("Final", n)
    elif level in ("ef", "qf", "sf"):
        word, n = _LEVEL_WORD[level], num
        title = f"{word} {set_n} Match {num}" if set_n is not None and num is not None else _numbered(word, set_n)
    elif level in ("qm", "p"):
        word, n = _LEVEL_WORD[level], num
        title = _numbered(word, n)
    else:
        return (m.name or m.key or "Match") + _replay_suffix(replay), "", ""
    return title + _replay_suffix(replay) + _partial_suffix(m), word, "" if n is None else str(n)


def _partial_suffix(m: Match) -> str:
    """ (Partial) for the pipeline's "<key>_partial" clips (an attempt that was stopped), (Partial 2) for the next."""
    hit = _KEY.search(m.name or "")
    if not hit or hit.group(5) is None:
        return ""
    return f" (Partial {hit.group(5)})" if hit.group(5) else " (Partial)"


def _replay_word(replay: int) -> str:
    return "" if replay <= 1 else "Replay" if replay == 2 else f"Replay {replay - 1}"


def _replay_suffix(replay: int) -> str:
    word = _replay_word(replay)
    return f" ({word})" if word else ""


def _teams(teams: list[str] | None) -> str:
    return ", ".join(str(t).removeprefix("frc") for t in teams or [] if t)


def _score(s: int | None) -> str:
    # TBA posts -1 for a match that has not been played or scored yet
    return "" if s is None or s < 0 else str(s)


def values(m: Match, alliances: int = 8) -> dict[str, str]:
    """Every field for `m` as text; "" when not known."""
    title, level, number = match_title(m, alliances)
    year = _year(m)
    event_key = m.event_key or (m.key.split("_")[0] if m.key and "_" in m.key else "")
    level_code, _set, _num, replay = _parts(m)
    tba_id = m.key[len(event_key) + 1:] if m.key and event_key and m.key.startswith(f"{event_key}_") else ""
    if not _TBA_ID.match(tba_id) or "_partial" in (m.name or ""):
        tba_id = ""
    return {
        "year": str(year) if year else "",
        "event_name": m.event_name or "",
        "event_key": event_key or "",
        "match_title": title,
        "level": level,
        "number": number,
        "replay": _replay_word(replay),
        "red": _teams(m.red),
        "blue": _teams(m.blue),
        "red_score": _score(m.red_score),
        "blue_score": _score(m.blue_score),
        "match_key": m.key or "",
        "tba_id": tba_id,
        # practice matches are not on TBA
        "tba_url": f"https://www.thebluealliance.com/match/{m.key}" if m.key and level_code != "p" else "",
        "event_url": f"https://www.thebluealliance.com/event/{event_key}" if event_key else "",
    }


def _tidy(line: str) -> str:
    """A one-line format rendered with blanks: no doubled spaces, no dangling separators."""
    line = re.sub(r"\s{2,}", " ", line)
    line = re.sub(r"(\s[-|:,])+\s*$", "", line)
    line = re.sub(r"^\s*([-|:,]\s)+", "", line)
    return line.strip()


def _optional(line: str, vals: dict[str, str]) -> str:
    """Keep a [part] with fields when they all have values (without its brackets), drop it otherwise."""
    def one(hit: re.Match) -> str:
        inner = hit.group(1)
        used = fields_used(inner)
        if not used:
            return hit.group(0)             # ordinary brackets
        return inner if all(vals[f] for f in used) else ""
    return _OPTIONAL.sub(one, line)


def render(fmt: str, match: Match, alliances: int = 8) -> str:
    """`fmt` filled in for `match` (blank-safe; see the module docstring). `alliances`: the playoff bracket size."""
    fields_used(fmt)
    vals = values(match, alliances)
    lines = fmt.replace("\r\n", "\n").split("\n")
    out = []
    for line in lines:
        line = _optional(line, vals)
        if any(not vals[f] for f in fields_used(line)):
            if len(lines) == 1:
                return _tidy(line.format_map(vals))
            continue
        out.append(line.format_map(vals))
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)        # a dropped line between two blank lines
    return text.strip("\n")


def render_playlist(fmt: str, event_key: str | None, event_name: str | None, year: int | None) -> str:
    """The playlist name for an event (only the event fields mean anything here)."""
    return render(fmt, Match(key=None, name="", event_key=event_key, event_name=event_name, year=year))


def example(fmt: str, match: Match | None = None) -> str:
    """`fmt` filled in for a sample match (Qualification 12 of the 2026 MadTown Throwdown), for the wizard to show."""
    return render(fmt, match or SAMPLE)
