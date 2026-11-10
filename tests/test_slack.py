"""Slack client and destination with all HTTP faked: FakeSlack answers slack.com's Web API in place of slack_sdk's
HTTP call and the files.slack.com upload URL in place of urlopen, and any real connection fails the test. No token,
no Slack account.

    .venv\\Scripts\\python.exe -m pytest -p no:cacheprovider tests\\test_slack.py
"""

from __future__ import annotations

import email.message
import email.utils
import io
import json
import socket
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from urllib.error import URLError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from streamsplitter.publish import slack  # noqa: E402
from streamsplitter.publish.base import Job, Match, PermanentError, TransientError  # noqa: E402

TOKEN = "xoxb-0000-not-a-real-token"
CH = "C0ERRATA01"
MATCH = Match(key="2026mttd_qm12", name="2026mttd_qm12", event_key="2026mttd", event_name="Madtown Throwdown",
              year=2026, comp_level="qm", set_number=1, match_number=12, red=["5026", "254", "1678"],
              blue=["604", "8033", "199"])


def _headers(d: dict) -> email.message.Message:
    m = email.message.Message()
    m["Content-Type"] = "application/json; charset=utf-8"
    for k, v in d.items():
        m[k] = str(v)
    return m


class _Resp:
    def __init__(self, status: int, body: bytes):
        self.status, self._body = status, body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSlack:
    """Slack as seen through HTTP. `handlers` maps an API method to fn(params) returning a body dict, a
    (status, headers, body) tuple, or an exception to raise. Uploaded files are shared the way Slack does it: the
    share shows in files.info only after `share_after` polls."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.uploads: list[tuple[str, int]] = []
        self.upload_error: Exception | None = None
        self.files: dict[str, dict] = {}
        self.slept: list[float] = []
        self.share_after = 1
        self.next_ts = 1730000000.0
        self.handlers = {
            "auth.test": lambda p: (200, {"x-oauth-scopes": "chat:write,files:write,users:read"},
                                    {"ok": True, "team": "Errata", "team_id": "T01", "user_id": "U0BOT00001",
                                     "user": "streamsplitter", "url": "https://errata.slack.com/"}),
            "files.getUploadURLExternal": self.get_upload_url,
            "files.completeUploadExternal": self.complete,
            "files.info": self.files_info,
            "chat.getPermalink": lambda p: {"ok": True, "permalink": f"https://errata.slack.com/archives/"
                                                                     f"{p['channel']}/p{p['message_ts']}"},
            "chat.postMessage": lambda p: {"ok": True, "ts": self.ts(), "channel": p["channel"]},
            "conversations.open": lambda p: {"ok": True, "channel": {"id": "D" + p["users"][1:]}},
            "chat.scheduleMessage": lambda p: {"ok": True, "scheduled_message_id": "Q0ALARM001",
                                               "post_at": int(p["post_at"]), "channel": p["channel"]},
            "chat.deleteScheduledMessage": lambda p: {"ok": True},
        }

    def ts(self) -> str:
        self.next_ts += 1
        return f"{self.next_ts:.6f}"

    def get_upload_url(self, p):
        fid = f"F{len(self.files) + 1:09d}"
        self.files[fid] = {"id": fid, "length": int(p["length"]), "completed": False, "polls": 0}
        return {"ok": True, "upload_url": f"https://files.slack.com/upload/v1/{fid}", "file_id": fid}

    def complete(self, p):
        files = json.loads(p["files"])
        for f in files:
            self.files[f["id"]].update(completed=True, title=f.get("title"), channel=p["channel_id"],
                                       comment=p.get("initial_comment"),
                                       share={"ts": self.ts(), "thread_ts": p.get("thread_ts")})
        return {"ok": True, "files": [{"id": f["id"], "title": f.get("title")} for f in files]}

    def files_info(self, p):
        rec = self.files.get(p["file"])
        if rec is None or not rec["completed"]:
            return {"ok": False, "error": "file_not_found"}
        rec["polls"] += 1
        shares = {"public": {rec["channel"]: [rec["share"]]}} if rec["polls"] > self.share_after else {}
        return {"ok": True, "file": {"id": rec["id"], "permalink": f"https://errata.slack.com/files/{rec['id']}",
                                     "shares": shares}}

    # stands in for slack_sdk's WebClient._perform_urllib_http_request_internal(url, req)
    def api(self, url, req):
        parts = urllib.parse.urlsplit(url)
        method = parts.path.rsplit("/", 1)[-1]
        params = dict(urllib.parse.parse_qsl(parts.query))
        if req.data:
            body = req.data.decode("utf-8")
            params.update(json.loads(body) if body.startswith("{") else dict(urllib.parse.parse_qsl(body)))
        assert TOKEN in (req.get_header("Authorization") or "")
        self.calls.append((method, params))
        out = self.handlers[method](params)
        if isinstance(out, BaseException):
            raise out
        status, headers, body = out if isinstance(out, tuple) else (200, {}, out)
        text = body if isinstance(body, str) else json.dumps(body)
        if status != 200:
            from urllib.error import HTTPError
            raise HTTPError(url, status, "error", _headers(headers), io.BytesIO(text.encode()))
        return {"status": 200, "headers": {"Content-Type": "application/json", **headers}, "body": text}

    # stands in for urlopen on the upload URL
    def urlopen(self, req, timeout=None):
        assert req.full_url.startswith("https://files.slack.com/upload/v1/")
        self.uploads.append((req.full_url.rsplit("/", 1)[-1], len(req.data)))
        if self.upload_error:
            raise self.upload_error
        return _Resp(200, b"OK - %d" % len(req.data))

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    def params(self, method: str) -> list[dict]:
        return [p for m, p in self.calls if m == method]


@pytest.fixture
def fake(monkeypatch):
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(k, raising=False)

    def no_network(*a, **k):
        raise AssertionError("a test tried to reach the network")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    f = FakeSlack()
    monkeypatch.setattr(slack, "urlopen", f.urlopen)
    return f


def client(fake, **kw) -> slack.SlackClient:
    c = slack.SlackClient(TOKEN, **kw)
    c.web._perform_urllib_http_request_internal = fake.api
    c.sleep = fake.slept.append
    return c


@pytest.fixture
def clip(tmp_path) -> Path:
    p = tmp_path / "2026mttd_qm12.mp4"
    p.write_bytes(b"\x00\x01" * 2500)
    return p


def dest(fake, **kw) -> slack.SlackDestination:
    return slack.SlackDestination(client(fake), CH, channel_name="errata", teams=["5026"], **kw)


def job(path, kind="clip", state=None) -> Job:
    return Job(id=f"2026mttd_qm12:{kind}:slack", destination="slack", kind=kind, path=str(path), match=MATCH,
               state=state or {})


# ---------------------------------------------------------------------------------------------------------------
# uploads and threads
# ---------------------------------------------------------------------------------------------------------------

def test_clip_is_posted_with_comment_and_the_message_ts_is_returned(fake, clip):
    j = job(clip)
    res = dest(fake).send(j)
    assert fake.methods()[:2] == ["files.getUploadURLExternal", "files.completeUploadExternal"]
    get = fake.params("files.getUploadURLExternal")[0]
    assert get["filename"] == clip.name and int(get["length"]) == clip.stat().st_size
    fid = res["file_id"]
    assert fake.uploads == [(fid, clip.stat().st_size)]
    comp = fake.params("files.completeUploadExternal")[0]
    assert comp["channel_id"] == CH and "thread_ts" not in comp
    text = comp["initial_comment"]
    assert "Qualification 12" in text and "Madtown Throwdown" in text
    assert "*5026*" in text and "604" in text and "*604*" not in text
    assert res["ts"] == fake.files[fid]["share"]["ts"]       # found in files.info once the share showed
    assert res["permalink"].startswith("https://errata.slack.com/archives/")
    assert fake.slept, "the share shows late, so the client waited for it"
    assert "slack_file_id" not in j.state


def test_score_version_is_a_reply_in_the_clip_thread(fake, tmp_path):
    score = tmp_path / "2026mttd_qm12_score.mp4"
    score.write_bytes(b"s" * 3000)
    MATCH_SCORED = Match(**{**MATCH.__dict__, "red_score": 112, "blue_score": 98})
    j = Job(id="2026mttd_qm12:score:slack", destination="slack", kind="score", path=str(score), match=MATCH_SCORED,
            state={"thread_ts": "1730000001.000000"})
    res = dest(fake).send(j)
    comp = fake.params("files.completeUploadExternal")[0]
    assert comp["thread_ts"] == "1730000001.000000"
    assert "with the results screen" in comp["initial_comment"] and "Red 112 - Blue 98" in comp["initial_comment"]
    assert res["thread_ts"] == "1730000001.000000" and res["ts"]


def test_score_finds_the_thread_from_the_clip_file_or_posts_to_the_channel(fake, clip, tmp_path):
    d = dest(fake)
    first = d.send(job(clip))
    score = tmp_path / "2026mttd_qm12_score.mp4"
    score.write_bytes(b"s" * 3000)
    d.send(job(score, "score", {"thread_file_id": first["file_id"]}))     # the clip's ts was not known yet
    assert fake.params("files.completeUploadExternal")[1]["thread_ts"] == first["ts"]
    d.send(job(score, "score"))                                           # no thread at all: the channel
    assert "thread_ts" not in fake.params("files.completeUploadExternal")[2]


def test_score_whose_thread_was_deleted_is_posted_on_its_own(fake, tmp_path):
    score = tmp_path / "2026mttd_qm12_score.mp4"
    score.write_bytes(b"s" * 3000)

    def complete(p):
        return {"ok": False, "error": "thread_not_found"} if p.get("thread_ts") else fake.complete(p)
    fake.handlers["files.completeUploadExternal"] = complete
    res = dest(fake).send(job(score, "score", {"thread_ts": "1.0"}))
    assert res["ts"] and "thread_ts" not in res


def test_ts_is_none_when_the_share_does_not_show_in_time(fake, clip):
    fake.share_after = 10 ** 6
    c = client(fake, share_wait_s=5)
    res = slack.SlackDestination(c, CH).send(job(clip))
    assert res["ts"] is None and res["file_id"]
    assert sum(fake.slept) <= 5


def test_setup_test_posts_the_bundled_sample(fake):
    res = dest(fake).send(Job(id="test:slack", destination="slack", kind="test", path="", match=MATCH))
    assert fake.uploads[0][1] == slack.SAMPLE_CLIP.stat().st_size
    assert fake.params("files.completeUploadExternal")[0]["initial_comment"] == slack.TEST_FORMAT
    assert res["ts"]


# ---------------------------------------------------------------------------------------------------------------
# failures: what retries and what needs a person
# ---------------------------------------------------------------------------------------------------------------

def test_rate_limit_is_transient_with_retry_after(fake, clip):
    fake.handlers["files.getUploadURLExternal"] = lambda p: (429, {"Retry-After": "7"},
                                                             {"ok": False, "error": "ratelimited"})
    with pytest.raises(TransientError) as e:
        dest(fake).send(job(clip))
    assert e.value.retry_after == 7.0
    assert not fake.uploads


def test_not_in_channel_is_permanent_and_says_how_to_invite(fake, clip):
    fake.handlers["files.completeUploadExternal"] = lambda p: {"ok": False, "error": "not_in_channel"}
    j = job(clip)
    with pytest.raises(PermanentError) as e:
        dest(fake).send(j)
    assert "/invite @StreamSplitter" in str(e.value) and "#errata" in str(e.value)
    assert "slack_file_id" not in j.state


def test_invalid_auth_is_permanent_and_never_shows_the_token(fake, clip):
    fake.handlers["files.getUploadURLExternal"] = lambda p: {"ok": False, "error": "invalid_auth"}
    with pytest.raises(PermanentError) as e:
        dest(fake).send(job(clip))
    assert "xoxb-" in str(e.value) and "streamsplitter setup" in str(e.value)
    assert TOKEN not in str(e.value)


def test_network_errors_are_transient(fake, clip):
    fake.handlers["files.getUploadURLExternal"] = lambda p: URLError("getaddrinfo failed")
    with pytest.raises(TransientError):
        dest(fake).send(job(clip))
    fake.handlers["files.getUploadURLExternal"] = fake.get_upload_url
    fake.upload_error = ConnectionResetError("reset by peer")            # the upload itself breaks off
    with pytest.raises(TransientError):
        dest(fake).send(job(clip))
    assert "files.completeUploadExternal" not in fake.methods()


@pytest.mark.parametrize("status, body", [(502, "<html>Bad Gateway</html>"),
                                          (200, "<html>Sign in to Venue Wi-Fi</html>")])
def test_server_errors_and_wifi_login_pages_are_transient(fake, clip, status, body):
    fake.handlers["files.getUploadURLExternal"] = lambda p: (status, {}, body)
    with pytest.raises(TransientError):
        dest(fake).send(job(clip))


def test_missing_file_is_permanent(fake, tmp_path):
    with pytest.raises(PermanentError) as e:
        dest(fake).send(job(tmp_path / "gone.mp4"))
    assert "gone" in str(e.value)
    assert not fake.calls


def test_lost_completion_reply_does_not_post_the_clip_twice(fake, clip):
    def complete_then_drop(p):
        fake.complete(p)                     # Slack did it, but the reply never arrived
        return TimeoutError("The read operation timed out")
    fake.handlers["files.completeUploadExternal"] = complete_then_drop
    d, j = dest(fake), job(clip)
    with pytest.raises(TransientError):
        d.send(j)
    assert j.state.get("slack_file_id")
    fake.handlers["files.completeUploadExternal"] = fake.complete
    res = d.send(j)
    assert fake.methods().count("files.getUploadURLExternal") == 1
    assert res["ts"] == next(iter(fake.files.values()))["share"]["ts"]
    assert "slack_file_id" not in j.state


def test_file_id_is_saved_before_the_share_so_a_crash_does_not_post_twice(fake, clip):
    """Review 2026-11-04: the file id was kept in memory only during the share (and its up-to-30 s wait); a crash
    then posted the clip again. The destination's on_state (the outbox's save_state) now has it on disk first."""
    class PowerCut(BaseException):
        pass
    saved = []
    d, j = dest(fake, on_state=lambda job: saved.append(json.loads(json.dumps(job.state)))), job(clip)

    def complete_then_die(p):
        fake.complete(p)                     # Slack shares it, and the computer dies before the answer is read
        return PowerCut()
    fake.handlers["files.completeUploadExternal"] = complete_then_die
    with pytest.raises(PowerCut):
        d.send(j)
    assert saved and saved[-1].get("slack_file_id")
    fake.handlers["files.completeUploadExternal"] = fake.complete
    res = dest(fake).send(job(clip, state=saved[-1]))                     # a new process, the job as saved
    assert fake.methods().count("files.getUploadURLExternal") == 1 and fake.methods().count(
        "files.completeUploadExternal") == 1
    assert res["ts"] == next(iter(fake.files.values()))["share"]["ts"]


def test_too_large_says_how_to_send_smaller_copies(fake, clip):
    from urllib.error import HTTPError
    fake.upload_error = HTTPError("https://files.slack.com/upload/v1/x", 413, "too large", None, None)
    with pytest.raises(PermanentError) as e:
        dest(fake).send(job(clip))
    assert "streamsplitter setup --section slack" in str(e.value) and "config.toml" not in str(e.value)


def test_completion_that_never_happened_uploads_again(fake, clip):
    fake.handlers["files.completeUploadExternal"] = lambda p: URLError("network is unreachable")
    d, j = dest(fake), job(clip)
    with pytest.raises(TransientError):
        d.send(j)
    fake.handlers["files.completeUploadExternal"] = fake.complete
    res = d.send(j)
    assert fake.methods().count("files.getUploadURLExternal") == 2 and res["ts"]


TRANSIENT = ["ratelimited", "fatal_error", "internal_error", "service_unavailable", "request_timeout"]
PERMANENT = ["invalid_auth", "not_authed", "token_revoked", "account_inactive", "missing_scope", "channel_not_found",
             "not_in_channel", "is_archived", "some_new_error"]


@pytest.mark.parametrize("code", TRANSIENT + PERMANENT)
def test_error_codes_map_to_transient_or_permanent(fake, code):
    fake.handlers["chat.postMessage"] = lambda p: {"ok": False, "error": code, "needed": "chat:write"}
    c = client(fake)
    c.channel_names[CH] = "errata"
    with pytest.raises(TransientError if code in TRANSIENT else PermanentError) as e:
        c.post_message(CH, "hello")
    assert TOKEN not in str(e.value)
    if code == "missing_scope":
        assert "chat:write" in str(e.value)


# ---------------------------------------------------------------------------------------------------------------
# DMs and the stopped-running alarm
# ---------------------------------------------------------------------------------------------------------------

def test_dm_opens_the_conversation_once(fake):
    c = client(fake)
    assert c.dm("U0CONTACT1", "Recording started")["channel"] == "D0CONTACT1"
    c.dm("U0CONTACT1", "Clip posted")
    assert fake.methods().count("conversations.open") == 1
    assert [p["channel"] for p in fake.params("chat.postMessage")] == ["D0CONTACT1", "D0CONTACT1"]


def test_schedule_and_delete_the_alarm(fake):
    c = client(fake)
    at = int(time.time()) + 600
    sid = c.schedule_message("U0CONTACT1", "StreamSplitter stopped running", at)
    sched = fake.params("chat.scheduleMessage")[0]
    assert sid == "Q0ALARM001" and sched["channel"] == "D0CONTACT1" and int(sched["post_at"]) == at
    assert c.delete_scheduled("U0CONTACT1", sid) is True
    dele = fake.params("chat.deleteScheduledMessage")[0]
    assert dele["channel"] == "D0CONTACT1" and dele["scheduled_message_id"] == sid
    # already posted, or due within 60 s: Slack will not delete it
    fake.handlers["chat.deleteScheduledMessage"] = lambda p: {"ok": False, "error": "invalid_scheduled_message_id"}
    assert c.delete_scheduled("D0CONTACT1", sid) is False
    # after a crash: the alarm of the run before is found again
    fake.handlers["chat.scheduledMessages.list"] = lambda p: {
        "ok": True, "response_metadata": {"next_cursor": ""},
        "scheduled_messages": [{"id": sid, "channel_id": p["channel"], "post_at": at, "date_created": at - 600,
                                "text": "StreamSplitter stopped running"}]}
    assert c.list_scheduled("U0CONTACT1") == [{"id": sid, "channel_id": "D0CONTACT1", "post_at": at,
                                               "text": "StreamSplitter stopped running"}]


def test_schedule_keeps_the_alarm_cancellable_and_corrects_a_slow_clock(fake):
    c = client(fake)
    c.schedule_message("D0CONTACT1", "soon", int(time.time()) + 5)
    assert int(fake.params("chat.scheduleMessage")[0]["post_at"]) >= time.time() + slack.MIN_LEAD_S - 2
    ahead = time.time() + 3600                         # Slack's clock is an hour ahead of this computer's
    calls = []

    def schedule(p):
        calls.append(int(p["post_at"]))
        if len(calls) == 1:
            return 200, {"Date": email.utils.formatdate(ahead, usegmt=True)}, {"ok": False, "error": "time_in_past"}
        return {"ok": True, "scheduled_message_id": "Q2", "post_at": calls[-1]}
    fake.handlers["chat.scheduleMessage"] = schedule
    at = int(time.time()) + 600
    assert c.schedule_message("D0CONTACT1", "alarm", at) == "Q2"
    assert abs(calls[1] - (at + 3600)) <= 3


# ---------------------------------------------------------------------------------------------------------------
# people and channels (the setup wizard)
# ---------------------------------------------------------------------------------------------------------------

def _member(uid, name, real, email_addr=None, **kw):
    return {"id": uid, "name": name, "real_name": real,
            "profile": {"real_name": real, "display_name": name, "email": email_addr}, **kw}


def test_find_users_by_email_and_by_name(fake):
    ian = _member("U0IAN00001", "ian", "Ian Holt", "ian@example.org")

    def lookup(p):
        return {"ok": True, "user": ian} if p["email"].lower() == "ian@example.org" else \
            {"ok": False, "error": "users_not_found"}
    pages = {None: ([_member("U0BRIAN001", "brian.k", "Brian Kim"), _member("U0OLD00001", "ian.old", "Ian Old",
                                                                         deleted=True)], "page2"),
             "page2": ([ian, _member("U0BOT00002", "ianbot", "Ian Bot", is_bot=True)], "")}

    def users_list(p):
        members, nxt = pages[p.get("cursor")]
        return {"ok": True, "members": members, "response_metadata": {"next_cursor": nxt}}
    fake.handlers.update({"users.lookupByEmail": lookup, "users.list": users_list})
    c = client(fake)
    assert [u["id"] for u in c.find_users("Ian@Example.org")] == ["U0IAN00001"]
    assert c.find_users("ian@example.org")[0]["email"] == "ian@example.org"
    assert c.find_users("nobody@example.org") == []
    found = c.find_users("IAN")
    assert [u["id"] for u in found] == ["U0IAN00001", "U0BRIAN001"]      # exact name first; no bots, no deleted
    assert found[0]["real_name"] == "Ian Holt"


def test_find_channel_by_name_and_private_join_advice(fake):
    pages = {None: ([{"id": "C0GENERAL1", "name": "general", "is_member": True}], "p2"),
             "p2": ([{"id": "G0ERRATA01", "name": "errata", "is_private": True, "is_member": True}], "")}

    def conv_list(p):
        chans, nxt = pages[p.get("cursor")]
        return {"ok": True, "channels": chans, "response_metadata": {"next_cursor": nxt}}
    fake.handlers.update({"conversations.list": conv_list,
                          "conversations.join": lambda p: {"ok": False,
                                                           "error": "method_not_supported_for_channel_type"}})
    fake.handlers["conversations.info"] = lambda p: (
        {"ok": True, "channel": {"id": p["channel"], "name": "errata", "is_private": True, "is_member": True}}
        if p["channel"] == "G0ERRATA01" else {"ok": False, "error": "channel_not_found"})
    c = client(fake)
    assert c.find_channel("#Errata") == {"id": "G0ERRATA01", "name": "errata", "is_private": True, "is_member": True}
    assert c.find_channel("nope") is None
    assert c.find_channel("G0ERRATA01")["name"] == "errata" and c.find_channel("C0MISSING01") is None
    with pytest.raises(PermanentError) as e:
        c.join_channel("G0ERRATA01")
    assert "#errata" in str(e.value) and "/invite @StreamSplitter" in str(e.value)


def test_auth_test_reports_missing_scopes(fake):
    info = client(fake).auth_test()
    assert info["bot_user_id"] == "U0BOT00001" and info["team"] == "Errata"
    assert "files:read" in info["missing_scopes"] and "chat:write" not in info["missing_scopes"]


# ---------------------------------------------------------------------------------------------------------------
# the smaller copy for phones
# ---------------------------------------------------------------------------------------------------------------

def _fake_ffmpeg(monkeypatch, fail=False):
    runs = []

    def run(args, **kw):
        runs.append(args)
        if fail:
            raise subprocess.CalledProcessError(1, args)
        Path(args[-1]).write_bytes(b"small" * 100)
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(slack.ffmpeg, "find", lambda tool="ffmpeg": "ffmpeg")
    monkeypatch.setattr(slack.subprocess, "run", run)
    return runs


def test_clip_height_copy_is_made_once_and_reused_on_retry(fake, clip, monkeypatch):
    runs = _fake_ffmpeg(monkeypatch)
    d, j = dest(fake, clip_height=720), job(clip)
    fake.handlers["files.getUploadURLExternal"] = lambda p: (503, {}, {"ok": False, "error": "service_unavailable"})
    with pytest.raises(TransientError):
        d.send(j)
    fake.handlers["files.getUploadURLExternal"] = fake.get_upload_url
    res = d.send(j)
    assert len(runs) == 1 and "scale=-2:720,fps=30" in runs[0]
    small = clip.parent / "slack_720p" / clip.name
    assert small.is_file() and fake.uploads == [(res["file_id"], small.stat().st_size)]
    assert sorted(p.name for p in clip.parent.glob("*.mp4")) == [clip.name]   # the follower's clip scan is unchanged


def test_clip_height_failure_sends_the_clip_as_cut(fake, clip, monkeypatch):
    _fake_ffmpeg(monkeypatch, fail=True)
    res = dest(fake, clip_height=720).send(job(clip))
    assert fake.uploads[0][1] == clip.stat().st_size and "720p copy failed" in res["note"]


# ---------------------------------------------------------------------------------------------------------------
# the app manifest
# ---------------------------------------------------------------------------------------------------------------

def test_create_app_url_prefills_the_manifest():
    url = slack.create_app_url()
    parts = urllib.parse.urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://api.slack.com/apps"
    q = dict(urllib.parse.parse_qsl(parts.query))
    assert q["new_app"] == "1"
    m = json.loads(q["manifest_json"])
    assert m == slack.manifest() and len(url) < 4000
    assert m["features"]["bot_user"]["display_name"] == "StreamSplitter"
    assert set(m["oauth_config"]["scopes"]["bot"]) == {
        "chat:write", "files:write", "files:read", "im:write", "im:history", "users:read", "users:read.email",
        "channels:read", "groups:read", "channels:join", "reactions:write"}
    # reactions:write (the :eyes: on a command read) is optional: an app made before it keeps working, and the
    # setup does not ask for it (2026-11-10)
    assert slack.OPTIONAL_SCOPES == ("reactions:write",)
    assert set(slack.REQUIRED_SCOPES) == set(m["oauth_config"]["scopes"]["bot"]) - {"reactions:write"}
    assert m["settings"]["token_rotation_enabled"] is False     # a rotating token would expire every 12 hours
    # the contact replies to the app (Slack commands): the messages tab must accept messages
    assert m["features"]["app_home"]["messages_tab_enabled"] is True
    assert m["features"]["app_home"]["messages_tab_read_only_enabled"] is False
    assert len(m["display_information"]["description"]) <= 140   # Slack's limit


# ---------------------------------------------------------------------------------------------------------------
# reading the contact's replies (Slack commands)
# ---------------------------------------------------------------------------------------------------------------

def test_auth_test_names_im_history_for_an_old_install(fake):
    info = client(fake).auth_test()                          # FakeSlack's token has no im:history
    assert "im:history" in info["missing_scopes"]
    fake.handlers["auth.test"] = lambda p: (200, {"x-oauth-scopes": ",".join(slack.REQUIRED_SCOPES)},
                                            {"ok": True, "user_id": "U0BOT00001"})
    assert client(fake).auth_test()["missing_scopes"] == []


def test_history_and_replies_read_the_dm(fake):
    msgs = [{"type": "message", "ts": "1730000003.000100", "user": "U0CONTACT1", "text": "status",
             "client_msg_id": "x", "blocks": []},
            {"type": "message", "ts": "1730000002.000100", "bot_id": "B1", "text": "Stream started",
             "reply_count": 1, "latest_reply": "1730000004.000100", "thread_ts": "1730000002.000100"}]
    fake.handlers["conversations.history"] = lambda p: {"ok": True, "messages": msgs, "has_more": False}
    fake.handlers["conversations.replies"] = lambda p: {"ok": True, "messages": [
        msgs[1], {"ts": "1730000004.000100", "user": "U0CONTACT1", "text": "help", "thread_ts": p["ts"]}]}
    c = client(fake)
    got = c.history("U0CONTACT1", limit=15)
    assert fake.params("conversations.history")[0]["channel"] == "D0CONTACT1"       # the DM with the contact
    assert int(fake.params("conversations.history")[0]["limit"]) == 15
    assert got[0] == {"ts": "1730000003.000100", "user": "U0CONTACT1", "bot_id": None, "app_id": None,
                      "subtype": None, "text": "status", "thread_ts": None, "reply_count": None,
                      "latest_reply": None}
    rep = c.replies("D0CONTACT1", "1730000002.000100", oldest="1730000003.000100")
    assert fake.params("conversations.replies")[0]["oldest"] == "1730000003.000100"
    assert [r["text"] for r in rep] == ["Stream started", "help"]


def test_history_without_the_scope_says_missing_scope(fake):
    fake.handlers["conversations.history"] = lambda p: {"ok": False, "error": "missing_scope", "needed": "im:history"}
    with pytest.raises(PermanentError) as e:
        client(fake).history("D0CONTACT1")
    assert e.value.code == "missing_scope" and "im:history" in str(e.value) and TOKEN not in str(e.value)


def test_add_reaction_and_an_app_without_the_optional_scope(fake):
    """The :eyes: on a command read (publish/commands.py): reactions.add with the DM's channel and the message ts; an
    app made before 2026-11-10 lacks reactions:write, which comes back as missing_scope (the poller then stops trying
    and just answers)."""
    fake.handlers["reactions.add"] = lambda p: {"ok": True}
    c = client(fake)
    c.add_reaction("U0CONTACT1", "1730000003.000100", "eyes")
    p = fake.params("reactions.add")[0]
    assert (p["channel"], p["timestamp"], p["name"]) == ("D0CONTACT1", "1730000003.000100", "eyes")
    fake.handlers["reactions.add"] = lambda p: {"ok": False, "error": "missing_scope", "needed": "reactions:write"}
    with pytest.raises(PermanentError) as e:
        c.add_reaction("D0CONTACT1", "1730000003.000100", "eyes")
    assert e.value.code == "missing_scope"


def test_setup_test_post_ends_with_the_commands():
    text = slack.TEST_FORMAT
    assert text.startswith("StreamSplitter setup test: if you can see this sample match video")
    last = text.splitlines()[-1]
    for cmd in ("`help`", "`status`", "`split 2026mttd`", "`set stream &lt;link&gt;`", "`set stream auto`",
                "`pause youtube`", "`resume youtube`", "`settings`"):
        assert cmd in last
    assert "<link>" not in text                               # escaped, or Slack would read it as a link
