"""Clock reader on the three open research benchmark clips, fed as 60 s MPEG-TS chunks in order.

    .venv\\Scripts\\python.exe -m pytest -p no:cacheprovider tests/test_clock.py -v -s

(-p no:cacheprovider keeps pytest from writing .pytest_cache into the project root.)

References (research products of the same clips; casnv's numbers are from docs/phase0-broadcast-layer.md section 2.4,
whose Q1.mp4 has the same 12869 frames as the benchmark copy):
  AUTO start / TELEOP start / match end (video seconds) and the six team numbers.
The research marks a change at the first frame showing the new value (late by half a frame on average); this reader
uses the midpoint between samples, so differences of 10-20 ms are expected.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import clock_harness as H  # noqa: E402
from streamsplitter.detect.clock import ClockReader  # noqa: E402

BENCH = Path(r"X:\_ai\YoutubeVideoDownloaderForFRCMatches\Matches\_benchmark")

CLIPS = {
    "casnv_qm1": {"video": BENCH / "2026casnv" / "2026casnv_qm1.mp4", "timing": None,
                  "ref": {"auto_start": 5.584, "teleop_start": 29.877, "match_end": 171.515, "rate": 1.0117},
                  "teams": {"blue": ["254", "1351", "6059"], "red": ["6665", "6822", "1700"]}},
    "necmp1_qm10": {"video": BENCH / "2026necmp1" / "2026necmp1_qm10.mp4",
                    "timing": BENCH / "2026necmp1" / "2026necmp1_qm10.timing.json",
                    "teams": {"red": ["3958", "2876", "175"], "blue": ["2370", "5112", "10254"]}},
    "mawor_qm10": {"video": BENCH / "2026mawor" / "Q10_video.mp4",
                   "timing": BENCH / "2026mawor" / "Q10_video.timing.json",
                   "teams": None},          # the research reader reads none here either (soft 13-14 px digits)
}


def _ref(clip: dict) -> dict:
    if clip["timing"] is None:
        return clip["ref"]
    cm = json.loads(Path(clip["timing"]).read_text(encoding="utf-8"))["clock_model"]
    return {"auto_start": cm["auto_start_s"], "teleop_start": cm["teleop_start_s"], "match_end": cm["match_end_s"],
            "rate": cm["fps_ratio"]}


_cache: dict = {}


def _run(name: str) -> dict:
    if name not in _cache:
        clip = CLIPS[name]
        if not clip["video"].is_file():
            pytest.skip(f"{clip['video']} not present")
        chunks = H.split_chunks(clip["video"], 60.0)
        res = H.run(chunks, ClockReader(), verbose=False)
        res["chunks"] = chunks
        _cache[name] = res
    return _cache[name]


@pytest.mark.parametrize("name", list(CLIPS))
def test_locates_in_first_chunk(name):
    res = _run(name)
    ev = [e for e in res["events"] if e["event"] == "located"]
    assert ev, "clock never located"
    first_end = res["chunks"][1][1]
    assert ev[0]["t"] <= first_end + 1.0, f"located only at {ev[0]['t']} s"
    assert res["readings"] and res["readings"][0]["t"] < 10.0, "readings should start in the first chunk"


@pytest.mark.parametrize("name", list(CLIPS))
def test_match_anchors_vs_research(name):
    res = _run(name)
    ok = [m for m in res["matches"] if m.get("ok")]
    assert len(ok) == 1, [(m.get("ok"), m.get("failures")) for m in res["matches"]]
    m = ok[0]
    ref = _ref(CLIPS[name])
    d = H.compare_anchors(m, ref)
    print(name, "anchor differences vs research (s):", d, "rate", m["rate"], "vs", ref["rate"])
    assert all(abs(v) <= 0.06 for v in d.values()), d
    assert abs(m["rate"] - ref["rate"]) < 0.002
    assert not m["auto_inferred"]


@pytest.mark.parametrize("name", list(CLIPS))
def test_readings_vs_model(name):
    res = _run(name)
    chk = H.check_against_model(res["readings"], res["matches"])
    print(name, {k: v for k, v in chk.items() if k != "bad"}, chk["bad"][:5])
    assert chk["n"] > 500
    assert chk["agreement"] >= 0.995


@pytest.mark.parametrize("name", [n for n, c in CLIPS.items() if c["timing"] is not None])
def test_readings_vs_research_per_frame(name):
    res = _run(name)
    chk = H.check_against_research(res["readings"], CLIPS[name]["timing"])
    print(name, {k: v for k, v in chk.items() if k != "bad"}, chk["bad"][:5])
    assert chk["n"] > 500
    assert chk["agreement"] >= 0.995


@pytest.mark.parametrize("name", list(CLIPS))
def test_teams(name):
    res = _run(name)
    want = CLIPS[name]["teams"]
    got = res["reader"].teams(100.0)
    print(name, got)
    if want is None:
        assert got is None or not got["complete"], "an unreadable overlay must not give a complete team read"
        return
    assert got is not None and got["complete"]
    assert got["red"] == want["red"] and got["blue"] == want["blue"]


@pytest.mark.parametrize("name", list(CLIPS))
def test_speed(name):
    res = _run(name)
    tot = res["reader"].totals
    x_cpu = tot["media_s"] / tot["cpu_s"]
    print(name, f"{tot['media_s']:.0f} s media, cpu {tot['cpu_s']:.1f} s = {x_cpu:.1f}x real time per core, "
                f"wall {tot['wall_s']:.1f} s")
    assert x_cpu >= 10.0


def test_relocates_after_zoom_mid_match():
    """necmp1 qm10 with the picture zoomed 1.33x from 100 s on (synthetic, re-encoded once into the scratch folder):
    the clock moves and grows (63 -> 86 px digits) in the middle of TELEOP.  The reader must find it again in the same
    chunk, keep what the old position read, and still fit the match as before."""
    src = CLIPS["necmp1_qm10"]["video"]
    if not src.is_file():
        pytest.skip("necmp1 clip not present")
    zoom = H.SCRATCH / "necmp1_qm10_zoom133_at100.mp4"
    if not zoom.is_file():
        H.SCRATCH.mkdir(parents=True, exist_ok=True)
        fc = ("[0:v]split[a][b];[a]trim=0:100,setpts=PTS-STARTPTS[a1];[b]trim=100,setpts=PTS-STARTPTS,"
              "scale=2560:1440,crop=1920:1080:320:0[b1];[a1][b1]concat=n=2:v=1:a=0[v]")
        import subprocess
        from streamsplitter import ffmpeg as _ff
        subprocess.run([_ff.find("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                        "-filter_complex", fc, "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
                        "-g", "60", "-bf", "3", "-threads", "4", str(zoom)], check=True)
    res = H.run(H.split_chunks(zoom, 60.0), ClockReader(), verbose=False)
    located = [e for e in res["events"] if e["event"] == "located"]
    assert len(located) == 2 and located[1]["glyph_h"] > 80, located
    gaps = [(a["t"], b["t"]) for a, b in zip(res["readings"], res["readings"][1:]) if b["t"] - a["t"] > 1.0
            and a["t"] < 165]
    assert not gaps, gaps
    ok = [m for m in res["matches"] if m.get("ok")]
    assert len(ok) == 1
    d = H.compare_anchors(ok[0], _ref(CLIPS["necmp1_qm10"]))
    assert all(abs(v) <= 0.06 for v in d.values()), d
    chk = H.check_against_model(res["readings"], res["matches"])
    assert chk["agreement"] >= 0.995
