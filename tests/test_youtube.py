"""YouTube client and destination against a fake YouTube (no network, no Google): resumable upload in pieces, resume
after a drop or a restart, expired sessions, error mapping, token renewal, quota, and idempotent sends.

    .venv\\Scripts\\python.exe -m pytest -p no:cacheprovider tests\\test_youtube.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from requests.structures import CaseInsensitiveDict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from streamsplitter.publish import templates  # noqa: E402
from streamsplitter.publish import youtube as yt  # noqa: E402
from streamsplitter.publish.base import Job, Match, PermanentError, TransientError  # noqa: E402

KIB = 1024
PIECE = 256 * KIB
CLIENT = {"installed": {"client_id": "cid.apps.googleusercontent.com", "client_secret": "client-secret-x",
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": yt.TOKEN_URI,
                        "redirect_uris": ["http://localhost"]}}
SECRETS = ("tok0", "refresh-secret-x", "client-secret-x")
UTC = timezone.utc


class Resp:
    def __init__(self, status: int, body=None, headers: dict | None = None):
        self.status_code = status
        self.headers = CaseInsensitiveDict(headers or {})
        self.content = b"" if body is None else json.dumps(body).encode()
        self.text = self.content.decode()

    def json(self):
        if not self.content:
            raise ValueError("no body")
        return json.loads(self.content)


def gerr(status: int, reason: str, message: str = "refused") -> Resp:
    return Resp(status, {"error": {"code": status, "message": message,
                                   "errors": [{"reason": reason, "message": message, "domain": "youtube"}]}})


class FakeYouTube:
    """Just enough of YouTube's upload and Data API, and Google's token endpoint, to drive the client; with faults."""

    def __init__(self):
        self.calls: list[dict] = []
        self.valid_tokens = {"tok0"}
        self.refreshes = 0
        self.refresh_error: str | None = None
        self.always_401 = False
        self.sessions: dict[str, dict] = {}
        self.videos: dict[str, dict] = {}
        self.playlists: dict[str, dict] = {"PL1": {"title": "2026 Madtown Throwdown", "privacy": "unlisted"}}
        self.playlist_items: list[tuple[str, str]] = []
        self.faults: list = []              # fault(method, path, query) -> Resp | Exception | None (used once)
        self.drop_at: int | None = None     # cut the connection once a session holds this many bytes (kept)
        self.lose_final_answer = False      # the last piece lands, the answer is lost
        self.lose_playlist_answer = False
        self.expire_on_put: int | None = None   # the n-th piece PUT finds the session gone (410)
        self.lock_private = False           # an unaudited project: YouTube keeps the upload private
        self._puts = 0

    # helpers for assertions
    def of(self, method: str, path: str) -> list[dict]:
        return [c for c in self.calls if c["method"] == method and c["path"] == path]

    def piece_ranges(self) -> list[str]:
        return [c["headers"]["Content-Range"] for c in self.of("PUT", "/upload/youtube/v3/videos")]

    def close(self):
        pass

    def request(self, method, url, headers=None, timeout=None, params=None, json=None, data=None,
                allow_redirects=True, **kw):
        headers = dict(headers or {})
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        q.update({k: str(v) for k, v in (params or {}).items()})
        self.calls.append({"method": method, "path": u.path, "query": q, "headers": headers, "json": json,
                           "data": data, "allow_redirects": allow_redirects})
        if url == yt.TOKEN_URI:
            return self._token()
        for i, fault in enumerate(self.faults):
            r = fault(method, u.path, q)
            if r is not None:
                del self.faults[i]
                if isinstance(r, Exception):
                    raise r
                return r
        token = headers.get("Authorization", "").removeprefix("Bearer ")
        if self.always_401 or token not in self.valid_tokens:
            return gerr(401, "authError", "Invalid Credentials")
        if u.path == "/upload/youtube/v3/videos":
            return self._upload(method, q, headers, json, data)
        return self._api(method, u.path.removeprefix("/youtube/v3/"), q, json)

    def _token(self):
        if self.refresh_error:
            return Resp(400, {"error": self.refresh_error, "error_description": "Token has been expired or revoked."})
        self.refreshes += 1
        tok = f"tok{self.refreshes}"
        self.valid_tokens.add(tok)
        return Resp(200, {"access_token": tok, "expires_in": 3599, "token_type": "Bearer"})

    def _video(self, vid: str) -> dict:
        v = self.videos[vid]
        return {"kind": "youtube#video", "id": vid, "snippet": v["meta"]["snippet"],
                "status": {**v["meta"]["status"], "uploadStatus": "uploaded"}}

    def _upload(self, method, q, headers, body, data):
        if method == "POST":
            assert q["uploadType"] == "resumable"
            sid = f"s{len(self.sessions) + 1}"
            self.sessions[sid] = {"size": int(headers["X-Upload-Content-Length"]), "meta": body, "query": q,
                                  "data": bytearray(), "gone": None, "video": None}
            return Resp(200, None, {"Location": f"https://www.googleapis.com/upload/youtube/v3/videos"
                                                f"?uploadType=resumable&upload_id={sid}"})
        s = self.sessions[q["upload_id"]]
        if s["gone"]:
            return gerr(s["gone"], "notFound", "upload session not found")
        rng = headers["Content-Range"]
        have = s["data"]
        if rng.startswith("bytes */"):
            if s["video"]:
                return Resp(200, self._video(s["video"]))
            return Resp(308, None, {"Range": f"bytes=0-{len(have) - 1}"} if have else {})
        self._puts += 1
        if self.expire_on_put == self._puts:
            s["gone"] = 410
            return gerr(410, "notFound", "upload session expired")
        span, size = rng.removeprefix("bytes ").split("/")
        a, b = (int(x) for x in span.split("-"))
        assert int(size) == s["size"] and b - a + 1 == len(data) == int(headers["Content-Length"])
        if a > len(have):
            return gerr(400, "badContent", "gap in upload")
        del have[a:]
        if self.drop_at is not None and len(have) < self.drop_at <= len(have) + len(data):
            have += data[:self.drop_at - len(have)]
            self.drop_at = None
            raise requests.exceptions.ConnectionError("connection reset by peer")
        have += data
        if len(have) < s["size"]:
            return Resp(308, None, {"Range": f"bytes=0-{len(have) - 1}"})
        vid = f"v{len(self.videos) + 1}"
        meta = json_copy(s["meta"])
        if self.lock_private:
            meta["status"]["privacyStatus"] = "private"
        self.videos[vid] = {"bytes": bytes(have), "meta": meta}
        s["video"] = vid
        if self.lose_final_answer:
            self.lose_final_answer = False
            raise requests.exceptions.ReadTimeout("read timed out")
        return Resp(201, self._video(vid))

    def _api(self, method, res, q, body):
        if (method, res) == ("GET", "channels"):
            return Resp(200, {"items": [{"id": "UC5026", "snippet": {"title": "Team 5026", "customUrl": "@team5026"}}]})
        if (method, res) == ("GET", "playlists") and "id" in q:
            p = self.playlists.get(q["id"])
            return Resp(200, {"items": [{"id": q["id"], "snippet": {"title": p["title"],
                                                                    "description": p.get("description", "")},
                                         "status": {"privacyStatus": p["privacy"]}}] if p else []})
        if (method, res) == ("PUT", "playlists"):
            p = self.playlists[body["id"]]
            assert q["part"] == "snippet,status" and body["snippet"]["title"]      # YouTube requires the title
            p.update(title=body["snippet"]["title"], description=body["snippet"].get("description", ""),
                     privacy=body["status"]["privacyStatus"])
            return Resp(200, {"id": body["id"], "snippet": body["snippet"], "status": body["status"]})
        if (method, res) == ("GET", "playlists"):
            ids = sorted(self.playlists)
            start = int(q.get("pageToken") or 0)
            page = ids[start:start + 2]             # two per page, to exercise paging
            out = {"items": [{"id": i, "snippet": {"title": self.playlists[i]["title"]},
                              "status": {"privacyStatus": self.playlists[i]["privacy"]}} for i in page]}
            if start + 2 < len(ids):
                out["nextPageToken"] = str(start + 2)
            return Resp(200, out)
        if (method, res) == ("POST", "playlists"):
            pid = f"PL{len(self.playlists) + 1}"
            self.playlists[pid] = {"title": body["snippet"]["title"], "privacy": body["status"]["privacyStatus"]}
            return Resp(200, {"id": pid, "snippet": body["snippet"], "status": body["status"]})
        if (method, res) == ("POST", "playlistItems"):
            pid, vid = body["snippet"]["playlistId"], body["snippet"]["resourceId"]["videoId"]
            if pid not in self.playlists:
                return gerr(404, "playlistNotFound")
            if vid not in self.videos:
                return gerr(404, "videoNotFound")
            self.playlist_items.append((pid, vid))
            if self.lose_playlist_answer:
                self.lose_playlist_answer = False
                raise requests.exceptions.ConnectionError("connection reset by peer")
            return Resp(200, {"id": f"item{len(self.playlist_items)}"})
        if (method, res) == ("GET", "playlistItems"):
            hits = [i for i, (p, v) in enumerate(self.playlist_items)
                    if p == q["playlistId"] and v == q.get("videoId", v)]
            return Resp(200, {"items": [{"id": f"item{i + 1}"} for i in hits]})
        if (method, res) == ("PUT", "videos"):
            v = self.videos[body["id"]]
            v["meta"]["snippet"] = body["snippet"]
            return Resp(200, self._video(body["id"]))
        if (method, res) == ("GET", "videos"):
            return Resp(200, {"items": [self._video(q["id"])] if q["id"] in self.videos else []})
        if (method, res) == ("DELETE", "videos"):
            if self.videos.pop(q["id"], None) is None:
                return gerr(404, "videoNotFound")
            return Resp(204)
        raise AssertionError(f"unexpected call {method} {res}")


def json_copy(x):
    return json.loads(json.dumps(x))


def fail(method: str, path: str, result, when=None):
    """A fault for FakeYouTube.faults: the first matching call gets `result` (a Resp or an exception)."""
    def f(m, p, q):
        if m == method and p == path and (when is None or when(q)):
            return result
        return None
    return f


def token_json(token="tok0", expires_in=3600) -> str:
    expiry = (datetime.now(UTC) + timedelta(seconds=expires_in)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return json.dumps({"token": token, "refresh_token": "refresh-secret-x", "client_id": CLIENT["installed"]["client_id"],
                       "client_secret": "client-secret-x", "scopes": yt.SCOPES, "expiry": expiry})


def client(fake, tmp_path, *, expires_in=3600, on_token=None, clock=time.time) -> yt.YouTubeClient:
    quota = yt.QuotaTracker(tmp_path / "youtube_quota.json", clock=clock)
    return yt.YouTubeClient(token_json(expires_in=expires_in), CLIENT, on_token, session=fake, quota=quota,
                            chunk_size=PIECE, clock=clock)


@pytest.fixture
def clip(tmp_path) -> Path:
    p = tmp_path / "2026mttd_qm12.mp4"
    p.write_bytes(os.urandom(3 * PIECE + 1000))         # four pieces: three whole, one short
    return p


def no_secrets(text: str) -> bool:
    return not any(s in text for s in SECRETS)


def match(**kw) -> Match:
    base = dict(key="2026mttd_qm12", name="2026mttd_qm12", event_key="2026mttd", event_name="Madtown Throwdown",
                year=2026, comp_level="qm", set_number=1, match_number=12, red=["5026", "254", "1678"],
                blue=["604", "8033", "199"], red_score=112, blue_score=98)
    return Match(**{**base, **kw})


# --- resumable upload -------------------------------------------------------------------------------------------------

def test_upload_in_pieces(tmp_path, clip):
    fake = FakeYouTube()
    c = client(fake, tmp_path)
    state, seen, saves = {}, [], []
    res = c.upload(str(clip), "Qualification 12 - 2026 Madtown Throwdown", "a description", "unlisted", state,
                   progress=lambda sent, total: seen.append((sent, total)), on_state=lambda: saves.append(dict(state)))
    size = clip.stat().st_size
    vid = res["video_id"]
    assert fake.videos[vid]["bytes"] == clip.read_bytes()
    assert res == {"video_id": vid, "url": f"https://www.youtube.com/watch?v={vid}", "privacy": "unlisted",
                   "locked_private": False}
    assert fake.piece_ranges() == [f"bytes 0-{PIECE - 1}/{size}", f"bytes {PIECE}-{2 * PIECE - 1}/{size}",
                                   f"bytes {2 * PIECE}-{3 * PIECE - 1}/{size}", f"bytes {3 * PIECE}-{size - 1}/{size}"]
    assert all(not p["allow_redirects"] for p in fake.of("PUT", "/upload/youtube/v3/videos"))   # 308 is not a redirect
    s = fake.sessions["s1"]
    assert s["meta"]["status"] == {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False, "embeddable": True}
    assert s["meta"]["snippet"]["categoryId"] == yt.CATEGORY_ID
    assert s["query"]["part"] == "snippet,status" and s["query"]["notifySubscribers"] == "false"
    assert seen == [(PIECE, size), (2 * PIECE, size), (3 * PIECE, size), (size, size)]
    assert saves[0]["upload_uri"] and saves[0]["upload_offset"] == 0        # saved before the first byte goes
    assert state["video_id"] == vid and "upload_uri" not in state
    q = c.quota.status()
    assert q["uploads"] == 1 and q["units"] == 0                    # videos.insert has its own bucket


def test_resume_after_a_drop_asks_youtube_where_it_got_to(tmp_path, clip):
    fake = FakeYouTube()
    c = client(fake, tmp_path)
    state = {}
    fake.drop_at = PIECE + 100 * KIB                 # mid second piece: YouTube keeps the 100 KiB that arrived
    with pytest.raises(TransientError) as e:
        c.upload(str(clip), "t", "d", "unlisted", state)
    assert no_secrets(str(e.value)) and "upload_id" not in str(e.value)
    assert state["upload_uri"] and state["upload_offset"] == PIECE
    n = len(fake.piece_ranges())
    res = c.upload(str(clip), "t", "d", "unlisted", state)
    size = clip.stat().st_size
    after = fake.piece_ranges()[n:]
    assert after[0] == f"bytes */{size}"             # the empty PUT that asks for the received range
    assert after[1] == f"bytes {PIECE + 100 * KIB}-{PIECE + 100 * KIB + PIECE - 1}/{size}"
    assert len(fake.of("POST", "/upload/youtube/v3/videos")) == 1
    assert fake.videos[res["video_id"]]["bytes"] == clip.read_bytes()


def test_restart_resumes_the_stored_session(tmp_path, clip):
    fake = FakeYouTube()
    job = Job(id="2026mttd_qm12:score:youtube", destination="youtube", kind="score", path=str(clip), match=match())
    fake.drop_at = 2 * PIECE
    with pytest.raises(TransientError):
        client(fake, tmp_path).upload(str(clip), "t", "d", "unlisted", job.state)
    stored = json.dumps(job.to_dict())               # what the outbox keeps on disk
    job2 = Job.from_dict(json.loads(stored))         # a new process
    c2 = client(fake, tmp_path)
    n = len(fake.calls)
    res = c2.upload(str(clip), "t", "d", "unlisted", job2.state)
    assert fake.calls[n]["headers"]["Content-Range"] == f"bytes */{clip.stat().st_size}"
    assert len(fake.of("POST", "/upload/youtube/v3/videos")) == 1
    assert fake.videos[res["video_id"]]["bytes"] == clip.read_bytes()


@pytest.mark.parametrize("gone", [404, 410])
def test_expired_session_starts_a_new_one(tmp_path, clip, gone):
    fake = FakeYouTube()
    c = client(fake, tmp_path)
    state = {}
    fake.drop_at = PIECE
    with pytest.raises(TransientError):
        c.upload(str(clip), "t", "d", "unlisted", state)
    fake.sessions["s1"]["gone"] = gone
    res = c.upload(str(clip), "t", "d", "unlisted", state)
    assert len(fake.of("POST", "/upload/youtube/v3/videos")) == 2 and state["upload_sessions"] == 2
    assert fake.videos[res["video_id"]]["bytes"] == clip.read_bytes()
    assert fake.piece_ranges()[-4].startswith("bytes 0-")


def test_session_expiring_mid_upload_restarts_within_the_attempt(tmp_path, clip):
    fake = FakeYouTube()
    fake.expire_on_put = 2
    res = client(fake, tmp_path).upload(str(clip), "t", "d", "unlisted", {})
    assert len(fake.sessions) == 2 and fake.videos[res["video_id"]]["bytes"] == clip.read_bytes()


def test_finished_upload_whose_answer_was_lost_is_not_uploaded_twice(tmp_path, clip):
    fake = FakeYouTube()
    c = client(fake, tmp_path)
    state = {}
    fake.lose_final_answer = True
    with pytest.raises(TransientError):
        c.upload(str(clip), "t", "d", "unlisted", state)
    res = c.upload(str(clip), "t", "d", "unlisted", state)
    assert len(fake.videos) == 1 and len(fake.sessions) == 1 and res["video_id"] == "v1"


def test_server_error_on_a_piece_is_transient_and_resumes(tmp_path, clip):
    fake = FakeYouTube()
    c = client(fake, tmp_path)
    state = {}
    fake.faults.append(fail("PUT", "/upload/youtube/v3/videos", Resp(503),
                            when=lambda q: len(fake.of("PUT", "/upload/youtube/v3/videos")) == 2))
    with pytest.raises(TransientError):
        c.upload(str(clip), "t", "d", "unlisted", state)
    res = c.upload(str(clip), "t", "d", "unlisted", state)
    assert fake.videos[res["video_id"]]["bytes"] == clip.read_bytes() and len(fake.sessions) == 1


def test_changed_file_gets_a_new_session(tmp_path, clip):
    fake = FakeYouTube()
    c = client(fake, tmp_path)
    state = {}
    fake.drop_at = PIECE
    with pytest.raises(TransientError):
        c.upload(str(clip), "t", "d", "unlisted", state)
    clip.write_bytes(os.urandom(PIECE + 5))
    res = c.upload(str(clip), "t", "d", "unlisted", state)
    assert len(fake.sessions) == 2 and fake.videos[res["video_id"]]["bytes"] == clip.read_bytes()


def test_private_lock_is_reported(tmp_path, clip):
    fake = FakeYouTube()
    fake.lock_private = True
    res = client(fake, tmp_path).upload(str(clip), "t", "d", "unlisted", {})
    assert res["privacy"] == "private" and res["locked_private"] is True


def test_missing_clip_is_permanent(tmp_path):
    with pytest.raises(PermanentError):
        client(FakeYouTube(), tmp_path).upload(str(tmp_path / "gone.mp4"), "t", "d", "unlisted", {})


# --- errors -----------------------------------------------------------------------------------------------------------

def test_quota_exceeded_waits_until_pacific_midnight(tmp_path, clip):
    now = datetime(2026, 11, 6, 20, 0, tzinfo=UTC).timestamp()           # 12:00 PST, the Friday of the event
    fake = FakeYouTube()
    fake.faults.append(fail("POST", "/upload/youtube/v3/videos",
                            gerr(403, "quotaExceeded", "The request cannot be completed because you have exceeded "
                                                       "your quota.")))
    c = client(fake, tmp_path, clock=lambda: now)
    state = {}
    with pytest.raises(TransientError) as e:
        c.upload(str(clip), "t", "d", "unlisted", state)
    assert e.value.retry_after == 12 * 3600 + yt.QUOTA_RESET_MARGIN_S
    assert "daily API quota ran out (100 uploads a day" in str(e.value)
    q = c.quota.status()
    assert q["day"] == "2026-11-06" and q["exhausted"] == ["uploads"] and q["blocked"] and q["uploads_left"] == 0
    assert q["remaining"] == 10_000                                 # the general bucket is untouched
    assert "upload_uri" not in state


def test_channel_upload_limit_waits_until_pacific_midnight(tmp_path, clip):
    now = datetime(2026, 11, 7, 3, 0, tzinfo=UTC).timestamp()            # 19:00 PST Friday
    fake = FakeYouTube()
    fake.faults.append(fail("POST", "/upload/youtube/v3/videos", gerr(400, "uploadLimitExceeded")))
    c = client(fake, tmp_path, clock=lambda: now)
    with pytest.raises(TransientError) as e:
        c.upload(str(clip), "t", "d", "unlisted", {})
    assert e.value.retry_after == 5 * 3600 + yt.QUOTA_RESET_MARGIN_S
    assert "daily upload limit" in str(e.value) and "not the API quota" in str(e.value)
    assert c.quota.status()["exhausted"] == ["channel"] and c.quota.status()["blocked"]


@pytest.mark.parametrize("utc, hours", [
    (datetime(2026, 11, 6, 20, 0, tzinfo=UTC), 12.0),        # PST
    (datetime(2026, 7, 1, 12, 0, tzinfo=UTC), 19.0),         # 05:00 PDT
    (datetime(2026, 11, 1, 7, 30, tzinfo=UTC), 24.5),        # 00:30 PDT on the day daylight time ends (25 h day)
    (datetime(2026, 3, 8, 7, 30, tzinfo=UTC), 0.5),          # 23:30 PST the night before daylight time
    (datetime(2026, 3, 8, 9, 30, tzinfo=UTC), 21.5),         # 01:30 PST on the day daylight time starts (23 h day)
])
def test_seconds_to_pacific_midnight(utc, hours):
    assert yt.seconds_to_pacific_midnight(utc.timestamp()) == hours * 3600


def test_us_rule_and_pacific_day():
    assert yt._us_pacific_offset(datetime(2026, 11, 1, 8, 59, tzinfo=UTC)) == timedelta(hours=-7)
    assert yt._us_pacific_offset(datetime(2026, 11, 1, 9, 0, tzinfo=UTC)) == timedelta(hours=-8)
    assert yt._us_pacific_offset(datetime(2026, 3, 8, 10, 0, tzinfo=UTC)) == timedelta(hours=-7)
    assert yt.pacific_day(datetime(2026, 11, 7, 7, 59, tzinfo=UTC).timestamp()) == "2026-11-06"
    assert yt.pacific_day(datetime(2026, 11, 7, 8, 0, tzinfo=UTC).timestamp()) == "2026-11-07"


@pytest.mark.parametrize("result, kind, words", [
    (gerr(403, "rateLimitExceeded"), TransientError, "rateLimitExceeded"),
    (gerr(403, "userRateLimitExceeded"), TransientError, "userRateLimitExceeded"),
    (Resp(429, None, {"Retry-After": "30"}), TransientError, "429"),
    (Resp(503), TransientError, "503"),
    (gerr(500, "backendError"), TransientError, "500"),
    (requests.exceptions.ConnectTimeout("timed out"), TransientError, "no connection"),
    (requests.exceptions.ConnectionError("name resolution failed"), TransientError, "no connection"),
    (gerr(403, "forbidden"), PermanentError, "owner or a manager"),
    (gerr(403, "insufficientPermissions"), PermanentError, "streamsplitter setup --section youtube"),
    (gerr(403, "youtubeSignupRequired"), PermanentError, "no YouTube channel"),
    (gerr(403, "accessNotConfigured"), PermanentError, "YouTube Data API v3 is not enabled"),
    (gerr(400, "invalidTitle"), PermanentError, "title format"),
    (gerr(400, "invalidDescription"), PermanentError, "description format"),
])
def test_error_mapping(tmp_path, clip, result, kind, words):
    fake = FakeYouTube()
    fake.faults.append(fail("POST", "/upload/youtube/v3/videos", result))
    with pytest.raises((TransientError, PermanentError)) as e:
        client(fake, tmp_path).upload(str(clip), "t", "d", "unlisted", {})
    assert type(e.value) is kind and words in str(e.value) and no_secrets(str(e.value))
    if isinstance(result, Resp) and result.status_code == 429:
        assert e.value.retry_after == 30


def test_401_renews_the_token_then_succeeds(tmp_path):
    fake = FakeYouTube()
    fake.valid_tokens = set()                        # the stored access token was revoked; the refresh token works
    renewed = []
    c = client(fake, tmp_path, on_token=renewed.append)
    assert c.channel() == {"id": "UC5026", "title": "Team 5026", "url": "https://www.youtube.com/@team5026"}
    assert fake.refreshes == 1 and len(renewed) == 1
    tok = json.loads(renewed[0])
    assert tok["token"] == "tok1" and tok["refresh_token"] == "refresh-secret-x"


def test_expired_token_is_renewed_before_the_call(tmp_path):
    fake = FakeYouTube()
    renewed = []
    c = client(fake, tmp_path, expires_in=-60, on_token=renewed.append)
    c.channel()
    assert [x["path"] for x in fake.calls] == ["/token", "/youtube/v3/channels"] and len(renewed) == 1


def test_401_that_persists_means_log_in_again(tmp_path):
    fake = FakeYouTube()
    fake.always_401 = True
    with pytest.raises(PermanentError) as e:
        client(fake, tmp_path).channel()
    assert "streamsplitter setup --section youtube" in str(e.value) and fake.refreshes == 1
    assert no_secrets(str(e.value))


def test_revoked_login_is_permanent(tmp_path):
    fake = FakeYouTube()
    fake.refresh_error = "invalid_grant"
    with pytest.raises(PermanentError) as e:
        client(fake, tmp_path, expires_in=-60).channel()
    assert "log in to YouTube again" in str(e.value) and no_secrets(str(e.value))


def test_login_asks_for_a_refresh_token_and_the_channel_chooser(monkeypatch):
    # the browser flow itself is Google's; only what StreamSplitter asks of it is checked (nothing reaches Google)
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    seen = {}

    def fake_run(self, **kw):
        seen.update(kw, scopes=list(self.oauth2session.scope))
        return Credentials("tok0", refresh_token="refresh-secret-x", client_id="cid", client_secret="cs",
                           token_uri=yt.TOKEN_URI, scopes=yt.SCOPES)
    monkeypatch.setattr(InstalledAppFlow, "run_local_server", fake_run)
    tok = json.loads(yt.login(CLIENT, open_browser=False))
    assert tok["refresh_token"] == "refresh-secret-x" and tok["scopes"] == yt.SCOPES
    assert seen["port"] == 0 and seen["host"] == "127.0.0.1" and seen["prompt"] == "consent select_account"
    assert seen["access_type"] == "offline" and seen["scopes"] == ["https://www.googleapis.com/auth/youtube"]

    monkeypatch.setattr(InstalledAppFlow, "run_local_server",
                        lambda self, **kw: Credentials("tok0", client_id="cid", client_secret="cs"))
    with pytest.raises(yt.LoginError, match="no refresh token"):
        yt.login(CLIENT, open_browser=False)


def test_web_client_is_refused_with_a_fix():
    with pytest.raises(yt.LoginError) as e:
        yt.check_client_config({"web": {"client_id": "x", "client_secret": "y"}})
    assert "Desktop app" in str(e.value)
    assert yt.check_client_config(CLIENT) is CLIENT


# --- playlists and quota ----------------------------------------------------------------------------------------------

def test_find_and_ensure_playlist(tmp_path):
    fake = FakeYouTube()
    fake.playlists.update({"PLa": {"title": "Robots", "privacy": "public"},
                           "PLb": {"title": "2025 Madtown Throwdown", "privacy": "unlisted"}})
    c = client(fake, tmp_path)
    assert c.find_playlist(" 2026 madtown throwdown ")["id"] == "PL1"      # on the second page
    made = yt.ensure_playlist(c, "2026 Chezy Champs", "Matches", "unlisted")
    assert made["url"] == f"https://www.youtube.com/playlist?list={made['id']}" and made["privacy"] == "unlisted"
    assert yt.ensure_playlist(c, "2026 Chezy Champs")["id"] == made["id"]
    assert len(fake.of("POST", "/youtube/v3/playlists")) == 1


def test_set_playlist_privacy_keeps_its_title_and_description(tmp_path):
    fake = FakeYouTube()
    fake.playlists["PL1"]["description"] = "Matches from the 2026 Madtown Throwdown."
    c = client(fake, tmp_path)
    got = c.set_playlist_privacy("PL1", "private")
    assert got["id"] == "PL1" and got["privacy"] == "private"
    assert fake.playlists["PL1"] == {"title": "2026 Madtown Throwdown", "privacy": "private",
                                     "description": "Matches from the 2026 Madtown Throwdown."}
    q = c.quota.status()
    assert q["calls"] == {"playlists.list": 1, "playlists.update": 1} and q["units"] == 51
    with pytest.raises(PermanentError):
        c.set_playlist_privacy("PL1", "friends only")
    with pytest.raises(PermanentError):
        c.set_playlist_privacy("PLgone", "private")


def test_quota_counts_only_calls_that_reached_youtube(tmp_path):
    """Review 2026-11-04: a call was counted before it was sent, so retries while offline used up the estimate and
    could DM a false "daily limit used up". Now: an answer (any status) or a lost answer counts; no connection
    does not."""
    fake = FakeYouTube()
    c = client(fake, tmp_path)
    for _ in range(5):
        fake.faults.append(fail("GET", "/youtube/v3/channels", requests.exceptions.ConnectionError("offline")))
        with pytest.raises(TransientError):
            c.channel()
    assert c.quota.status()["calls"] == {}
    fake.faults.append(fail("GET", "/youtube/v3/channels", Resp(503)))
    with pytest.raises(TransientError):
        c.channel()
    fake.faults.append(fail("GET", "/youtube/v3/channels", requests.exceptions.ReadTimeout("read timed out")))
    with pytest.raises(TransientError):
        c.channel()
    c.channel()
    assert c.quota.status()["calls"] == {"channels.list": 3}


def test_quota_tracker_counts_per_pacific_day(tmp_path):
    now = [datetime(2026, 11, 6, 20, 0, tzinfo=UTC).timestamp()]
    path = tmp_path / "outbox" / "youtube_quota.json"
    q = yt.QuotaTracker(path, clock=lambda: now[0])
    q.add("videos.insert")
    q.add("playlistItems.insert")
    s = yt.QuotaTracker(path, clock=lambda: now[0]).status()                # read back by another instance
    assert (s["uploads"], s["uploads_remaining"], s["units"], s["remaining"]) == (1, 99, 50, 9950)
    assert s["uploads_left"] == 99 and not s["blocked"] and s["costs_confirmed"]
    assert s["calls"] == {"videos.insert": 1, "playlistItems.insert": 1} and s["resets_in_s"] == 12 * 3600
    for _ in range(99):
        q.add("videos.update")                                              # 4950 more units: 5000 left
    assert q.status()["uploads_left"] == 50                                 # the general bucket binds now
    now[0] += 12 * 3600                                                     # past midnight Pacific
    assert q.status()["units"] == 0 and q.status()["day"] == "2026-11-07"
    assert "2026-11-06" in json.loads(path.read_text())["days"]


# --- destination ------------------------------------------------------------------------------------------------------

def destination(c, **kw) -> yt.YouTubeDestination:
    return yt.YouTubeDestination(c, "PL1", templates.TITLE_FORMAT, templates.DESCRIPTION_FORMAT, **kw)


def job_for(clip, kind="score", **mkw) -> Job:
    m = match(**mkw)
    return Job(id=f"{m.name}:{kind}:youtube", destination="youtube", kind=kind, path=str(clip), match=m)


def test_destination_uploads_then_adds_to_playlist_once(tmp_path, clip):
    fake = FakeYouTube()
    saves = []
    dest = destination(client(fake, tmp_path), on_state=lambda j: saves.append(dict(j.state)))
    job = job_for(clip)
    res = dest.send(job)
    assert res["playlist_id"] == "PL1" and fake.playlist_items == [("PL1", res["video_id"])]
    snippet = fake.videos[res["video_id"]]["meta"]["snippet"]
    assert snippet["title"] == "Qualification 12 - 2026 Madtown Throwdown"
    assert snippet["description"].splitlines()[:4] == [
        "Qualification 12 - 2026 Madtown Throwdown", "Red (Teams 5026, 254, 1678) - 112",
        "Blue (Teams 604, 8033, 199) - 98", "https://www.thebluealliance.com/match/2026mttd_qm12"]
    assert saves and saves[-1]["playlist_done"] is True
    n = len(fake.calls)
    assert dest.send(job)["video_id"] == res["video_id"] and len(fake.calls) == n      # nothing left to do


def test_destination_with_video_already_uploaded_only_adds_to_playlist(tmp_path, clip):
    fake = FakeYouTube()
    c = client(fake, tmp_path)
    vid = c.upload(str(clip), "t", "d", "unlisted", {})["video_id"]
    n = len(fake.calls)
    job = job_for(clip)
    job.state = {"video_id": vid, "privacy": "unlisted"}
    destination(c).send(job)
    assert [(x["method"], x["path"]) for x in fake.calls[n:]] == [("POST", "/youtube/v3/playlistItems")]
    assert fake.playlist_items == [("PL1", vid)]


def test_destination_playlist_add_with_lost_answer_is_not_repeated(tmp_path, clip):
    fake = FakeYouTube()
    dest = destination(client(fake, tmp_path))
    job = job_for(clip)
    fake.lose_playlist_answer = True
    with pytest.raises(TransientError):
        dest.send(job)
    res = dest.send(job)
    assert fake.playlist_items == [("PL1", res["video_id"])] and len(fake.videos) == 1
    assert len(fake.of("GET", "/youtube/v3/playlistItems")) == 1


def test_destination_test_upload_title(tmp_path, clip):
    fake = FakeYouTube()
    res = destination(client(fake, tmp_path)).send(job_for(clip, kind="test"))
    assert fake.videos[res["video_id"]]["meta"]["snippet"]["title"] == yt.TEST_TITLE and res["test"] is True


def test_destination_brings_the_description_up_to_date_when_scores_arrive(tmp_path, clip):
    fake = FakeYouTube()
    dest = destination(client(fake, tmp_path))
    job = job_for(clip, red_score=None, blue_score=None)
    vid = dest.send(job)["video_id"]
    lines = fake.videos[vid]["meta"]["snippet"]["description"].splitlines()
    assert lines[1:3] == ["Red (Teams 5026, 254, 1678)", "Blue (Teams 604, 8033, 199)"]     # no " - None"
    job.match.red_score, job.match.blue_score = 101, 99
    dest.send(job)
    (update,) = fake.of("PUT", "/youtube/v3/videos")
    assert update["json"]["snippet"]["categoryId"] == yt.CATEGORY_ID and update["query"]["part"] == "snippet"
    lines = fake.videos[vid]["meta"]["snippet"]["description"].splitlines()
    assert lines[1:3] == ["Red (Teams 5026, 254, 1678) - 101", "Blue (Teams 604, 8033, 199) - 99"]
    dest.send(job)
    assert len(fake.of("PUT", "/youtube/v3/videos")) == 1


def test_destination_refuses_a_bad_format(tmp_path):
    with pytest.raises(PermanentError) as e:
        yt.YouTubeDestination(client(FakeYouTube(), tmp_path), "PL1", "{colour} match", templates.DESCRIPTION_FORMAT)
    assert "{colour}" in str(e.value)
