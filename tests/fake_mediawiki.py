"""A MediaWiki api.php in memory, for the wiki tests (tests/test_wiki.py, tests/test_wizard.py): no network.

It stands in for requests.Session (get/post) and answers the calls publish/wiki.py makes the way MediaWiki 1.43 does
(formatversion=2): login and CSRF tokens with bot passwords, chunked stash uploads (offset checks, a new filekey for
the assembled file, checkstatus, list=mystashedfiles), publishing with warnings (exists, nochange, duplicate,
was-deleted), createonly / section=new edits, page reads, sections, search, delete, and the rights a bot password's
grants give. fail() makes the next matching request drop, lose its answer after doing the work, or answer something
else (an error, HTTP 429, a Cloudflare challenge).
"""

from __future__ import annotations

import hashlib
import re

import requests

DEFAULT_RIGHTS = {"read", "edit", "createpage", "upload", "reupload-own", "writeapi"}


class Resp:
    def __init__(self, status: int = 200, body=None, headers: dict | None = None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


def norm_title(t: str) -> str:
    t = " ".join(str(t).replace("_", " ").split())
    if ":" in t:
        ns, rest = t.split(":", 1)
        if ns.lower() in ("file", "image"):
            return "File:" + rest[:1].upper() + rest[1:]
    return t[:1].upper() + t[1:]


def sha1(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


class FakeMediaWiki:
    def __init__(self, rights=None):
        self.users = {"Ian@StreamSplitter": ("goodpassword", "Ian")}     # lgname -> (bot password, account)
        self.main_passwords = {"Ian": "mainpassword"}
        self.rights = set(DEFAULT_RIGHTS if rights is None else rights)
        self.user: str | None = None
        self.login_token = "LOGINTOKEN+\\"
        self.csrf: str | None = None
        self.pages: dict[str, str] = {}
        self.files: dict[str, dict] = {}            # "2026mttd qm12.mp4" -> {data, sha1, user, comment}
        self.deleted: set[str] = set()
        self.stash: dict[str, dict] = {}            # filekey -> {data, size, assembled}
        self.status: dict[str, dict] = {}           # filekey -> session status ("Continue", offset)
        self.calls: list[tuple[str, dict]] = []
        self.faults: list[list] = []
        self.max_body: int | None = None            # a piece bigger than this: HTTP 413 (PHP / the web server)
        self.max_upload = 500 * 1024 * 1024         # $wgMaxUploadSize
        self.extensions = ["png", "jpg", "pdf", "mp4"]
        self.url: str | None = None                 # when set, only this api.php address answers
        self._n = 0
        self.revid = 1000

    # -- test controls ---------------------------------------------------------------------------------------------
    def fail(self, kind: str, effect, skip: int = 0, times: int = 1) -> None:
        """The next request of `kind` (after `skip` of them) gets `effect`: "drop" (no connection), "lose" (done, but
        the answer is lost: a read timeout), a Resp, or a dict (an API error object)."""
        self.faults.append([kind, effect, skip, times])

    def kinds(self, kind: str | None = None) -> list:
        return [k for k, _ in self.calls] if kind is None else [p for k, p in self.calls if k == kind]

    def add_file(self, name: str, data: bytes, user: str = "Mentor") -> None:
        name = norm_title("File:" + name)[5:]
        self.files[name] = {"data": bytes(data), "sha1": sha1(data), "user": user, "comment": ""}
        self.pages.setdefault("File:" + name, "")

    def file(self, name: str) -> dict | None:
        return self.files.get(norm_title("File:" + name)[5:])

    # -- requests.Session --------------------------------------------------------------------------------------------
    def get(self, url, params=None, headers=None, timeout=None):
        return self._handle("GET", dict(params or {}), None, url)

    def post(self, url, data=None, files=None, headers=None, timeout=None):
        return self._handle("POST", dict(data or {}), files, url)

    @staticmethod
    def _kind(p: dict, files) -> str:
        a = p.get("action")
        if a == "query":
            return "query:" + (p.get("meta") or p.get("prop") or p.get("list") or "")
        if a == "upload":
            return "chunk" if files and "chunk" in files else "checkstatus" if p.get("checkstatus") else "publish"
        if a == "edit":
            return "section" if p.get("section") == "new" else "create" if p.get("createonly") else "edit"
        return str(a)

    def _handle(self, method: str, p: dict, files, url: str):
        if self.url is not None and url != self.url:
            raise requests.exceptions.ConnectionError(f"no wiki at {url}")
        kind = self._kind(p, files)
        self.calls.append((kind, {**p, **({"chunk_len": len(files["chunk"][1])} if files else {})}))
        for f in self.faults:
            if f[0] != kind or f[3] <= 0:
                continue
            if f[2] > 0:
                f[2] -= 1
                continue
            f[3] -= 1
            effect = f[1]
            if effect == "drop":
                raise requests.exceptions.ConnectionError("dropped")
            if effect == "lose":
                self._do(method, p, files, kind)
                raise requests.exceptions.ReadTimeout("answer lost")
            if isinstance(effect, Resp):
                return effect
            return Resp(200, {"error": dict(effect)})
        return self._do(method, p, files, kind)

    # -- api.php -------------------------------------------------------------------------------------------------------
    @staticmethod
    def _err(code: str, info: str = "", **data) -> Resp:
        return Resp(200, {"error": {"code": code, "info": info or code, **data}})

    def _key(self) -> str:
        self._n += 1
        return f"key{self._n}.mp4"

    def _ii(self, name: str) -> dict:
        f = self.files[name]
        u = name.replace(" ", "_")
        return {"sha1": f["sha1"], "size": len(f["data"]), "user": f["user"],
                "url": f"https://wiki.example.org/images/{u}",
                "descriptionurl": f"https://wiki.example.org/index.php/File:{u}"}

    def _do(self, method: str, p: dict, files, kind: str) -> Resp:
        assert p.get("format") == "json" and p.get("formatversion") == "2", p
        a = p.get("action")
        if a in ("upload", "edit", "delete"):
            assert method == "POST", "writes are POSTed"
            if p.get("assert") == "user" and self.user is None:
                return self._err("assertuserfailed", "You are no longer logged in")
            if not self.csrf or p.get("token") != self.csrf:
                return self._err("badtoken", "Invalid CSRF token.")
        if a == "query":
            return self._query(p)
        if a == "login":
            return self._login(p)
        if a == "upload":
            return {"chunk": self._chunk, "checkstatus": self._checkstatus, "publish": self._publish}[kind](p, files)
        if a == "edit":
            return self._edit(p)
        if a == "parse":
            title = norm_title(p["page"])
            if title not in self.pages:
                return self._err("missingtitle", "The page you specified doesn't exist.")
            heads = re.findall(r"^==\s*(.+?)\s*==\s*$", self.pages[title], re.M)
            return Resp(200, {"parse": {"title": title, "sections": [
                {"line": h, "level": "2", "index": str(i), "anchor": h.replace(" ", "_")}
                for i, h in enumerate(heads, 1)]}})
        if a == "delete":
            if "delete" not in self.rights:
                return self._err("permissiondenied", "You don't have permission to delete pages.")
            title = norm_title(p["title"])
            if title not in self.pages:
                return self._err("missingtitle")
            del self.pages[title]
            if title.startswith("File:"):
                self.files.pop(title[5:], None)
                self.deleted.add(title[5:])
            return Resp(200, {"delete": {"title": title}})
        return self._err("badvalue", f"Unrecognized value for parameter action: {a}")

    def _query(self, p: dict) -> Resp:
        if p.get("meta") == "tokens":
            if p.get("type") == "login":
                return Resp(200, {"query": {"tokens": {"logintoken": self.login_token}}})
            return Resp(200, {"query": {"tokens": {"csrftoken": self.csrf if self.user else "+\\"}}})
        if p.get("meta") == "siteinfo":
            return Resp(200, {"query": {"general": {"sitename": "Team Wiki", "generator": "MediaWiki 1.43.0",
                                                    "server": "https://wiki.example.org",
                                                    "articlepath": "/index.php/$1", "maxuploadsize": self.max_upload},
                                        "fileextensions": [{"ext": e} for e in self.extensions]}})
        if p.get("meta") == "userinfo":
            return Resp(200, {"query": {"userinfo": {"id": 7 if self.user else 0, "name": self.user or "127.0.0.1",
                                                     **({} if self.user else {"anon": True}),
                                                     "rights": sorted(self.rights) if self.user else ["read"],
                                                     "groups": ["*", "user"]}}})
        if p.get("prop") == "revisions":
            title = norm_title(p["titles"])
            if title not in self.pages:
                return Resp(200, {"query": {"pages": [{"ns": 0, "title": title, "missing": True}]}})
            return Resp(200, {"query": {"pages": [{"pageid": 1, "title": title, "revisions": [
                {"slots": {"main": {"contentmodel": "wikitext", "content": self.pages[title]}}}]}]}})
        if p.get("prop") == "imageinfo":
            title = norm_title(p["titles"])
            name = title[5:]
            if name not in self.files:
                return Resp(200, {"query": {"pages": [{"ns": 6, "title": title, "missing": True}]}})
            return Resp(200, {"query": {"pages": [{"pageid": 2, "ns": 6, "title": title,
                                                   "imageinfo": [self._ii(name)]}]}})
        if p.get("prop") == "stashimageinfo":
            st = self.stash.get(p["siifilekey"])
            if st is None:
                return self._err("stashnosuchfilekey")
            return Resp(200, {"query": {"stashimageinfo": [{"sha1": sha1(bytes(st["data"])), "size": st["size"]}]}})
        if p.get("list") == "mystashedfiles":
            return Resp(200, {"query": {"mystashedfiles": [{"filekey": k, "size": v["size"]}
                                                           for k, v in self.stash.items() if v["assembled"]]}})
        if p.get("list") == "search":
            words = p["srsearch"].casefold().split()
            hits = [t for t, text in self.pages.items() if ":" not in t
                    and all(w in (t + " " + text).casefold() for w in words)]
            return Resp(200, {"query": {"search": [{"ns": 0, "title": t} for t in hits]}})
        if p.get("list") == "prefixsearch":
            return Resp(200, {"query": {"prefixsearch": [{"ns": 0, "title": t} for t in self.pages
                                                         if t.startswith(p["pssearch"])]}})
        return self._err("badvalue", "unknown query")

    def _login(self, p: dict) -> Resp:
        if p.get("lgtoken") != self.login_token:
            return Resp(200, {"login": {"result": "WrongToken"}})
        name, password = p.get("lgname"), p.get("lgpassword")
        if self.main_passwords.get(name) == password:
            return Resp(200, {"login": {"result": "Failed", "reason": "Login with your main password via action=login "
                                                                      "is not supported; use action=clientlogin."}})
        entry = self.users.get(name)
        if entry is None or entry[0] != password:
            return Resp(200, {"login": {"result": "Failed",
                                        "reason": "Incorrect username or password entered. Please try again."}})
        self.user = entry[1]
        self._n += 1
        self.csrf = f"csrf{self._n}+\\"
        return Resp(200, {"login": {"result": "Success", "lguserid": 7, "lgusername": self.user}})

    def _chunk(self, p: dict, files) -> Resp:
        data = files["chunk"][1]
        if self.max_body is not None and len(data) > self.max_body:
            return Resp(413, None)
        if "upload" not in self.rights:
            return self._err("permissiondenied", "You don't have permission to upload this file.")
        size, off = int(p["filesize"]), int(p["offset"])
        if size > self.max_upload:
            return self._err("file-too-large", "The file you submitted was too large.")
        if off == 0:
            key = self._key()
            self.stash[key] = {"data": bytearray(data), "size": size, "assembled": False}
        else:
            key = p.get("filekey")
            st = self.stash.get(key)
            if st is None or st["assembled"]:
                return self._err("stashfailed", "No chunked upload session with this key.")
            if off != len(st["data"]):
                return self._err("stashfailed", "Invalid chunk offset", offset=len(st["data"]))
            st["data"] += data
        st = self.stash[key]
        if len(st["data"]) >= size:
            # the wiki puts the file together and gives it a new filekey; the old key's status is forgotten
            new = self._key()
            self.stash[new] = {**st, "assembled": True}
            del self.stash[key]
            self.status.pop(key, None)
            return Resp(200, {"upload": {"result": "Success", "filekey": new}})
        self.status[key] = {"result": "Continue", "stage": "uploading", "offset": len(st["data"])}
        return Resp(200, {"upload": {"result": "Continue", "offset": len(st["data"]), "filekey": key}})

    def _checkstatus(self, p: dict, files) -> Resp:
        key = p.get("filekey")
        if key in self.status:
            return Resp(200, {"upload": dict(self.status[key])})
        return self._err("missingresult", "No result in status data.")

    def _publish(self, p: dict, files) -> Resp:
        key = p.get("filekey")
        st = self.stash.get(key)
        if st is None or not st["assembled"]:
            return self._err("stashnosuchfilekey", "No such filekey.")
        name = norm_title("File:" + p["filename"])[5:]
        data = bytes(st["data"])
        digest = sha1(data)
        old = self.files.get(name)
        if not p.get("ignorewarnings"):
            w = {}
            if old:
                w["exists"] = name.replace(" ", "_")
                if old["sha1"] == digest:
                    w["nochange"] = {"timestamp": "2026-11-06T10:00:00Z"}
            dups = [n.replace(" ", "_") for n, f in self.files.items() if f["sha1"] == digest and n != name]
            if dups:
                w["duplicate"] = dups
            if name in self.deleted:
                w["was-deleted"] = name.replace(" ", "_")
            if w:
                return Resp(200, {"upload": {"result": "Warning", "warnings": w, "filekey": key}})
        if old:
            need = "reupload-own" if old["user"] == self.user else "reupload"
            if need not in self.rights and "reupload" not in self.rights:
                return self._err("fileexists-forbidden", "You may not overwrite this file.")
        self.files[name] = {"data": data, "sha1": digest, "user": self.user, "comment": p.get("comment")}
        if "File:" + name not in self.pages:
            self.pages["File:" + name] = p.get("text") or ""
        del self.stash[key]
        return Resp(200, {"upload": {"result": "Success", "filename": name.replace(" ", "_"),
                                     "imageinfo": self._ii(name)}})

    def _edit(self, p: dict) -> Resp:
        title = norm_title(p["title"])
        if p.get("createonly"):
            if title in self.pages:
                return self._err("articleexists", "The article you tried to create has been created already.")
            if "createpage" not in self.rights:
                return self._err("permissiondenied", "You don't have permission to create pages.")
            self.pages[title] = p["text"]
        elif p.get("section") == "new":
            if title not in self.pages and p.get("nocreate"):
                return self._err("missingtitle", "The page you specified doesn't exist.")
            if "edit" not in self.rights:
                return self._err("permissiondenied", "You don't have permission to edit pages.")
            before = self.pages.get(title, "").rstrip("\n")
            self.pages[title] = (before + "\n\n" if before else "") + f"== {p['sectiontitle']} ==\n\n{p['text']}"
        else:
            self.pages[title] = p["text"]
        self.revid += 1
        return Resp(200, {"edit": {"result": "Success", "title": title, "newrevid": self.revid}})
