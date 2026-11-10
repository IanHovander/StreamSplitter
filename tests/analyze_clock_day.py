"""Score clock-reader output on a day stream against TBA: which played matches got a fitted episode, their offset from
TBA's actual_time, team numbers, and readings outside any match.

    python tests/analyze_clock_day.py --tba F:/.../tba_2026cancmp.json --date 2026-04-11 --t0-epoch 1775921308 \
        out_0.json out_1.json ...

--t0-epoch is a rough epoch of video second 0 (the stream's release_timestamp); the real offset is then fitted as the
median of (auto_start - (actual_time - t0_epoch)) over matched episodes.
"""

from __future__ import annotations

import argparse
import datetime
import json
import statistics
from collections import Counter
from pathlib import Path


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("outs", nargs="+")
    ap.add_argument("--tba", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--t0-epoch", type=float, required=True)
    ap.add_argument("--window", type=float, default=90.0, help="max |episode - TBA| after the offset, s")
    args = ap.parse_args(argv)
    runs = [json.loads(Path(p).read_text()) for p in args.outs]
    lo = min(r["range"][0] for r in runs)
    hi = max(r["range"][1] for r in runs)
    readings = [x for r in runs for x in r["readings"]]
    # the last run's matches() holds every episode (state carried over)
    matches = runs[-1]["matches"]
    events = runs[-1]["events"]
    tba = json.loads(Path(args.tba).read_text())["matches"]
    day = [m for m in tba if m.get("actual_time")
           and datetime.datetime.fromtimestamp(m["actual_time"]).date().isoformat() == args.date]
    day.sort(key=lambda m: m["actual_time"])
    for m in day:
        m["tv"] = m["actual_time"] - args.t0_epoch
    eps = [e for e in matches if e.get("auto_start") is not None or e.get("teleop_start") is not None]
    # coarse offset: nearest TBA match for every ok episode, then the median
    diffs = []
    for e in eps:
        if not e.get("ok"):
            continue
        near = min(day, key=lambda m: abs(e["auto_start"] - m["tv"]))
        if abs(e["auto_start"] - near["tv"]) < 600:
            diffs.append(e["auto_start"] - near["tv"])
    off = statistics.median(diffs) if diffs else 0.0
    print(f"range {lo:.0f}-{hi:.0f} s; {len(readings)} readings; {len(matches)} episodes "
          f"({sum(1 for e in matches if e.get('ok'))} ok); stream offset (AUTO start - TBA actual_time) {off:+.1f} s")
    print("events:")
    for ev in events:
        print("  ", {k: v for k, v in ev.items() if k in ("t", "event", "roi", "how", "why", "glyph_h")})
    in_range = [m for m in day if lo + 30 <= m["tv"] + off <= hi - 200]
    used = set()
    rows = []
    for m in in_range:
        cand = [e for e in eps if e.get("auto_start") is not None and abs(e["auto_start"] - (m["tv"] + off)) < args.window]
        e = min(cand, key=lambda e: abs(e["auto_start"] - (m["tv"] + off))) if cand else None
        if e is not None:
            used.add(id(e))
        teams_ok = None
        if e is not None and e.get("teams") and e["teams"].get("complete"):
            want = {c: [k[3:] for k in m["alliances"][c]["team_keys"]] for c in ("red", "blue")}
            teams_ok = all(sorted(e["teams"][c]) == sorted(want[c]) for c in ("red", "blue"))
        rows.append((m["key"], m["tv"] + off, e, teams_ok))
    print(f"\nTBA matches inside the range: {len(in_range)}")
    n_ok = n_any = n_teams = n_teams_ok = 0
    resid = []
    for key, tv, e, tok in rows:
        if e is None:
            print(f"  {key:18s} TBA~{tv:8.1f}  NO EPISODE")
            continue
        n_any += 1
        n_ok += bool(e.get("ok"))
        resid.append(e["auto_start"] - tv)
        if e.get("teams") and e["teams"].get("complete"):
            n_teams += 1
            n_teams_ok += bool(tok)
        tm = e.get("teams") or {}
        print(f"  {key:18s} TBA~{tv:8.1f} AUTO {e['auto_start']:8.2f} ({e['auto_start'] - tv:+5.1f}) end "
              f"{(e.get('match_end') or float('nan')):8.2f} rate {e.get('rate')} ok={e.get('ok')} "
              f"{'inferred' if e.get('auto_inferred') else ''} teams {tm.get('red')}/{tm.get('blue')} "
              f"complete={tm.get('complete')} right={tok} {'' if e.get('ok') else e.get('failures')}")
    print(f"\nmatched {n_any}/{len(rows)}, ok {n_ok}/{len(rows)}; residual of AUTO start vs TBA+offset: "
          f"median {statistics.median(resid) if resid else float('nan'):+.2f} s, "
          f"spread {min(resid) if resid else 0:+.2f}..{max(resid) if resid else 0:+.2f} s")
    print(f"team numbers: complete on {n_teams}/{n_any}, right {n_teams_ok}/{n_teams}")
    extra = [e for e in eps if id(e) not in used]
    print(f"\nepisodes not matched to a TBA match: {len(extra)}")
    for e in extra:
        print("   ", {k: e.get(k) for k in ("first_t", "last_t", "auto_start", "match_end", "ok", "n_transitions",
                                         "close_reason", "mode", "reason")})
    # readings outside every matched episode's [auto_start - 10, match_end + 10]
    spans = [(e["auto_start"] - 10, (e.get("match_end") or e["auto_start"] + 170) + 10)
             for _, _, e, _ in rows if e is not None]
    outside = [x for x in readings if not any(a <= x["t"] <= b for a, b in spans)]
    c = Counter(x["text"] for x in outside)
    print(f"\nreadings outside matched matches: {len(outside)} of {len(readings)}; values: {c.most_common(25)}")
    static = sum(v for k, v in c.items() if k in ("0:00", "0:20", "2:20"))
    print(f"  of which the static displays 0:00 / 0:20 / 2:20: {static}; other values: {len(outside) - static}")
    pc = [s for r in runs for s in r["per_chunk"]]
    for mode in ("search", "track"):
        sel = [s for s in pc if s["mode"] == mode]
        if sel:
            cpu = sum(s["python_cpu_s"] + s["ffmpeg_cpu_s"] for s in sel)
            med = sum(s["media_s"] for s in sel)
            print(f"chunks ending in {mode}: {len(sel)}, cpu {cpu:.0f} s for {med:.0f} s media "
                  f"({med / max(cpu, 1e-6):.1f}x real time per core), max chunk cpu "
                  f"{max(s['python_cpu_s'] + s['ffmpeg_cpu_s'] for s in sel):.1f} s")
    Path(args.outs[-1]).with_suffix(".outside.json").write_text(json.dumps(outside, indent=0))


if __name__ == "__main__":
    main()
