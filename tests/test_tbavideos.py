"""streamsplitter tba (streamsplitter/tbavideos.py): the uploaded YouTube videos onto The Blue Alliance afterwards.
Outboxes of the test's own, a fake TBA read, a fake Trusted API session and a fake YouTube client; no network.

    .venv\\Scripts\\python.exe -m pytest -q tests\\test_tbavideos.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from streamsplitter import cli, config, tba, tbavideos  # noqa: E402
from streamsplitter.publish import outbox, tbalink, templates, youtube  # noqa: E402
from streamsplitter.publish.base import Job, Match, PermanentError, TransientError  # noqa: E402
from test_tbalink import Resp, Session  # noqa: E402
from test_wizard import env  # noqa: E402,F401 - env is a fixture

EVENT = "2026mttd"


def tba_match(part, *videos):
    return {"key": f"{EVENT}_{part}", "videos": [{"type": "youtube", "key": v} for v in videos]}


def vid(n):
    return f"video{n:06d}"                      # 11 characters, like a YouTube id


def upload(db_path, part, video_id, *, done_at=1000.0, kind="score", name=None, replay=1, privacy="unlisted",
           title=None, status="done", destination="youtube", playlist="PL1", event=EVENT, locked=False):
    """One finished YouTube job in an outbox file, stored as the real outbox stores it."""
    key = f"{event}_{part}"
    m = Match(key=key, name=name or key, event_key=event, event_name="MadTown Throwdown", year=2026, replay=replay)
    state = {"title": title if title is not None else templates.render(templates.TITLE_FORMAT, m),
             "privacy": privacy, "playlist_id": playlist, "video_id": video_id, "playlist_done": True}
    job = Job(id=f"{name or key}@{done_at}:{kind}:{destination}", destination=destination, kind=kind,
              path="C:/clips/x.mp4", match=m, state=state)
    result = {"video_id": video_id, "url": youtube.watch_url(video_id), "privacy": privacy, "playlist_id": playlist,
              "locked_private": locked}
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA journal_mode=WAL")       # as the real outbox
    db.executescript(outbox._SCHEMA)
    db.execute("INSERT INTO jobs (id, destination, kind, json, status, result, created, updated, done_at) "
               "VALUES (?,?,?,?,?,?,?,?,?)", (job.id, destination, kind, json.dumps(job.to_dict()), status,
                                              json.dumps(result), done_at - 30, done_at, done_at))
    db.commit()
    db.close()


class FakeYT:
    """YouTubeClient.videos and .set_title over a dict of what "YouTube" has."""

    def __init__(self, have: dict[str, dict]):
        self.have, self.asked, self.renamed = have, [], []

    def videos(self, ids):
        self.asked.append(list(ids))
        return {i: self.have[i] for i in ids if i in self.have}

    def set_title(self, video_id, title, snippet):
        self.renamed.append((video_id, title, snippet))


def yt_record(title, privacy="unlisted"):
    return {"snippet": {"title": title, "description": "d", "categoryId": "17"}, "status": {"privacyStatus": privacy}}


class Out:
    def __init__(self):
        self.lines = []

    def __call__(self, *a):
        self.lines.append(" ".join(str(x) for x in a))

    @property
    def text(self):
        return "\n".join(self.lines)


def set_up(env):                                # noqa: F811
    config.save_sections({"event": {"key": EVENT, "name": "MadTown Throwdown", "year": 2026}})
    return env / "data" / "outbox"


# ---------------------------------------------------------------------------------------------------------------
# what was uploaded


def test_uploaded_reads_every_outbox_and_puts_the_best_upload_first(env):   # noqa: F811
    box = set_up(env)
    upload(box / "2026mttd.sqlite", "qm1", vid(1), done_at=100)
    upload(box / "2026mttd.sqlite", "qm2", vid(2), done_at=200)
    upload(box / "2026mttd_vod.sqlite", "qm2", vid(3), done_at=900)            # a later re-run from a better stream
    upload(box / "2026mttd.sqlite", "qm3", vid(4), done_at=300)
    upload(box / "2026mttd.sqlite", "qm3", vid(5), done_at=250, name="2026mttd_qm3_r2", replay=2)   # the replay
    upload(box / "2026mttd.sqlite", "sf7m1", vid(6), kind="clip")
    # none of these count: a partial clip, a private video, a failed upload, another destination, another event,
    # a practice match
    upload(box / "2026mttd.sqlite", "qm4", vid(7), name="2026mttd_qm4_partial")
    upload(box / "2026mttd.sqlite", "qm5", vid(8), privacy="private")
    upload(box / "2026mttd.sqlite", "qm6", vid(9), status="failed")
    upload(box / "2026mttd.sqlite", "qm7", vid(10), destination="drive")
    upload(box / "2026cc.sqlite", "qm8", vid(11), event="2026cc")
    upload(box / "2026mttd.sqlite", "p3", vid(12))
    upload(box / "2026mttd.sqlite", "qm9", vid(13), locked=True)               # YouTube locked it private
    (box / "broken.sqlite").write_text("not a database", encoding="utf-8")
    notes = []
    ours = tbavideos.uploaded(box, EVENT, log=notes.append)
    assert {p: [u["video_id"] for u in ups] for p, ups in ours.items()} == {
        "qm1": [vid(1)], "qm2": [vid(3), vid(2)], "qm3": [vid(5), vid(4)], "sf7m1": [vid(6)]}
    assert ours["qm1"][0]["title"] == "Qualification 1 - 2026 MadTown Throwdown" and ours["qm1"][0]["playlist_id"] == "PL1"
    assert any("broken.sqlite" in n for n in notes)


def test_uploaded_never_writes_to_an_outbox(env):   # noqa: F811
    box = set_up(env)
    upload(box / "2026mttd.sqlite", "qm1", vid(1))
    before = (box / "2026mttd.sqlite").read_bytes()
    tbavideos.uploaded(box, EVENT)
    assert (box / "2026mttd.sqlite").read_bytes() == before


def test_plan_counts_and_orders():
    ours = {"qm1": [{"video_id": vid(1)}], "qm2": [{"video_id": vid(2)}], "qm10": [{"video_id": vid(10)}],
            "sf2m1": [{"video_id": vid(20)}, {"video_id": vid(21)}], "f1m1": [{"video_id": vid(30)}]}
    matches = [tba_match("f1m1"), tba_match("sf2m1", "someoneelse1"), tba_match("qm10"), tba_match("qm2", vid(2)),
               tba_match("qm1"), tba_match("qm3", "someoneelse2"), {"key": "2026mttd_p1"}]
    p = tbavideos.plan(matches, ours, EVENT)
    assert (p["total"], p["with_video"], p["ours"], p["linked"]) == (6, 3, 5, ["qm2"])
    assert list(p["todo"]) == ["qm1", "qm10", "f1m1"]                          # playing order; no video on TBA yet
    assert p["todo"]["qm10"]["video_id"] == vid(10)
    every = tbavideos.plan(matches, ours, EVENT, every=True)
    assert list(every["todo"]) == ["qm1", "qm10", "sf2m1", "f1m1"] and every["todo"]["sf2m1"]["video_id"] == vid(20)


def test_a_video_youtube_no_longer_has_gives_way_to_the_next_one():
    ours = {"qm1": [{"video_id": vid(1), "title": "old"}, {"video_id": vid(2), "title": "old"}],
            "qm2": [{"video_id": vid(3), "title": "old"}], "qm3": [{"video_id": vid(4), "title": "old"}]}
    kept, dropped = tbavideos.on_youtube(ours, {vid(2): yt_record("Renamed by hand"), vid(4): yt_record("x", "private")})
    assert dropped == 3 and list(kept) == ["qm1"]
    assert kept["qm1"][0]["video_id"] == vid(2) and kept["qm1"][0]["title"] == "Renamed by hand"


# ---------------------------------------------------------------------------------------------------------------
# the command


def test_without_a_key_it_prints_tbas_page_and_the_playlist(env):   # noqa: F811
    box = set_up(env)
    upload(box / "2026mttd.sqlite", "qm1", vid(1))
    upload(box / "2026mttd.sqlite", "qm2", vid(2), title="Qualification 2 - 2026 MadTown Throwdown (qm2)")
    upload(box / "2026mttd.sqlite", "qm3", vid(3))
    out = Out()
    matches = [tba_match("qm1"), tba_match("qm2"), tba_match("qm3", "someoneelse1"), tba_match("qm4")]
    assert tbavideos.run(say=out, get=lambda path: matches, yt=False) == 0
    assert "MadTown Throwdown (2026mttd) on The Blue Alliance: 4 matches, 1 with a video." in out.text
    assert "uploaded a YouTube video for 3 of them; 0 of those are on The Blue Alliance already, 2 can be added" in out.text
    assert "https://www.thebluealliance.com/suggest/event/video?event_key=2026mttd" in out.text
    assert youtube.playlist_url("PL1") in out.text and tbalink.REQUEST_URL in out.text
    assert "1 of the 2 videos to add have the id in their title" in out.text
    assert "streamsplitter tba --retitle" in out.text and "not logged in to YouTube" in out.text


def test_no_event_and_no_tba(env):   # noqa: F811
    out = Out()
    assert tbavideos.run(say=out, get=lambda p: [], yt=False) == 2 and "No event" in out.text

    def down(path):
        raise tba.TBAError("HTTP 503")
    out = Out()
    assert tbavideos.run("2026mttd", say=out, get=down, yt=False) == 1 and "could not be read" in out.text


def test_nothing_to_add_says_so(env):   # noqa: F811
    box = set_up(env)
    upload(box / "2026mttd.sqlite", "qm1", vid(1))
    upload(box / "2026mttd.sqlite", "qm2", vid(2))
    out = Out()
    matches = [tba_match("qm1", vid(1)), tba_match("qm2", "someoneelse1")]
    assert tbavideos.run(say=out, get=lambda p: matches, yt=False) == 0
    assert "Nothing to add. (--all would add 1 more" in out.text and "suggest/event" not in out.text


def test_with_a_key_it_adds_one_match_per_request_after_asking(env):   # noqa: F811
    box = set_up(env)
    for n in (1, 2, 3):
        upload(box / "2026mttd.sqlite", f"qm{n}", vid(n))
    matches = [tba_match("qm1"), tba_match("qm2"), tba_match("qm3")]
    asked = []

    def no(q):
        asked.append(q)
        return False
    s = Session()
    out = Out()
    client = tbalink.TrustedClient("id", "sec", session=s)
    assert tbavideos.run(say=out, confirm=no, get=lambda p: matches, trusted=client, yt=False) == 0
    assert asked == ["Add 3 video link(s) to 2026mttd on The Blue Alliance?"] and s.posts == []
    assert "Only turn this on if you run the event" in out.text and "Nothing was added." in out.text

    # yes: the key is checked (an empty request), then one request per match; a match TBA lacks does not stop it
    s = Session(Resp(200, {"Success": "ok"}), Resp(200, {"Success": "ok"}), Resp(404, {"Error": "do not exist"}),
                Resp(200, {"Success": "ok"}))
    out = Out()
    client = tbalink.TrustedClient("id", "sec", session=s)
    assert tbavideos.run(say=out, yes=True, get=lambda p: matches, trusted=client, yt=False) == 0
    assert [json.loads(p["data"]) for p in s.posts] == [{}, {"qm1": vid(1)}, {"qm2": vid(2)}, {"qm3": vid(3)}]
    assert "Added 2 video link(s)" in out.text and "Not on The Blue Alliance yet: qm2." in out.text


def test_a_refused_key_adds_nothing_and_an_outage_stops_with_a_count(env):   # noqa: F811
    box = set_up(env)
    for n in (1, 2, 3):
        upload(box / "2026mttd.sqlite", f"qm{n}", vid(n))
    matches = [tba_match("qm1"), tba_match("qm2"), tba_match("qm3")]
    s = Session(Resp(401, {"Error": "Invalid"}))
    out = Out()
    client = tbalink.TrustedClient("id", "sec", session=s)
    assert tbavideos.run(say=out, yes=True, get=lambda p: matches, trusted=client, yt=False) == 1
    assert len(s.posts) == 1 and "did not work for 2026mttd" in out.text and "Nothing was added" in out.text

    s = Session(Resp(200, {}), Resp(200, {}), Resp(503, None))
    out = Out()
    client = tbalink.TrustedClient("id", "sec", session=s)
    assert tbavideos.run(say=out, yes=True, get=lambda p: matches, trusted=client, yt=False) == 1
    assert "Stopped at qm2" in out.text and "1 of 3 were added" in out.text and len(s.posts) == 3


def test_the_saved_key_is_used_and_never_shown(env):   # noqa: F811
    box = set_up(env)
    upload(box / "2026mttd.sqlite", "qm1", vid(1))
    config.set_secret("tba_write_id", "org-key-1")
    config.set_secret("tba_write_secret", "very-secret")
    out = Out()
    assert tbavideos.run(say=out, confirm=lambda q: False, get=lambda p: [tba_match("qm1")], yt=False) == 0
    assert "Add 1 video link(s)" not in out.text and "Nothing was added." in out.text      # asked, answered no
    assert "very-secret" not in out.text and "suggest/event" not in out.text


def test_videos_are_checked_on_youtube_when_logged_in(env):   # noqa: F811
    box = set_up(env)
    upload(box / "2026mttd.sqlite", "qm1", vid(1), done_at=100)
    upload(box / "2026mttd_vod.sqlite", "qm1", vid(2), done_at=900)            # the newer one was deleted since
    upload(box / "2026mttd.sqlite", "qm2", vid(3))
    yt = FakeYT({vid(1): yt_record("Qualification 1 - 2026 MadTown Throwdown"), vid(3): yt_record("Q2 by hand")})
    s = Session()
    out = Out()
    client = tbalink.TrustedClient("id", "sec", session=s)
    matches = [tba_match("qm1"), tba_match("qm2")]
    assert tbavideos.run(say=out, yes=True, get=lambda p: matches, trusted=client, yt=yt) == 0
    assert sorted(yt.asked[0]) == [vid(1), vid(2), vid(3)]
    assert [json.loads(p["data"]) for p in s.posts[1:]] == [{"qm1": vid(1)}, {"qm2": vid(3)}]
    assert "1 upload(s) are no longer on YouTube" in out.text and "not logged in" not in out.text

    class Down(FakeYT):
        def videos(self, ids):
            raise TransientError("YouTube: no connection")
    out = Out()
    assert tbavideos.run(say=out, get=lambda p: matches, yt=Down({})) == 0
    assert "YouTube could not be asked" in out.text and "not logged in" not in out.text


def test_retitle_adds_the_id_to_the_title_youtube_has_and_changes_nothing_else(env):   # noqa: F811
    box = set_up(env)
    upload(box / "2026mttd.sqlite", "qm1", vid(1))
    upload(box / "2026mttd.sqlite", "qm2", vid(2))
    upload(box / "2026mttd.sqlite", "sf7m1", vid(3))
    upload(box / "2026mttd.sqlite", "qm4", vid(4))                             # TBA has a video for it: left alone
    yt = FakeYT({vid(1): yt_record("Qualification 1 - 2026 MadTown Throwdown"),
                 vid(2): yt_record("Qualification 2 - 2026 MadTown Throwdown (qm2)"),      # has it already
                 vid(3): yt_record("Our edit of Match 7"), vid(4): yt_record("Qualification 4 - 2026 MadTown Throwdown")})
    matches = [tba_match("qm1"), tba_match("qm2"), tba_match("sf7m1"), tba_match("qm4", "someoneelse1")]
    asked = []
    out = Out()
    assert tbavideos.run(retitle=True, say=out, confirm=lambda q: asked.append(q) or False,
                         get=lambda p: matches, yt=yt) == 0
    assert asked == ["Rename 2 video(s) on YouTube?"] and yt.renamed == [] and "Nothing was renamed." in out.text
    assert "100 of YouTube's 10000 quota units" in out.text

    out = Out()
    assert tbavideos.run(retitle=True, yes=True, say=out, get=lambda p: matches, yt=yt) == 0
    assert [(v, t) for v, t, _ in yt.renamed] == [
        (vid(1), "Qualification 1 - 2026 MadTown Throwdown (qm1)"), (vid(3), "Our edit of Match 7 (sf7m1)")]
    assert yt.renamed[0][2] == {"title": "Qualification 1 - 2026 MadTown Throwdown", "description": "d",
                                "categoryId": "17"}                              # the snippet goes back with it
    assert all(tbalink.guess_partial(t) == p for (_, t, _), p in zip(yt.renamed, ("qm1", "sf7m1")))
    assert "Renamed 2 video(s)" in out.text


def test_retitle_needs_youtube_and_stops_cleanly(env):   # noqa: F811
    box = set_up(env)
    upload(box / "2026mttd.sqlite", "qm1", vid(1))
    upload(box / "2026mttd.sqlite", "qm2", vid(2), title="Q5 warm-up - Qualification 2")
    matches = [tba_match("qm1"), tba_match("qm2")]
    out = Out()
    assert tbavideos.run(retitle=True, yes=True, say=out, get=lambda p: matches, yt=False) == 1
    assert "setup --section youtube" in out.text

    class Full(FakeYT):
        def set_title(self, video_id, title, snippet):
            raise PermanentError("YouTube: the daily quota is used up")
    yt = Full({vid(1): yt_record("Qualification 1 - 2026 MadTown Throwdown"),
               vid(2): yt_record("Q5 warm-up - Qualification 2")})
    out = Out()
    assert tbavideos.run(retitle=True, yes=True, say=out, get=lambda p: matches, yt=yt) == 1
    assert "would read 'Q5 warm-up - Qualification 2 (qm2)' as qm5" in out.text   # never renamed to a wrong match
    assert "Stopped at qm1" in out.text and "0 of 1 were renamed" in out.text


def test_the_cli_has_the_command(env, monkeypatch):   # noqa: F811
    seen = {}
    monkeypatch.setattr(tbavideos, "run", lambda event, **kw: seen.update(event=event, **kw) or 0)
    assert cli._main(["tba", "--event", "2026cc", "--all", "--retitle", "--yes"]) == 0
    assert seen == {"event": "2026cc", "every": True, "retitle": True, "yes": True}
    assert cli._main(["tba"]) == 0 and seen == {"event": None, "every": False, "retitle": False, "yes": False}
