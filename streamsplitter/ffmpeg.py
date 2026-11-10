"""Locate ffmpeg and ffprobe: PATH, then the installer's copy beside the venv's python, then winget's Links (Windows)."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def find(tool: str = "ffmpeg") -> str:
    on_path = shutil.which(tool)
    if on_path:
        return on_path
    # the installer (run.bat / run.sh) puts its own ffmpeg and ffprobe next to the venv's python
    here = shutil.which(tool, path=str(Path(sys.executable).parent))
    if here:
        return here
    links = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / f"{tool}.exe"
    if links.is_file():
        return str(links)
    raise FileNotFoundError(f"{tool} not found; install ffmpeg or put it on PATH")
