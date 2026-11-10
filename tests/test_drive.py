"""Google Drive client and destination against a fake Drive (tests/fake_drive.py; no network, no Google): file names
and play-order codes, resumable upload in pieces, resume after a drop or a restart, expired sessions, duplicate
avoidance through appProperties, the event folder, the smaller copy, a full Drive (paused, one DM), error mapping, a
late score's description update; and Drive's place in routing, the Publisher, build_publishing, the commands and
status.

    .venv\\Scripts\\python.exe -m pytest -q -p no:cacheprovider tests\\test_drive.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_drive import FakeDrive, Resp, fail, gerr  # noqa: E402
from streamsplitter import config, live  # noqa: E402
from streamsplitter.publish import commands, notify, templates  # noqa: E402
from streamsplitter.publish import drive as dr  # noqa: E402
from streamsplitter.publish.base import Job, Match, PermanentError, TransientError  # noqa: E402
from streamsplitter.publish.outbox import Outbox  # noqa: E402
from streamsplitter.publish.publisher import Publisher  # noqa: E402
from streamsplitter.publish.router import route  # noqa: E402

KIB = 1024
PIECE = 256 * KIB
CLIENT = {"installed": {"client_id": "cid.apps.googleusercontent.com", "client_secret": "client-secret-x",
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": dr.TOKEN_URI,
                        "redirect_uris": ["http://localhost"]}}
SECRETS = ("tok0", "refresh-secret-x", "client-secret-x")
EVENT = {"key": "2026mttd", "name": "MadTown Throwdown", "year": 2026}


def token_json(token="tok0", expires_in=3600) -> str:
    expiry = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return json.dumps({"token": token, "refresh_token": "refresh-secret-x", "client_id": CLIENT["installed"]["client_id"],
                       "client_secret": "client-secret-x", "scopes": dr.SCOPES, "expiry": expiry})


def client(fake, **kw) -> dr.DriveClient:
    return dr.DriveClient(token_json(), CLIENT, session=fake, chunk_size=PIECE, **kw)


def qm(n=7, **kw) -> Match:
    base = dict(key=f"2026mttd_qm{n}", name=f"2026mttd_qm{n}", event_key="2026mttd", event_name="MadTown Throwdown",
                year=2026, comp_level="qm", set_number=1, match_number=n, red=["5026", "254", "1678"],
                blue=["971", "8", "649"], red_score=None, blue_score=None)
    return Match(**{**base, **kw})


@pytest.fixture
def clip(tmp_path) -> Path:
    p = tmp_path / "2026mttd_qm7.mp4"
    p.write_bytes(os.urandom(3 * PIECE + 1000))        # four pieces: three whole, one short
    return p


def drive_setup(fake: FakeDrive):
    """The top folder the setup made (its id is what config.toml keeps)."""
    return fake.add_folder("FRC match videos", props={"ss": "top"})


def destination(fake, top, **kw) -> dr.DriveDestination:
    kw.setdefault("event", EVENT)
    kw.setdefault("height", 0)                          # as cut unless a test is about the copy
    kw.setdefault("log", lambda s: None)
    return dr.DriveDestination(client(fake), top["id"], folder_title=top["name"], **kw)


def job(path, match=None, kind="score", jid=None, **state) -> Job:
    m = match or qm()
    return Job(id=jid or f"{m.name}@100.0:{kind}:drive", destination="drive", kind=kind, path=str(path), match=m,
               state=dict(state))


def no_secrets(text: str) -> bool:
    return not any(s in text for s in SECRETS)


# --- names and codes --------------------------------------------------------------------------------------------------

def test_codes_sort_each_stage_in_play_order():
    assert dr.match_code(qm(7)) == "Q07" and dr.match_code(qm(7), quals=64) == "Q07"
    assert dr.match_code(qm(7), quals=120) == "Q007" and dr.match_code(qm(120), quals=120) == "Q120"
    assert dr.match_code(qm(3, comp_level="p", key="2026mttd_p3", name="2026mttd_p3")) == "PR03"
    sf = qm(key="2026mttd_sf3m1", name="2026mttd_sf3m1", comp_level="sf", set_number=3, match_number=1)
    assert dr.match_code(sf) == "P03"
    final = qm(key="2026mttd_f1m3", name="2026mttd_f1m3", comp_level="f", set_number=1, match_number=3)
    assert dr.match_code(final) == "PF3"
    old_qf = qm(key="2019cada_qf2m1", name="2019cada_qf2m1", event_key="2019cada", year=2019, comp_level="qf",
                set_number=2, match_number=1)
    assert dr.match_code(old_qf) == "QF2-1"
    assert dr.match_code(qm(key="2019cada_f1m2", name="2019cada_f1m2", event_key="2019cada", year=2019,
                            comp_level="f", set_number=1, match_number=2)) == "F2"
    assert dr.match_code(Match(key=None, name="t00012345.6")) == ""
    # name sort: playoffs (P..) above quals (Q..), each stage in order, finals after the playoff matches
    codes = ["Q10", "Q09", "P03", "P13", "PF1", "Q01"]
    assert sorted(codes) == ["P03", "P13", "PF1", "Q01", "Q09", "Q10"]


def test_file_names():
    assert dr.render_name(None, qm(7)) == "Q07 Qualification 7 - 5026 254 1678 vs 971 8 649"
    sf = qm(key="2026mttd_sf3m1", name="2026mttd_sf3m1", comp_level="sf", set_number=3, match_number=1,
            red=["1323", "6619", "254"], blue=["5026", "5499", "114"])
    assert dr.render_name(None, sf) == "P03 Match 3 (R1) - 1323 6619 254 vs 5026 5499 114"
    final = qm(key="2026mttd_f1m3", name="2026mttd_f1m3", comp_level="f", set_number=1, match_number=3, red=[], blue=[])
    assert dr.render_name(None, final) == "PF3 Final Tiebreaker"                  # no teams known: no " - "
    replay = qm(12, name="2026mttd_qm12_r2", replay=2)
    assert dr.render_name(None, replay, quals=80) == "Q12 Qualification 12 (Replay) - 5026 254 1678 vs 971 8 649"
    assert dr.render_name(None, Match(key=None, name="t00012345.6", event_key="2026mttd")) == "t00012345.6"
    assert dr.render_name("{code} {match_title} ({event_key})", qm(7)) == "Q07 Qualification 7 (2026mttd)"
    assert dr.render_name("{match_title}: a/b", qm(7)) == "Qualification 7_ a_b"   # safe on a Windows download
    assert dr.name_format_problem("{code} {nope}").startswith("unknown field {nope}")
    assert dr.name_format_problem("") and dr.name_format_problem("{code}") is None
    assert dr.event_title("2026mttd", "MadTown Throwdown", 2026) == "2026 MadTown Throwdown"
    assert dr.event_title("2026xyz", None, None) == "2026xyz"


# --- resumable upload -------------------------------------------------------------------------------------------------

def test_upload_in_pieces_into_the_event_folder(clip):
    fake = FakeDrive()
    top = drive_setup(fake)
    folders, saves = [], []
    d = destination(fake, top, on_folder=lambda key, f: folders.append((key, f["name"])))
    j = job(clip, played=1_794_000_000.25)
    d.on_state = lambda job_: saves.append(dict(job_.state))
    res = d.send(j)
    size = clip.stat().st_size
    ev = fake.folder("2026 MadTown Throwdown")
    assert ev["parents"] == [top["id"]] and ev["appProperties"] == {"ss": "event", "ss_event": "2026mttd"}
    assert folders == [("2026mttd", "2026 MadTown Throwdown")]
    [v] = fake.videos()
    assert v["data"] == clip.read_bytes() and v["parents"] == [ev["id"]]
    assert v["name"] == "Q07 Qualification 7 - 5026 254 1678 vs 971 8 649.mp4"
    assert v["appProperties"] == {"ss": "video", "ss_match": "2026mttd_qm7", "ss_job": j.id}
    assert v["modifiedTime"] == "2026-11-06T21:20:00.250Z"            # when the match was played
    assert "Red (Teams 5026, 254, 1678)" in v["description"] and "thebluealliance.com/match/2026mttd_qm7" in \
        v["description"]
    assert fake.piece_ranges() == [f"bytes 0-{PIECE - 1}/{size}", f"bytes {PIECE}-{2 * PIECE - 1}/{size}",
                                   f"bytes {2 * PIECE}-{3 * PIECE - 1}/{size}", f"bytes {3 * PIECE}-{size - 1}/{size}"]
    start = fake.of("POST", "/upload/drive/v3/files")[0]
    assert start["headers"]["X-Upload-Content-Length"] == str(size) and start["query"]["uploadType"] == "resumable"
    assert saves[0]["upload_uri"] and saves[0]["upload_offset"] == 0      # saved before the first byte goes
    assert res["url"].startswith("https://drive.google.com/file/d/") and res["file_id"] == v["id"]
    assert "upload_uri" not in j.state and j.state["name"] == v["name"]


def test_resume_after_a_drop_asks_drive_where_it_got_to(clip, tmp_path):
    fake = FakeDrive()
    top = drive_setup(fake)
    d = destination(fake, top)
    j = job(clip)
    fake.drop_at = PIECE + 100 * KIB                  # mid second piece: Drive keeps what arrived
    with pytest.raises(TransientError) as e:
        d.send(j)
    assert no_secrets(str(e.value)) and "upload_id" not in str(e.value)
    assert j.state["upload_uri"] and j.state["upload_offset"] == PIECE
    stored = Job.from_dict(json.loads(json.dumps(j.to_dict())))          # a restart: what the outbox keeps
    n = len(fake.piece_ranges())
    destination(fake, top).send(stored)
    size = clip.stat().st_size
    after = fake.piece_ranges()[n:]
    assert after[0] == f"bytes */{size}"
    assert after[1] == f"bytes {PIECE + 100 * KIB}-{PIECE + 100 * KIB + PIECE - 1}/{size}"
    assert len(fake.of("POST", "/upload/drive/v3/files")) == 1
    assert [v["data"] for v in fake.videos()] == [clip.read_bytes()]


@pytest.mark.parametrize("gone", [404, 410])
def test_expired_session_starts_a_new_one(clip, gone):
    fake = FakeDrive()
    top = drive_setup(fake)
    d = destination(fake, top)
    j = job(clip)
    fake.drop_at = PIECE
    with pytest.raises(TransientError):
        d.send(j)
    fake.sessions["s1"]["gone"] = gone
    d.send(j)
    assert len(fake.sessions) == 2 and j.state["upload_sessions"] == 2
    assert [v["data"] for v in fake.videos()] == [clip.read_bytes()]


def test_a_refused_piece_starts_over_and_the_third_refusal_is_reported(clip):
    fake = FakeDrive()
    top = drive_setup(fake)
    d = destination(fake, top)
    j = job(clip)
    fake.faults.append(fail("PUT", "/upload/drive/v3/files", gerr(400, "badRequest", "Bad piece"),
                            when=lambda q: True))
    with pytest.raises(TransientError, match="starting it again"):
        d.send(j)
    assert "upload_uri" not in j.state
    d.send(j)
    assert len(fake.sessions) == 2 and [v["data"] for v in fake.videos()] == [clip.read_bytes()]
    j2 = job(clip.with_name("x.mp4"), qm(8))
    Path(j2.path).write_bytes(b"w" * 900)
    for _ in range(2):
        fake.faults.append(fail("PUT", "/upload/drive/v3/files", gerr(403, "forbidden", "No")))
        with pytest.raises(TransientError):
            d.send(j2)
    fake.faults.append(fail("PUT", "/upload/drive/v3/files", gerr(403, "forbidden", "No")))
    with pytest.raises(PermanentError, match="HTTP 403 forbidden"):
        d.send(j2)


def test_lost_final_answer_and_lost_state_never_make_a_second_copy(clip):
    fake = FakeDrive()
    top = drive_setup(fake)
    d = destination(fake, top)
    j = job(clip)
    fake.lose_final_answer = True
    with pytest.raises(TransientError):
        d.send(j)
    d.send(j)                                          # the session says it finished: the file is used
    assert len(fake.videos()) == 1 and len(fake.sessions) == 1
    # a restart that lost the job's state, or a second laptop on the same account: found by its appProperties
    other = destination(fake, top)
    j2 = job(clip, jid="2026mttd_qm7@555.0:clip:drive", kind="clip")
    res = other.send(j2)
    assert len(fake.videos()) == 1 and res["existed"] and res["file_id"] == fake.videos()[0]["id"]
    assert "appProperties has { key='ss_match' and value='2026mttd_qm7' }" in fake.queries[-1]
    # another match is uploaded as usual, into the same event folder (found by its appProperties, not made again)
    clip8 = clip.with_name("2026mttd_qm8.mp4")
    clip8.write_bytes(b"x" * 1000)
    other.send(job(clip8, qm(8)))
    assert len(fake.videos()) == 2 and len([f for f in fake.files.values() if f["name"].startswith("2026 ")]) == 1


def test_a_second_laptop_reuses_the_event_folder_by_its_app_properties(clip):
    fake = FakeDrive()
    top = drive_setup(fake)
    made = fake.add_folder("2026 Madtown Throwdown (renamed by a person)", top["id"],
                           {"ss": "event", "ss_event": "2026mttd"})
    destination(fake, top).send(job(clip))
    assert fake.videos()[0]["parents"] == [made["id"]] and fake.folder("2026 MadTown Throwdown") is None


# --- the smaller copy -------------------------------------------------------------------------------------------------

def test_small_copy_is_made_once_reused_on_retry_and_removed_after(clip):
    fake = FakeDrive()
    top = drive_setup(fake)
    made = []

    def encode(src, dst, height):
        made.append((Path(src).name, Path(dst).name, height))
        Path(dst).write_bytes(Path(src).read_bytes()[:PIECE + 500])      # "smaller"
    d = destination(fake, top, height=720, encode=encode)
    j = job(clip)
    fake.drop_at = 100
    with pytest.raises(TransientError):
        d.send(j)
    copy = clip.parent / "drive_720p" / clip.name
    assert copy.is_file() and not (clip.parent / "drive_720p" / f".{clip.name}.part").exists()
    assert made == [(clip.name, f".{clip.name}.part", 720)]
    d.send(j)                                          # the retry uploads the same copy: no second encode
    assert len(made) == 1 and fake.videos()[0]["data"] == clip.read_bytes()[:PIECE + 500]
    assert not copy.exists() and clip.is_file()        # the copy goes once Drive has it; the clip stays


def test_small_copy_failing_uploads_as_cut_and_says_so_once(tmp_path):
    fake = FakeDrive()
    top = drive_setup(fake)
    said = []

    def broken(src, dst, height):
        raise FileNotFoundError("ffmpeg not found; install ffmpeg or put it on PATH")
    d = destination(fake, top, height=540, encode=broken, warn=lambda text, key: said.append(key))
    for n in (1, 2):
        c = tmp_path / f"2026mttd_qm{n}.mp4"
        c.write_bytes(b"y" * 2000)
        d.send(job(c, qm(n)))
    assert [v["data"] for v in fake.videos()] == [b"y" * 2000] * 2 and said == ["drive_copy"]


def test_a_copy_no_smaller_than_the_clip_is_not_used(clip):
    """A webcast at 720p already: the re-encode saves nothing (the bundled 2024 sample grows from 13 to 17 MB)."""
    fake = FakeDrive()
    top = drive_setup(fake)
    made = []

    def bigger(src, dst, height):
        made.append(1)
        Path(dst).write_bytes(Path(src).read_bytes() + b"more")
    d = destination(fake, top, height=720, encode=bigger)
    j = job(clip)
    d.send(j)
    assert fake.videos()[0]["data"] == clip.read_bytes() and j.state["as_cut"] is True
    assert not (clip.parent / "drive_720p" / clip.name).exists() and made == [1]


def test_encode_runs_ffmpeg_at_below_normal_priority(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(dr.ffmpeg, "find", lambda tool="ffmpeg": "ffmpeg")
    monkeypatch.setattr(dr.subprocess, "run", lambda cmd, **kw: seen.update(cmd=cmd, kw=kw))
    dr.encode_small(tmp_path / "a.mp4", tmp_path / ".a.mp4.part", 720)
    assert "scale=-2:720,fps=30" in seen["cmd"] and "+faststart" in seen["cmd"] and seen["kw"]["check"]
    if os.name == "nt":
        assert seen["kw"]["creationflags"] & dr.subprocess.BELOW_NORMAL_PRIORITY_CLASS


# --- folders, storage, errors -----------------------------------------------------------------------------------------

def test_top_folder_gone_or_trashed_says_what_to_do(clip):
    fake = FakeDrive()
    with pytest.raises(PermanentError) as e:
        destination(fake, {"id": "gone1", "name": "FRC match videos"}).send(job(clip))
    assert "another Google account" in str(e.value) and dr.SETUP_AGAIN in str(e.value)
    top = fake.add_folder("FRC match videos", trashed=True)
    with pytest.raises(PermanentError, match="in the trash"):
        destination(fake, top).send(job(clip))


def test_event_folder_deleted_while_running_is_made_again(clip):
    fake = FakeDrive()
    top = drive_setup(fake)
    d = destination(fake, top)
    d.send(job(clip))
    del fake.files[fake.folder("2026 MadTown Throwdown")["id"]]          # deleted for good meanwhile
    c2 = clip.with_name("2026mttd_qm8.mp4")
    c2.write_bytes(b"z" * 999)
    d.send(job(c2, qm(8)))
    assert fake.files[fake.videos()[-1]["parents"][0]]["name"] == "2026 MadTown Throwdown"


def test_drive_full_pauses_the_queue_and_dms_once_then_resume_sends(tmp_path, monkeypatch):
    monkeypatch.setenv("STREAMSPLITTER_CONFIG", str(tmp_path / "config.toml"))
    fake = FakeDrive()
    top = drive_setup(fake)
    note = []
    notifier = types.SimpleNamespace(warn=lambda text, key=None, dm=True: note.append(text), log=lambda s: None,
                                     error=lambda *a, **k: True, info=lambda *a, **k: True)
    d = destination(fake, top)
    box = live.PausableOutbox(tmp_path / "o.sqlite", {"drive": d}, notifier, stage=False)
    d.on_full = lambda text: live._drive_full(box, notifier, text)
    for n in (1, 2):
        c = tmp_path / f"2026mttd_qm{n}.mp4"
        c.write_bytes(b"v" * 5000)
        box.add(job(c, qm(n)))
    fake.full = True
    box.step("drive")
    assert box.paused == {"drive"} and config.section("drive")["paused"] is True
    assert len(note) == 1 and "Google Drive (team5026@example.com) is full" in note[0] and "`resume drive`" in note[0]
    for _ in range(3):
        assert box.step("drive") == live.IDLE_S                          # held: nothing tried, nothing said
    assert len(note) == 1 and box.status()["drive"]["queued"] == 2
    fake.full = False
    ctl = live.LiveControls(None, None, publisher=types.SimpleNamespace(outbox=box))
    reply = commands.dispatch(commands.parse("resume drive"), ctl)
    assert reply.startswith("OK: Google Drive uploads resumed; 2 waiting") and "paused" not in config.section("drive")
    while box.step("drive") == 0 and box.status()["drive"]["queued"]:
        pass
    assert len(fake.videos()) == 2
    box.close()


def test_too_little_space_for_the_next_video_is_full_before_uploading(clip):
    fake = FakeDrive(limit=10 * 10 ** 9, usage=10 * 10 ** 9 - 1000)
    top = drive_setup(fake)
    said = []
    d = destination(fake, top, on_full=said.append)
    with pytest.raises(dr.DriveFull):
        d.send(job(clip))
    assert said and not fake.of("POST", "/upload/drive/v3/files")       # no 1 MB sent for nothing


def test_low_space_warns_once_and_status_shows_it(clip, tmp_path):
    fake = FakeDrive(limit=15 * 10 ** 9, usage=12 * 10 ** 9)            # 3 GB free: under one event at 720p
    top = drive_setup(fake)
    said = []
    d = destination(fake, top, warn=lambda text, key: said.append((key, text)))
    d.send(job(clip))
    c2 = tmp_path / "2026mttd_qm8.mp4"
    c2.write_bytes(b"q" * 500)
    d.send(job(c2, qm(8)))
    assert [k for k, _ in said] == ["drive_space"] and "less than one event" in said[0][1]
    st = d.storage_status()
    assert st["free_gb"] == 3.0 and st["limit_gb"] == 15.0 and st["account"] == "team5026@example.com"
    assert st["matches_left"] == 3 * 10 ** 9 // (130 * 10 ** 6)          # height 0 here: as cut, 130 MB a match


def test_unlimited_or_unreadable_storage_does_not_stop_uploads(clip):
    fake = FakeDrive(limit=None)
    top = drive_setup(fake)
    destination(fake, top).send(job(clip))
    fake2 = FakeDrive()
    top2 = drive_setup(fake2)
    fake2.faults.append(fail("GET", "/drive/v3/about", gerr(403, "insufficientPermissions")))
    destination(fake2, top2).send(job(clip))
    assert len(fake.videos()) == len(fake2.videos()) == 1


@pytest.mark.parametrize("resp, kind, words", [
    (Resp(429), TransientError, "HTTP 429"),
    (Resp(503), TransientError, "HTTP 503"),
    (gerr(403, "rateLimitExceeded"), TransientError, "rateLimitExceeded"),
    (gerr(403, "userRateLimitExceeded"), TransientError, "userRateLimitExceeded"),
    (gerr(403, "storageQuotaExceeded"), dr.DriveFull, "full"),
    (gerr(403, "accessNotConfigured"), PermanentError, "drive.googleapis.com"),
    (gerr(403, "domainPolicy"), PermanentError, "another Google"),
    (gerr(400, "badRequest", "Invalid value"), PermanentError, "Invalid value"),
    (requests.exceptions.ConnectionError("https://www.googleapis.com/upload?upload_id=SECRET"), TransientError,
     "no connection"),
])
def test_error_mapping(clip, resp, kind, words):
    fake = FakeDrive()
    top = drive_setup(fake)
    fake.faults.append(fail("POST", "/upload/drive/v3/files", resp))
    with pytest.raises(kind) as e:
        destination(fake, top).send(job(clip))
    assert words in str(e.value) and "SECRET" not in str(e.value) and no_secrets(str(e.value))


def test_api_not_enabled_and_revoked_login_are_permanent(clip):
    fake = FakeDrive()
    top = drive_setup(fake)
    fake.api_disabled = True
    with pytest.raises(PermanentError, match="not enabled"):
        destination(fake, top).send(job(clip))
    fake.api_disabled = False
    fake.valid_tokens = set()                          # the login was revoked: refused, and no renewal
    fake.refresh_error = "invalid_grant"
    with pytest.raises(PermanentError) as e:
        destination(fake, top).send(job(clip))
    assert dr.SETUP_AGAIN in str(e.value) and no_secrets(str(e.value))


def test_renewed_token_is_handed_back(clip):
    fake = FakeDrive()
    top = drive_setup(fake)
    got = []
    c = dr.DriveClient(token_json(expires_in=-10), CLIENT, on_token=got.append, session=fake, chunk_size=PIECE)
    dr.DriveDestination(c, top["id"], event=EVENT, height=0, log=lambda s: None).send(job(clip))
    assert fake.refreshes == 1 and json.loads(got[0])["token"] == "tok1"


def test_trash_and_test_upload_into_the_top_folder(tmp_path):
    fake = FakeDrive()
    top = drive_setup(fake)
    sample = tmp_path / "sample.mp4"
    sample.write_bytes(b"s" * 3000)
    d = destination(fake, top, height=720, encode=lambda *a: pytest.fail("the test upload is sent as cut"))
    m = Match(key="2024mttd_qm6", name="2024mttd_qm6", event_key="2024mttd", event_name="Madtown Throwdown",
              year=2024, comp_level="qm", set_number=1, match_number=6, red=["1323", "6619", "254B"],
              blue=["5026", "5499", "114"], red_score=81, blue_score=63)
    res = d.send(Job(id="setup-test:test:drive", destination="drive", kind="test", path=str(sample), match=m))
    [v] = fake.videos()
    assert v["parents"] == [top["id"]] and res["test"]
    assert v["name"] == "StreamSplitter setup test - Q06 Qualification 6 - 1323 6619 254B vs 5026 5499 114.mp4"
    assert v["description"].startswith(dr.TEST_DESCRIPTION)
    assert d.client.trash(res["file_id"]) is True and fake.files[res["file_id"]]["trashed"] is True
    assert d.client.trash("nope") is False


# --- the Publisher: routing, late scores ------------------------------------------------------------------------------

SLACK = {"enabled": True, "teams": ["5026"]}
YT = {"enabled": True, "route": "all"}
DRIVE = {"enabled": True}


def test_routing_sends_every_match_to_drive_in_order():
    assert route({"5026"}, SLACK, YT, {"enabled": True}, DRIVE) == ["slack", "youtube", "drive", "wiki"]
    assert route({"100"}, SLACK, {"enabled": False}, None, DRIVE) == ["drive"]
    assert route({"5026"}, SLACK, {**YT, "route": "others"}, None, DRIVE) == ["slack", "drive"]
    assert route({"100"}, SLACK, YT, None, {"enabled": False}) == ["youtube"] == route({"100"}, SLACK, YT)


def row(n, tmp_path, partial=False, score=True, teams=("5026",)):
    c = tmp_path / f"2026mttd_qm{n}.mp4"
    c.write_bytes(b"clip")
    os.utime(c, (1_794_000_000 + n, 1_794_000_000 + n))
    r = {"name": f"2026mttd_qm{n}", "naming": {"name": f"2026mttd_qm{n}", "key": f"2026mttd_qm{n}",
                                               "teams_read": list(teams)},
         "start": 10.0 * n, "end": 10.0 * n + 150, "partial": partial, "clip": {"path": str(c)}}
    if score:
        s = tmp_path / f"2026mttd_qm{n}_score.mp4"
        s.write_bytes(b"score")
        r["score_clip"] = {"path": str(s)}
    return r


class Dest:
    def __init__(self, name):
        self.name, self.sent, self.quals, self.playoff_alliances = name, [], None, 8

    def send(self, job_):
        self.sent.append(job_)
        job_.state.setdefault("file_id", "f1")
        return {"url": "u"}


def test_the_publisher_queues_every_match_for_drive_with_its_play_time(tmp_path):
    dests = {"slack": Dest("slack"), "drive": Dest("drive")}
    box = Outbox(tmp_path / "o.sqlite", dests, notify.Notifier(log=lambda s: None), stage=False)
    sched = [{"key": f"2026mttd_qm{n}", "comp_level": "qm", "set_number": 1, "match_number": n,
              "alliances": {"red": {"team_keys": [], "score": -1}, "blue": {"team_keys": [], "score": -1}}}
             for n in range(1, 73)]
    pub = Publisher(box, cfg_event=EVENT, cfg_slack=SLACK, cfg_drive=DRIVE, schedule=lambda: sched,
                    tba_get=lambda path: {})
    own, other, part = row(1, tmp_path), row(2, tmp_path, teams=("100",)), row(3, tmp_path, partial=True, score=False)
    for r in (own, other, part):
        pub.on_clip(r)
    pub.on_score(own)
    pub.on_score(other)
    jobs = box.jobs("drive")
    assert [j["id"] for j in jobs] == ["2026mttd_qm3@30.0:clip:drive", "2026mttd_qm1@10.0:score:drive",
                                       "2026mttd_qm2@20.0:score:drive"]
    assert jobs[1]["job"].path.endswith("_score.mp4") and jobs[1]["job"].state["played"] == 1_794_000_001
    box.hooks["drive"](jobs[1]["job"])
    assert dests["drive"].quals == 72
    box.close()


def test_a_late_score_updates_the_description(tmp_path):
    fake = FakeDrive()
    top = drive_setup(fake)
    d = destination(fake, top)
    t = [1_794_000_000.0]
    box = Outbox(tmp_path / "o.sqlite", {"drive": d}, notify.Notifier(log=lambda s: None), stage=False,
                 clock=lambda: t[0])
    d.on_state = box.save_state
    tba = {"/match/2026mttd_qm7": {"alliances": {"red": {"score": -1, "team_keys": []},
                                                 "blue": {"score": -1, "team_keys": []}}}}
    Publisher(box, cfg_event=EVENT, cfg_drive=DRIVE, tba_get=lambda path: tba[path], clock=lambda: t[0])
    c = tmp_path / "2026mttd_qm7.mp4"
    c.write_bytes(b"m" * 4000)
    box.add(job(c, played=1_794_000_000.0))
    box.step("drive")
    [v] = fake.videos()
    assert " - 112" not in v["description"]
    describe = [j for j in box.jobs("drive") if j["kind"] == "describe"]
    assert len(describe) == 1 and describe[0]["status"] == "queued"
    t[0] += 700
    tba["/match/2026mttd_qm7"]["alliances"]["red"]["score"] = 112
    tba["/match/2026mttd_qm7"]["alliances"]["blue"]["score"] = 98
    box.step("drive")
    assert box.get(describe[0]["id"])["status"] == "done"
    assert "Red (Teams 5026, 254, 1678) - 112" in v["description"] and "Blue (Teams 971, 8, 649) - 98" in \
        v["description"]
    assert v["modifiedTime"] == "2026-11-06T21:20:00.000Z" and len(fake.videos()) == 1
    assert len(fake.of("PATCH", "/drive/v3/files/")) == 1
    box.close()


# --- the live run: build_publishing, commands, status -----------------------------------------------------------------

@pytest.fixture
def roots(tmp_path, monkeypatch):
    monkeypatch.setenv("STREAMSPLITTER_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("STREAMSPLITTER_CONFIG", str(tmp_path / "config.toml"))
    return tmp_path


def test_build_publishing_without_the_drive_login_says_how_to_fix_it(roots, monkeypatch):
    sections = {"event": {"key": "2026mttd"}, "drive": {"enabled": True, "folder_id": "fo1"}}
    monkeypatch.setattr(config, "section", lambda name: dict(sections.get(name, {})))
    monkeypatch.setattr(config, "get_secret", lambda name: None)
    lines = []
    notifier, outbox, publisher, hb_s, summary = live.build_publishing("2026mttd", log=lambda s, **_: lines.append(s))
    try:
        assert "drive" not in outbox.destinations and publisher is not None
        assert "streamsplitter setup --section drive" in "\n".join(lines)
    finally:
        outbox.close()


def test_build_publishing_wires_drive(roots, monkeypatch):
    sections = {"event": dict(EVENT), "slack": {"enabled": False, "teams": ["5026"]},
                "youtube": {"enabled": False, "description_format": "{match_title}\nBy 5026"},
                "drive": {"enabled": True, "folder_id": "fo1", "folder_title": "FRC match videos", "height": 540,
                          "event_folder_id": "fo2", "event_folder_event": "2026mttd", "paused": True}}
    secrets = {"drive_token": token_json(), "youtube_client": json.dumps(CLIENT)}
    monkeypatch.setattr(config, "section", lambda name: dict(sections.get(name, {})))
    monkeypatch.setattr(config, "get_secret", lambda name: secrets.get(name))
    notifier, outbox, publisher, hb_s, summary = live.build_publishing("2026mttd", log=lambda s, **_: None)
    try:
        d = outbox.destinations["drive"]
        assert isinstance(d, dr.DriveDestination) and d.on_state == outbox.save_state and d.on_full is not None
        assert d.height == 540 and d.folder_id == "fo1" and d.event_folder_id == "fo2"
        assert d.description_format == "{match_title}\nBy 5026"           # YouTube's format when Drive has none
        assert outbox.paused == {"drive"} and "FRC match videos" in summary and "resume drive" in summary
        assert publisher.cfg_drive["enabled"] and outbox.hooks["drive"] == publisher._before_drive
        assert outbox.after_send["drive"] == publisher._after_drive
    finally:
        outbox.close()
        notifier.close()


def test_pause_resume_help_and_status(roots):
    w = Dest("drive")
    w.storage_status = lambda: {"account": "team5026@example.com", "free_gb": 9.6, "limit_gb": 15.0,
                                "matches_left": 240, "height": 720, "checked": 1.0}
    box = live.PausableOutbox(roots / "data" / "outbox" / "p.sqlite", {"drive": w},
                              notify.Notifier(log=lambda s: None), stage=False)
    pub = types.SimpleNamespace(outbox=box)
    mon = live.Monitor(roots / "f", notify.Notifier(log=lambda s: None), pub)
    ctl = live.LiveControls(types.SimpleNamespace(url=None, can_reply=True), mon, publisher=pub)
    for text in ("pause drive", "Pause Google Drive", "hold gdrive"):
        assert commands.parse(text).arg == "drive"
    assert "Only YouTube, Google Drive and wiki" in commands.parse("pause slack").error
    assert "pause drive" in commands.help_text(drive=True) and "pause drive" not in commands.help_text()
    assert "pause drive" in commands.dispatch(commands.parse("help"), ctl) and "`pause drive`" in commands.short_help()
    reply = commands.dispatch(commands.parse("pause drive"), ctl)
    assert reply.startswith("OK: Google Drive uploads are paused") and config.section("drive")["paused"] is True
    box.add(job("x.mp4", qm(1)))
    assert box.step("drive") == live.IDLE_S and w.sent == []
    mon.poll()
    text = ctl.status_text()
    assert "Google Drive: 0 sent, 1 waiting, 0 failed (paused: reply resume drive to send them)." in text
    assert "Google Drive space: 9.6 GB free of 15.0 GB, about 240 more matches (team5026@example.com)." in text
    assert json.loads(mon.status_path.read_text())["drive_storage"]["free_gb"] == 9.6
    reply = commands.dispatch(commands.parse("resume drive"), ctl)
    assert reply == "OK: Google Drive uploads resumed; 1 waiting go out now, in order."
    box.step("drive")
    assert [j.match.name for j in w.sent] == ["2026mttd_qm1"]
    box.close()


def test_templates_description_is_the_youtube_one():
    d = dr.DriveDestination(object.__new__(dr.DriveClient), "fo1", event=EVENT,
                            description_format=templates.default_description("5026"), log=lambda s: None)
    name, desc = d.render(job("x.mp4", qm(7, red_score=112, blue_score=98)))
    assert name == "Q07 Qualification 7 - 5026 254 1678 vs 971 8 649.mp4"
    assert desc.splitlines()[:3] == ["Qualification 7 - 2026 MadTown Throwdown", "Red (Teams 5026, 254, 1678) - 112",
                                     "Blue (Teams 971, 8, 649) - 98"]
    assert desc.endswith("Recorded from the event webcast by FRC Team 5026.")
    with pytest.raises(PermanentError, match="name format"):
        dr.DriveDestination(object.__new__(dr.DriveClient), "fo1", name_format="{bad}")
