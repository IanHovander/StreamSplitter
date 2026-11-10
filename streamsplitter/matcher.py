"""Matcher: turns detector events into match windows, and names them from the schedule.

A match is a hypothesis anchored on its start time S. Every cue implies a value of S through its offset from the start
(2026: start sound +0, AUTO buzzer +20.75 s, 3 Bells +24.8 s, steam whistle +135.1 s, end buzzer +165.05 s; measured
on 2026casnv qm1's audio, see phase0/build_templates.py). The buzzer is ambiguous: it implies two starts, one of
which will not collect any support. Cues whose implied starts agree within `tol` support the same hypothesis.

The scoreboard clock (detect/clock.py) adds a second, independent source: a countdown episode that passed the clock
checks implies S = auto_start - lag, where lag is how far the overlay runs behind the field sounds (median over
matches where the audio heard >= 3 cues; `clock_lag_s` until there are any). It counts as two roles, so it makes a
match on its own when the commentary drowns the field sounds. The boundaries still come from the audio when it heard
enough: the start sound itself, else the median start implied by >= 2 cues, else the clock.

A hypothesis is final once the stream has run `finalize_after_s` past its expected end; it is kept if it has at least
`min_roles` roles, evidence from the end of the match (the endgame whistle, the end buzzer, or a whole clock
countdown), and does not overlap a better-supported match. The live service cuts the clip then.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

SEASON_2026 = {
    "match_s": 165.05,
    "roles": {  # role -> (detector cue, offset from the start sound in seconds)
        "start": ("start", 0.0),
        "auto_end": ("buzzer", 20.75),
        "teleop": ("bells", 24.8),
        "endgame": ("whistle", 135.1),
        "end": ("buzzer", 165.05),
    },
}

CLOCK_KEEP = ("auto_start", "auto_end", "teleop_start", "match_end", "rate", "auto_inferred", "ok", "failures",
              "close_reason", "teams")


@dataclass
class Match:
    start: float
    end: float
    roles: dict[str, float] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)
    name: str | None = None
    start_src: str = "audio"
    clock: dict | None = None
    naming: dict | None = None

    def as_dict(self) -> dict:
        return {"start": round(self.start, 3), "end": round(self.end, 3), "name": self.name,
                "start_src": self.start_src, "naming": self.naming,
                "roles": {k: round(v, 3) for k, v in sorted(self.roles.items(), key=lambda kv: kv[1])},
                "scores": self.scores, "n_roles": len(self.roles), "clock": self.clock}


class Matcher:
    def __init__(self, season: dict = SEASON_2026, tol: float = 1.5, min_roles: int = 2,
                 finalize_after_s: float = 8.0, clock_lag_s: float = 0.6):
        self.season = season
        self.tol = tol
        self.min_roles = min_roles
        self.finalize_after_s = finalize_after_s
        self.clock_lag_s = clock_lag_s
        self.hyps: list[dict] = []          # {"S": [implied starts], "roles": {role: t}, "scores": {role: s}, "clock"}
        self.final: list[Match] = []
        self.lags: list[float] = []         # clock auto_start - audio start, from matches with >= 3 audio cues
        self._clock_seen: set[float] = set()

    def lag(self) -> float:
        return median(self.lags[-15:]) if self.lags else self.clock_lag_s

    def add(self, events: list[dict]) -> None:
        for e in events:
            if e.get("kind") != "audio":
                continue
            for role, (cue, off) in self.season["roles"].items():
                if cue != e["cue"]:
                    continue
                s = e["t"] - off
                h = next((h for h in self.hyps if role not in h["roles"] and abs(self._s(h) - s) <= self.tol), None)
                if h is None:
                    h = {"S": [], "roles": {}, "scores": {}}
                    self.hyps.append(h)
                h["S"].append(s)
                h["roles"][role] = e["t"]
                h["scores"][role] = e.get("score")

    def add_clock(self, episodes: list[dict]) -> None:
        """Final scoreboard-clock episodes (ClockReader.matches()); those failing the clock checks are ignored (instant
        replays, aborted attempts). An episode arriving after its match was finalized only annotates it."""
        for ep in episodes:
            if not ep.get("final") or not ep.get("ok") or ep.get("auto_start") is None:
                continue
            key = round(ep["auto_start"], 2)
            if key in self._clock_seen:
                continue
            self._clock_seen.add(key)
            info = {k: ep.get(k) for k in CLOCK_KEEP}
            s = ep["auto_start"] - self.lag()
            m = next((m for m in self.final if abs(m.start - s) <= self.tol and m.clock is None), None)
            if m is not None:
                m.clock = info
                continue
            h = next((h for h in self.hyps if h.get("clock") is None and abs(self._s(h) - s) <= self.tol), None)
            if h is None:
                h = {"S": [], "roles": {}, "scores": {}}
                self.hyps.append(h)
            h["clock"] = info
            h["clock_S"] = s

    @staticmethod
    def _s(h: dict) -> float:
        return median(h["S"]) if h["S"] else h["clock_S"]

    @staticmethod
    def _support(h: dict) -> int:
        return len(h["roles"]) + (2 if h.get("clock") else 0)

    def poll(self, now_t: float) -> list[Match]:
        """Finalize hypotheses whose expected end + finalize_after_s has passed; return the new matches."""
        new = []
        due = [h for h in self.hyps if self._s(h) + self.season["match_s"] + self.finalize_after_s <= now_t]
        # Best-supported first, so an overlapping weaker hypothesis (e.g. a buzzer read as the wrong role) loses.
        for h in sorted(due, key=lambda h: (-self._support(h), self._s(h))):
            self.hyps.remove(h)
            if self._support(h) < self.min_roles:
                continue
            if not h.get("clock") and not {"endgame", "end"} & set(h["roles"]):
                # only the first 25 s heard: an instant replay of AUTO played with its field sounds (D3 2026hop, 66 s
                # after qm87 ended), an aborted attempt, or a stray pair (an end buzzer heard as AUTO's + a false bell)
                continue
            if "start" in h["roles"]:
                start, src = h["roles"]["start"], "audio_start"
            elif len(h["roles"]) >= 2:
                start, src = median(h["S"]), "audio"
            else:
                start, src = h["clock_S"], "clock"
            end = h["roles"].get("end", start + self.season["match_s"])
            if any(start < m.end and m.start < end for m in self.final):
                continue
            m = Match(start, end, dict(h["roles"]), dict(h["scores"]), start_src=src, clock=h.get("clock"))
            if m.clock and len(h["roles"]) >= 3:
                self.lags.append(m.clock["auto_start"] - start)
            self.final.append(m)
            new.append(m)
        return sorted(new, key=lambda m: m.start)


def fit_offset(starts: list[float], times: list[float], window: float = 3.0) -> float | None:
    """Offset d maximizing the number of detected starts within `window` of (schedule time + d); refined by median."""
    if not starts or not times:
        return None
    cands = [s - t for s in starts for t in times]
    best, best_n = None, 0
    for d in cands:
        n = sum(1 for s in starts if min(abs(s - (t + d)) for t in times) <= window)
        if n > best_n:
            best, best_n = d, n
    pairs = [s - t for s in starts for t in times if abs(s - (t + best)) <= window]
    return median(pairs) if pairs else best


def name_matches(matches: list[Match], schedule: list[dict], offset: float, window: float = 10.0) -> None:
    """Greedy one-to-one naming by nearest schedule time + offset. `schedule`: {"key", "t"} in stream seconds."""
    pairs = sorted(((abs(m.start - (s["t"] + offset)), i, j) for i, m in enumerate(matches)
                    for j, s in enumerate(schedule) if abs(m.start - (s["t"] + offset)) <= window))
    used_m, used_s = set(), set()
    for _, i, j in pairs:
        if i in used_m or j in used_s:
            continue
        matches[i].name = schedule[j]["key"]
        used_m.add(i)
        used_s.add(j)
