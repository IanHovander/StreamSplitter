"""Phase 0: average the field-sound templates over many matches of a dev day (v0 from one clip -> v1 from a day).

    .venv\\Scripts\\python.exe phase0\\refine_templates.py D1_run2 v1 [--min-roles 3] [--base cues_v0.npz]

Uses only matches whose start is pinned by at least --min-roles audio cues (or by two cues and the scoreboard clock),
so every position used is confirmed by independent timing. Each match's audio is decoded once. For each cue of each
such match, the whitened spectrogram is cut where the cue was heard, or, if it was not heard, where the season table
says it must be (start + offset; the start sound is rarely heard on commentary-heavy broadcasts, which is why it
needs this). Each cut is aligned within +-`search` s by correlation with the current template, the per-pixel median
over all cuts is taken, and the alignment is repeated once against that median. Crowd noise and commentary average
out; the FMS sound stays. Also prints the measured cue offsets from the start (median and spread) next to the season
table.

A template set built from a day makes that day a training day (phase0/PLAN.md): its later numbers are reported but
not used for the pass bar.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import median

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streamsplitter import config, media  # noqa: E402
from streamsplitter.detect import audio  # noqa: E402
from streamsplitter.matcher import SEASON_2026  # noqa: E402
from phase0.fake_live import chunk_dir  # noqa: E402


def audio_span(day: str, a: float, b: float) -> tuple[np.ndarray, float]:
    """Mono audio of stream seconds [a, b] from the day's chunks, and the stream time of its first sample."""
    cdir = chunk_dir(day)
    index = json.loads((cdir / "index.json").read_text(encoding="utf-8"))
    parts, t_first = [], None
    for c in index["chunks"]:
        if c["t1"] <= a or c["t0"] >= b:
            continue
        starts = media.stream_starts(cdir / c["path"])
        a0 = c["t0"] + starts.get("audio", 0.0) - starts.get("video", 0.0)
        x = media.decode_audio(cdir / c["path"], audio.SR)
        if t_first is None:
            t_first = a0
        else:  # place by time, padding or trimming the join
            want = int(round((a0 - t_first) * audio.SR))
            have = sum(len(p) for p in parts)
            if want > have:
                parts.append(np.zeros(want - have, np.float32))
            elif want < have:
                x = x[have - want:]
        parts.append(x)
    x = np.concatenate(parts)
    i0 = int(max(0.0, a - t_first) * audio.SR)
    return x[i0: int((b - t_first) * audio.SR)], t_first + i0 / audio.SR


def corr(p: np.ndarray, q: np.ndarray) -> float:
    p = p - p.mean()
    q = q - q.mean()
    return float((p * q).sum() / max(1e-9, np.sqrt((p * p).sum() * (q * q).sum())))


def cut(x: np.ndarray, x_t0: float, t: float, tpl: audio.Template, ref: np.ndarray, search: float) -> tuple:
    """Best-aligned patch near cue time t: (patch, shift_s, corr). The spectrogram is whitened in +-5 s of context,
    as when the v0 templates were cut."""
    L = tpl.patch.shape[1]
    a = max(0, int((t - x_t0 - tpl.lead_s - search - 5.0) * audio.SR))
    spec = audio.logspec(x[a: int((t - x_t0 + L / audio.FPS + search + 5.0) * audio.SR)])
    c_exp = int(round((t - x_t0 - tpl.lead_s) * audio.FPS)) - int(round(a / audio.HOP))
    s = int(round(search * audio.FPS))
    best = (None, 0.0, -2.0)
    for c in range(c_exp - s, c_exp + s + 1):
        if c < 0 or c + L > spec.shape[1]:
            continue
        r = corr(spec[:, c: c + L], ref)
        if r > best[2]:
            best = (spec[:, c: c + L].copy(), (c - c_exp) / audio.FPS, r)
    return best


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("version")
    ap.add_argument("--min-roles", type=int, default=3)
    ap.add_argument("--base", default="cues_v0.npz")
    ap.add_argument("--search", type=float, default=0.6)
    a = ap.parse_args(argv)
    rdir = config.data_root() / "phase0" / "runs" / a.run
    res = json.loads((rdir / "matches.json").read_text(encoding="utf-8"))
    day = res["day"]
    base = {t.name: t for t in audio.load_templates(ROOT / "streamsplitter" / "seasons" / "2026" / a.base)}
    good = [m for m in res["matches"] if m["n_roles"] >= a.min_roles or (m["n_roles"] >= 2 and m.get("clock"))]
    print(f"{len(good)} matches pinned by >= {a.min_roles} cues (or 2 cues + clock)", flush=True)
    offsets: dict[str, list[float]] = {}
    occ: dict[str, list[tuple]] = {}        # cue -> [(audio, its stream t0, cue time)]
    for k, m in enumerate(good):
        S = m["start"]
        x, x_t0 = audio_span(day, S - 8.0, S + SEASON_2026["match_s"] + 8.0)
        for role, (cue, off) in SEASON_2026["roles"].items():
            heard = m["roles"].get(role)
            if heard is not None:
                offsets.setdefault(role, []).append(heard - S)
            occ.setdefault(cue, []).append((x, x_t0, heard if heard is not None else S + off))
        print(f"  {k + 1}/{len(good)} {m.get('name')}", flush=True)
    for role, (cue, off) in SEASON_2026["roles"].items():
        v = offsets.get(role, [])
        if v:
            print(f"  {role:9} heard n={len(v):3} offset median {median(v):8.3f} s (table {off:7.2f}) "
                  f"spread {min(v):8.3f} .. {max(v):8.3f}")
    tpls = []
    for cue, tpl in base.items():
        ref, got = tpl.patch, []
        for _ in range(2):
            got = [cut(x, x_t0, t, tpl, ref, a.search) for x, x_t0, t in occ.get(cue, [])]
            got = [g for g in got if g[0] is not None and g[0].shape == tpl.patch.shape]
            if not got:
                break
            ref = np.median(np.stack([g[0] for g in got]), axis=0).astype(np.float32)
        shifts, rs = [g[1] for g in got], [g[2] for g in got]
        if got:
            print(f"  {cue:8} {len(got)} cuts; corr with the new median {median(rs):.2f}; shift median "
                  f"{median(shifts):+.2f} s, |shift| > 0.3 s: {sum(abs(s) > 0.3 for s in shifts)}; "
                  f"corr old vs new template {corr(tpl.patch, ref):.2f}")
        tpls.append(audio.Template(cue, ref, tpl.lead_s))
    out = ROOT / "streamsplitter" / "seasons" / "2026" / f"cues_{a.version}.npz"
    audio.save_templates(out, tpls)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
