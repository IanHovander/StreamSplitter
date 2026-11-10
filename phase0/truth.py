"""Phase 0 truth: which TBA match each found match is, allowing for time the saved video lost.

A saved day stream is not continuous wall time: when the broadcast drops, YouTube's video simply skips the missing
minutes, so (video time - TBA actual_time) steps down at each drop (D1 lost about 15 minutes after qm80). One offset
for the whole day then mis-joins everything after the first drop.

The join here is a monotone alignment: found starts and TBA times in order, each pair at offset d = found - TBA time;
consecutive joined pairs must keep d within `tol`, or start a new segment (cost `jump_cost` joins, so a new offset
needs two or more matches agreeing on it). Offsets may only step down (the video loses time, it never gains it) except
by `tol`. TBA matches between two segments are reported as `in_gap`: they may have been played while the stream was
down; phase 0 checks those by eye before counting them as missed.

Time alone cannot place a drop that lost about one match cycle: the next match then sits where the lost one would
have been (D1: qm83 lands 3.5 s from qm82's old-offset time). So the join takes `pins`, found matches identified by
eye from the scoreboard title (phase0/truth_notes.json), and the alignment must go through them.
"""

from __future__ import annotations

from statistics import median


def align(found: list[float], tba: list[float], tol: float = 4.0, jump_cost: float = 1.5,
          max_abs_d: float = 3600.0, forced: dict[int, int] | None = None) -> list[tuple[int, int, float]]:
    """Best monotone join [(i_found, j_tba, offset)]; offsets piecewise constant within `tol`. `forced` {i: j} pairs
    are the only pairs allowed for those i and j, and are worth far more than any other, so the join goes through
    them whenever monotonicity allows."""
    forced = forced or {}
    taken = set(forced.values())
    pairs = [(i, j, f - a) for i, f in enumerate(found) for j, a in enumerate(tba) if abs(f - a) <= max_abs_d
             and (forced.get(i) == j if i in forced else j not in taken)]
    pairs.sort(key=lambda p: (p[0], p[1]))
    gain = [100.0 if forced.get(i) == j else 1.0 for i, j, _ in pairs]
    best = list(gain)
    prev = [-1] * len(pairs)
    for k, (i, j, d) in enumerate(pairs):
        for q in range(k):
            i2, j2, d2 = pairs[q]
            if i2 >= i or j2 >= j:
                continue
            if abs(d - d2) <= tol:
                v = best[q] + gain[k]
            elif d < d2:
                v = best[q] + gain[k] - jump_cost
            else:
                continue
            if v > best[k]:
                best[k], prev[k] = v, q
    if not pairs:
        return []
    k = max(range(len(pairs)), key=lambda k: best[k])
    out = []
    while k >= 0:
        out.append(pairs[k])
        k = prev[k]
    return out[::-1]


def segments(joined: list[tuple[int, int, float]], tol: float = 4.0) -> list[dict]:
    """Group a join into constant-offset segments."""
    segs: list[dict] = []
    for i, j, d in joined:
        if segs and abs(d - median(segs[-1]["d"])) <= tol:
            segs[-1]["pairs"].append((i, j))
            segs[-1]["d"].append(d)
        else:
            segs.append({"pairs": [(i, j)], "d": [d]})
    for s in segs:
        s["offset"] = median(s["d"])
    return segs


def join(found_starts: list[float], sched: list[dict], duration: float, match_s: float = 175.0,
         pins: list[dict] | None = None, pin_tol: float = 30.0) -> dict:
    """`sched`: [{"key", "t"}] with t = actual_time - stream start, sorted. `pins`: [{"found_near": stream s, "key"}].
    Returns the truth join: keys (found index -> TBA key), segments, expected (TBA keys whose window lies inside the
    video within a segment), in_gap (TBA keys between segments, unresolved), unused_pins."""
    tba = [s["t"] for s in sched]
    j_of = {s["key"]: j for j, s in enumerate(sched)}
    forced, unused = {}, []
    for p in pins or []:
        near = [i for i, f in enumerate(found_starts) if abs(f - p["found_near"]) <= pin_tol]
        if len(near) == 1 and p["key"] in j_of:
            forced[near[0]] = j_of[p["key"]]
        else:
            unused.append(p["key"])
    joined = align(found_starts, tba, forced=forced)
    segs = segments(joined)
    keys = {i: sched[j]["key"] for i, j, _ in joined}
    expected, in_gap = [], []
    for j, s in enumerate(sched):
        if any(jj == j for _, jj, _ in joined):
            expected.append(s["key"])
            continue
        # which segment's range of TBA indices holds j?
        home = None
        for g in segs:
            j_lo, j_hi = g["pairs"][0][1], g["pairs"][-1][1]
            if j_lo < j < j_hi:
                home = g
        if home is None:
            before = [g for g in segs if g["pairs"][-1][1] < j]
            after = [g for g in segs if g["pairs"][0][1] > j]
            if before and after:
                in_gap.append(s["key"])
                continue
            home = (before or after or [None])[-1 if before else 0]
            if home is None:
                continue
        t = s["t"] + home["offset"]
        if 0 <= t and t + match_s <= duration:
            expected.append(s["key"])
    return {"keys": keys, "expected": expected, "in_gap": in_gap, "unused_pins": unused,
            "segments": [{"offset": round(g["offset"], 2), "n": len(g["pairs"]),
                          "first": sched[g["pairs"][0][1]]["key"], "last": sched[g["pairs"][-1][1]]["key"],
                          "found_from": round(found_starts[g["pairs"][0][0]], 1),
                          "found_to": round(found_starts[g["pairs"][-1][0]], 1)} for g in segs]}
