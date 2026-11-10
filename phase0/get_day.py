"""Phase 0: download saved day streams (1080p H.264 + AAC) into <data>/phase0/vod/.

    .venv\\Scripts\\python.exe phase0\\get_day.py D1 D2 D3

Day tags are the dev days of phase0/PLAN.md. Sealed days are refused here until the plan's freeze is recorded.
Re-running resumes partial downloads.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yt_dlp  # noqa: E402

from streamsplitter import config, ffmpeg  # noqa: E402
from streamsplitter.record import js_runtimes  # noqa: E402

DAYS = {
    "D1": ("2026cancmp", "2026-04-11", "VPIY_1F3Mvg"),
    "D2": ("2026cagle", "2026-03-29", "3-wNH_65zBM"),
    "D3": ("2026hop", "2026-05-01", "VqeEcZXbR5A"),
    "D4": ("2026micmp1", "2026-04-17", "_DTdFpvdGt0"),
    # Generalisation (user, 2026-11-04): Chezy Champs runs Cheesy Arena, not FMS (own screens and sounds), so it is the
    # dev event for the event-agnostic detectors; a 2025 Week 0 checks another game (the official Merrimack Week 0
    # has no saved full stream, so Kettering's); MadTown 2025 day 2 is the final timed test (FINAL: no dev use).
    "C1": ("2026cc", "2026-09-19", "zQ8Oibbg9Ro"),
    "C2": ("2026cc", "2026-09-20", "mDRbJRKPziE"),
    "W1": ("2025ketwk0", "2025-02-23", "4PYM0u9TUy0"),   # no overlay, field sounds inaudible: nothing to read
    "W2": ("2025mnbt", "2025-02-15", "C5qO30t9lgg"),     # Eagan Week Zero: its own overlay (timer box, team panels)
    "M2": ("2025mttd", "2025-11-09", "htdIVjA5_RM"),
    "M0": ("2025mttd", "2025-11-07", "jgP1NMNykds"),     # Friday practice: names from the overlay title only
}
SEALED = {"S1", "S2"}
FINAL = {"M2"}


def day_path(tag: str) -> Path:
    event, date, vid = DAYS[tag]
    return config.data_root() / "phase0" / "vod" / f"{tag}_{event}_{date}_{vid}.mp4"


def day_inputs(tag: str) -> list[Path] | None:
    """The day's media: the merged .mp4 (D1), or the separately downloaded video and audio files (merging a 17 GB
    file on the shared hard disk took over an hour and once died part-way, so later days are not merged)."""
    p = day_path(tag)
    if p.is_file():
        return [p]
    v = sorted(p.parent.glob(p.stem + ".f*.mp4"))
    au = sorted(p.parent.glob(p.stem + ".f*.m4a"))
    v = [x for x in v if not x.name.endswith((".part", ".temp.mp4"))]
    return [v[0], au[0]] if v and au else None


def download(tag: str) -> Path:
    event, date, vid = DAYS[tag]
    out = day_path(tag)
    out.parent.mkdir(parents=True, exist_ok=True)
    have = day_inputs(tag)
    if have:
        print(f"{tag}: already there ({sum(x.stat().st_size for x in have) / 1e9:.1f} GB)", flush=True)
        return out
    opts = {
        # video and audio as two files, not merged (see day_inputs)
        "format": "bv*[vcodec^=avc1][height<=1080],ba[ext=m4a]",
        "outtmpl": str(out.with_suffix(".f%(format_id)s.%(ext)s")),
        "ffmpeg_location": str(Path(ffmpeg.find()).parent),
        "concurrent_fragment_downloads": 4,
        "continuedl": True,
        "retries": 20,
        "fragment_retries": 50,
        "writeinfojson": True,
        "quiet": True,
        "noprogress": True,
        # a JS runtime on PATH (Node here) lets yt-dlp solve YouTube's throttling challenge: W2 crawled at 4 MB/s
        "js_runtimes": js_runtimes(),
    }
    t0 = time.time()
    print(f"{tag}: downloading {vid} ({event} {date})", flush=True)
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([f"https://www.youtube.com/watch?v={vid}"])
    got = day_inputs(tag) or []
    print(f"{tag}: done in {(time.time() - t0) / 60:.1f} min, {sum(x.stat().st_size for x in got) / 1e9:.1f} GB",
          flush=True)
    return out


def main(argv: list[str]) -> int:
    tags = argv or ["D1", "D2", "D3"]
    for tag in tags:
        if tag in SEALED:
            print(f"{tag} is sealed (phase0/PLAN.md); not downloaded before the freeze", flush=True)
            return 2
    for tag in tags:
        for attempt in range(3):
            try:
                download(tag)
                break
            except Exception as e:  # network or YouTube hiccup: resume
                print(f"{tag}: attempt {attempt + 1} failed: {e}", flush=True)
                time.sleep(30)
    status = {t: [x.name for x in day_inputs(t) or []] for t in tags}
    print("STATUS " + json.dumps(status), flush=True)
    print("DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
