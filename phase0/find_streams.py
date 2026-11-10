"""Phase 0: list 2026 events with YouTube webcasts and probe the saved stream videos (no download).

    .venv\\Scripts\\python.exe phase0\\find_streams.py                    -> <data>/phase0/events_2026.json
    .venv\\Scripts\\python.exe phase0\\find_streams.py --probe KEY [KEY ...] -> <data>/phase0/probe_<key>.json

Events used by the research project are excluded so phase 0 never opens their held-out matches.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yt_dlp  # noqa: E402

from streamsplitter import config, tba  # noqa: E402

# Every 2026 event the research tree touches (events/, tba_index defaults, refcalls). Never used here.
RESEARCH_EVENTS = {"2026casnv", "2026necmp", "2026necmp1", "2026necmp2", "2026mawor", "2026cur", "2026cacac",
                   "2026cc", "2026joh", "2026johlf", "2026necmp1lf"}
EVENT_TYPES = {0: "regional", 1: "district", 2: "district_cmp", 3: "cmp_division", 5: "district_cmp_division"}


def list_events(out: Path) -> list[dict]:
    rows = []
    team_events = set(tba.get("/team/frc5026/events/2026/keys"))
    for e in tba.get("/events/2026"):
        if e.get("event_type") not in EVENT_TYPES or e["key"] in RESEARCH_EVENTS:
            continue
        yt = [w for w in (e.get("webcasts") or []) if w.get("type") == "youtube"]
        rows.append({"key": e["key"], "name": e.get("name"), "type": EVENT_TYPES[e["event_type"]],
                     "week": e.get("week"), "start": e.get("start_date"), "end": e.get("end_date"),
                     "district": (e.get("district") or {}).get("abbreviation"), "is_5026": e["key"] in team_events,
                     "youtube": yt, "other_webcasts": [w.get("type") for w in (e.get("webcasts") or [])
                                                       if w.get("type") != "youtube"]})
    rows.sort(key=lambda r: (r["start"] or "", r["key"]))
    out.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return rows


def probe(key: str, out_dir: Path) -> list[dict]:
    """yt-dlp metadata of every YouTube webcast of one event: duration, live status, start time, best formats."""
    ev = tba.get(f"/event/{key}")
    res = []
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    for w in ev.get("webcasts") or []:
        if w.get("type") != "youtube":
            continue
        url = f"https://www.youtube.com/watch?v={w['channel']}"
        row = {"event": key, "webcast": w, "url": url}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            fmts = info.get("formats") or []
            row.update({"title": info.get("title"), "duration_s": info.get("duration"),
                        "live_status": info.get("live_status"), "release_timestamp": info.get("release_timestamp"),
                        "timestamp": info.get("timestamp"), "availability": info.get("availability"),
                        "max_height": max((f.get("height") or 0 for f in fmts), default=0),
                        "has_audio": any((f.get("acodec") or "none") != "none" for f in fmts),
                        "h264_1080": any((f.get("vcodec") or "").startswith("avc1") and f.get("height") == 1080
                                         for f in fmts)})
        except Exception as e:  # unavailable, private, removed
            row["error"] = str(e)[:300]
        res.append(row)
    (out_dir / f"probe_{key}.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", nargs="*", help="event keys to probe with yt-dlp")
    a = ap.parse_args(argv)
    out_dir = config.data_root() / "phase0"
    out_dir.mkdir(parents=True, exist_ok=True)
    if a.probe:
        for key in a.probe:
            if key in RESEARCH_EVENTS:
                print(f"{key}: research event, skipped")
                continue
            for r in probe(key, out_dir):
                d = r.get("duration_s")
                print(f"{key} {r['webcast'].get('date', '-'):10} {r['webcast']['channel']:12} "
                      f"{(d or 0) / 3600:5.2f} h {r.get('live_status', '-'):9} h{r.get('max_height', 0)} "
                      f"audio={r.get('has_audio')} avc1080={r.get('h264_1080')} {r.get('error', '')[:80]}")
        return 0
    rows = list_events(out_dir / "events_2026.json")
    n_yt = sum(1 for r in rows if r["youtube"])
    print(f"{len(rows)} non-research 2026 events, {n_yt} with YouTube webcasts")
    for r in rows:
        if r["is_5026"] or r["type"] in ("cmp_division", "district_cmp", "district_cmp_division"):
            print(f"  {r['key']:12} {r['type']:21} wk{r['week']} 5026={r['is_5026']!s:5} yt={len(r['youtube'])} "
                  f"other={r['other_webcasts']} {r['name']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
