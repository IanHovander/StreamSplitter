"""The docs stay navigable for an AI agent that reads AGENTS.md first and then one small file (user, 2026-11-10:
"minimize token usage required on first read by another agent"): AGENTS.md stays small, lists every doc with an
honest size, and no link in the front pages or the docs points nowhere."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
AGENTS = ROOT / "AGENTS.md"
FRONT = [ROOT / "AGENTS.md", ROOT / "README.md", ROOT / "START-HERE.md"]
DOCS = sorted((ROOT / "docs").rglob("*.md"))
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
SIZED = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)(?:\s*\||\s*\()\s*~([\d.]+)k")       # "[x](path) | ~1.5k" or "[x](path) (~5k)"


def _tokens(path: Path) -> float:
    """A rough token count: four bytes a token, close enough for English docs."""
    return len(path.read_bytes()) / 4


def test_agents_file_is_small():
    assert _tokens(AGENTS) < 1800, "AGENTS.md is read first by every agent: move detail into docs/"


def test_claude_file_points_at_agents():
    assert (ROOT / "CLAUDE.md").read_text(encoding="utf-8").strip() == "@AGENTS.md"


def test_agents_lists_every_doc():
    text = AGENTS.read_text(encoding="utf-8")
    missing = [p.relative_to(ROOT).as_posix() for p in DOCS if f"({p.relative_to(ROOT).as_posix()})" not in text]
    assert not missing, f"docs not in AGENTS.md's table: {missing}"


def test_agents_sizes_are_honest():
    sized = SIZED.findall(AGENTS.read_text(encoding="utf-8"))
    assert len(sized) >= len(DOCS)
    for rel, stated in sized:
        real = _tokens(ROOT / rel) / 1000
        assert real / 1.5 <= float(stated) <= real * 1.5 + 0.1, f"{rel}: AGENTS.md says ~{stated}k, it is ~{real:.1f}k"


@pytest.mark.parametrize("path", FRONT + DOCS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_relative_links_resolve(path):
    broken = []
    for target in LINK.findall(path.read_text(encoding="utf-8")):
        if re.match(r"[a-z]+:", target) or target.startswith("#"):
            continue
        if not (path.parent / target.split("#")[0]).exists():
            broken.append(target)
    assert not broken, f"{path.name} links to missing files: {broken}"


def test_no_doc_holds_a_key():
    """Nothing that looks like a Slack token, a Google key or client secret, or a playlist link (a team's playlist
    is unlisted). docs/research/ cites FIRST's public playlists as sources, so the playlist check skips it."""
    key = re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,}|GOCSPX-[0-9A-Za-z_-]{10,}")
    playlist = re.compile(r"playlist\?list=PL[0-9A-Za-z_-]{16,}")
    for path in FRONT + DOCS:
        text = path.read_text(encoding="utf-8")
        assert not key.search(text), path.name
        if "research" not in path.relative_to(ROOT).parts:
            assert not playlist.search(text), path.name
