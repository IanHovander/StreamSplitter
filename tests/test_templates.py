"""YouTube titles, descriptions and playlist names (publish/templates.py): FIRST's match labels, replays, unknown
values, and format checking for the setup wizard.

    .venv\\Scripts\\python.exe -m pytest -p no:cacheprovider tests\\test_templates.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from streamsplitter.publish import templates as T  # noqa: E402
from streamsplitter.publish.base import Match  # noqa: E402


def m(key: str | None, name: str | None = None, **kw) -> Match:
    level = set_n = num = None
    return Match(**{"key": key, "name": name or key or "", "event_key": "2026mttd", "event_name": "MadTown Throwdown",
                    "year": 2026, "comp_level": level, "set_number": set_n, "match_number": num, **kw})


def title(match: Match, **kw) -> str:
    return T.render(T.TITLE_FORMAT, match, **kw)


@pytest.mark.parametrize("key, expected", [
    ("2026mttd_qm12", "Qualification 12 - 2026 MadTown Throwdown"),
    ("2026mttd_sf1m1", "Match 1 (R1) - 2026 MadTown Throwdown"),
    ("2026mttd_sf5m1", "Match 5 (R2) - 2026 MadTown Throwdown"),
    ("2026mttd_sf10m1", "Match 10 (R3) - 2026 MadTown Throwdown"),
    ("2026mttd_sf12m1", "Match 12 (R4) - 2026 MadTown Throwdown"),
    ("2026mttd_sf13m1", "Match 13 (R5) - 2026 MadTown Throwdown"),
    ("2026mttd_f1m1", "Final 1 - 2026 MadTown Throwdown"),
    ("2026mttd_f1m2", "Final 2 - 2026 MadTown Throwdown"),
    ("2026mttd_f1m3", "Final Tiebreaker - 2026 MadTown Throwdown"),
    ("2026mttd_p3", "Practice 3 - 2026 MadTown Throwdown"),
])
def test_titles_follow_first(key, expected):
    assert title(m(key)) == expected


def test_titles_from_tba_fields_without_a_parsable_name():
    # the Match's own level and numbers win; the clip name need not carry them
    assert title(m("2026mttd_sf7m1", name="clip-0412", comp_level="sf", set_number=7, match_number=1)) \
        == "Match 7 (R2) - 2026 MadTown Throwdown"
    assert title(m(None, name="2026mttd_p3", comp_level="p", match_number=3)) == "Practice 3 - 2026 MadTown Throwdown"


def test_replays_are_marked():
    assert title(m("2026mttd_qm12", name="2026mttd_qm12_r2", replay=2)) \
        == "Qualification 12 (Replay) - 2026 MadTown Throwdown"
    assert title(m("2026mttd_qm12", name="2026mttd_qm12_r3")) == "Qualification 12 (Replay 2) - 2026 MadTown Throwdown"
    assert T.values(m("2026mttd_f1m2", name="2026mttd_f1m2_r2"))["replay"] == "Replay"
    # the pipeline keeps a stopped attempt as <key>_partial, <key>_partial2
    assert title(m("2026mttd_qm12", name="2026mttd_qm12_partial")) == "Qualification 12 (Partial) - 2026 MadTown Throwdown"
    assert title(m("2026mttd_sf3m1", name="2026mttd_sf3m1_partial2")) == "Match 3 (R1) (Partial 2) - 2026 MadTown Throwdown"


def test_four_alliance_bracket_and_older_seasons():
    assert title(m("2026mttd_sf5m1"), alliances=4) == "Match 5 (R3) - 2026 MadTown Throwdown"
    old = dict(event_key="2019casj", event_name="Silicon Valley Regional", year=2019)
    assert title(m("2019casj_qf2m1", **old)) == "Quarterfinal 2 Match 1 - 2019 Silicon Valley Regional"
    assert title(m("2019casj_sf1m2", **old)) == "Semifinal 1 Match 2 - 2019 Silicon Valley Regional"
    assert title(m("2019casj_f1m3", **old)) == "Final 3 - 2019 Silicon Valley Regional"


def test_description_with_scores():
    assert T.example(T.DESCRIPTION_FORMAT) == (
        "Qualification 12 - 2026 MadTown Throwdown\n"
        "Red (Teams 5026, 254, 1678) - 112\n"
        "Blue (Teams 604, 8033, 199) - 98\n"
        "https://www.thebluealliance.com/match/2026mttd_qm12\n"
        "\n"
        "Recorded from the event webcast with StreamSplitter.")


@pytest.mark.parametrize("score", [None, -1])          # TBA posts -1 for a match not scored yet
def test_unknown_scores_leave_out_the_score_not_the_teams(score):
    text = T.render(T.DESCRIPTION_FORMAT, m("2026mttd_qm12", red=["1", "2", "3"], blue=["4", "5", "6"],
                                            red_score=score, blue_score=score))
    assert "None" not in text and "-1" not in text
    assert text.splitlines()[1:3] == ["Red (Teams 1, 2, 3)", "Blue (Teams 4, 5, 6)"]


def test_zero_is_a_score():
    text = T.render(T.DESCRIPTION_FORMAT, m("2026mttd_qm12", red=["1"], blue=["2"], red_score=0, blue_score=7))
    assert "Red (Teams 1) - 0\nBlue (Teams 2) - 7" in text


def test_unnamed_clip_drops_unknown_lines_and_keeps_a_title():
    clip = Match(key=None, name="2026mttd_unnamed_1405", event_key="2026mttd", event_name="MadTown Throwdown",
                 year=2026)
    text = T.render(T.DESCRIPTION_FORMAT, clip)
    assert text == "2026mttd_unnamed_1405 - 2026 MadTown Throwdown\n\nRecorded from the event webcast with StreamSplitter."
    assert title(clip) == "2026mttd_unnamed_1405 - 2026 MadTown Throwdown"


def test_practice_has_no_tba_link():
    text = T.render(T.DESCRIPTION_FORMAT, m("2026mttd_p3", red=["1"], blue=["2"]))
    assert "thebluealliance" not in text and text.startswith("Practice 3 - 2026 MadTown Throwdown\n")


def test_one_line_format_with_blanks_is_tidied():
    assert title(Match(key="2026mttd_qm1", name="2026mttd_qm1")) == "Qualification 1 - 2026"
    assert T.render("{match_title} | {event_name}", Match(key="x_qm1", name="x_qm1")) == "Qualification 1"


def test_brackets_without_fields_are_text_and_braces_escape():
    assert T.render("[{red}] [Team 5026] {{x}}", m("2026mttd_qm1", red=["9"])) == "9 [Team 5026] {x}"


def test_playlist_and_default_description():
    assert T.render_playlist(T.PLAYLIST_FORMAT, "2026mttd", "MadTown Throwdown", 2026) == "2026 MadTown Throwdown"
    assert T.default_description("5026").endswith("Recorded from the event webcast by FRC Team 5026.")
    assert T.default_description(None) == T.DESCRIPTION_FORMAT


@pytest.mark.parametrize("fmt, words", [
    ("{colour} match", "unknown field {colour}"),
    ("{red!r}", "plainly"),
    ("{number:03}", "plainly"),
    ("{red.__class__}", "unknown field"),
    ("{}", "unknown field {}"),
    ("{match_title", "literal brace"),
    ("<b>{match_title}</b>", "< or >"),
    ("", "empty"),
])
def test_invalid_formats_say_what_is_wrong(fmt, words):
    with pytest.raises(T.TemplateError) as e:
        T.validate(fmt, "description")
    assert words in str(e.value)


def test_title_rules():
    T.validate(T.TITLE_FORMAT, "title")
    T.validate(T.PLAYLIST_FORMAT, "playlist")
    T.validate(T.DESCRIPTION_FORMAT, "description")
    with pytest.raises(T.TemplateError, match="one line"):
        T.validate("{match_title}\n{red}", "title")
    with pytest.raises(T.TemplateError, match="YouTube allows 100"):
        T.validate("{match_title} " + "x" * 100, "title")


def test_every_field_renders_for_the_sample():
    vals = T.values(T.SAMPLE)
    assert set(vals) == set(T.FIELDS)
    assert all(vals[f] for f in T.FIELDS if f != "replay")
    assert T.example("{" + "} {".join(T.FIELDS) + "}")
