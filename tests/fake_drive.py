"""A fake Google Drive (API v3 and Google's token endpoint) for the Drive tests and the wizard tests: about.get,
files.get/list/create/update, resumable uploads, with faults. Only what StreamSplitter calls; no network.

Queries are read as StreamSplitter writes them: clauses joined by " and ", each one of "'<id>' in parents",
"trashed = false", "mimeType = '...'", "name = '...'", "appProperties has { key='k' and value='v' }".
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlparse

import requests
from requests.structures import CaseInsensitiveDict

TOKEN_URI = "https://oauth2.googleapis.com/token"
FOLDER = "application/vnd.google-apps.folder"


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
                                   "errors": [{"reason": reason, "message": message, "domain": "global"}]}})


def fail(method: str, path: str, result, when=None):
    """A fault for FakeDrive.faults: the first matching call gets `result` (a Resp or an exception)."""
    def f(m, p, q):
        if m == method and p.startswith(path) and (when is None or when(q)):
            return result
        return None
    return f


_CLAUSE = re.compile(r"'((?:[^'\\]|\\.)*)' in parents|trashed = (true|false)|mimeType = '((?:[^'\\]|\\.)*)'|"
                     r"name = '((?:[^'\\]|\\.)*)'|appProperties has \{ key='((?:[^'\\]|\\.)*)' and "
                     r"value='((?:[^'\\]|\\.)*)' \}")


def _unq(s: str) -> str:
    return re.sub(r"\\(.)", r"\1", s)


class FakeDrive:
    def __init__(self, limit: int | None = 15 * 10 ** 9, usage: int = 2 * 10 ** 9, email="team5026@example.com"):
        self.calls: list[dict] = []
        self.valid_tokens = {"tok0"}
        self.refreshes = 0
        self.refresh_error: str | None = None
        self.files: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.limit, self.usage, self.email = limit, usage, email
        self.faults: list = []
        self.drop_at: int | None = None         # cut the connection once a session holds this many bytes (kept)
        self.lose_final_answer = False
        self.full = False                       # every upload start and piece answers storageQuotaExceeded
        self.api_disabled = False
        self.queries: list[str] = []
        self._n = 0

    # -- helpers -------------------------------------------------------------------------------------------------------
    def of(self, method: str, path: str) -> list[dict]:
        return [c for c in self.calls if c["method"] == method and c["path"].startswith(path)]

    def piece_ranges(self) -> list[str]:
        return [c["headers"]["Content-Range"] for c in self.of("PUT", "/upload/drive/v3/files")]

    def videos(self) -> list[dict]:
        return [f for f in self.files.values() if f["mimeType"] != FOLDER]

    def folder(self, name: str) -> dict | None:
        return next((f for f in self.files.values() if f["mimeType"] == FOLDER and f["name"] == name), None)

    def add_folder(self, name: str, parent: str | None = None, props: dict | None = None, trashed=False) -> dict:
        return self._new({"name": name, "mimeType": FOLDER, "parents": [parent] if parent else ["root"],
                          "appProperties": dict(props or {}), "trashed": trashed})

    def close(self):
        pass

    def _new(self, meta: dict, data: bytes | None = None) -> dict:
        self._n += 1
        fid = f"{'fo' if meta.get('mimeType') == FOLDER else 'f'}{self._n}"
        f = {"id": fid, "name": meta.get("name", "Untitled"), "mimeType": meta.get("mimeType") or "video/mp4",
             "parents": list(meta.get("parents") or ["root"]), "trashed": bool(meta.get("trashed")),
             "appProperties": dict(meta.get("appProperties") or {}), "description": meta.get("description"),
             "modifiedTime": meta.get("modifiedTime") or "2026-11-05T12:00:00.000Z"}
        if data is not None:
            f["data"] = bytes(data)
            f["size"] = str(len(data))
            self.usage += len(data)
        self.files[fid] = f
        return f

    def _view(self, f: dict) -> dict:
        out = {k: v for k, v in f.items() if k != "data"}
        out["webViewLink"] = (f"https://drive.google.com/drive/folders/{f['id']}" if f["mimeType"] == FOLDER
                              else f"https://drive.google.com/file/d/{f['id']}/view?usp=drivesdk")
        return out

    def _match(self, f: dict, q: str) -> bool:
        clauses = [c.strip() for c in re.split(r" and (?![^{]*\})", q)]
        for c in clauses:
            m = _CLAUSE.fullmatch(c)
            assert m, f"query clause the fake does not know: {c!r}"
            parent, trashed, mime, name, key, value = m.groups()
            if parent is not None and _unq(parent) not in f["parents"]:
                return False
            if trashed is not None and f["trashed"] != (trashed == "true"):
                return False
            if mime is not None and f["mimeType"] != _unq(mime):
                return False
            if name is not None and f["name"] != _unq(name):
                return False
            if key is not None and f["appProperties"].get(_unq(key)) != _unq(value):
                return False
        return True

    # -- the session interface ---------------------------------------------------------------------------------------------
    def request(self, method, url, headers=None, timeout=None, params=None, json=None, data=None,
                allow_redirects=True, **kw):
        headers = dict(headers or {})
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        q.update({k: str(v) for k, v in (params or {}).items()})
        self.calls.append({"method": method, "path": u.path, "query": q, "headers": headers, "json": json,
                           "data": data, "allow_redirects": allow_redirects})
        if url == TOKEN_URI:
            if self.refresh_error:
                return Resp(400, {"error": self.refresh_error, "error_description": "Token has been expired or "
                                                                                    "revoked."})
            self.refreshes += 1
            tok = f"tok{self.refreshes}"
            self.valid_tokens.add(tok)
            return Resp(200, {"access_token": tok, "expires_in": 3599, "token_type": "Bearer"})
        for i, fault in enumerate(self.faults):
            r = fault(method, u.path, q)
            if r is not None:
                del self.faults[i]
                if isinstance(r, Exception):
                    raise r
                return r
        token = headers.get("Authorization", "").removeprefix("Bearer ")
        if token not in self.valid_tokens:
            return gerr(401, "authError", "Invalid Credentials")
        if self.api_disabled:
            return Resp(403, {"error": {"code": 403, "message": "Google Drive API has not been used in project 1",
                                        "errors": [{"reason": "accessNotConfigured"}],
                                        "details": [{"reason": "SERVICE_DISABLED"}]}})
        if u.path == "/upload/drive/v3/files":
            return self._upload(method, q, headers, json, data)
        path = u.path.removeprefix("/drive/v3/")
        if (method, path) == ("GET", "about"):
            assert q.get("fields"), "about.get needs fields"
            quota = {"usage": str(self.usage)}
            if self.limit is not None:
                quota["limit"] = str(self.limit)
            return Resp(200, {"user": {"displayName": "Team 5026", "emailAddress": self.email},
                              "storageQuota": quota})
        if (method, path) == ("GET", "files"):
            self.queries.append(q["q"])
            return Resp(200, {"files": [self._view(f) for f in self.files.values() if self._match(f, q["q"])]})
        if (method, path) == ("POST", "files"):
            for p in json.get("parents") or []:
                if p not in self.files:
                    return gerr(404, "notFound", f"File not found: {p}.")
            return Resp(200, self._view(self._new(json)))
        if path.startswith("files/"):
            fid = path.removeprefix("files/")
            f = self.files.get(fid)
            if f is None:
                return gerr(404, "notFound", f"File not found: {fid}.")
            if method == "GET":
                return Resp(200, self._view(f))
            if method == "PATCH":
                for k in ("name", "description", "trashed", "modifiedTime"):
                    if k in json:
                        f[k] = json[k]
                return Resp(200, self._view(f))
        raise AssertionError(f"unexpected call {method} {u.path}")

    def _upload(self, method, q, headers, meta, data):
        if method == "POST":
            assert q["uploadType"] == "resumable"
            if self.full:
                return gerr(403, "storageQuotaExceeded", "The user's Drive storage quota has been exceeded.")
            for p in meta.get("parents") or []:
                if p not in self.files:
                    return gerr(404, "notFound", f"File not found: {p}.")
            sid = f"s{len(self.sessions) + 1}"
            self.sessions[sid] = {"size": int(headers["X-Upload-Content-Length"]), "meta": meta, "query": q,
                                  "data": bytearray(), "gone": None, "file": None}
            return Resp(200, None, {"Location": f"https://www.googleapis.com/upload/drive/v3/files"
                                                f"?uploadType=resumable&upload_id={sid}"})
        s = self.sessions[q["upload_id"]]
        if s["gone"]:
            return gerr(s["gone"], "notFound", "upload session not found")
        rng = headers["Content-Range"]
        have = s["data"]
        if rng.startswith("bytes */"):
            if s["file"]:
                return Resp(200, self._view(self.files[s["file"]]))
            return Resp(308, None, {"Range": f"bytes=0-{len(have) - 1}"} if have else {})
        if self.full:
            return gerr(403, "storageQuotaExceeded", "The user's Drive storage quota has been exceeded.")
        span, size = rng.removeprefix("bytes ").split("/")
        a, b = (int(x) for x in span.split("-"))
        assert int(size) == s["size"] and b - a + 1 == len(data) == int(headers["Content-Length"])
        if b + 1 < s["size"]:
            assert len(data) % (256 * 1024) == 0, "pieces must be multiples of 256 KiB"
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
        f = self._new(s["meta"], bytes(have))
        s["file"] = f["id"]
        if self.lose_final_answer:
            self.lose_final_answer = False
            raise requests.exceptions.ReadTimeout("read timed out")
        return Resp(200, self._view(f))
