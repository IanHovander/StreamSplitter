"""Phase 0 fake-live run of one day: chunks in order -> detectors -> matcher -> namer -> cutter, as a live run would.

    .venv\\Scripts\\python.exe phase0\\run_day.py D1 [--templates cues_v0.npz] [--tag run1] [--clock] [--cut] [--results]

Everything a clip gets (its boundaries, its name, its score piece) is decided from chunks that had arrived by then, and
from TBA data that was already posted by then (post_result_time). Writes <data>/phase0/runs/<day>_<tag>/:
events.jsonl (audio cues, results-screen samples, clock episodes), clock.jsonl (clock readings), matches.json (final
matches) and timing.json (processing time per chunk). Only the chunk index of the named day is read.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from streamsplitter import config, cutter  # noqa: E402
from streamsplitter.detect import audio, results  # noqa: E402
from streamsplitter.matcher import Matcher  # noqa: E402
from streamsplitter.namer import Namer  # noqa: E402
from phase0.fake_live import iter_chunks, make_chunks  # noqa: E402
from phase0.get_day import DAYS, SEALED  # noqa: E402


def tba_matches(tag: str) -> list[dict]:
    event = DAYS[tag][0]
    return json.loads((config.data_root() / "phase0" / f"tba_{event}.json").read_text(encoding="utf-8"))["matches"]


def tba_schedule(tag: str, release_ts: float, duration_s: float) -> list[dict]:
    """Played matches of the day's event inside the video, as {"key", "t"} with t = actual_time - stream start."""
    out = []
    for m in tba_matches(tag):
        a = m.get("actual_time")
        if a and -600 <= a - release_ts <= duration_s + 600:
            out.append({"key": m["key"], "t": float(a - release_ts), "post": (m.get("post_result_time") or 0) - a})
    return sorted(out, key=lambda s: s["t"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("day")
    ap.add_argument("--templates", default="cues_v0.npz")
    ap.add_argument("--tag", default="run1")
    ap.add_argument("--thr", type=float, default=0.40)
    ap.add_argument("--clock", action="store_true", help="run the scoreboard-clock reader (match source and teams)")
    ap.add_argument("--cut", action="store_true", help="cut each match clip as soon as the match is final")
    ap.add_argument("--results", action="store_true", help="run the results-screen detector; with --cut, score clips")
    ap.add_argument("--from-s", type=float, default=None, help="start at this stream second (single-match trials)")
    ap.add_argument("--limit-s", type=float, default=None, help="stop at this stream second (tests)")
    a = ap.parse_args(argv)
    if a.day in SEALED:
        sys.exit(f"{a.day} is sealed")
    index = json.loads(make_chunks(a.day).read_text(encoding="utf-8"))
    release_ts = index["release_timestamp"]
    out = config.data_root() / "phase0" / "runs" / f"{a.day}_{a.tag}"
    (out / "clips").mkdir(parents=True, exist_ok=True)
    season = ROOT / "streamsplitter" / "seasons" / "2026"
    det = audio.AudioDetector(audio.load_templates(season / a.templates), thr=a.thr)
    det_r = results.ResultsDetector(np.load(season / "results_refs.npy")) if a.results else None
    clock = None
    if a.clock:
        from streamsplitter.detect.clock import ClockReader
        clock = ClockReader()
    matcher = Matcher()
    namer = Namer(tba_matches(a.day), release_ts)
    timing, seen, res_samples, pending_score = [], [], [], []
    rows: dict[float, dict] = {}            # match start -> output row

    def apply_name(m, row, naming: dict) -> None:
        """Give the match a new name and rename its clip to it."""
        m.naming, m.name = naming, naming["name"]
        row["name"], row["naming"] = m.name, m.naming
        if row.get("clip") and m.name:
            new = Path(row["clip"]["path"]).with_name(f"{m.name}.mp4")
            Path(row["clip"]["path"]).replace(new)
            row["clip"]["path"] = str(new)

    def rename(m, row, now_t: float, piece: dict | None) -> None:
        """Late naming, when the score piece is settled: first ask the namer again if the match had no name, then let
        the results screen's QR code confirm or correct it."""
        if m.name is None:
            teams = (m.clock or {}).get("teams") or (clock.teams(m.start + 100.0) if clock else None)
            naming = namer.name(m.start, teams, release_ts + now_t)
            if naming["name"] is not None:
                apply_name(m, row, {**naming, "late": True, "named_at_s": round(now_t - m.end, 1)})
        if piece is None:
            return
        mid = 0.5 * (piece["start"] + piece["end"])
        url = results.read_qr(seen, [piece["appear"] + 2.0, mid, piece["end"] - 1.0, piece["appear"] + 6.0])
        final = namer.confirm(m.naming, url)
        if final is None:
            row["qr"] = url
        elif final["name"] == m.name:
            m.naming = row["naming"] = final
        else:
            apply_name(m, row, {**final, "named_at_s": round(now_t - m.end, 1)})

    def finish_score(m, row, now_t: float, nxt: float | None) -> bool:
        """Cut the score clip once its piece is settled (or give up); True when done with this match."""
        end_search = min(nxt if nxt is not None else float("inf"), m.end + 420.0)
        samples = [s for s in res_samples if m.end < s["t"] < end_search]
        piece = results.score_piece(samples, m.end)
        settled = (nxt is not None or now_t >= end_search or
                   (piece is not None and now_t >= piece["appear"] + 60.0))
        if not settled:
            return False
        row["score_piece"] = piece
        rename(m, row, now_t, piece)
        if piece and a.cut:
            stem = m.name or f"t{m.start:08.1f}"
            row["score_clip"] = cutter.cut_score(seen, m.start, m.end, piece["start"], piece["end"],
                                                 out / "clips" / f"{stem}_score.mp4")
            row["score_clip"]["latency_after_appear_s"] = round(now_t - piece["appear"], 1)
        return True

    with (out / "events.jsonl").open("w", encoding="utf-8") as ev_f, \
            (out / "clock.jsonl").open("w", encoding="utf-8") as ck_f:
        for path, c in iter_chunks(a.day):
            if a.limit_s is not None and c["t0"] >= a.limit_s:
                break
            if a.from_s is not None and c["t1"] <= a.from_s:
                continue
            seen.append({"path": str(path), "t0": c["t0"], "t1": c["t1"]})
            row_t = {"seq": c["seq"], "chunk_s": round(c["t1"] - c["t0"], 3)}
            t_start = time.perf_counter()
            events = det.feed(path, c["t0"])
            row_t["audio_s"] = round(time.perf_counter() - t_start, 3)
            matcher.add(events)
            if det_r:
                t_r = time.perf_counter()
                rs = det_r.feed(path, c["t0"])
                res_samples += rs
                events = events + rs
                row_t["results_s"] = round(time.perf_counter() - t_r, 3)
            if clock:
                t_c = time.perf_counter()
                for r in clock.feed(path, c["t0"]):
                    ck_f.write(json.dumps(r) + "\n")
                eps = clock.matches()
                new_eps = [e for e in eps if e.get("final") and e.get("auto_start") is not None
                           and round(e["auto_start"], 2) not in matcher._clock_seen]
                matcher.add_clock(eps)
                events = events + [{"kind": "clock_episode", **{k: e.get(k) for k in
                                    ("auto_start", "match_end", "rate", "ok", "failures", "close_reason", "teams")}}
                                   for e in new_eps]
                row_t["clock_s"] = round(time.perf_counter() - t_c, 3)
                row_t["clock_mode"] = clock.mode
            done = matcher.poll(c["t1"])
            proc = time.perf_counter() - t_start
            row_t["proc_s"] = round(proc, 3)
            timing.append(row_t)
            for e in events:
                ev_f.write(json.dumps(e) + "\n")
            # A live recorder has this chunk at wall time release + t1 (plus the platform's delay, ignored here: it
            # only makes more TBA results visible, so leaving it out is the conservative side).
            now_wall = release_ts + c["t1"] + proc
            for m in done:
                teams = (m.clock or {}).get("teams") or (clock.teams(m.start + 100.0) if clock else None)
                m.naming = namer.name(m.start, teams, now_wall)
                m.name = m.naming["name"]
                print(f"match {m.start:9.2f}-{m.end:9.2f} {m.start_src:11s} roles={sorted(m.roles)} "
                      f"clock={'y' if m.clock else 'n'} name={m.name} ({m.naming['source']})", flush=True)
                row = m.as_dict()
                if a.cut:
                    t_cut = time.perf_counter()
                    stem = m.name or f"t{m.start:08.1f}"
                    info = cutter.cut(seen, m.start, m.end, out / "clips" / f"{stem}.mp4")
                    # Simulated latency: the end buzzer reached us when its chunk arrived; the clip exists after
                    # this chunk's processing and the cut.
                    info["latency_s"] = round(c["t1"] - m.end + proc + time.perf_counter() - t_cut, 1)
                    row["clip"] = info
                rows[round(m.start, 3)] = row
                if det_r:
                    pending_score.append(m)
            if det_r:
                for m in list(pending_score):
                    later = [x.start for x in matcher.final if x.start > m.start]
                    later += [matcher._s(h) for h in matcher.hyps if matcher._support(h) >= matcher.min_roles
                              and matcher._s(h) > m.end and matcher._s(h) <= c["t1"]]
                    if finish_score(m, rows[round(m.start, 3)], c["t1"], min(later) if later else None):
                        pending_score.remove(m)
    matcher.poll(float("inf"))
    for m in pending_score:
        finish_score(m, rows[round(m.start, 3)], float("inf"), None)
    for m in matcher.final:                     # matches found only at the very end, or clock annotations
        r = rows.get(round(m.start, 3))
        if r is None:
            rows[round(m.start, 3)] = m.as_dict()
        else:
            r["clock"] = m.clock
    result = {"day": a.day, "templates": a.templates, "thr": a.thr, "clock": bool(clock),
              "clock_lag_s": round(matcher.lag(), 3), "matches": [rows[k] for k in sorted(rows)]}
    if clock:
        result["clock_status"] = {k: v for k, v in clock.status().items() if k in ("totals", "known_clocks")}
        result["clock_episodes"] = clock.matches()
        result["clock_events"] = clock.events
    (out / "matches.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    (out / "timing.json").write_text(json.dumps(timing), encoding="utf-8")
    named = sum(1 for r in rows.values() if r["name"])
    print(f"{a.day}: {len(rows)} matches found, {named} named", flush=True)
    print("DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
