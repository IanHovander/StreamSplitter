"""Phase 0: cut the 2026 field-sound templates.

v0 comes from the research repo's open dev clip 2026casnv qm1. The onsets were read off its spectrogram by eye on
2026-11-03 (F:/StreamSplitterData/phase0/scratch_audio/casnv_qm1_*.png): start sound 5.00 s, AUTO buzzer 25.75 s,
3 Bells 29.80 s, steam whistle 140.10 s, end buzzer 170.05 s.

    .venv\\Scripts\\python.exe phase0\\build_templates.py v0   -> streamsplitter/seasons/2026/cues_v0.npz

2026-11-03: rebuilt after logspec gained time smoothing (SMOOTH = 31 frames); templates must use the same features
as the detector, so any change to logspec means rebuilding them.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streamsplitter import media  # noqa: E402
from streamsplitter.detect import audio  # noqa: E402

CASNV_QM1 = ROOT.parent / "Matches" / "_benchmark" / "2026casnv" / "2026casnv_qm1.mp4"
V0_ONSETS = {"start": 5.00, "buzzer": 25.75, "bells": 29.80, "whistle": 140.10}
OUT = ROOT / "streamsplitter" / "seasons" / "2026"


def build_v0() -> Path:
    x = media.decode_audio(CASNV_QM1, audio.SR)
    tpls = [audio.Template.from_audio(name, x, onset) for name, onset in V0_ONSETS.items()]
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "cues_v0.npz"
    audio.save_templates(path, tpls)
    return path


if __name__ == "__main__":
    if sys.argv[1:] == ["v0"]:
        print(build_v0())
    else:
        sys.exit("usage: build_templates.py v0")
