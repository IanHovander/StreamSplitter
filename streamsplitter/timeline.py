"""Timeline: turns the overlay timer's countdown episodes (detect/overlay.py) into matches, for any game and display.

A match is two countdowns on the same timer place:
- a short one (autonomous: from_value <= AUTO_MAX);
- then, starting within GAP_MAX s of its zero, a long one (teleop: at least TELEOP_MIN_CHANGES one-second changes).
  Its zero is the match end.

A stream that lost video inside a period (detect/overlay.py SKIP_MAX_S) has C0 > C: the period began at C0 - its
length and ends at C, and the clip holds what the stream kept between them.

The start is C_auto - L_auto. L_auto is the value the display sat at before the match (from_value, the full auto time)
when it was seen frozen there for AUTO_FROZEN_S before counting began; otherwise see `period_length`.

The event's own values are learned from the matches seen whole: L_auto, the pause between auto and teleop, and L_tele.
With them, a teleop seen without its auto (overlay hidden during auto, or recording begun mid-match) still gives a
start, C_tele - L_tele - pause - L_auto (`start_src` "inferred"). Before anything is learned, a teleop alone is not
a match: other countdowns (timeouts, breaks) are long too; after, one first seen well above teleop's length is not
either (INFER_FROM_MARGIN_S).

A match is final, ready to cut, once its teleop either:
- closed at zero (`end_src` "zero"), or
- was seen until within END_SEEN_S of its zero time and then lost ("seen_to_end").

A teleop that stopped above zero is an aborted match ("aborted", not cut; the replay is a new match). A teleop lost
earlier than END_SEEN_S before its zero (a stream drop near the end) is cut at its extrapolated zero once that time
has passed by EXTRAPOLATE_WAIT_S ("extrapolated"). If it was last seen more than PARTIAL_S before its zero, the match
may have been aborted with the overlay taken down (Chezy 2026 qm54: gone at 1:10 left, replayed 5 minutes later), so
it is marked `partial`: cut, but under its own name, not counted as the match.
"""

from __future__ import annotations

import math
from statistics import median

AUTO_MAX = 30
GAP_MAX = 12.0
TELEOP_MIN_CHANGES = 20
AUTO_MIN_CHANGES = 3
AUTO_FROZEN_S = 1.2
END_SEEN_S = 1.5
PARTIAL_S = 20.0          # a teleop last seen more than this before its zero may have been aborted
HABIT_TOL_S = 10.0
EXTRAPOLATE_WAIT_S = 5.0
# a teleop without its auto is inferred only if its countdown was first seen at most this far above teleop's length:
# MadTown 2026's "Field Break" timer sat in the match timer's place and counted down from minutes, and its last 2:20
# was taken for f1m1 and f1m2 (an unproven teleop length is set to the learned one, so the length check alone passed)
INFER_FROM_MARGIN_S = 5


MERGE_C_S = 0.6
MERGE_GAP_S = 150.0       # one place counting to the same zero (+-0.6 s) is one countdown, even after a long hide


def merge(episodes: list[dict]) -> list[dict]:
    """Join consecutive episodes of one place that count to the same zero (a misread or a brief overlay change split
    them, D2 2026cagle f1m3 split its teleop three times): one countdown, the first one's start, the last one's end."""
    out: list[dict] = []
    for e in sorted(episodes, key=lambda e: (e["place"], e["first_change"])):
        p = out[-1] if out else None
        # e's zero when it began (C0) is p's zero when p ended (C), however much video either lost
        if (p is not None and p["place"] == e["place"] and abs(p["C"] - e.get("C0", e["C"])) <= MERGE_C_S
                and e["t_first"] - p["t_last"] <= MERGE_GAP_S):
            n = p["n_changes"] + e["n_changes"]
            skipped = round(p.get("skipped", 0.0) + e.get("skipped", 0.0), 3)
            C = e["C"] if e.get("skipped") else round((p["C"] * p["n_changes"] + e["C"] * e["n_changes"]) / n, 3)
            out[-1] = {**e, "t_first": p["t_first"], "first_change": p["first_change"], "from_value": p["from_value"],
                       "n_changes": n, "C": C, "C0": p.get("C0", p["C"]) if skipped else C, "skipped": skipped,
                       "pieces": p.get("pieces", 1) + 1}
        else:
            out.append(e)
    return sorted(out, key=lambda e: e["first_change"])


LOST_SLACK_S = 6.0        # teleop may seem to start this much before auto's zero when the stream lost video
LOST_TELEOP_MIN_CHANGES = 10   # a teleop that lost video and is paired with an auto (lost 1/3: ~2/3 the changes)
LOST_AUTO_BEFORE_S = 60.0      # its auto reached zero at most this long before the teleop was first seen


def _lost_slack(e1: dict, e2: dict) -> float:
    """When either countdown lost video, teleop's first seconds may be gone too (MadTown 2026 practice: its 2:20 and
    2:19 never shown), so its start (C0 - length) can fall before auto's zero; and auto, read once a second until it
    is seen counting, may show only 2 one-second steps (AUTO_MIN_CHANGES less one)."""
    return LOST_SLACK_S if (e1.get("skipped") or e2.get("skipped")) else 0.0


def period_length(e: dict) -> tuple[int, bool]:
    """(length, proven) of the period an episode counts down. Proven when the display sat at from_value for
    AUTO_FROZEN_S before counting began (FMS shows 0:20 before a match). Otherwise the period may have started before
    the display was on screen (Chezy 2026's broadcast fades the overlay in ~1.5 s into the match, first showing 0:19),
    so the length is the smallest multiple of 5 s at or above from_value: every FRC period so far is one (auto 15 or
    20 s, teleop 135 or 140 s)."""
    if e["first_change"] - e["t_first"] >= AUTO_FROZEN_S:
        return e["from_value"], True
    return 5 * math.ceil(e["from_value"] / 5), False


class Timeline:
    def __init__(self):
        self.learned: list[dict] = []        # {"L_auto", "pause", "L_tele"} of matches seen whole
        self.matches: list[dict] = []
        self.rejected: list[dict] = []
        self._done: list[tuple[int, float]] = []     # (place, C) of teleops already decided
        self.lost_left: list[float] = []     # how long before its zero each extrapolated teleop was last seen

    def values(self) -> dict | None:
        """The event's period lengths and pause: from matches whose displays proved them, else from all matches."""
        use = [x for x in self.learned if x["proven"]] or self.learned
        if not use:
            return None
        return {k: median(x[k] for x in use[-15:]) for k in ("L_auto", "pause", "L_tele")}

    def update(self, episodes: list[dict], now: float) -> list[dict]:
        """New final matches, given every episode so far (closed and open) and the current stream time."""
        out = []
        eps = merge(episodes)
        for e2 in eps:
            lost = bool(e2.get("skipped"))
            # a teleop counts down for longer than any auto (an auto has 20 changes too); one that lost video shows
            # fewer one-second changes, and is then a match only with an auto (LOST_TELEOP_MIN_CHANGES)
            if (e2["n_changes"] < (LOST_TELEOP_MIN_CHANGES if lost else TELEOP_MIN_CHANGES)
                    or e2["C"] - e2["first_change"] < AUTO_MAX + 5):
                continue
            key = (e2["place"], e2["C"])
            # one match per zero, whichever place counted it: MadTown 2025's overlay timer and a second timer
            # readable in its lower camera view both gave qm55 (the final test's duplicate qm55_r2)
            if any(abs(c - key[1]) < 2.0 for _, c in self._done):
                continue
            end_src = self._end_state(e2, now)
            if end_src is None:
                continue                      # still running, or not yet past its zero
            vals = self.values()
            L_tele, tele_proven = period_length(e2)
            if not tele_proven and vals:
                L_tele = vals["L_tele"]
            tele_start = e2.get("C0", e2["C"]) - L_tele          # where it began, even if video was lost since
            autos = [e1 for e1 in eps if e1 is not e2 and e1["place"] == e2["place"]
                     and e1["from_value"] <= AUTO_MAX
                     and e1["n_changes"] >= AUTO_MIN_CHANGES - (1 if _lost_slack(e1, e2) else 0)
                     and -0.5 - _lost_slack(e1, e2) <= tele_start - e1["C"] <= GAP_MAX]
            lost_left = e2["C"] - e2["t_last"]
            # Some broadcasts take the overlay down at the same point every match (Eagan 2025: at 0:40 left); after
            # one earlier match did the same, it is the broadcast's habit, not an abort
            habit = any(abs(lost_left - x) <= HABIT_TOL_S for x in self.lost_left)
            m = {"end": e2["C"], "end_src": end_src, "teleop": e2, "place": e2["place"], "L_tele": L_tele,
                 "partial": end_src == "extrapolated" and lost_left > PARTIAL_S and not habit}
            if end_src == "extrapolated":
                self.lost_left.append(lost_left)
            if end_src == "aborted":
                self._done.append(key)
                self.rejected.append({**m, "why": "aborted"})
                continue
            near = self._autos_before(e2, eps) if lost else []
            if near:
                # lost video: the auto pieces that reached zero just before teleop was first seen; the earliest start
                # any of them gives, so a clip never begins after its auto did
                def start_of(e):
                    L, proven = period_length(e)
                    return e.get("C0", e["C"]) - (L if proven or not vals else vals["L_auto"]), L, proven
                e1 = min(near, key=lambda e: start_of(e)[0])
                start, L_auto, auto_proven = start_of(e1)
                m.update(start=start, start_src="auto", auto=e1, L_auto=L_auto, L_auto_proven=auto_proven,
                         pause=round(tele_start - e1["C"], 3))
            elif autos:
                e1 = max(autos, key=lambda e: e["C"])
                L_auto, auto_proven = period_length(e1)
                if not auto_proven and vals:
                    L_auto = vals["L_auto"]
                m.update(start=e1.get("C0", e1["C"]) - L_auto, start_src="auto", auto=e1, L_auto=L_auto,
                         L_auto_proven=auto_proven,
                         pause=round(tele_start - e1["C"], 3))
                self.learned.append({"L_auto": L_auto, "pause": tele_start - e1["C"], "L_tele": L_tele,
                                     "proven": auto_proven and tele_proven})
            elif (vals and abs(L_tele - vals["L_tele"]) <= 1 and e2["n_changes"] >= TELEOP_MIN_CHANGES
                  and e2["from_value"] <= vals["L_tele"] + INFER_FROM_MARGIN_S):
                m.update(start=tele_start - vals["pause"] - vals["L_auto"], start_src="inferred", auto=None,
                         L_auto=vals["L_auto"], pause=vals["pause"])
            else:
                if now - e2["C"] > 60.0:      # give up on it: no auto, nothing learned that fits
                    self._done.append(key)
                    self.rejected.append({**m, "why": "no auto and nothing learned that fits"})
                continue
            m["start"], m["end"] = round(m["start"], 3), round(m["end"], 3)
            self._done.append(key)
            self.matches.append(m)
            out.append(m)
        return out

    @staticmethod
    def _autos_before(e2: dict, eps: list[dict]) -> list[dict]:
        """For a teleop that lost video: auto countdowns on its place (at least one one-second change) that reached
        zero from LOST_AUTO_BEFORE_S before teleop was first seen to 1 s after. Its start may not have been seen (the
        broadcast hid the overlay: MadTown 2026 practice 6 and 7 came back at 1:35 and 1:32), so neither its length
        nor its start is known; the auto just before it is its auto."""
        return [e1 for e1 in eps if e1 is not e2 and e1["place"] == e2["place"] and e1["from_value"] <= AUTO_MAX
                and e1["n_changes"] >= 1 and -LOST_AUTO_BEFORE_S <= e1["C"] - e2["t_first"] <= 1.0]

    @staticmethod
    def _end_state(e2: dict, now: float) -> str | None:
        if not e2["closed"]:
            return None
        if e2["reason"] == "zero":
            return "zero"
        if e2["reason"] == "stopped":
            return "aborted"                  # stopped above zero
        if e2["t_last"] >= e2["C"] - END_SEEN_S:
            return "seen_to_end"
        if now >= e2["C"] + EXTRAPOLATE_WAIT_S:
            return "extrapolated"
        return None
