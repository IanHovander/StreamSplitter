"""Build the zip a team member gets: the program, the setup (run.bat / run.sh), the docs and the sample clip, inside
one StreamSplitter/ folder. Never settings, keys, recordings, logs or the installed packages.

    .venv\\Scripts\\python.exe tools\\package.py            # -> dist\\StreamSplitter-<version>.zip

run.sh keeps its LF line endings and is marked executable; run.bat keeps CRLF (both are stored byte for byte).
"""

from __future__ import annotations

import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = ["README.md", "START-HERE.md", "AGENTS.md", "CLAUDE.md", "LICENSE", "NOTICE", "run.bat", "run.sh",
         "pyproject.toml", "config.example.toml"]
TREES = ["streamsplitter", "docs"]
SKIP_DIRS = {"__pycache__", ".pytest_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".tmp", ".part", ".bak"}
NEVER = {"config.toml", "secrets.json", "tba_key.txt"}


def members() -> list[Path]:
    out = [ROOT / f for f in FILES]
    for tree in TREES:
        for p in sorted((ROOT / tree).rglob("*")):
            if p.is_file() and not (set(p.relative_to(ROOT).parts) & SKIP_DIRS) and p.suffix not in SKIP_SUFFIXES:
                out.append(p)
    missing = [str(p) for p in out if not p.is_file()]
    if missing:
        raise SystemExit(f"missing: {missing}")
    bad = [p.name for p in out if p.name in NEVER]
    if bad:
        raise SystemExit(f"refusing to package {bad}")
    return out


def main() -> int:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    out = dist / f"StreamSplitter-{version}.zip"
    files = members()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in files:
            arc = "StreamSplitter/" + p.relative_to(ROOT).as_posix()
            info = zipfile.ZipInfo.from_file(p, arc)
            info.compress_type = zipfile.ZIP_STORED if p.suffix == ".mp4" else zipfile.ZIP_DEFLATED
            if p.name == "run.sh":
                info.external_attr = (0o100755 << 16)
            z.writestr(info, p.read_bytes())
    print(f"{out} ({out.stat().st_size / 1e6:.1f} MB, {len(files)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
