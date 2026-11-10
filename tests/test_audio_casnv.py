"""The v0 field-sound templates find their own cues in 2026casnv qm1 when the clip is fed as 60 s chunks, and the
matcher turns them into exactly one match with the right start and end. Weak stray cues are allowed (the threshold
is low on purpose); the matcher must not turn them into a match."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streamsplitter import config, ffmpeg  # noqa: E402
from streamsplitter.detect import audio  # noqa: E402
from streamsplitter.matcher import Matcher  # noqa: E402

CLIP = ROOT.parent / "Matches" / "_benchmark" / "2026casnv" / "2026casnv_qm1.mp4"
EXPECTED = [("start", 5.00), ("buzzer", 25.75), ("bells", 29.80), ("whistle", 140.10), ("buzzer", 170.05)]


def chunks_of(src: Path, out: Path) -> list[tuple[Path, float]]:
    out.mkdir(parents=True, exist_ok=True)
    csv = out / "segments.csv"
    if not csv.is_file():
        subprocess.run([ffmpeg.find(), "-v", "error", "-y", "-i", str(src), "-c", "copy", "-f", "segment",
                        "-segment_time", "60", "-segment_format", "mpegts", "-segment_list", str(csv),
                        "-segment_list_type", "csv", str(out / "c_%03d.ts")], check=True)
    rows = [line.split(",") for line in csv.read_text().splitlines() if line]
    return [(out / name, float(t0)) for name, t0, _ in rows]


@pytest.mark.skipif(not CLIP.is_file(), reason="2026casnv qm1 clip not present (research data, not in the zip)")
def test_casnv_qm1_cues():
    tpls = audio.load_templates(ROOT / "streamsplitter" / "seasons" / "2026" / "cues_v0.npz")
    det = audio.AudioDetector(tpls)
    events = []
    for path, t0 in chunks_of(CLIP, config.data_root() / "phase0" / "scratch_audio" / "casnv_qm1_chunks"):
        events += det.feed(path, t0)
    for e in events:
        print(e)
    for cue, t in EXPECTED:
        hits = [e for e in events if e["cue"] == cue and abs(e["t"] - t) < 0.3]
        assert hits, f"{cue} at {t} not found"
    m = Matcher()
    m.add(events)
    m.poll(float("inf"))
    assert len(m.final) == 1, f"{len(m.final)} matches"
    assert abs(m.final[0].start - 5.00) < 0.3 and abs(m.final[0].end - 170.05) < 0.3, m.final[0].as_dict()


if __name__ == "__main__":
    test_casnv_qm1_cues()
    print("ok")
