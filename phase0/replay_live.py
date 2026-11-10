"""Phase 0: replay a saved day's chunks into a live folder, as the recorder would write them, to test `streamsplitter
live --no-record` (the follower, naming and delivery) end to end without a live event.

    .venv\\Scripts\\python.exe phase0\\replay_live.py C1 --tag replay_c1 --from-seq 19 --to-seq 26 --every-s 15

Each chunk is copied into <work_root>/live/<tag>/ and then listed in index.json (written atomically), one every
`--every-s` seconds (60 = live speed). A FINAL day replays only with --final.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from streamsplitter import config  # noqa: E402
from phase0.fake_live import chunk_dir  # noqa: E402
from phase0.get_day import FINAL, SEALED  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("day")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--from-seq", type=int, default=0)
    ap.add_argument("--to-seq", type=int, default=None)
    ap.add_argument("--every-s", type=float, default=60.0)
    ap.add_argument("--final", action="store_true")
    a = ap.parse_args(argv)
    if a.day in SEALED or (a.day in FINAL and not a.final):
        sys.exit(f"{a.day} is not open for replay")
    src = chunk_dir(a.day)
    index = json.loads((src / "index.json").read_text(encoding="utf-8"))
    out = config.work_root() / "live" / a.tag
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    live = {k: index[k] for k in ("tag", "event", "date", "video", "release_timestamp")}
    live.update(live=True, status="recording", chunks=[])
    for c in index["chunks"]:
        if c["seq"] < a.from_seq or (a.to_seq is not None and c["seq"] > a.to_seq):
            continue
        t_next = time.time() + a.every_s
        shutil.copyfile(src / c["path"], out / c["path"])
        live["chunks"].append({**c, "wall_closed": time.time(), "gap_before_s": 0.0 if live["chunks"] else None})
        tmp = out / "index.json.tmp"
        tmp.write_text(json.dumps(live, indent=1), encoding="utf-8")
        os.replace(tmp, out / "index.json")
        print(f"listed seq {c['seq']} t={c['t0']:.0f}", flush=True)
        time.sleep(max(0.0, t_next - time.time()))
    print("DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
