"""Minimal The Blue Alliance API v3 client (standard library only)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import config

BASE = "https://www.thebluealliance.com/api/v3"


class TBAError(RuntimeError):
    pass


def get(path: str, key: str | None = None):
    """GET a TBA path such as '/event/2026casj' and return the parsed JSON."""
    key = key or config.tba_key()
    if not key:
        raise TBAError("No TBA key: set TBA_API_KEY or tba_key / tba_key_file in config.toml")
    req = urllib.request.Request(BASE + path, headers={"X-TBA-Auth-Key": key, "User-Agent": "streamsplitter"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise TBAError(f"TBA {path}: HTTP {e.code}") from None
