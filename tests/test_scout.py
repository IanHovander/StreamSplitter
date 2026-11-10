"""A better stream than the one being recorded (user, 2026-11-10): scout.py's search for a live YouTube stream of the
event that is on no list, and what live.EventWatch does with the recorder's list of links.

MadTown Throwdown 2026 is the case throughout: TBA listed official YouTube videos that never ran and the event's
Twitch channel, which was recorded for ten hours of Saturday and glitched, while Team 1671 streamed the whole event
on YouTube under a link nobody listed.

No network: the search, the stream resolver, the frame grab and the OCR read are stand-ins.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from streamsplitter import live, record, scout, streams  # noqa: E402
from tests.test_live_publish import (MTTD, URL1, URL2, V2, Clock, at, event_watch, infos, roots,  # noqa: E402,F401
                                     write_runs)

EVENT = {"key": "2026mttd", "name": "MadTown Throwdown", "short_name": "", "year": 2026}
FOUND_V = "CI6Dm6TSHzs"
FOUND = f"https://www.youtube.com/watch?v={FOUND_V}"
TWITCH = "https://www.twitch.tv/madtownrobotics"
TEAMS = {"1671", "254", "1678", "971", "4414", "2073"}


def hit(video, title, viewers=10, channel="FRC1671"):
    return {"video": video, "url": f"https://www.youtube.com/watch?v={video}", "title": title, "channel": channel,
            "viewers": viewers}


# ---------------------------------------------------------------------------------------------------------------
# the search
# ---------------------------------------------------------------------------------------------------------------

def test_a_title_names_the_event():
    yes = ["Madtown Throwdown 2026 Day 1", "MAD TOWN THROWDOWN - Finals", "2026 MadTown Throwdown | Qualifications",
           "FRC 1671 - Madtown ThrowDown day 2 (2026)"]
    no = ["2022 Madtown Throwdown", "Madtown Throwdown 2025 Day 1", "Madtown Robotics build stream",
          "Throwdown in Sacramento", ""]
    assert all(scout.title_matches(t, EVENT) for t in yes)
    assert not any(scout.title_matches(t, EVENT) for t in no)


def test_event_names_need_two_words_and_drop_the_sponsor():
    ev = {"name": "Sacramento Regional presented by Someone", "short_name": "Sacramento", "year": 2026}
    assert scout.event_names(ev) == ["Sacramento Regional"]                 # one word is in too many titles
    assert scout.event_names({"name": "Champs", "short_name": ""}) == []
    assert scout.title_matches("2026 Sacramento Regional Day 2", ev)
    assert not scout.title_matches("Sacramento Kings live", ev)


def test_candidates_are_matching_titles_most_watched_first_without_the_known():
    asked = []

    def search(q):
        asked.append(q)
        return [hit("aaaaaaaaaaa", "Madtown Throwdown 2026 Day 1", 5), hit("bbbbbbbbbbb", "Some other event live", 900),
                hit(FOUND_V, "Madtown Throwdown 2026 Day 1 - full field", 70), hit("ccccccccccc", "Madtown Throwdown 2026")]
    got = scout.candidates(EVENT, search, known={"ccccccccccc"})
    assert asked == ["MadTown Throwdown 2026"]
    assert [v["video"] for v in got] == [FOUND_V, "aaaaaaaaaaa"]


def test_vet_wants_three_of_the_events_teams_on_the_picture():
    src = types.SimpleNamespace(video=FOUND_V)
    seen = {"numbers": {"1671", "254", "1678", "2026", "135"}}

    def vet(**kw):
        return scout.vet(FOUND, TEAMS, resolver=lambda url, h: src, grab=lambda s: "frame",
                         read=lambda f: seen["numbers"], **kw)
    v = vet()
    assert v["live"] and v["ok"] and v["seen"] == ["254", "1671", "1678"]
    seen["numbers"] = {"1671", "2026"}                                      # between matches: a sponsor reel
    v = vet()
    assert v["live"] and not v["ok"] and "only 1 of the event's team numbers is" in v["why"]
    seen["numbers"] = set()
    assert "none of the event's team numbers" in vet()["why"]
    assert scout.vet(FOUND, TEAMS, resolver=lambda u, h: src, grab=lambda s: None)["why"].startswith("its picture")
    assert not scout.vet(FOUND, set(), resolver=lambda u, h: src)["ok"]     # no team list: nothing is confirmed

    def off(url, h):
        raise record.NotLive("not live")
    assert scout.vet(FOUND, TEAMS, resolver=off) == {"live": False, "ok": False, "seen": [], "why": "not live"}


def test_roster_reads_tbas_team_keys():
    got = scout.roster("2026mttd", lambda path: ["frc254", "frc1671", "frc9999B", "x"] if path.endswith("/keys") else 0)
    assert got == {"254", "1671"}


def test_a_round_vets_a_few_and_survives_a_failed_search():
    looked = []

    def vet(url, teams):
        looked.append((url, sorted(teams)))
        return {"live": True, "ok": True, "seen": ["254", "1671", "1678"], "why": "x"}
    many = [hit(f"{i:011d}", "Madtown Throwdown 2026", viewers=i) for i in range(6)]
    sc = scout.Scout("2026mttd", search=lambda q: many, vet=vet, roster=lambda key: {"254", "1671"},
                     log=lambda *a, **k: None)
    res = sc.round(EVENT)
    assert res["error"] is None and len(res["streams"]) == scout.VET_MAX == len(looked)
    assert res["streams"][0]["viewers"] == 5 and res["streams"][0]["ok"] and looked[0][1] == ["1671", "254"]
    assert sc.round(None)["error"].startswith("the event's record is not known")

    def broken(q):
        raise OSError("no internet")
    res = scout.Scout("2026mttd", search=broken, vet=vet, roster=lambda key: set()).round(EVENT)
    assert res == {"streams": [], "error": "OSError: no internet"}

    sc = scout.Scout("2026mttd", search=lambda q: many[:1], vet=vet, roster=lambda key: {"254"},
                     log=lambda *a, **k: None)
    assert sc.take() is None
    assert sc.start(None, refresh=lambda: EVENT)                            # TBA's record is asked on the round's thread
    sc._thread.join(timeout=10)
    assert len(sc.take()["streams"]) == 1 and sc.take() is None             # handed over once


# ---------------------------------------------------------------------------------------------------------------
# EventWatch: the recorder's list of links
# ---------------------------------------------------------------------------------------------------------------

class FakeScout:
    """A scout whose round is finished at once: start() remembers what it was asked, take() hands `result` over."""

    def __init__(self, streams_=(), error=None):
        self.result = {"streams": list(streams_), "error": error}
        self.started, self._ready = [], None

    def busy(self):
        return False

    def start(self, event, known=(), refresh=None):
        self.started.append(set(known))
        self._ready = self.result
        return True

    def take(self):
        out, self._ready = self._ready, None
        return out


def vetted(ok=True, video=FOUND_V, **kw):
    return {**hit(video, "Madtown Throwdown 2026 Day 1"), "live": True, "ok": ok,
            "seen": ["254", "1671", "1678"] if ok else [],
            "why": "x" if ok else "none of the event's team numbers are on its picture right now", **kw}


def twitch_only(roots, clock, sc=None, cli_url=None):   # noqa: F811 - roots is the fixture's value
    """MadTown, Saturday: TBA's dated YouTube video is the link, the Twitch channel the other one."""
    w, rec, started, stopped, note, mon = event_watch(roots, clock, cli_url=cli_url)
    if sc is not None:
        w.find_streams(sc)
    rec.also_for = w.others
    rec.start(w.choose())
    return w, rec, started, stopped, note, mon


def links_file(w):
    return json.loads((w.folder / record.LINKS_NAME).read_text(encoding="utf-8"))


def test_the_links_are_ranked_and_written_for_the_recorder(roots):   # noqa: F811
    clock = Clock()
    clock.t = at(7, 7)
    w, rec, started, *_ = twitch_only(roots, clock)
    assert w.url == URL2 and w.links() == [URL2, TWITCH] and rec.started_also == [[TWITCH]]
    assert links_file(w) == {"for": URL2, "links": [URL2, TWITCH]}
    assert w.links_text(URL2, rec.also) == f"stream link {URL2} (from The Blue Alliance); when it is not live: {TWITCH}"
    w.found[FOUND] = {"video": FOUND_V, "at": clock.t}
    assert w.links() == [URL2, FOUND, TWITCH]                               # YouTube before Twitch
    w.write_links()
    assert links_file(w)["links"] == [URL2, FOUND, TWITCH]


def test_a_twitch_link_goes_behind_youtube_unless_it_was_chosen_for_this_run(roots):   # noqa: F811
    from streamsplitter import config
    config.save_sections({"event": {"key": "2026mttd", "stream_url": TWITCH}})      # set up before the event
    clock = Clock()
    clock.t = at(7, 7)
    w, rec, started, *_ = twitch_only(roots, clock)
    assert w.url == TWITCH and not w.pinned() and w.links() == [URL2, TWITCH]
    assert started == [("2026mttd", TWITCH)] and rec.started_also == [[URL2]]
    assert links_file(w) == {"for": TWITCH, "links": [URL2, TWITCH]}
    text = w.links_text(TWITCH, rec.also)
    assert text.startswith(f"stream links, best first: {URL2}, {TWITCH} (YouTube before Twitch; {TWITCH} is the one "
                           f"from config.toml)") and "every 5 minutes" in text
    w.switch(TWITCH, live.SRC_SLACK)                                        # `stream <link>` in Slack: it is first
    assert w.pinned() and w.links() == [TWITCH, URL2] and rec.started_also[-1] == [URL2]
    assert links_file(w) == {"for": TWITCH, "links": [TWITCH, URL2]}
    w.mode, w.source = "auto", live.SRC_TBA                                 # `stream auto`
    assert not w.pinned()


def test_the_search_runs_every_five_minutes_on_the_events_days(roots):   # noqa: F811
    clock = Clock()
    clock.t = at(7, 7)
    sc = FakeScout()
    w, *_ = twitch_only(roots, clock, sc)
    w.tick()
    assert sc.started == []                                                 # the first one a minute after the start
    clock.t += live.SCOUT_FIRST_S
    w.tick()
    assert sc.started == [{V2}]                                             # what is listed is not looked for
    clock.t += live.SCOUT_S - 1
    w.tick()
    assert len(sc.started) == 1
    clock.t += 1
    w.tick()
    assert len(sc.started) == 2
    write_runs(w.folder, [{"run": 0, "video": V2, "link": URL2, "webpage_url": URL2, "started": clock.t,
                           "ended": None}])
    clock.t += live.SCOUT_S
    w.tick()                                                                # a YouTube stream is recorded: seldom
    clock.t += live.SCOUT_S
    w.tick()
    assert len(sc.started) == 3
    clock.t += live.SCOUT_IDLE_S
    w.tick()
    assert len(sc.started) == 4
    clock.t = at(10, 9)                                                     # the event is over
    w.tick()
    assert len(sc.started) == 4
    off, *_ = twitch_only(roots, Clock())                                   # [event] find_streams = false
    off.tick()
    assert off.scout is None


def test_a_found_stream_joins_the_list_and_the_contact_is_told(roots):   # noqa: F811
    clock = Clock()
    clock.t = at(7, 8)
    sc = FakeScout([vetted()])
    w, rec, started, stopped, note, _ = twitch_only(roots, clock, sc)
    write_runs(w.folder, [{"run": 0, "video": "v2921730738", "title": "MadTown Throwdown", "link": TWITCH,
                           "webpage_url": TWITCH, "started": clock.t + 5, "ended": None}])
    clock.t += live.SCOUT_FIRST_S
    w.tick()                                                                # the search starts ...
    w.tick()                                                                # ... and its result is taken
    assert list(w.found) == [FOUND] and w.links() == [URL2, FOUND, TWITCH]
    assert links_file(w) == {"for": URL2, "links": [URL2, FOUND, TWITCH]}   # no restart: the recorder reads this
    assert len(started) == 1 and not stopped
    text = infos(note)[-1]
    assert text.startswith("Found a YouTube stream of Madtown Throwdown that is on no list: Madtown Throwdown 2026 "
                           f"Day 1 (FRC1671) {FOUND}. Its picture shows this event's teams (254, 1671, 1678).")
    assert f"ahead of {TWITCH}: the recorder changes to it within about 5 minutes, between matches" in text
    n = len(infos(note))
    clock.t += live.SCOUT_S
    w.tick()
    w.tick()
    assert len(infos(note)) == n and FOUND_V in sc.started[-1]              # found once, said once

    again, *_ = twitch_only(roots, clock, FakeScout())                      # StreamSplitter is started again
    assert list(again.found) == [FOUND] and again.links() == [URL2, FOUND, TWITCH]
    clock.t += live.FOUND_KEEP_S + 1
    later, *_ = twitch_only(roots, clock, FakeScout())                      # ... and two days later
    assert later.found == {}
    plain, *_ = twitch_only(roots, clock)                                   # the search is off: nothing found is used
    assert plain.found == {}


def test_the_recorder_changing_to_the_better_stream_is_announced(roots):   # noqa: F811
    clock = Clock()
    clock.t = at(7, 8)
    w, rec, started, stopped, note, _ = twitch_only(roots, clock, FakeScout())
    w.found[FOUND] = {"video": FOUND_V, "at": clock.t}
    r0 = {"run": 0, "video": "v2921730738", "title": "MadTown Throwdown", "link": TWITCH, "webpage_url": TWITCH,
          "started": clock.t + 5, "ended": None, "end_reason": None}
    clock.t += 10
    write_runs(w.folder, [r0])
    w.tick()
    assert infos(note)[-1] == (f"Stream started: MadTown Throwdown {TWITCH}. Recording; your team's matches will be "
                               f"posted to #errata. The first choice, {URL2}, is not live; it is looked at every 5 "
                               f"minutes and taken as soon as it delivers.")
    r0 = {**r0, "ended": clock.t + 300, "end_reason": f"a better stream is live: {FOUND}"}
    r1 = {"run": 1, "video": FOUND_V, "title": "Madtown Throwdown 2026 Day 1", "link": FOUND, "webpage_url": FOUND,
          "started": clock.t + 301, "ended": None}
    clock.t += 310
    write_runs(w.folder, [r0, r1])
    w.tick()
    assert infos(note)[-1] == (f"Changed to a better stream: Madtown Throwdown 2026 Day 1 {FOUND} (it was {TWITCH}). "
                               f"Recording; your team's matches will be posted to #errata. To go back, reply here: "
                               f"stream {TWITCH}")
    assert len(started) == 1 and not stopped                                # the recorder did it by itself


def test_a_stream_that_could_not_be_confirmed_is_only_suggested_once(roots):   # noqa: F811
    clock = Clock()
    clock.t = at(7, 8)
    sc = FakeScout([vetted(ok=False), {**vetted(ok=False, video="ddddddddddd"), "live": False}])
    w, rec, started, stopped, note, _ = twitch_only(roots, clock, sc)
    clock.t += live.SCOUT_FIRST_S
    w.tick()
    w.tick()
    assert w.found == {} and w.links() == [URL2, TWITCH]
    (text,) = [t for t in infos(note) if "may be" in t]
    assert text.startswith(f"A live YouTube stream may be Madtown Throwdown: Madtown Throwdown 2026 Day 1 (FRC1671) "
                           f"{FOUND}. It is not recorded: StreamSplitter could not confirm it is this event (none of")
    assert text.endswith(f"To record it now, reply here: stream {FOUND}")
    clock.t += live.SCOUT_S
    w.tick()
    w.tick()
    assert len([t for t in infos(note) if "may be" in t]) == 1 and FOUND_V not in sc.started[-1]   # looked at again
    sc.result = {"streams": [vetted()], "error": None}                      # a match is on: its picture shows teams
    clock.t += live.SCOUT_S
    w.tick()
    w.tick()
    assert list(w.found) == [FOUND] and "Found a YouTube stream" in infos(note)[-1]


def test_a_link_chosen_in_slack_stays_first_and_a_find_is_offered(roots):   # noqa: F811
    clock = Clock()
    clock.t = at(7, 8)
    sc = FakeScout([vetted()])
    w, rec, started, stopped, note, _ = twitch_only(roots, clock, sc)
    w.mode = "fixed"                                                        # as `stream <link>` does (LiveControls)
    w.switch(TWITCH, live.SRC_SLACK)
    write_runs(w.folder, [{"run": 0, "video": "v1", "link": TWITCH, "webpage_url": TWITCH, "started": clock.t + 5,
                           "ended": None}])
    clock.t += live.SCOUT_FIRST_S
    w.tick()
    w.tick()
    assert w.links() == [TWITCH, URL2, FOUND]
    text = infos(note)[-1]
    assert f"It is not recorded: you chose {TWITCH}, which stays the first choice." in text
    assert text.endswith(f"To record the found one, reply here: stream {FOUND}")


def test_with_no_link_at_all_a_confirmed_find_is_recorded(roots):   # noqa: F811
    clock = Clock()
    clock.t = at(7, 8)
    event = {**MTTD, "webcasts": []}
    w, rec, started, stopped, note, _ = event_watch(roots, clock, tba=lambda p: dict(event))
    sc = FakeScout([vetted()])
    w.find_streams(sc)
    rec.also_for = w.others
    rec.start(w.choose())
    assert w.url is None and started == []
    clock.t += live.SCOUT_FIRST_S
    w.tick()
    w.tick()
    assert started == [("2026mttd", FOUND)] and w.url == FOUND and w.source == live.SRC_SEARCH
    assert "No other link is set, so it is recorded from now." in infos(note)[-1]
    assert links_file(w) == {"for": FOUND, "links": [FOUND]}


def test_a_failed_search_is_only_logged(roots):   # noqa: F811
    clock = Clock()
    clock.t = at(7, 8)
    w, rec, started, stopped, note, _ = twitch_only(roots, clock, FakeScout(error="OSError: no internet"))
    lines = []
    w.log = lambda s, **_: lines.append(s)
    clock.t += live.SCOUT_FIRST_S
    w.tick()
    w.tick()
    assert lines == ["stream search: OSError: no internet"] and not infos(note)


# ---------------------------------------------------------------------------------------------------------------
# a match is being played: the recorder waits with a change
# ---------------------------------------------------------------------------------------------------------------

def test_match_on_says_whether_a_timer_was_read_near_the_chunks_end(tmp_path):
    episodes = []
    pipe = types.SimpleNamespace(reader=types.SimpleNamespace(episodes=lambda: episodes))

    def state():
        return json.loads((tmp_path / record.MATCH_ON_NAME).read_text(encoding="utf-8"))
    live._match_on(tmp_path, pipe, {"t0": 100.0, "t1": 120.0}, 5000.0)
    assert state() == {"counting": False, "wall": 5000.0}
    episodes.append({"t_first": 40.0, "t_last": 119.0})
    live._match_on(tmp_path, pipe, {"t0": 100.0, "t1": 120.0}, 5020.0)
    assert state() == {"counting": True, "wall": 5020.0}
    live._match_on(tmp_path, pipe, {"t0": 160.0, "t1": 180.0}, 5080.0)      # the timer was last read a minute ago
    assert state() == {"counting": False, "wall": 5080.0}
    live._match_on(tmp_path, types.SimpleNamespace(), {"t0": 0.0, "t1": 20.0}, 6000.0)   # no overlay reader
    assert state()["wall"] == 5080.0


# ---------------------------------------------------------------------------------------------------------------
# streams.py helpers
# ---------------------------------------------------------------------------------------------------------------

def test_kind_and_video_id():
    assert streams.kind(FOUND) == "youtube" and streams.kind("youtu.be/" + FOUND_V) == "youtube"
    assert streams.kind(TWITCH) == "twitch" and streams.kind("https://example.com/x") is None
    assert streams.kind(None) is None and streams.kind("") is None
    assert streams.video_id(FOUND) == FOUND_V and streams.video_id("https://youtu.be/" + FOUND_V) == FOUND_V
    assert streams.video_id("https://www.youtube.com/@FRC1671/live") is None and streams.video_id(TWITCH) is None
    assert streams.video_id(None) is None


def test_an_undated_single_video_is_one_of_the_events_other_streams():
    ev = {"webcasts": [{"type": "youtube", "channel": "bAr8ht1xDEI"},       # MadTown, Sunday: Team 1671's, no date
                       {"type": "youtube", "channel": "y7wcg1gn8-M", "date": "2026-11-08"},
                       {"type": "youtube", "channel": "QEtvEsSF9_I", "date": "2026-11-07"},
                       {"type": "twitch", "channel": "madtownthrowdown"}]}
    import datetime as dt
    got = streams.alternates(ev, dt.date(2026, 11, 8), "https://www.twitch.tv/madtownthrowdown")
    assert got == ["https://www.youtube.com/watch?v=y7wcg1gn8-M", "https://www.youtube.com/watch?v=bAr8ht1xDEI"]
