"""Results-screen titles: labels naming the next match are not this match's title; break banners are not matches."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from streamsplitter.pipeline import Pipeline  # noqa: E402


def _t(text, box):
    return {"t": 1.0, "text": text, "box": box, "score": 0.9}


def test_advances_to_label_is_not_a_title():
    """MadTown 2026 sf11's results screen: "Advances to" / "Final 1" under the winner renamed sf11m1 to f1m1."""
    read = [_t("Match 11 – Round 4 Upper", [570, 405, 735, 425]),
            _t("Advances to", [640, 360, 690, 368]), _t("Final 1", [650, 369, 685, 378]),
            _t("Advances to", [295, 360, 345, 368]), _t("Match 13 – Round 5 Lower", [275, 369, 365, 378]),
            _t("3256", [650, 260, 690, 278])]
    kept = [x["text"] for x in Pipeline._title_texts(read)]
    assert kept == ["Match 11 – Round 4 Upper", "3256"]
    assert [x["text"] for x in Pipeline._title_texts([_t("Advances to Final 1", [0, 0, 90, 9])])] == []


def test_break_banner_words():
    for s in ("Field Break", "Timeout", "TIME OUT", "Next Up:"):
        assert Pipeline.BREAK_WORDS.search(s), s
    for s in ("Qualification 12", "Madtown Throwdown", "Breakdown"):
        assert not Pipeline.BREAK_WORDS.search(s), s
