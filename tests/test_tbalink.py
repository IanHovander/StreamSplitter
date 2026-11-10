"""The Blue Alliance video links (publish/tbalink.py): the signed Trusted API call, the "link" destination, and the
Publisher queueing a link after each YouTube upload only when [youtube] tba_links is on. Fake HTTP session; no
network.

    .venv\\Scripts\\python.exe -m pytest -q tests\\test_tbalink.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from streamsplitter import config  # noqa: E402
from streamsplitter.publish import tbalink  # noqa: E402
from streamsplitter.publish.base import Job, Match, PermanentError, TransientError  # noqa: E402
from test_publisher import YT, Clock, publisher, row  # noqa: E402
from test_wizard import FakeServices, ScriptedIO, Wizard, env  # noqa: E402,F401 - env is a fixture

VID = "dQw4w9WgXcQ"


class Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class Session:
    """Records each POST; answers from a list (last one repeats)."""

    def __init__(self, *answers):
        self.answers = list(answers) or [Resp(200, {"Success": "Match videos successfully updated"})]
        self.posts = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append({"url": url, "data": data, "headers": dict(headers or {})})
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return a


def match(key="2026mttd_qm6", name=None, event="2026mttd"):
    return Match(key=key, name=name or key, event_key=event, event_name="MadTown Throwdown", year=2026,
                 comp_level="qm", set_number=1, match_number=6)


def link_job(m=None, **state):
    return Job(id="2026mttd_qm6@0.0:link:tba", destination="tba", kind="link", path="C:/clips/x.mp4",
               match=m or match(), state={"video_id": VID, "link_until": 2_000_000_000.0, **state})


def test_partial_keys():
    assert tbalink.partial_key("2026mttd_qm6", "2026mttd") == "qm6"
    assert tbalink.partial_key("2026mttd_sf3m1", "2026mttd") == "sf3m1"
    assert tbalink.partial_key("2026mttd_f1m2", "2026mttd") == "f1m2"
    assert tbalink.partial_key("2026mttd_p3", "2026mttd") is None          # practice: no TBA page
    assert tbalink.partial_key("2026cc_qm6", "2026mttd") is None
    assert tbalink.partial_key(None, "2026mttd") is None


def test_the_call_is_signed_as_tba_checks_it():
    """TBA: md5(secret + request path + body), the path without the host, the body byte for byte as sent."""
    s = Session()
    c = tbalink.TrustedClient("my-id", "s3cret", session=s)
    assert c.add_videos("2026mttd", {"qm6": VID}) == {"Success": "Match videos successfully updated"}
    p = s.posts[0]
    path = "/api/trusted/v1/event/2026mttd/match_videos/add"
    assert p["url"] == "https://www.thebluealliance.com" + path
    body = p["data"].decode("utf-8")
    assert json.loads(body) == {"qm6": VID} and body == '{"qm6":"' + VID + '"}'
    assert p["headers"]["X-TBA-Auth-Id"] == "my-id"
    assert p["headers"]["X-TBA-Auth-Sig"] == hashlib.md5(f"s3cret{path}{body}".encode()).hexdigest()
    assert "s3cret" not in json.dumps(p["headers"])


def test_errors_map_to_what_a_person_does():
    def call(resp):
        return tbalink.TrustedClient("id", "sec", session=Session(resp)).add_videos("2026mttd", {"qm6": VID})
    with pytest.raises(PermanentError, match="refused the write key: Only allowed to edit events"):
        call(Resp(401, {"Error": "Only allowed to edit events: 2026cc"}))
    with pytest.raises(tbalink.NotOnTBA):
        call(Resp(404, {"Error": "Matches ['2026mttd_sf9m1'] do not exist!"}))
    with pytest.raises(TransientError) as e:
        call(Resp(503, None, {"Retry-After": "30"}))
    assert e.value.retry_after == 30
    with pytest.raises(TransientError, match="no connection"):
        call(requests.exceptions.ConnectionError("offline"))
    with pytest.raises(tbalink.Refused):
        call(Resp(400, {"Error": "Invalid video"}))
    with pytest.raises(PermanentError, match="no TBA write key"):
        tbalink.TrustedClient("", "sec")


def test_key_check_changes_nothing_and_says_why():
    s = Session(Resp(200, {"Success": "Match videos successfully updated"}))
    assert tbalink.TrustedClient("id", "sec", session=s).check("2026mttd") is None
    assert s.posts[0]["data"] == b"{}"
    assert tbalink.TrustedClient("id", "sec", session=Session(Resp(400, {"Error": "empty"}))).check("2026mttd") is None
    why = tbalink.TrustedClient("id", "sec", session=Session(Resp(401, {"Error": "Invalid"}))).check("2026mttd")
    assert "refused the write key" in why


def test_destination_links_once_and_waits_for_a_match_tba_lacks():
    clock = Clock(1_900_000_000.0)
    s = Session(Resp(404, {"Error": "Matches do not exist!"}), Resp(200, {"Success": "ok"}))
    d = tbalink.TBALinkDestination(tbalink.TrustedClient("id", "sec", session=s), clock=clock, log=lambda t: None)
    job = link_job()
    with pytest.raises(tbalink.NotOnTBA) as e:                # not scheduled on TBA yet: try again later
        d.send(job)
    assert e.value.retry_after == tbalink.NOT_YET_EVERY_S
    out = d.send(job)
    assert out == {"linked": "2026mttd_qm6", "video_id": VID, "url": "https://www.thebluealliance.com/match/2026mttd_qm6"}
    assert d.send(job)["linked"] == "2026mttd_qm6" and len(s.posts) == 2      # done: not posted again
    late = link_job(link_until=clock.t - 1)
    d2 = tbalink.TBALinkDestination(tbalink.TrustedClient("id", "sec", session=Session(Resp(404, {}))), clock=clock)
    with pytest.raises(PermanentError, match="still has no match 2026mttd_qm6"):
        d2.send(late)


def test_destination_refuses_what_it_cannot_link():
    d = tbalink.TBALinkDestination(tbalink.TrustedClient("id", "sec", session=Session()), log=lambda t: None)
    with pytest.raises(PermanentError, match="no YouTube video id"):
        d.send(link_job(video_id="not-an-id"))
    assert "skipped" in d.send(link_job(match(key="2026mttd_p3")))
    assert d.client.session.posts == []


def test_publisher_queues_a_link_only_when_turned_on(tmp_path):
    pub, box, dests, clock = publisher(tmp_path)               # tba_links off (the default)
    pub.on_score(row("2026mttd_qm2", "2026mttd_qm2", score="C:/clips/2026mttd_qm2_score.mp4"))
    box.step("youtube")
    assert box.get("2026mttd_qm2@0.0:link:tba") is None

    pub, box, dests, clock = publisher(tmp_path / "on", yt={**YT, "tba_links": True})
    pub.on_score(row("2026mttd_qm2", "2026mttd_qm2", score="C:/clips/2026mttd_qm2_score.mp4"))
    pub.on_clip(row("2026mttd_qm3_partial", "2026mttd_qm3", partial=True))
    pub.on_score(row(None, shown=["11", "22"], clip="C:/clips/t00000900.0.mp4"))
    for _ in range(3):
        box.step("youtube")
    link = box.get("2026mttd_qm2@0.0:link:tba")
    assert link["status"] == "queued" and link["job"].kind == "link"
    assert link["job"].state["video_id"] == "v1" and link["job"].state["link_until"] == clock.t + tbalink.LINK_FOR_S
    assert [j["id"] for j in box.jobs("tba")] == ["2026mttd_qm2@0.0:link:tba"]   # not the partial or unnamed clip


# ---------------------------------------------------------------------------------------------------------------
# the setup step (setup --section tbalinks)

def set_up_youtube(privacy="unlisted", **extra):
    config.save_sections({"event": {"key": "2026mttd", "name": "MadTown Throwdown", "year": 2026},
                          "youtube": {"enabled": True, "privacy": privacy, "playlist_id": "PL1", **extra}})


def test_setup_warns_first_and_stays_off_by_default(env):
    set_up_youtube()
    io = ScriptedIO([("Add the videos to The Blue Alliance", ""), ("TBA id at the end of new video titles", "")])
    Wizard(io, FakeServices()).run("tbalinks")
    assert not io.script and "[y/N]" in io.prompts[0] and "[y/N]" in io.prompts[1]
    assert "Only turn this on if you run the event" in io.text and "Not recommended otherwise." in io.text
    assert not config.section("youtube").get("tba_links")
    assert "title_format" not in config.section("youtube")              # Enter: the titles stay as they are


def test_setup_without_a_key_offers_the_match_id_in_new_titles(env):
    """No write key: TBA's playlist page fills in each video's match when the title ends with the match's id."""
    set_up_youtube(title_format="{match_title} at {event_name}")
    io = ScriptedIO([("Add the videos to The Blue Alliance", "n"), ("TBA id at the end of new video titles", "y")])
    Wizard(io, FakeServices()).run("tbalinks")
    assert "streamsplitter tba" in io.text and "(qm12)" in io.text
    yt = config.section("youtube")
    assert yt["title_format"] == "{match_title} at {event_name}[ ({tba_id})]" and not yt.get("tba_links")
    io = ScriptedIO([("Add the videos to The Blue Alliance", "n")])      # there already: not asked again
    Wizard(io, FakeServices()).run("tbalinks")
    assert not io.script and "already carry the id" in io.text


def test_titles_with_the_tba_id_are_read_by_tbas_playlist_page():
    """guess_partial is TBA's own guess (youtube_video_helper.guessMatchPartial); the default titles give it nothing,
    TBA_TITLE_FORMAT gives it the right match for every level, and a title cut to YouTube's limit keeps the id."""
    from streamsplitter.publish import templates, youtube

    def m(part, name=None):
        return Match(key=f"2026mttd_{part}", name=name or f"2026mttd_{part}", event_key="2026mttd",
                     event_name="MadTown Throwdown", year=2026)
    assert [tbalink.guess_partial(t) for t in ("Qualification 12 - 2026 MadTown Throwdown", "Match 7 (R2)", None,
                                               "QM 12", "Q12 MadTown", "quals qm3", "MTTD SF7M1", "Finals f1m2")] \
        == ["", "", "", "", "qm12", "qm3", "sf7m1", "f1m2"]
    for part in ("qm1", "qm72", "sf1m1", "sf13m1", "f1m1", "f1m3"):
        title = templates.render(templates.TBA_TITLE_FORMAT, m(part))
        assert title.endswith(f" ({part})") and tbalink.guess_partial(title) == part
        assert tbalink.guess_partial(templates.render(templates.TITLE_FORMAT, m(part))) == ""
    assert templates.render(templates.TBA_TITLE_FORMAT, m("qm12")) == "Qualification 12 - 2026 MadTown Throwdown (qm12)"
    # no id on what TBA has no match page for: practice, an unnamed clip, a stopped attempt
    for odd in (m("p3"), m("qm4", "2026mttd_qm4_partial"),
                Match(key=None, name="t00000900.0", event_key="2026mttd", event_name="MadTown Throwdown", year=2026)):
        assert "(" not in templates.render("{event_name}" + templates.TBA_TITLE_SUFFIX, odd)
    long = templates.render(templates.TBA_TITLE_FORMAT, Match(
        key="2026mttd_sf12m1", name="2026mttd_sf12m1", event_key="2026mttd", year=2026,
        event_name="The Longest Named Offseason Robotics Competition Anyone Has Ever Held Presented By Many Sponsors"))
    cut = youtube.clean_title(long)
    assert len(long) > 100 and len(cut) <= 100 and cut.endswith(" (sf12m1)") and tbalink.guess_partial(cut) == "sf12m1"
    assert tbalink.suggest_url("2026mttd") == "https://www.thebluealliance.com/suggest/event/video?event_key=2026mttd"


def test_setup_asks_nothing_for_private_videos_or_youtube_off(env):
    set_up_youtube("private", tba_links=True)
    io = ScriptedIO([])
    Wizard(io, FakeServices()).run("tbalinks")
    assert "would not play" in io.text and config.section("youtube")["tba_links"] is False
    config.save_sections({"youtube": {"enabled": False}})
    io = ScriptedIO([])
    Wizard(io, FakeServices()).run("tbalinks")
    assert "nothing to link" in io.text


def test_setup_checks_the_key_and_keeps_it_only_when_it_works(env, monkeypatch):
    set_up_youtube()
    answers = {"secret": "refused"}
    monkeypatch.setattr(tbalink.TrustedClient, "check",
                        lambda self, ev: None if self._secret == "good-secret" else answers["secret"])
    io = ScriptedIO([("Add the videos", "y"), ("Auth ID", "org-key-1"), ("Auth Secret", "bad-secret")])
    Wizard(io, FakeServices()).run("tbalinks")
    assert "did not work for 2026mttd: refused" in io.text and io.opened == []
    assert config.section("youtube")["tba_links"] is False and config.get_secret("tba_write_secret") is None
    assert tbalink.REQUEST_URL in io.text

    io = ScriptedIO([("Add the videos", "y"), ("Auth ID", "org-key-1"), ("Auth Secret", "good-secret")])
    Wizard(io, FakeServices()).run("tbalinks")
    assert config.section("youtube")["tba_links"] is True
    assert config.get_secret("tba_write_id") == "org-key-1" and config.get_secret("tba_write_secret") == "good-secret"
    assert "good-secret" not in io.text and "good-secret" not in (env / "config.toml").read_text(encoding="utf-8")

    io = ScriptedIO([("Add the videos", ""), ("saved on this computer", "")])   # on now: Enter keeps it
    Wizard(io, FakeServices()).run("tbalinks")
    assert config.section("youtube")["tba_links"] is True
