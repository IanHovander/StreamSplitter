"""Namer: gives a found match its schedule key using only what is known when its clip is cut.

Signals, strongest first:

1. Team numbers read off the scoreboard (detect/clock.py): the schedule entry sharing the most of the six teams, if it
   shares at least `min_shared` and no other entry shares as many, or, with fewer read, the one entry in which all
   three teams of one read side play together. The schedule is published before matches are
   played, so this works live and for a team's first match. When several entries tie (playoff matches between the
   same two alliances, e.g. finals 1-3), the tied entry whose posted TBA time fits is taken, else the first tied entry
   in schedule order that is neither named yet nor posted by TBA as played at another time (`source`
   "teams+tba_time" / "teams+order"; on a cold start in D2's finals, f1m1 was already posted when f1m2 was cut).
2. TBA's actual_time, only for matches whose result was already posted when the clip is cut (post_result_time <=
   now): the entry whose actual_time + stream offset is nearest the found start, within `window`. The offset (stream
   seconds minus TBA time) is learned from matches the team numbers named. Before any is (cold start, or no
   readable scoreboard), the offset is taken as 0 and the window widened to `cold_window`: the stream runs only
   seconds behind the field, and matches are minutes apart. A match the namer cannot name when its clip is cut is
   asked again later (the live service retries when it cuts the score clip, by which time TBA has usually posted).
3. Order: the first unplayed scheduled match after the last named one. Provisional (`source` "order").

These names are provisional. The final word is the results screen shown after the match: its QR code links to the
official match page (frc.events/2026/CAGLE/qualification/63, .../playoffs/16, .../practice/7), read when the score clip
is cut (`Namer.confirm`). It overrides the names above. On D2 the finals all share six teams and the saved video had
lost 27 minutes to stream drops, so the time rule called the Final Tiebreaker f1m2; its QR said playoffs/16, f1m3.

A match whose teams name an entry already used is a replay of it (FMS replays keep the key): it gets the key with
`_r2`, `_r3`, ... Schedule entries are TBA match records (key, comp_level, alliances, time, actual_time,
post_result_time). Practice matches are not in TBA: only their QR names them (`<event>_p<N>`).
"""

from __future__ import annotations

import re
from statistics import median

LEVELS = {"qm": 0, "ef": 1, "qf": 2, "sf": 3, "f": 4}


def alliance(entry: dict, colour: str) -> set[str]:
    return {k.removeprefix("frc") for k in entry["alliances"][colour]["team_keys"]}


def teams_of(entry: dict) -> set[str]:
    return alliance(entry, "red") | alliance(entry, "blue")


def order_key(entry: dict) -> tuple:
    return (LEVELS.get(entry["comp_level"], 9), entry["set_number"], entry["match_number"])


_RESULTS_URL = re.compile(r"/(\d{4})/([A-Za-z0-9]+)/(qualification|playoffs?|practice)/(\d+)", re.I)


_TITLE = re.compile(r"\b(qualifications?|quals?|practice|playoffs?|semi-?finals?|finals?)\b\s*(?:match\s*)?"
                    r"(\d{1,3}|tiebreaker)\b", re.I)
# double elimination: FMS's "Match 10 (R3)"; MadTown 2026's results screen "Match 11 – Round 4 Upper"
_PLAYOFF_MATCH = re.compile(r"\bmatch\s*(\d{1,2})\s*(?:\(\s*R\s*\d|[-–—]?\s*round\s*\d)", re.I)


def parse_title(text: str) -> dict | None:
    """The match title an overlay shows -> {"level", "n"}. "Qualification 40 of 120" -> qualification 40; "Practice
    3"; "Playoff 5" and FMS's "Match 10 (R3)" -> playoffs (the N-th playoff match); "Semifinal 3" -> semifinal;
    "Final 2" and "Final Tiebreaker" -> final (2, 3). Bare "Match 12" is ambiguous and gives None."""
    m = _PLAYOFF_MATCH.search(text or "")
    if m:
        return {"level": "playoffs", "n": int(m.group(1))}
    m = _TITLE.search(text or "")
    if not m:
        return None
    word, num = m.group(1).lower(), m.group(2).lower()
    n = 3 if num == "tiebreaker" else int(num)
    if word.startswith("qual"):
        return {"level": "qualification", "n": n}
    if word.startswith("practice"):
        return {"level": "practice", "n": n}
    if word.startswith("playoff"):
        return {"level": "playoffs", "n": n}
    if word.startswith("semi"):
        return {"level": "semifinal", "n": n}
    return {"level": "final", "n": n}


def parse_results_url(url: str) -> dict | None:
    """frc.events/2026/CAGLE/qualification/63 -> {"year": 2026, "event": "CAGLE", "level": "qualification", "n": 63}."""
    m = _RESULTS_URL.search(url or "")
    if not m:
        return None
    level = m.group(3).lower()
    return {"year": int(m.group(1)), "event": m.group(2).upper(), "n": int(m.group(4)),
            "level": "playoffs" if level.startswith("playoff") else level}


class Namer:
    def __init__(self, schedule: list[dict], release_ts: float, min_shared: int = 4, window: float = 30.0,
                 cold_window: float = 150.0, event: str | None = None):
        """`schedule`: TBA match records of the event. `release_ts`: wall time of stream second 0. `event`: the event
        key, for names when there is no schedule (practice, or an event TBA does not list)."""
        self.event = event or next((e["event_key"] for e in schedule if e.get("event_key")), None)
        self.schedule = sorted(schedule, key=order_key)
        self.release_ts = release_ts
        self.min_shared = min_shared
        self.window = window
        self.cold_window = cold_window
        self.used: dict[str, int] = {}          # key -> times named
        self.anchors: list[tuple[float, dict]] = []   # (found start, entry) of team-named matches
        self.last: dict | None = None

    def offset(self, now_wall: float) -> float | None:
        """Stream start minus TBA time over team-named matches whose result was posted by `now_wall`."""
        offs = [s - (e["actual_time"] - self.release_ts) for s, e in self.anchors
                if e.get("actual_time") and e.get("post_result_time") and e["post_result_time"] <= now_wall]
        return median(offs[-15:]) if offs else None

    def by_teams(self, teams: dict | None) -> tuple[dict | None, dict, list[dict]]:
        """(unique best entry or None, info, entries tied for best when they share >= min_shared teams)."""
        if not teams:
            return None, {}, []
        read = {t for c in ("red", "blue") for t in teams.get(c) or [] if t}
        if not read or not self.schedule:
            return None, {}, []
        ranked = sorted(((len(read & teams_of(e)), i) for i, e in enumerate(self.schedule)), reverse=True)
        best, second = ranked[0][0], (ranked[1][0] if len(ranked) > 1 else 0)
        info = {"read": sorted(read), "shared": best, "runner_up": second}
        if best < self.min_shared:
            # one side read whole (D2 qm63: red's three, blue's none): the entries that have those three together
            whole = [e for e in self.schedule if any(alliance(e, c) <= read for c in ("red", "blue"))]
            info["whole_alliance"] = bool(whole)
            if len(whole) == 1:
                return whole[0], info, []
            return None, info, sorted(whole, key=order_key)
        if best > second:
            return self.schedule[ranked[0][1]], info, []
        return None, info, sorted((self.schedule[i] for n, i in ranked if n == best), key=order_key)

    def by_time(self, start: float, now_wall: float) -> dict | None:
        off = self.offset(now_wall)
        best, best_d = None, self.window
        if off is None:
            off, best_d = 0.0, self.cold_window
        for e in self.schedule:
            a, p = e.get("actual_time"), e.get("post_result_time")
            if not a or not p or p > now_wall:
                continue
            d = abs(start - (a - self.release_ts + off))
            if d <= best_d:
                best, best_d = e, d
        return best

    def played_elsewhere(self, e: dict, start: float, now_wall: float) -> bool:
        """TBA has posted this match as played, at a time that does not fit the found start."""
        a, p = e.get("actual_time"), e.get("post_result_time")
        if not a or not p or p > now_wall:
            return False
        off = self.offset(now_wall)
        off, win = (0.0, self.cold_window) if off is None else (off, self.window)
        return abs(start - (a - self.release_ts + off)) > win

    def by_order(self, start: float, now_wall: float) -> dict | None:
        if self.last is None:
            return None
        k = order_key(self.last)
        return next((e for e in self.schedule if order_key(e) > k and e["key"] not in self.used
                     and not self.played_elsewhere(e, start, now_wall)), None)

    def name(self, start: float, teams: dict | None, now_wall: float) -> dict:
        """Name the match found at stream second `start`; `now_wall` is the wall time the clip is cut."""
        entry, info, tied = self.by_teams(teams)
        source = "teams"
        if entry is None and tied:
            t = self.by_time(start, now_wall)
            if t is not None and any(t["key"] == e["key"] for e in tied):
                entry, source = t, "teams+tba_time"
            else:
                entry = next((e for e in tied if e["key"] not in self.used
                              and not self.played_elsewhere(e, start, now_wall)), None)
                source = "teams+order"
        if entry is None:
            entry, source = self.by_time(start, now_wall), "tba_time"
        if entry is None:
            entry, source = self.by_order(start, now_wall), "order"
        if entry is None:
            return {"name": None, "source": None, **info}
        if source == "teams":
            self.anchors.append((start, entry))
        n = self.used.get(entry["key"], 0) + 1
        self.used[entry["key"]] = n
        if source != "order" and (self.last is None or order_key(entry) >= order_key(self.last)):
            self.last = entry
        name = entry["key"] if n == 1 else f"{entry['key']}_r{n}"
        return {"name": name, "key": entry["key"], "source": source, **info}

    def key_for(self, parsed: dict) -> str | None:
        """The schedule key a results-screen QR or an overlay title names. Playoff N is the N-th playoff match in
        TBA's order (any bracket size: 8-alliance double elimination has sf1-sf13 then f1m1-f1m3 as 14-16). Practice is
        not in TBA. Without a schedule (an event TBA does not list), the key is built from the title alone."""
        event, n, level = self.event, parsed["n"], parsed["level"]
        if event is None:
            return None
        known = {e["key"] for e in self.schedule}
        if level == "practice":
            return f"{event}_p{n}"
        if level == "qualification":
            key = f"{event}_qm{n}"
        elif level == "semifinal":
            key = f"{event}_sf{n}m1"
        elif level == "final":
            key = f"{event}_f1m{n}"
        else:
            po = [e for e in self.schedule if e["comp_level"] != "qm"]
            if po:
                return po[n - 1]["key"] if 1 <= n <= len(po) else None
            key = f"{event}_playoff{n}"
        return key if not known or key in known else None

    def _take(self, key: str, source: str, info: dict) -> dict:
        n = self.used.get(key, 0) + 1
        self.used[key] = n
        e = next((x for x in self.schedule if x["key"] == key), None)
        if e is not None and (self.last is None or order_key(e) >= order_key(self.last)):
            self.last = e
        return {"name": key if n == 1 else f"{key}_r{n}", "key": key, "source": source, **info}

    def name_overlay(self, start: float, teams: set[str] | None, title: dict | None, now_wall: float) -> dict:
        """Name a match from what its overlay showed: its title ("Qualification 12"), checked against the team numbers
        read during it, else the team numbers, posted TBA time and schedule order (`name`).

        A title whose schedule entry shares fewer than 3 of the read teams, when the teams alone name another entry
        uniquely, is taken as a misread title and the teams win."""
        info = {"title": title, "teams_read": sorted(teams or [])}
        key = self.key_for(title) if title else None
        if key is not None:
            e = next((x for x in self.schedule if x["key"] == key), None)
            if e is None or not teams:
                return self._take(key, "title", info)
            shared = len(teams & teams_of(e))
            if shared >= 3:
                self.anchors.append((start, e))
                return self._take(key, "title+teams", {**info, "shared": shared})
            other, tinfo, _ = self.by_teams({"red": sorted(teams)})
            if other is None or other["key"] == key:
                return self._take(key, "title", {**info, "shared": shared})
            self.anchors.append((start, other))
            return self._take(other["key"], "teams", {**info, **tinfo, "title_disagrees": key})
        naming = self.name(start, {"red": sorted(teams)} if teams else None, now_wall)
        return {**naming, **info}

    def confirm(self, naming: dict | None, url: str | None) -> dict | None:
        """Final name from the results screen's QR `url`; None when it does not name a match. Moves the use count from
        the provisional key, so a later replay of either is still numbered right."""
        return self.confirm_parsed(naming, parse_results_url(url), url)

    def confirm_parsed(self, naming: dict | None, parsed: dict | None, evidence: str | None = None,
                       source: str = "results_qr") -> dict | None:
        """`confirm` for a match title already parsed (a results-screen QR, or the title the results screen shows)."""
        url = evidence
        key = self.key_for(parsed) if parsed else None
        if key is None:
            return None
        old = (naming or {}).get("key")
        if old == key:
            return {**naming, "qr": url, "qr_confirmed": True}
        if old:
            self.used[old] = self.used.get(old, 1) - 1
            if self.used[old] <= 0:
                del self.used[old]
        n = self.used.get(key, 0) + 1
        self.used[key] = n
        return {"name": key if n == 1 else f"{key}_r{n}", "key": key, "source": source, "qr": url,
                "provisional": (naming or {}).get("name"), "provisional_source": (naming or {}).get("source")}
