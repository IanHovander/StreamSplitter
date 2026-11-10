"""Phase 0 boundary and score-clip check 2: a seeded random sample of found matches, one picture sheet each.

    .venv\\Scripts\\python.exe phase0\\visual_sample.py D1_run2 [--n 20] [--seed 5026] [--crop x0,y0,x1,y1]

The sample is drawn after the run from all found matches (not from failures). Each sheet, in
<data>/phase0/visual/<run>/<nn>_<name>.png, shows full-resolution crops around the scoreboard clock (title, clock,
scores; placed where the run's clock reader found the clock), two per row:
  the clip's first frame, the stream at detected start - 1 s, start, start + 1 s,
  the stream at detected end - 1 s, end, end + 1 s, the clip's last frame,
  then the score clip's appended piece (first, middle, last frame), whole frame scaled
and a caption with the TBA record the run's name points to (teams, score), so the reader can check that the results
screen is that match's. Answers go in <data>/phase0/visual/<run>/labels.json (written by hand, never read by the run).
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streamsplitter import config, ffmpeg  # noqa: E402
from phase0.frames import frame_at  # noqa: E402
from phase0.run_day import tba_matches  # noqa: E402


def clip_frame(path: Path, first: bool) -> np.ndarray | None:
    pos = ["-ss", "0"] if first else ["-sseof", "-0.2"]
    raw = subprocess.run([ffmpeg.find(), "-v", "error", *pos, "-i", str(path), "-frames:v", "1", "-f", "image2pipe",
                          "-vcodec", "png", "-"], capture_output=True).stdout
    return cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR) if raw else None


def tile(fr: np.ndarray | None, label: str, crop=None, width: int | None = None, size=(1160, 110)) -> np.ndarray:
    if fr is None:
        fr = np.zeros((size[1], size[0], 3), np.uint8)
    elif crop:
        x0, y0, x1, y1 = crop
        fr = fr[y0:y1, x0:x1]
    if width:
        fr = cv2.resize(fr, (width, int(fr.shape[0] * width / fr.shape[1])), interpolation=cv2.INTER_AREA)
    lab = np.zeros((26, fr.shape[1], 3), np.uint8)
    cv2.putText(lab, label, (6, 19), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
    return np.vstack([lab, fr])


def clock_crop(res: dict, t: float, default: tuple) -> tuple:
    """An 800 x ~110 px crop centred on the clock the run's clock reader had located by stream time t (title above it,
    scores beside it), or `default` when the run has no clock."""
    events = res.get("clock_events") or []
    # places the reader later abandoned (switched away from, or dropped as never counting down) were not the clock
    dropped = [e.get("old") or e.get("roi") for e in events
               if e.get("event") == "switched" or (e.get("event") == "lost" and "excluded" in (e.get("why") or ""))]
    located = [e for e in events if e.get("event") == "located" and e.get("roi") and e["roi"] not in dropped]
    if not located:
        return default
    before = [e for e in located if e["t"] <= t]
    x0, y0, x1, y1 = (before[-1] if before else located[0])["roi"]
    cx, h = 0.5 * (x0 + x1), y1 - y0
    return (max(0, int(cx - 400)), max(0, int(y0 - 2.2 * h)), int(cx + 400), int(y1 + 0.6 * h))


def grid(rows: list[list[np.ndarray]]) -> np.ndarray:
    w = max(sum(t.shape[1] for t in r) for r in rows)
    out = []
    for r in rows:
        h = max(t.shape[0] for t in r)
        r = [cv2.copyMakeBorder(t, 0, h - t.shape[0], 0, 0, cv2.BORDER_CONSTANT) for t in r]
        line = np.hstack(r)
        out.append(cv2.copyMakeBorder(line, 0, 4, 0, w - line.shape[1], cv2.BORDER_CONSTANT))
    return np.vstack(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--seed", type=int, default=5026)
    ap.add_argument("--crop", default="560,0,1360,110", help="crop when the run has no clock (1080p scoreboard)")
    ap.add_argument("--fixed-crop", action="store_true", help="always use --crop (e.g. the clock reader locked wrongly)")
    a = ap.parse_args(argv)
    default = tuple(int(v) for v in a.crop.split(","))
    rdir = config.data_root() / "phase0" / "runs" / a.run
    res = json.loads((rdir / "matches.json").read_text(encoding="utf-8"))
    day = res["day"]
    tba = {m["key"]: m for m in tba_matches(day)}
    found = [m for m in res["matches"] if m.get("clip")]
    sample = sorted(random.Random(a.seed).sample(range(len(found)), min(a.n, len(found))))
    out = config.data_root() / "phase0" / "visual" / a.run
    out.mkdir(parents=True, exist_ok=True)
    listing = []
    for k, i in enumerate(sample):
        m = found[i]
        s, e = m["start"], m["end"]
        crop = default if a.fixed_crop else clock_crop(res, s + 100.0, default)
        tiles = ([tile(clip_frame(Path(m["clip"]["path"]), True), "clip first frame", crop)] +
                 [tile(frame_at(day, t), f"start {d:+d} s ({t:.2f})", crop) for d, t in ((-1, s - 1), (0, s), (1, s + 1))] +
                 [tile(frame_at(day, t), f"end {d:+d} s ({t:.2f})", crop) for d, t in ((-1, e - 1), (0, e), (1, e + 1))] +
                 [tile(clip_frame(Path(m["clip"]["path"]), False), "clip last frame", crop)])
        rows = [tiles[i:i + 2] for i in range(0, len(tiles), 2)]
        p = m.get("score_piece")
        if p:
            mid = 0.5 * (p["start"] + p["end"])
            rows.append([tile(frame_at(day, t), f"score piece {nm} ({t:.1f})", width=533)
                         for nm, t in (("first", p["start"] + 0.2), ("middle", mid), ("last", p["end"] - 0.3))])
        sheet = grid(rows)
        rec = tba.get((m.get("naming") or {}).get("key") or "")
        cap = f"#{k:02d} found {s:.2f}-{e:.2f} ({m.get('start_src')})  name {m.get('name')}"
        if rec:
            al = rec["alliances"]
            cap += (f"  | TBA {rec['key']}: red {' '.join(t[3:] for t in al['red']['team_keys'])} = {al['red']['score']}"
                    f"  blue {' '.join(t[3:] for t in al['blue']['team_keys'])} = {al['blue']['score']}")
        head = np.zeros((30, sheet.shape[1], 3), np.uint8)
        cv2.putText(head, cap, (6, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 255), 1, cv2.LINE_AA)
        path = out / f"{k:02d}_{m.get('name') or f't{s:.0f}'}.png"
        cv2.imwrite(str(path), np.vstack([head, sheet]))
        listing.append({"n": k, "start": s, "end": e, "name": m.get("name"), "sheet": path.name})
        print(path, flush=True)
    (out / "sample.json").write_text(json.dumps({"run": a.run, "seed": a.seed, "sample": listing}, indent=1),
                                     encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
