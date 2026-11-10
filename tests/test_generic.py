"""Event-agnostic path: overlay timer episodes (synthetic readings), the timeline, and title naming."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from streamsplitter.detect.overlay import Place, parse_time  # noqa: E402
from streamsplitter.namer import Namer, parse_title  # noqa: E402
from streamsplitter.timeline import Timeline, merge  # noqa: E402


def feed_countdown(p: Place, t_start: float, length: int, fps: float = 4.0, t_from: float | None = None,
                   misread: dict | None = None, until: float | None = None) -> float:
    """Readings of a display that sits at `length` from `t_from`, counts down from `t_start` (ceil rounding: the change
    to v happens at t_start + length - v) and then sits at 0. Returns the zero time."""
    zero = t_start + length
    t = t_from if t_from is not None else t_start - 2.0
    end = until if until is not None else zero + 3.0
    while t < end:
        rem = zero - t
        v = length if rem >= length else max(0, int(-(-rem // 1)))
        if misread and round(t, 2) in misread:
            v = misread[round(t, 2)]
        p.add(round(t, 3), v)
        t += 1.0 / fps
    return zero


def test_parse_time():
    assert parse_time("1:34") == (94, "mss")
    assert parse_time("0:11") == (11, "mss")
    assert parse_time("15") == (15, "plain")
    assert parse_time("314") is None
    assert parse_time("1:75") is None


def test_one_match_two_periods():
    p = Place(0, (900, 100, 1000, 150), "mss", 0.0, 1920, 1080)
    z1 = feed_countdown(p, 100.0, 20, t_from=40.0, until=100.0 + 20 + 3.0)   # pre-match 0:20, auto, 3 s pause at 0:00
    z2 = feed_countdown(p, z1 + 3.0, 140, t_from=z1 + 1.0)                     # display shows 2:20, then teleop
    eps = merge(p.episodes())
    assert len(eps) == 2
    assert abs(eps[0]["C"] - z1) < 0.2 and eps[0]["from_value"] == 20 and eps[0]["reason"] == "zero"
    assert abs(eps[1]["C"] - z2) < 0.2 and eps[1]["from_value"] == 140
    tl = Timeline()
    ms = tl.update(eps, now=z2 + 10)
    assert len(ms) == 1
    m = ms[0]
    assert abs(m["start"] - 100.0) < 0.2 and abs(m["end"] - z2) < 0.2 and m["start_src"] == "auto"
    assert tl.values() == {"L_auto": 20, "pause": tl.values()["pause"], "L_tele": 140}
    assert abs(tl.values()["pause"] - 3.0) < 0.3
    assert tl.update(eps, now=z2 + 70) == []           # decided once


def test_misreads_do_not_split_or_shift():
    p = Place(0, (900, 100, 1000, 150), "mss", 0.0, 1920, 1080)
    bad = {round(130.0 + i * 0.25, 2): 7 for i in range(4)}   # one second misread as 0:07
    z = feed_countdown(p, 100.0, 140, t_from=98.0, misread=bad)
    eps = merge(p.episodes())
    assert len(eps) == 1 and abs(eps[0]["C"] - z) < 0.2 and eps[0]["reason"] == "zero"


def lossy_readings(p: Place, keep: float, drop: float, s_until: float, fps_auto: float = 1.0, fps: float = 4.0,
                   hide: tuple[float, float] | None = None):
    """A match (auto 20 s from real time 100, 3 s pause, teleop 140 s) seen through a stream that keeps `keep` s and
    loses `drop` s, over and over (MadTown 2026's practice-day Twitch VOD: 61 of 94 min kept). Stream time 0 is real
    time 95; the auto's place is read at `fps_auto` (a place not yet seen counting is read once a second). Returns
    real_to_stream(r): the stream time showing real time r (the first frame at or after it)."""
    def real(s):
        return 95.0 + s + drop * (s // keep)

    def value(r):
        for start, length in ((100.0, 20), (123.0, 140)):
            if r < start + length:
                rem = start + length - r
                return length if rem >= length else max(0, int(-(-rem // 1)))
            if start == 100.0 and r < 123.0:
                return 0 if r >= 120.0 else None
        return 0
    s = 0.0
    while s < s_until:
        hidden = hide is not None and hide[0] <= real(s) < hide[1]     # the broadcast took the overlay down
        p.add(round(s, 3), None if hidden else value(real(s)))
        s += 1.0 / (fps_auto if real(s) < 121.0 else fps)

    def real_to_stream(r):
        s = 0.0
        while real(s) < r:
            s += 0.01
        return s
    return real_to_stream


def test_a_stream_that_loses_video_still_gives_the_match():
    p = Place(0, (900, 100, 1000, 150), "mss", 0.0, 1920, 1080)
    at = lossy_readings(p, keep=2.0, drop=1.0, s_until=120.0)
    eps = merge(p.episodes())
    tele = [e for e in eps if e["from_value"] >= 100]
    assert len(tele) == 1 and tele[0]["skipped"] > 30 and tele[0]["reason"] == "zero"
    ms = Timeline().update(eps, now=130.0)
    assert len(ms) == 1 and ms[0]["start_src"] == "auto"
    # never after the auto began (real 100 s fell in lost video here: at() is the first frame after it), at most 3 s
    # before; the end within 1 s of teleop's 0:00 (the clip adds 4 s)
    assert at(100.0) - 3.0 <= ms[0]["start"] <= at(100.0) + 0.5
    assert abs(ms[0]["end"] - at(263.0)) < 1.0
    assert ms[0]["end"] - ms[0]["start"] < 120                    # 163 s of match in what the stream kept


def test_lost_video_patterns():
    """Pieces kept and lost (s): every pattern gives the match, its start never late and at most 2 s early, its end
    within -0.5..+2 s (the clip adds 4 s). Not covered: an auto whose very first step is lost (keep 5, drop 2: it
    shows 20 then 18), which starts no countdown."""
    for keep, drop in ((2, 1), (3, 1), (1.5, 1), (2, 0.5), (4, 1), (1, 0.5), (10, 3)):
        p = Place(0, (900, 100, 1000, 150), "mss", 0.0, 1920, 1080)
        at = lossy_readings(p, keep=keep, drop=drop, s_until=200.0)
        ms = Timeline().update(merge(p.episodes()), now=200.0)
        assert len(ms) == 1, (keep, drop)
        assert -2.0 <= ms[0]["start"] - at(100.0) <= 0.5 and -0.5 <= ms[0]["end"] - at(263.0) <= 2.0, (keep, drop)


def test_lost_video_and_teleop_start_hidden():
    """MadTown 2026 practice 6: the overlay came back at 1:35 of teleop, and the stream lost a third of the video, so
    teleop showed 19 one-second changes and neither its start nor its length: the auto just before it is its auto."""
    p = Place(0, (900, 100, 1000, 150), "mss", 0.0, 1920, 1080)
    at = lossy_readings(p, keep=2.0, drop=1.0, s_until=120.0, hide=(121.0, 185.0))
    eps = merge(p.episodes())
    tele = [e for e in eps if e["n_changes"] >= 10 and e["from_value"] > 30]
    assert len(tele) == 1 and tele[0]["from_value"] < 100 and tele[0]["n_changes"] < 20 and tele[0]["skipped"] > 0
    tl = Timeline()
    ms = tl.update(eps, now=130.0)
    assert len(ms) == 1 and -2.0 <= ms[0]["start"] - at(100.0) <= 0.5 and -0.5 <= ms[0]["end"] - at(263.0) <= 2.0
    assert tl.learned == []                                        # a match that lost video teaches nothing


def test_a_hidden_teleop_start_alone_is_not_a_match():
    """The same teleop without its auto in view: under 20 changes and no auto, it stays uncut (not inferred)."""
    p = Place(0, (900, 100, 1000, 150), "mss", 0.0, 1920, 1080)
    lossy_readings(p, keep=2.0, drop=1.0, s_until=120.0, hide=(90.0, 185.0))
    tl = Timeline()
    tl.learned.append({"L_auto": 20, "pause": 3.0, "L_tele": 140, "proven": True})
    assert tl.update(merge(p.episodes()), now=200.0) == []


def test_a_whole_stream_never_reports_lost_video():
    p = Place(0, (900, 100, 1000, 150), "mss", 0.0, 1920, 1080)
    z1 = feed_countdown(p, 100.0, 20, t_from=40.0, until=123.0)
    bad = {round(z1 + 60.0 + i * 0.25, 2): 7 for i in range(4)}
    feed_countdown(p, z1 + 3.0, 140, t_from=z1 + 1.0, misread=bad)
    assert all(e["skipped"] == 0.0 and e["C0"] == e["C"] for e in p.episodes())


def test_a_jump_beyond_skip_max_is_a_new_countdown():
    p = Place(0, (900, 100, 1000, 150), "mss", 0.0, 1920, 1080)
    z = feed_countdown(p, 100.0, 140, t_from=98.0, until=150.0)
    feed_countdown(p, 145.0, 70, t_from=150.0, until=z)               # from 150 s on, 25 s ahead (zero 215, not 240)
    eps = p.episodes()
    assert len(eps) == 2 and eps[0]["reason"] == "broken" and eps[0]["skipped"] == 0.0


def test_teleop_alone_needs_learned_values():
    p = Place(0, (900, 100, 1000, 150), "mss", 0.0, 1920, 1080)
    z2 = feed_countdown(p, 200.0, 140, t_from=198.0)
    tl = Timeline()
    assert tl.update(merge(p.episodes()), now=z2 + 10) == []      # cold start: a long countdown alone is not a match
    tl.learned.append({"L_auto": 20, "pause": 3.0, "L_tele": 140, "proven": True})
    tl._done.clear()
    ms = tl.update(merge(p.episodes()), now=z2 + 10)
    assert len(ms) == 1 and ms[0]["start_src"] == "inferred" and abs(ms[0]["start"] - (200.0 - 23.0)) < 0.3


def test_a_break_timer_is_not_inferred():
    """MadTown 2026 finals: the "Field Break" timer counted down in the match timer's place, first seen already
    counting at 2:40; its unproven length became the learned 140 s, and its last 2:20 was cut as f1m1 and f1m2."""
    p = Place(0, (900, 100, 1000, 150), "mss", 0.0, 1920, 1080)
    z = feed_countdown(p, 0.0, 600, t_from=440.0)                    # first read at 2:40, counting
    tl = Timeline()
    tl.learned.append({"L_auto": 20, "pause": 3.0, "L_tele": 140, "proven": True})
    assert tl.update(merge(p.episodes()), now=z + 10) == []
    p2 = Place(1, (900, 100, 1000, 150), "mss", 0.0, 1920, 1080)
    z2 = feed_countdown(p2, 1000.0, 140, t_from=1002.0)               # a teleop joined at 2:18 is still inferred
    ms = tl.update(merge(p2.episodes()), now=z2 + 10)
    assert len(ms) == 1 and ms[0]["start_src"] == "inferred"


def test_aborted_teleop_not_cut():
    p = Place(0, (900, 100, 1000, 150), "mss", 0.0, 1920, 1080)
    z1 = feed_countdown(p, 100.0, 20, t_from=90.0)
    # teleop starts, then the display stops at 1:40 (field fault) and sits there
    feed_countdown(p, z1 + 3.0, 140, t_from=z1 + 1.0, until=z1 + 3.0 + 40.0)
    for k in range(40):
        p.add(z1 + 43.0 + k * 0.25, 100)
    tl = Timeline()
    assert tl.update(merge(p.episodes()), now=z1 + 400) == []
    assert [r["why"] for r in tl.rejected] == ["aborted"]


def test_parse_title():
    assert parse_title("Qualification 1") == {"level": "qualification", "n": 1}
    assert parse_title("Qualification 40 of 120") == {"level": "qualification", "n": 40}
    assert parse_title("Practice Match 3") == {"level": "practice", "n": 3}
    assert parse_title("Final Tiebreaker") == {"level": "final", "n": 3}
    assert parse_title("Final 2") == {"level": "final", "n": 2}
    assert parse_title("Match 10 (R3)") == {"level": "playoffs", "n": 10}
    assert parse_title("Match 11 – Round 4 Upper") == {"level": "playoffs", "n": 11}     # MadTown 2026 results
    assert parse_title("Match 13 - Round 5 Lower") == {"level": "playoffs", "n": 13}
    assert parse_title("Playoff 5") == {"level": "playoffs", "n": 5}
    assert parse_title("Match 12") is None
    assert parse_title("Chezy Champs") is None


def _entry(key, red, blue, level="qm", n=1):
    return {"key": key, "event_key": key.split("_")[0], "comp_level": level, "set_number": 1, "match_number": n,
            "alliances": {"red": {"team_keys": [f"frc{t}" for t in red]},
                          "blue": {"team_keys": [f"frc{t}" for t in blue]}}}


def test_name_overlay_title_and_teams():
    sched = [_entry("2026cc_qm1", [2910, 971, 3256], [9032, 5940, 973], n=1),
             _entry("2026cc_qm2", [254, 1678, 604], [649, 6036, 8033], n=2)]
    nm = Namer(sched, 0.0)
    r = nm.name_overlay(10.0, {"2910", "971", "3256", "9032", "5940", "973"}, {"level": "qualification", "n": 1}, 0.0)
    assert r["name"] == "2026cc_qm1" and r["source"] == "title+teams"
    # the title misread as 7 but the teams are qm2's: the teams win
    r = nm.name_overlay(400.0, {"254", "1678", "604", "649"}, {"level": "qualification", "n": 7}, 0.0)
    assert r["name"] == "2026cc_qm2"
    # a replay of qm1
    r = nm.name_overlay(900.0, {"2910", "971"}, {"level": "qualification", "n": 1}, 0.0)
    assert r["name"] == "2026cc_qm1_r2"


def test_name_overlay_without_schedule():
    nm = Namer([], 0.0, event="2025ketwk0")
    assert nm.name_overlay(5.0, set(), {"level": "qualification", "n": 4}, 0.0)["name"] == "2025ketwk0_qm4"
    assert nm.name_overlay(9.0, set(), {"level": "practice", "n": 2}, 0.0)["name"] == "2025ketwk0_p2"


def test_overlay_fading_in_after_the_start():
    """Chezy 2026: the overlay appears ~1.5 s into the match showing 0:19, never 0:20; the auto is still 20 s."""
    p = Place(0, (900, 100, 1000, 150), "mss", 0.0, 1920, 1080)
    z1 = feed_countdown(p, 100.0, 20, t_from=101.5, until=100.0 + 20 + 3.0)
    z2 = feed_countdown(p, z1 + 3.0, 140, t_from=z1 + 2.5)
    ms = Timeline().update(merge(p.episodes()), now=z2 + 10)
    assert len(ms) == 1 and ms[0]["L_auto"] == 20 and not ms[0]["L_auto_proven"]
    assert abs(ms[0]["start"] - 100.0) < 0.2


def test_overlay_gone_mid_teleop_is_partial():
    """Chezy 2026 qm54: the overlay disappeared at 1:10 left and the match was replayed; the attempt is partial."""
    p = Place(0, (900, 100, 1000, 150), "mss", 0.0, 1920, 1080)
    z1 = feed_countdown(p, 100.0, 20, t_from=90.0)
    z2 = feed_countdown(p, z1 + 3.0, 140, t_from=z1 + 1.0, until=z1 + 3.0 + 70.0)
    for k in range(40):                                   # overlay gone: nothing readable
        p.add(z1 + 73.0 + k * 0.25, None)
    ms = Timeline().update(merge(p.episodes()), now=z2 + 10)
    assert len(ms) == 1 and ms[0]["end_src"] == "extrapolated" and ms[0]["partial"]


def test_long_overlay_hide_is_one_countdown():
    """The overlay hidden for 40 s mid-teleop (a full-screen camera) and back: one match, end at zero, not partial."""
    p = Place(0, (900, 100, 1000, 150), "mss", 0.0, 1920, 1080)
    z1 = feed_countdown(p, 100.0, 20, t_from=90.0)
    z2 = z1 + 3.0 + 140
    feed_countdown(p, z1 + 3.0, 140, t_from=z1 + 1.0, until=z1 + 3.0 + 50.0)
    for k in range(160):
        p.add(z1 + 53.0 + k * 0.25, None)
    feed_countdown(p, z1 + 3.0, 140, t_from=z1 + 93.0)
    ms = Timeline().update(merge(p.episodes()), now=z2 + 10)
    assert len(ms) == 1 and ms[0]["end_src"] == "zero" and not ms[0]["partial"] and abs(ms[0]["end"] - z2) < 0.2


def test_two_places_one_match():
    """MadTown 2025: the overlay timer and a second timer in a camera view count the same match: one clip."""
    a = Place(0, (900, 100, 1000, 150), "mss", 0.0, 1920, 1080)
    b = Place(1, (1495, 683, 1533, 701), "mss", 0.0, 1920, 1080)
    for p in (a, b):
        z1 = feed_countdown(p, 100.0, 15, t_from=95.0)
        z2 = feed_countdown(p, z1 + 3.0, 135, t_from=z1 + 1.0)
    ms = Timeline().update(merge(a.episodes() + b.episodes()), now=z2 + 10)
    assert len(ms) == 1


def test_results_screen_with_stand_in_numbers(tmp_path):
    """MadTown 2024 qm7: the overlay shows three B teams under stand-in numbers (9991, 9992, 9971), so only 3 of the
    schedule's teams are on the results screen. The numbers the overlay showed all match long count as the match's
    teams too; a single static number (a score stuck at 20) does not make an unrelated screen a results screen."""
    from streamsplitter.pipeline import Pipeline

    class Reader:
        def timer_read_near(self, place, t, tol=1.0):
            return False

    sched = [{"key": "2024mttd_qm7", "event_key": "2024mttd", "comp_level": "qm", "set_number": 1,
              "match_number": 7, "alliances": {"red": {"team_keys": ["frc6657B", "frc1671", "frc3256"]},
                                               "blue": {"team_keys": ["frc5940", "frc1967B", "frc971B"]}}}]
    pipe = Pipeline(tmp_path, event="2024mttd", schedule=sched, reader=Reader(), fms_sounds=False)
    row = {"place": 0, "naming": {"key": "2024mttd_qm7", "teams_read": ["1671", "3256", "5940"]},
           "numbers_shown": ["1671", "20", "3256", "5940", "9971", "9991", "9992"]}
    screen = [{"t": 5800.0, "text": x} for x in ("5940", "9992", "9971", "9991", "1671", "3256", "104", "21")]
    assert pipe._is_results(screen, row)
    assert not pipe._is_results(screen, {**row, "numbers_shown": []})       # before the fix: 3 of 6 teams
    other = [{"t": 5800.0, "text": x} for x in ("20", "5940", "254", "1678")]
    assert not pipe._is_results(other, row)
