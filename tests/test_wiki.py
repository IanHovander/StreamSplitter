"""The team wiki (streamsplitter/publish/wiki.py) against a MediaWiki in memory (tests/fake_mediawiki.py): bot login,
chunked upload in pieces, resuming from a stored filekey, idempotent pages and sections, the error mapping; and the
wiki's place in routing, the Publisher, build_publishing and the pause/resume commands. No network.

    .venv\\Scripts\\python.exe -m pytest -q tests\\test_wiki.py
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from fake_mediawiki import FakeMediaWiki, Resp  # noqa: E402
from streamsplitter import config, live  # noqa: E402
from streamsplitter.publish import commands, notify  # noqa: E402
from streamsplitter.publish import wiki as wiki_mod  # noqa: E402
from streamsplitter.publish.base import Job, Match, PermanentError, TransientError  # noqa: E402
from streamsplitter.publish.outbox import Outbox  # noqa: E402
from streamsplitter.publish.publisher import Publisher  # noqa: E402
from streamsplitter.publish.router import route  # noqa: E402
from streamsplitter.publish.wiki import WikiClient, WikiDestination  # noqa: E402

API = "https://wiki.example.org/api.php"
USER, PASSWORD = "Ian@StreamSplitter", "goodpassword"
PAGE = "Madtown Throwdown (2026) match videos"
NAME = "2026mttd qm12.mp4"                 # as the wiki keys it (spaces)


def client(wiki, **kw) -> WikiClient:
    kw.setdefault("chunk_size", 1000)
    kw.setdefault("min_interval", 0)
    return WikiClient(API, kw.pop("user", USER), kw.pop("password", PASSWORD), session=wiki,
                      sleep=lambda s: None, **kw)


@pytest.fixture
def clip(tmp_path) -> Path:
    p = tmp_path / "2026mttd_qm12.mp4"
    p.write_bytes(os.urandom(2500))         # three pieces of 1000 bytes
    return p


def qm(n: int = 12, name: str | None = None, **kw) -> Match:
    return Match(**{"key": f"2026mttd_qm{n}", "name": name or f"2026mttd_qm{n}", "event_key": "2026mttd",
                    "event_name": "Madtown Throwdown", "year": 2026, "comp_level": "qm", "set_number": 1,
                    "match_number": n, "red": ["1323", "6619", "254B"], "blue": ["5026", "5499", "114"],
                    "red_score": 81, "blue_score": 63, **kw})


def job(path, match=None, kind="score") -> Job:
    m = match or qm()
    return Job(id=f"{m.name}@10.0:{kind}:wiki", destination="wiki", kind=kind, path=str(path), match=m)


def offsets(wiki) -> list[int]:
    return [int(p["offset"]) for p in wiki.kinds("chunk")]


# ---------------------------------------------------------------------------------------------------------------------
# Login

def test_login_with_the_bot_password_and_a_csrf_token():
    wiki = FakeMediaWiki()
    c = client(wiki)
    assert c.login() == {"user": "Ian"}
    assert c._token() == wiki.csrf and wiki.csrf != "+\\"
    assert wiki.kinds()[:2] == ["query:tokens", "login"]
    login = wiki.kinds("login")[0]
    assert login["lgname"] == USER and login["lgtoken"] == wiki.login_token
    assert PASSWORD not in repr(c)


def test_refused_logins_say_what_to_do_and_never_show_the_password():
    wiki = FakeMediaWiki()
    with pytest.raises(PermanentError) as e:
        client(wiki, password="wrong-password-123").login()
    assert "Special:BotPasswords" in str(e.value) and "wrong-password-123" not in str(e.value)
    with pytest.raises(PermanentError) as e:
        client(wiki, user="Ian", password="mainpassword").login()          # the account's own password
    assert "bot password, not the account's own password" in str(e.value)
    wiki.fail("login", Resp(200, {"login": {"result": "Failed", "reason": "You have made too many recent login "
                                                                          "attempts. Please wait 5 minutes."}}))
    with pytest.raises(TransientError) as e:
        client(wiki).login()
    assert e.value.retry_after == 300
    with pytest.raises(PermanentError) as e:
        WikiClient(API, USER, None, session=wiki).login()
    assert "setup --section wiki" in str(e.value)


# ---------------------------------------------------------------------------------------------------------------------
# Chunked, resumable upload

def test_chunked_upload_in_pieces_then_published(clip):
    wiki = FakeMediaWiki()
    c = client(wiki)
    state, saved, seen = {}, [], []
    r = c.upload(str(clip), "2026mttd_qm12.mp4", "StreamSplitter: Qualification 12", "{{Private}}\nQual 12", state,
                 on_state=lambda: saved.append(dict(state)), progress=lambda sent, total: seen.append((sent, total)))
    assert r == {"file": "2026mttd_qm12.mp4", "url": "https://wiki.example.org/images/2026mttd_qm12.mp4",
                 "page_url": "https://wiki.example.org/index.php/File:2026mttd_qm12.mp4", "existed": False}
    assert wiki.file(NAME)["data"] == clip.read_bytes() and wiki.file(NAME)["user"] == "Ian"
    chunks = wiki.kinds("chunk")
    assert offsets(wiki) == [0, 1000, 2000] and [p["chunk_len"] for p in chunks] == [1000, 1000, 500]
    assert all(p["stash"] == "1" and p["filesize"] == "2500" and p["ignorewarnings"] == "1" for p in chunks)
    assert "filekey" not in chunks[0] and chunks[1]["filekey"] == chunks[2]["filekey"]
    publish = wiki.kinds("publish")
    assert len(publish) == 1 and "ignorewarnings" not in publish[0]       # the wiki's warnings are read
    assert publish[0]["text"] == "{{Private}}\nQual 12" and wiki.pages["File:" + NAME].startswith("{{Private}}")
    assert all(p.get("assert") == "user" and p.get("token") for k, p in wiki.calls if k in ("chunk", "publish"))
    assert seen == [(1000, 2500), (2000, 2500), (2500, 2500)]
    assert any(s.get("filekey") and s.get("offset") == 1000 for s in saved)  # saved after each piece
    assert state["uploaded"] and state["file"] == "2026mttd_qm12.mp4" and "filekey" not in state
    n = len(wiki.calls)
    assert c.upload(str(clip), "2026mttd_qm12.mp4", "c", "t", state) == {**r, "existed": False}
    assert len(wiki.calls) == n                                           # done: nothing sent again


def test_restart_resumes_from_the_stored_filekey_and_offset(clip):
    wiki = FakeMediaWiki()
    wiki.fail("chunk", "drop", skip=1)
    state = {}
    with pytest.raises(TransientError):
        client(wiki).upload(str(clip), "2026mttd_qm12.mp4", "c", "t", state)
    assert state["offset"] == 1000 and state["filekey"]
    stored = json.loads(json.dumps(state))                                # as the outbox keeps it on disk
    wiki.calls.clear()
    r = client(wiki).upload(str(clip), "2026mttd_qm12.mp4", "c", "t", stored)    # a new process
    assert r["file"] == "2026mttd_qm12.mp4" and wiki.file(NAME)["data"] == clip.read_bytes()
    assert wiki.kinds()[:5] == ["query:tokens", "login", "query:tokens", "checkstatus", "chunk"]
    assert offsets(wiki) == [1000, 2000]                                  # only what was missing


def test_a_lost_answer_to_a_piece_goes_on_from_the_wikis_offset(clip):
    wiki = FakeMediaWiki()
    wiki.fail("chunk", "lose", skip=1)                                    # the wiki has bytes 0-1999; we heard 999
    state = {}
    c = client(wiki)
    with pytest.raises(TransientError):
        c.upload(str(clip), "2026mttd_qm12.mp4", "c", "t", state)
    assert state["offset"] == 1000
    wiki.calls.clear()
    c.upload(str(clip), "2026mttd_qm12.mp4", "c", "t", state)
    assert offsets(wiki) == [2000] and wiki.file(NAME)["data"] == clip.read_bytes()


def test_an_offset_mismatch_takes_the_wikis_offset(clip):
    wiki = FakeMediaWiki()
    wiki.fail("chunk", "drop", skip=2)
    state = {}
    c = client(wiki)
    with pytest.raises(TransientError):
        c.upload(str(clip), "2026mttd_qm12.mp4", "c", "t", state)
    wiki.status[state["filekey"]]["offset"] = 1000                        # a stale answer: it really has 2000
    wiki.calls.clear()
    c.upload(str(clip), "2026mttd_qm12.mp4", "c", "t", state)
    assert offsets(wiki) == [1000, 2000] and wiki.file(NAME)["data"] == clip.read_bytes()


def test_an_expired_filekey_starts_over(clip):
    wiki = FakeMediaWiki()
    wiki.fail("chunk", "drop", skip=1)
    state = {}
    c = client(wiki)
    with pytest.raises(TransientError):
        c.upload(str(clip), "2026mttd_qm12.mp4", "c", "t", state)
    wiki.stash.clear()                                                    # hours later: the wiki forgot the stash
    wiki.status.clear()
    wiki.calls.clear()
    c.upload(str(clip), "2026mttd_qm12.mp4", "c", "t", state)
    assert offsets(wiki) == [0, 1000, 2000] and wiki.file(NAME)["data"] == clip.read_bytes()
    # a continuing piece the wiki no longer knows (stashfailed without an offset) also starts over, once
    wiki2 = FakeMediaWiki()
    state = {}
    c2 = client(wiki2)
    wiki2.fail("chunk", "drop", skip=1)
    with pytest.raises(TransientError):
        c2.upload(str(clip), "2026mttd_qm12.mp4", "c", "t", state)
    wiki2.status.clear()                                                  # checkstatus has nothing ...
    key = state["filekey"]
    wiki2.status[key] = {"result": "Continue", "offset": 1000}            # ... no: it says continue, then
    del wiki2.stash[key]                                                  # the piece finds no stash
    wiki2.calls.clear()
    c2.upload(str(clip), "2026mttd_qm12.mp4", "c", "t", state)
    assert offsets(wiki2) == [1000, 0, 1000, 2000] and wiki2.file(NAME)["data"] == clip.read_bytes()


def test_a_lost_answer_to_the_last_piece_finds_the_assembled_file(clip):
    """The wiki gives the assembled file a new filekey and forgets the old one: list=mystashedfiles finds it by size
    and SHA-1, so the clip is not sent again."""
    wiki = FakeMediaWiki()
    wiki.fail("chunk", "lose", skip=2)
    state = {}
    c = client(wiki)
    with pytest.raises(TransientError):
        c.upload(str(clip), "2026mttd_qm12.mp4", "c", "t", state)
    wiki.calls.clear()
    c.upload(str(clip), "2026mttd_qm12.mp4", "c", "t", state)
    assert offsets(wiki) == [] and "query:mystashedfiles" in wiki.kinds()
    assert wiki.file(NAME)["data"] == clip.read_bytes()


def test_a_lost_answer_to_publishing_is_found_by_its_sha1(clip):
    wiki = FakeMediaWiki()
    wiki.fail("publish", "lose")
    state = {}
    c = client(wiki)
    with pytest.raises(TransientError):
        c.upload(str(clip), "2026mttd_qm12.mp4", "c", "t", state)
    assert state["stage"] == "assembled"
    wiki.calls.clear()
    r = c.upload(str(clip), "2026mttd_qm12.mp4", "c", "t", state)
    assert r["existed"] and wiki.kinds("publish") == [] and wiki.kinds("chunk") == []


def test_the_same_file_already_on_the_wiki_counts_as_done(clip):
    wiki = FakeMediaWiki()
    wiki.add_file("2026mttd_qm12.mp4", clip.read_bytes(), user="Mentor")
    r = client(wiki).upload(str(clip), "2026mttd_qm12.mp4", "c", "t", {})
    assert r["existed"] and r["file"] == "2026mttd_qm12.mp4" and wiki.kinds("chunk") == []
    # the same video under another name: no second copy; the section will show that file
    wiki = FakeMediaWiki()
    wiki.add_file("Qual 12 MadTown.mp4", clip.read_bytes(), user="Mentor")
    state = {}
    r = client(wiki).upload(str(clip), "2026mttd_qm12.mp4", "c", "t", state)
    assert r["file"] == "Qual_12_MadTown.mp4" and r["existed"] and wiki.file(NAME) is None
    assert state["file"] == "Qual_12_MadTown.mp4"


def test_a_persons_different_file_is_never_overwritten_but_our_own_gets_a_new_version(clip):
    wiki = FakeMediaWiki()
    wiki.add_file("2026mttd_qm12.mp4", b"someone else's video", user="Mentor")
    with pytest.raises(PermanentError) as e:
        client(wiki).upload(str(clip), "2026mttd_qm12.mp4", "c", "t", {})
    assert "uploaded by Mentor" in str(e.value) and "never overwrites" in str(e.value)
    assert wiki.file(NAME)["data"] == b"someone else's video"
    wiki = FakeMediaWiki()
    wiki.add_file("2026mttd_qm12.mp4", b"our earlier cut", user="Ian")
    r = client(wiki).upload(str(clip), "2026mttd_qm12.mp4", "c", "t", {})
    assert wiki.file(NAME)["data"] == clip.read_bytes() and not r["existed"]
    assert [("ignorewarnings" in p) for p in wiki.kinds("publish")] == [False, True]


def test_harmless_warnings_are_passed(clip):
    wiki = FakeMediaWiki()
    wiki.deleted.add(NAME)                                                # a file of that name was deleted once
    client(wiki).upload(str(clip), "2026mttd_qm12.mp4", "c", "t", {})
    assert wiki.file(NAME)["data"] == clip.read_bytes()


def test_a_piece_too_large_for_the_web_server_is_halved(clip, monkeypatch):
    monkeypatch.setattr(wiki_mod, "MIN_CHUNK", 250)
    wiki = FakeMediaWiki()
    wiki.max_body = 600                                                   # PHP's upload_max_filesize, say
    state = {}
    client(wiki).upload(str(clip), "2026mttd_qm12.mp4", "c", "t", state)
    assert wiki.file(NAME)["data"] == clip.read_bytes() and state["chunk_size"] == 500
    sent = [p["chunk_len"] for p in wiki.kinds("chunk")]
    assert sent[0] == 1000 and all(n <= 500 for n in sent[1:])
    wiki = FakeMediaWiki()
    wiki.max_body = 100
    with pytest.raises(PermanentError) as e:
        client(wiki).upload(str(clip), "2026mttd_qm12.mp4", "c", "t", {})
    assert "$wgMaxUploadSize" in str(e.value) and "upload_max_filesize" in str(e.value)


# ---------------------------------------------------------------------------------------------------------------------
# The destination: page once, a section per match, members-only

def test_the_event_page_is_made_once_with_a_section_per_match(tmp_path):
    wiki = FakeMediaWiki()
    dest = WikiDestination(client(wiki), page_title=PAGE, page_event="2026mttd")
    saved = []
    dest.on_state = lambda j: saved.append(dict(j.state))
    for n in (12, 13):
        p = tmp_path / f"2026mttd_qm{n}.mp4"
        p.write_bytes(os.urandom(1500))
        r = dest.send(job(p, qm(n, blue_score=None) if n == 13 else qm(n)))
    assert r["url"] == "https://wiki.example.org/index.php/Madtown_Throwdown_(2026)_match_videos" and r["page"] == PAGE
    text = wiki.pages[PAGE]
    assert text.startswith("{{Private}}\nMatch videos from the 2026 Madtown Throwdown")
    assert text.count("== Qualification 12 ==") == 1 and text.count("== Qualification 13 ==") == 1
    assert text.index("Qualification 12") < text.index("Qualification 13")
    assert "[[File:2026mttd_qm12.mp4|800px]]\n\nRed 1323, 6619, 254B - 81 | Blue 5026, 5499, 114 - 63" in text
    assert "Red 1323, 6619, 254B | Blue 5026, 5499, 114" in text          # qm13: no score yet, numbers only
    assert len(wiki.kinds("create")) == 1                                 # the page is made once
    section = wiki.kinds("section")[0]
    assert section["sectiontitle"] == "Qualification 12" and section["nocreate"] == "1"
    file_page = wiki.pages["File:2026mttd qm12.mp4"]
    assert file_page.startswith("{{Private}}") and "Qualification 12, 2026 Madtown Throwdown" in file_page
    assert f"[[{PAGE}]]" in file_page
    assert saved[-1]["section_done"] and saved[-1]["section_rev"]


def test_a_page_someone_made_first_is_kept_and_its_missing_private_markup_reported(tmp_path, clip):
    wiki = FakeMediaWiki()
    wiki.pages[PAGE] = "Our notes from MadTown.\n"
    warned = []
    dest = WikiDestination(client(wiki), page_title=PAGE, warn=warned.append)
    dest.send(job(clip))
    assert wiki.pages[PAGE].startswith("Our notes from MadTown.\n\n== Qualification 12 ==")
    assert len(warned) == 1 and "{{Private}}" in warned[0] and PAGE in warned[0]
    dest.send(job(clip, qm(12, name="2026mttd_qm12_r2")))                 # warned once per run
    assert len(warned) == 1


def test_a_retry_after_a_lost_answer_adds_no_second_section(clip):
    wiki = FakeMediaWiki()
    wiki.fail("section", "lose")
    dest = WikiDestination(client(wiki), page_title=PAGE)
    j = job(clip)
    with pytest.raises(TransientError):
        dest.send(j)
    assert not j.state.get("section_done")
    stored = Job.from_dict(json.loads(json.dumps(j.to_dict())))
    WikiDestination(client(wiki), page_title=PAGE).send(stored)           # after a restart
    assert wiki.pages[PAGE].count("== Qualification 12 ==") == 1 and len(wiki.kinds("section")) == 1
    assert stored.state["section_done"] and len(wiki.kinds("chunk")) == 3   # not uploaded again either


def test_not_private_and_a_page_deleted_meanwhile(tmp_path, clip):
    wiki = FakeMediaWiki()
    dest = WikiDestination(client(wiki), page_title=PAGE, private=False)
    dest.send(job(clip))
    assert "{{Private}}" not in wiki.pages[PAGE] and "{{Private}}" not in wiki.pages["File:" + NAME]
    wiki = FakeMediaWiki()
    dest = WikiDestination(client(wiki), page_title=PAGE)
    dest.send(job(clip))
    del wiki.pages[PAGE]                                                  # a person deleted the page
    p = tmp_path / "qm13.mp4"
    p.write_bytes(os.urandom(800))
    dest.send(job(p, qm(13)))
    assert wiki.pages[PAGE].startswith("{{Private}}") and "== Qualification 13 ==" in wiki.pages[PAGE]


def test_file_names_section_titles_and_formats(tmp_path):
    d = WikiDestination(client(FakeMediaWiki()), page_format="{year} {event_name} videos",
                        embed_format="<video>File:{file}</video>")
    cases = [(qm(12, name="2026mttd_qm12_r2"), "2026mttd_qm12_r2.mp4", "Qualification 12 (Replay)"),
             (qm(12, name="2026mttd_qm12_partial"), "2026mttd_qm12_partial.mp4", "Qualification 12 (Partial)"),
             (Match(key="2026mttd_sf7m1", name="2026mttd_sf7m1", event_key="2026mttd", year=2026, comp_level="sf",
                    set_number=7, match_number=1), "2026mttd_sf7m1.mp4", "Match 7 (R2)"),
             (Match(key="2026mttd_f1m2", name="2026mttd_f1m2", event_key="2026mttd", year=2026, comp_level="f",
                    set_number=1, match_number=2), "2026mttd_f1m2.mp4", "Final 2"),
             (Match(key=None, name="2026mttd_p3", event_key="2026mttd", year=2026), "2026mttd_p3.mp4", "Practice 3"),
             (Match(key=None, name="match 0123", event_key="2026mttd"), "2026mttd_match_0123.mp4", "match 0123")]
    for m, file, heading in cases:
        j = job(tmp_path / "x.mp4", m)
        assert (d.file_name(j), d.heading(j)) == (file, heading)
    j = job(tmp_path / "x.mp4", qm(12, event_name="MadTown Throwdown"))
    assert d.page_for(j) == "2026 MadTown Throwdown videos"
    assert d.section_text(j, "2026mttd_qm12.mp4", "Qualification 12").startswith(
        "<video>File:2026mttd_qm12.mp4</video>\n\nRed 1323")
    # a page title confirmed for another event is not used for this one
    d2 = WikiDestination(client(FakeMediaWiki()), page_title="Old Event (2025) match videos", page_event="2025mttd")
    assert d2.page_for(j) == "MadTown Throwdown (2026) match videos"
    with pytest.raises(PermanentError) as e:
        WikiDestination(client(FakeMediaWiki()), embed_format="[[File:x.mp4]]")
    assert "{file}" in str(e.value)
    assert wiki_mod.render_page_title(wiki_mod.PAGE_FORMAT, None, None, "2026mttd") == "2026mttd match videos"
    assert wiki_mod.title_problem("A [bad] title") and wiki_mod.title_problem("File:x") and not wiki_mod.title_problem(PAGE)


def test_event_page_candidates_and_api_addresses():
    titles = ["Madtown Throwdown (2025)", "Madtown Throwdown (2026)", "Madtown Throwdown (2026) match videos",
              "Chezy Champs (2026)", "Madtown Throwdown", "File:Madtown.jpg"]
    assert wiki_mod.event_page_candidates(titles, "MadTown Throwdown", 2026) == ["Madtown Throwdown (2026)",
                                                                                "Madtown Throwdown"]
    assert wiki_mod.wiki_event_name("Madtown Throwdown (2026)") == "Madtown Throwdown"
    assert wiki_mod.api_candidates("team5026.com") == ["https://team5026.com/api.php", "https://team5026.com/w/api.php"]
    assert wiki_mod.api_candidates("https://www.team5026.com/index.php/Main_Page")[0] == \
        "https://www.team5026.com/api.php"
    assert wiki_mod.api_candidates("https://wiki.example.org/w/api.php")[0] == "https://wiki.example.org/w/api.php"


# ---------------------------------------------------------------------------------------------------------------------
# Errors

def test_badtoken_logs_in_again_once():
    wiki = FakeMediaWiki()
    c = client(wiki)
    c.login()
    c._token()
    wiki.csrf = "rotated+\\"                                              # the session lapsed on the wiki's side
    assert c.create_page("P", "{{Private}}\nhi", "s") is True
    assert len(wiki.kinds("login")) == 2 and wiki.pages["P"].startswith("{{Private}}")
    wiki.fail("create", {"code": "badtoken", "info": "Invalid CSRF token."}, times=2)
    with pytest.raises(TransientError):
        c.create_page("Q", "x", "s")                                      # still after logging in again: later


def test_error_mapping():
    wiki = FakeMediaWiki()
    c = client(wiki)
    wiki.fail("create", {"code": "ratelimited", "info": "You've exceeded your rate limit."})
    with pytest.raises(TransientError) as e:
        c.create_page("P", "x", "s")
    assert e.value.retry_after == 60
    wiki.fail("create", Resp(200, {"error": {"code": "maxlag", "info": "Waiting for a database server"}},
                             headers={"Retry-After": "7"}))
    with pytest.raises(TransientError) as e:
        c.create_page("P", "x", "s")
    assert e.value.retry_after == 7
    wiki.fail("create", Resp(429, None, headers={"Retry-After": "30"}))
    with pytest.raises(TransientError) as e:
        c.create_page("P", "x", "s")
    assert e.value.retry_after == 30
    for effect in ("drop", Resp(502, None), Resp(200, None)):             # offline, Cloudflare 502, a Wi-Fi page
        wiki.fail("create", effect)
        with pytest.raises(TransientError):
            c.create_page("P", "x", "s")
    wiki.fail("create", Resp(403, None, headers={"cf-mitigated": "challenge"}))
    with pytest.raises(PermanentError) as e:
        c.create_page("P", "x", "s")
    assert "Cloudflare" in str(e.value) and "api.php" in str(e.value)
    for code, words in (("assertbotfailed", "bot group"), ("uploaddisabled", "$wgEnableUploads"),
                        ("filetype-banned", "$wgFileExtensions"), ("blocked", "blocked"),
                        ("protectedpage", "protected"), ("somethingnew", "somethingnew")):
        wiki.fail("create", {"code": code, "info": "the wiki's words"})
        with pytest.raises(PermanentError) as e:
            c.create_page("P", "x", "s")
        assert words in str(e.value), code
    assert c.create_page("P", "x", "s") is True and c.create_page("P", "y", "s") is False   # articleexists: fine


def test_missing_grants_are_permanent_with_the_fix(clip):
    wiki = FakeMediaWiki(rights={"read", "edit", "createpage"})
    with pytest.raises(PermanentError) as e:
        client(wiki).upload(str(clip), "2026mttd_qm12.mp4", "c", "t", {})
    assert "Special:BotPasswords" in str(e.value) and "Upload new files" in str(e.value)
    c = client(wiki)
    assert wiki_mod.missing_rights(c.user_info()["rights"]) == ["Upload new files"]


def test_requests_are_paced():
    """Cloudflare's free plan allows 300 requests per 10 s per address: requests are MIN_INTERVAL_S apart."""
    wiki = FakeMediaWiki()
    t = [100.0]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        t[0] += s
    c = WikiClient(API, USER, PASSWORD, session=wiki, clock=lambda: t[0], sleep=sleep)
    c.page_text("Nothing")
    n = len(wiki.calls)
    assert n == 4 and len(sleeps) == n - 1 and all(abs(s - wiki_mod.MIN_INTERVAL_S) < 1e-9 for s in sleeps)
    assert n / (n * wiki_mod.MIN_INTERVAL_S) <= 300 / 10


# ---------------------------------------------------------------------------------------------------------------------
# Routing, the Publisher, the live run, the commands

SLACK = {"enabled": True, "teams": ["5026"]}
YT = {"enabled": True, "route": "all"}
WIKI = {"enabled": True}


def test_routing_sends_every_match_to_the_wiki():
    assert route({"5026", "254"}, SLACK, YT, WIKI) == ["slack", "youtube", "wiki"]
    assert route({"100"}, SLACK, YT, WIKI) == ["youtube", "wiki"]
    assert route({"100"}, SLACK, {"enabled": False}, WIKI) == ["wiki"]            # independent of YouTube
    assert route({"5026"}, SLACK, {**YT, "route": "others"}, WIKI) == ["slack", "wiki"]
    assert route(set(), {"enabled": False}, {"enabled": False}, WIKI) == ["wiki"]
    assert route({"100"}, SLACK, YT, {"enabled": False}) == ["youtube"] == route({"100"}, SLACK, YT)


class Dest:
    def __init__(self, name):
        self.name, self.sent = name, []

    def send(self, job):
        self.sent.append(job)
        return {"url": f"https://example/{job.id}"}


def row(n, tmp_path, partial=False, score=True, teams=("5026",)):
    clip = tmp_path / f"2026mttd_qm{n}.mp4"
    clip.write_bytes(b"clip")
    r = {"name": f"2026mttd_qm{n}", "naming": {"name": f"2026mttd_qm{n}", "key": f"2026mttd_qm{n}",
                                               "teams_read": list(teams)},
         "start": 10.0 * n, "end": 10.0 * n + 150, "partial": partial, "clip": {"path": str(clip)}}
    if score:
        s = tmp_path / f"2026mttd_qm{n}_score.mp4"
        s.write_bytes(b"score")
        r["score_clip"] = {"path": str(s)}
    return r


def test_the_publisher_queues_every_match_for_the_wiki(tmp_path):
    dests = {d: Dest(d) for d in ("slack", "wiki")}
    box = Outbox(tmp_path / "o.sqlite", dests, notify.Notifier(log=lambda s: None), stage=False)
    official = {"key": "2026casj", "name": "Silicon Valley Regional", "year": 2026, "event_type": 0}
    pub = Publisher(box, cfg_event=official, cfg_slack=SLACK, cfg_youtube={"enabled": False}, cfg_wiki=WIKI,
                    tba_get=lambda path: {})
    own, other, part = row(1, tmp_path), row(2, tmp_path, teams=("100",)), row(3, tmp_path, partial=True, score=False)
    for r in (own, other, part):
        pub.on_clip(r)
    pub.on_score(own)
    pub.on_score(other)
    ids = [j["id"] for j in box.jobs("wiki")]
    assert ids == ["2026mttd_qm3@30.0:clip:wiki", "2026mttd_qm1@10.0:score:wiki", "2026mttd_qm2@20.0:score:wiki"]
    assert box.get("2026mttd_qm1@10.0:score:wiki")["job"].path.endswith("_score.mp4")
    assert [j["id"] for j in box.jobs("slack")][0] == "2026mttd_qm1@10.0:clip:slack"
    assert box.hooks["wiki"] is not None
    pub2 = Publisher(box, cfg_slack=SLACK, cfg_wiki={**WIKI, "version": "match"}, tba_get=lambda path: {})
    pub2.on_score(row(4, tmp_path, teams=("100",)))
    assert box.get("2026mttd_qm4@40.0:clip:wiki")["job"].path.endswith("qm4.mp4")
    box.close()


def test_the_wiki_hook_fills_in_tbas_score(tmp_path):
    box = Outbox(tmp_path / "o.sqlite", {"wiki": Dest("wiki")}, notify.Notifier(log=lambda s: None), stage=False)
    tba = {"/match/2026mttd_qm12": {"alliances": {"red": {"score": 81, "team_keys": ["frc1323"]},
                                                  "blue": {"score": 63, "team_keys": ["frc5026"]}}}}
    Publisher(box, cfg_wiki=WIKI, tba_get=lambda path: tba[path])
    j = Job(id="x:score:wiki", destination="wiki", kind="score", path="x.mp4",
            match=Match(key="2026mttd_qm12", name="2026mttd_qm12", event_key="2026mttd"))
    box.hooks["wiki"](j)
    assert (j.match.red, j.match.red_score, j.match.blue_score) == (["1323"], 81, 63)
    assert wiki_mod.teams_line(j.match) == "Red 1323 - 81 | Blue 5026 - 63"
    box.close()


@pytest.fixture
def roots(tmp_path, monkeypatch):
    monkeypatch.setenv("STREAMSPLITTER_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("STREAMSPLITTER_CONFIG", str(tmp_path / "config.toml"))
    return tmp_path


def test_build_publishing_without_the_bot_password_says_how_to_fix_it(roots, monkeypatch):
    sections = {"event": {"key": "2026mttd"}, "wiki": {"enabled": True, "api_url": API, "username": USER}}
    monkeypatch.setattr(config, "section", lambda name: dict(sections.get(name, {})))
    monkeypatch.setattr(config, "get_secret", lambda name: None)
    lines = []
    notifier, outbox, publisher, hb_s, summary = live.build_publishing("2026mttd", log=lambda s, **_: lines.append(s))
    try:
        assert "wiki" not in outbox.destinations and publisher is not None
        assert "streamsplitter setup --section wiki" in "\n".join(lines)
    finally:
        outbox.close()


def test_build_publishing_wires_the_wiki(roots, monkeypatch):
    sections = {"event": {"key": "2026mttd", "name": "MadTown Throwdown", "year": 2026},
                "wiki": {"enabled": True, "api_url": API, "username": USER, "page_title": PAGE,
                         "page_event": "2026mttd", "embed_format": "<video>File:{file}</video>", "paused": True},
                "youtube": {"enabled": False}}
    monkeypatch.setattr(config, "section", lambda name: dict(sections.get(name, {})))
    monkeypatch.setattr(config, "get_secret", lambda name: PASSWORD if name == "wiki_bot_password" else None)
    notifier, outbox, publisher, hb_s, summary = live.build_publishing("2026mttd", log=lambda s, **_: None)
    try:
        d = outbox.destinations["wiki"]
        assert isinstance(d, WikiDestination) and d.on_state == outbox.save_state
        assert d.page_title == PAGE and d.embed_format == "<video>File:{file}</video>" and d.private
        assert outbox.paused == {"wiki"} and PAGE in summary and "resume wiki" in summary
        assert publisher.cfg_wiki["enabled"] and outbox.hooks["wiki"] == publisher._before_wiki
    finally:
        outbox.close()
    sections["event"]["key"] = "2026cafr"                     # the event changed; the wiki step was not run again
    lines = []
    notifier, outbox, publisher, hb_s, summary = live.build_publishing("2026cafr", log=lambda s, **_: lines.append(s))
    try:
        assert outbox.destinations["wiki"].page_title is None
        assert "is for 2026mttd" in "\n".join(lines)
    finally:
        outbox.close()


def test_pause_and_resume_wiki_from_slack(roots):
    w = Dest("wiki")
    box = live.PausableOutbox(roots / "data" / "outbox" / "p.sqlite", {"wiki": w}, notify.Notifier(log=lambda s: None),
                              stage=False)
    ctl = live.LiveControls(None, None, publisher=types.SimpleNamespace(outbox=box))
    assert commands.parse("pause wiki").arg == "wiki" and commands.parse("Resume the wiki").name == "resume"
    assert "pause wiki" in commands.help_text(wiki=True) and "pause wiki" not in commands.help_text()
    assert "pause wiki" in commands.dispatch(commands.parse("help"), ctl)                  # the wiki is set up here
    reply = commands.dispatch(commands.parse("pause wiki"), ctl)
    assert reply.startswith("OK: Wiki uploads are paused") and config.section("wiki")["paused"] is True
    box.add(Job(id="qm1:score:wiki", destination="wiki", kind="score", path="x.mp4", match=qm(1)))
    assert box.step("wiki") == live.IDLE_S and w.sent == []
    assert commands.dispatch(commands.parse("pause youtube"), ctl).startswith("YouTube uploads are not set up")
    reply = commands.dispatch(commands.parse("resume wiki"), ctl)
    assert reply == "OK: Wiki uploads resumed; 1 waiting go out now, in order." and "paused" not in config.section("wiki")
    box.step("wiki")
    assert [j.id for j in w.sent] == ["qm1:score:wiki"]
    box.close()
