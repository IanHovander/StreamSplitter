"""Label-free boundary check for FMS events: compare each clip's overlay-timer start and end with the field's own
start sound and end buzzer (the FMS templates of streamsplitter/seasons/2026; FMS plays the same sounds every year).

    .venv\\Scripts\\python.exe phase0\\audio_check.py W2_any2

For every clip it looks for the start cue within +-4 s of the start and an end buzzer within +-4 s of the end. It
prints the offsets (cue - timer). A constant offset is the display running ahead of the field (Eagan 2025: ~1 s); a
clip with neither cue is unchecked, not wrong (Cheesy Arena plays its own sounds; some streams carry no field sound).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streamsplitter import config  # noqa: E402
from streamsplitter.detect import audio  # noqa: E402


def main(argv=None) -> int:
    run = (argv or sys.argv[1:])[0]
    out = config.data_root() / "phase0" / "runs" / run
    res = json.loads((out / "matches.json").read_text(encoding="utf-8"))
    cdir = config.work_root() / "phase0" / "chunks" / res["day"]
    index = json.loads((cdir / "index.json").read_text(encoding="utf-8"))
    tpl = audio.load_templates(ROOT / "streamsplitter" / "seasons" / "2026" / "cues_v1.npz")
    rows, starts, ends = [], [], []
    for r in res["rows"]:
        det = audio.AudioDetector(tpl, thr=0.40)
        ev = []
        for c in index["chunks"]:
            if c["t1"] > r["start"] - 70 and c["t0"] < r["end"] + 10:
                ev += det.feed(cdir / c["path"], c["t0"])
        s = [e["t"] - r["start"] for e in ev if e["cue"] == "start" and abs(e["t"] - r["start"]) <= 4]
        b = [e["t"] - r["end"] for e in ev if e["cue"] == "buzzer" and abs(e["t"] - r["end"]) <= 4]
        row = {"name": r["name"], "start_cue_minus_start": round(min(s, key=abs), 2) if s else None,
               "end_buzzer_minus_end": round(min(b, key=abs), 2) if b else None}
        rows.append(row)
        if s:
            starts.append(row["start_cue_minus_start"])
        if b:
            ends.append(row["end_buzzer_minus_end"])
        print(row, flush=True)
    summ = {"clips": len(rows), "start_checked": len(starts), "end_checked": len(ends),
            "start_offset_median": round(median(starts), 2) if starts else None,
            "start_offset_range": [min(starts), max(starts)] if starts else None,
            "end_offset_median": round(median(ends), 2) if ends else None,
            "end_offset_range": [min(ends), max(ends)] if ends else None}
    print(json.dumps(summ, indent=1))
    (out / "audio_check.json").write_text(json.dumps({"summary": summ, "rows": rows}, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
