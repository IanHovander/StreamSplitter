"""Matcher fusion (field sounds + scoreboard clock) and namer, on synthetic events.

    .venv\\Scripts\\python.exe -m pytest -p no:cacheprovider tests\\test_matcher.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from streamsplitter.matcher import Matcher  # noqa: E402
from streamsplitter.namer import Namer  # noqa: E402


def cues(S: float, roles=("auto_end", "teleop", "endgame", "end")) -> list[dict]:
    t = {"start": ("start", 0.0), "auto_end": ("buzzer", 20.75), "teleop": ("bells", 24.8),
         "endgame": ("whistle", 135.1), "end": ("buzzer", 165.05)}
    return [{"kind": "audio", "cue": t[r][0], "t": S + t[r][1], "score": 0.6} for r in roles]


def episode(S: float, lag: float = 0.8, ok: bool = True, teams=None) -> dict:
    return {"final": True, "ok": ok, "auto_start": S + lag, "match_end": S + lag + 165.0, "rate": 1.0,
            "teams": teams}


def test_audio_and_clock_fuse_and_learn_lag():
    m = Matcher()
    m.add(cues(1000.0))
    m.add_clock([episode(1000.0)])
    (a,) = m.poll(1200.0)
    assert abs(a.start - 1000.0) < 1e-6 and a.start_src == "audio" and a.clock is not None
    assert abs(m.lag() - 0.8) < 1e-6


def test_clock_alone_makes_a_match_and_uses_the_learned_lag():
    m = Matcher()
    m.add(cues(1000.0))
    m.add_clock([episode(1000.0)])
    m.poll(1200.0)
    m.add(cues(2000.0, roles=("teleop",)))           # one cue only: not enough by itself
    m.add_clock([episode(2000.0)])
    (b,) = m.poll(2200.0)
    assert b.start_src == "clock"
    assert abs(b.start - 2000.0) < 0.05


def test_failed_clock_episode_and_single_cue_make_nothing():
    m = Matcher()
    m.add(cues(1000.0, roles=("teleop",)))
    m.add_clock([episode(1000.0, ok=False)])
    assert m.poll(1300.0) == []


def test_auto_only_replay_is_not_a_match():
    # an instant replay of AUTO with its field sounds: start, AUTO buzzer and bells, nothing from the end
    m = Matcher()
    m.add(cues(1000.0, roles=("start", "auto_end", "teleop")))
    assert m.poll(1300.0) == []


def test_late_clock_episode_annotates_a_final_match():
    m = Matcher()
    m.add(cues(1000.0))
    (a,) = m.poll(1200.0)
    assert a.clock is None
    m.add_clock([episode(1000.0)])
    assert a.clock is not None and m.poll(1400.0) == []


def entry(key, red, blue, actual=None, posted=None, n=1):
    return {"key": key, "comp_level": "qm", "set_number": 1, "match_number": n, "actual_time": actual,
            "post_result_time": posted,
            "alliances": {"red": {"team_keys": [f"frc{t}" for t in red]},
                          "blue": {"team_keys": [f"frc{t}" for t in blue]}}}


def test_namer_teams_time_order_and_replay():
    sched = [entry("e_qm1", [1, 2, 3], [4, 5, 6], 10_000, 10_200, 1),
             entry("e_qm2", [7, 8, 9], [10, 11, 12], 10_400, 10_600, 2),
             entry("e_qm3", [13, 14, 15], [16, 17, 18], 10_800, 11_000, 3)]
    nm = Namer(sched, release_ts=9_000)
    # four of six teams read (one misread): named by teams
    r = nm.name(1010.0, {"red": ["1", "2", "3"], "blue": ["4", "55", ""]}, now_wall=10_300)
    assert r["name"] == "e_qm1" and r["source"] == "teams"
    # no teams: qm2's result is posted by now, and the offset (+10 s) comes from qm1
    r = nm.name(1410.0, None, now_wall=10_700)
    assert r["name"] == "e_qm2" and r["source"] == "tba_time"
    # nothing posted, no teams: next in order, provisional
    r = nm.name(1810.0, None, now_wall=10_900)
    assert r["name"] == "e_qm3" and r["source"] == "order"
    # qm1's teams again: a replay
    r = nm.name(2500.0, {"red": ["1", "2", "3"], "blue": ["4", "5", "6"]}, now_wall=11_600)
    assert r["name"] == "e_qm1_r2"


def test_namer_tied_teams_take_the_first_unnamed_in_order():
    # finals: the same six teams in every match
    sched = [dict(entry("e_f1m2", [1, 2, 3], [4, 5, 6], n=2), comp_level="f"),
             dict(entry("e_f1m1", [1, 2, 3], [4, 5, 6], n=1), comp_level="f")]
    nm = Namer(sched, release_ts=0)
    six = {"red": ["1", "2", "3"], "blue": ["4", "5", "6"]}
    r = nm.name(10.0, six, now_wall=100)
    assert r["name"] == "e_f1m1" and r["source"] == "teams+order" and r["runner_up"] == 6
    assert nm.name(600.0, six, now_wall=700)["name"] == "e_f1m2"


def test_namer_tie_skips_a_match_tba_already_posted_elsewhere():
    # D2 finals, cold start: f1m1 was played and posted long before; f1m2 is not posted yet when its clip is cut
    six_red, six_blue = [1, 2, 3], [4, 5, 6]
    sched = [dict(entry("e_f1m1", six_red, six_blue, 1_000, 2_600, n=1), comp_level="f"),
             dict(entry("e_f1m2", six_red, six_blue, 3_900, 4_200, n=2), comp_level="f"),
             dict(entry("e_f1m3", six_red, six_blue, None, None, n=3), comp_level="f")]
    nm = Namer(sched, release_ts=0)
    r = nm.name(3_960.0, {"red": ["1", "2", "3"], "blue": ["4", "5", "6"]}, now_wall=4_150)
    assert r["name"] == "e_f1m2" and r["source"] == "teams+order"


def test_namer_whole_alliance_names_its_match():
    sched = [entry("e_qm1", [1, 2, 3], [4, 5, 6], n=1), entry("e_qm2", [1, 7, 3], [2, 8, 9], n=2)]
    nm = Namer(sched, release_ts=0)
    # red read whole, blue not at all (D2 qm63); qm2 also has 1, 2 and 3, but not together on one side
    r = nm.name(10.0, {"red": ["1", "2", "3"], "blue": []}, now_wall=100)
    assert r["name"] == "e_qm1" and r["source"] == "teams" and r["whole_alliance"]


def test_namer_too_few_teams_falls_back_to_nothing():
    sched = [entry("e_qm1", [1, 2, 3], [4, 5, 6], n=1), entry("e_qm2", [1, 2, 7], [4, 5, 8], n=2)]
    nm = Namer(sched, release_ts=0)
    r = nm.name(10.0, {"red": ["1", "2", ""], "blue": ["4", "", ""]}, now_wall=100)
    assert r["name"] is None and r["shared"] == 3 and not r["whole_alliance"]


def test_results_qr_names_and_corrects():
    from streamsplitter.namer import parse_results_url
    assert parse_results_url("https://frc.events/2026/CAGLE/playoffs/16") == \
        {"year": 2026, "event": "CAGLE", "n": 16, "level": "playoffs"}
    assert parse_results_url("https://example.com/nothing") is None
    sf = [dict(entry(f"e_sf{k}m1", [1, 2, 3], [4, 5, 6], n=1), comp_level="sf", set_number=k) for k in range(1, 14)]
    f = [dict(entry(f"e_f1m{k}", [1, 2, 3], [4, 5, 6], n=k), comp_level="f") for k in (1, 2, 3)]
    q = [entry("e_qm63", [7, 8, 9], [10, 11, 12], n=63)]
    sched = [dict(e, event_key="e") for e in q + sf + f]
    nm = Namer(sched, release_ts=0)
    # D2 Final Tiebreaker: provisionally f1m2 by time, the QR says playoffs/16 = the 16th playoff match = f1m3
    prov = nm.name(100.0, None, now_wall=0)            # nothing to go on yet
    assert prov["name"] is None
    prov = {"name": "e_f1m2", "key": "e_f1m2", "source": "teams+tba_time"}
    nm.used["e_f1m2"] = 1
    final = nm.confirm(prov, "https://frc.events/2026/CAGLE/playoffs/16")
    assert final["name"] == "e_f1m3" and final["source"] == "results_qr" and "e_f1m2" not in nm.used
    assert nm.confirm({"name": "e_qm63", "key": "e_qm63"}, "https://frc.events/2026/CAGLE/qualification/63")["qr_confirmed"]
    assert nm.confirm(None, "https://frc.events/2026/CAGLE/practice/7")["name"] == "e_p7"
    assert nm.confirm(prov, None) is None
