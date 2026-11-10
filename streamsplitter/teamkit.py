"""Team settings file: set Slack, YouTube and the team wiki up once, then give other laptops one file (and its
passphrase, if it has one).

A workspace can be short of Slack app slots, and one StreamSplitter app (one bot token) serves every laptop at every
event all season. So the person who set it up exports a team settings file; on another laptop the setup imports it
and skips the Slack and Google console steps. What it carries:

- Slack: the bot token, the channel, the teams whose matches go there.
- YouTube: the Google sign-in app (OAuth client JSON), the title/description formats and privacy, and optionally the
  channel login itself (then nobody needs the channel owner's Google account at each event).
- Google Drive: the top folder (id, link, name), the video size and name format, the sign-in app when YouTube does
  not carry it, and optionally the Drive login. A laptop that logs in with another account cannot see that folder
  (drive.file): its Drive step notices and offers a new one.
- The team wiki: the api.php address, the bot user name and its bot password, members-only or not, the video markup
  and the page title format.
- The Blue Alliance read key, when there is one.

Not carried: the event, the stream, the playlist, the event's Drive folder and the wiki page (each event sets its
own) and the alert contact (whoever runs that laptop).

Two kinds of file, both with the SUFFIX name:
- Encrypted (version 1): AES-256-GCM, key from the passphrase by scrypt, so it can travel through a password manager
  or a USB stick; share the passphrase separately.
- Plain (version 2, "encrypted": false; user, 2026-11-06: "I don't want it password protected. These are api keys we
  can easily deal with if there are issues."): the settings as JSON, so anyone who gets the file can use the keys in
  it. Its SHA-256 is stored beside it. Version 2 makes StreamSplitter 0.1.0, which cannot read it, say "update this
  copy" instead of "damaged".
A wrong passphrase or a changed or damaged file is refused, never half-read.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import time
import urllib.parse
from pathlib import Path

from . import config

FORMAT = "streamsplitter-team-settings"
VERSION = 1                                  # encrypted with a passphrase
PLAIN_VERSION = 2                            # no passphrase
SUFFIX = ".streamsplitter-team"
PLAIN_NOTE = ("StreamSplitter team settings, NOT encrypted: anyone with this file can post to the team's Slack and use "
              "its YouTube, Google Drive and Blue Alliance keys. Import it with the setup (run.bat / run.sh).")
_SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}     # ~32 MB and ~0.1 s per try: cheap to use, slow to guess
MIN_PASSPHRASE = 12
WIKI_KEYS = ("api_url", "username", "version", "private", "private_markup", "embed_format", "page_format")
DRIVE_KEYS = ("version", "height", "name_format", "description_format", "folder_id", "folder_url", "folder_title")
DRIVE_EVENT_KEYS = ("event_folder_id", "event_folder_event", "event_folder_title", "event_folder_url")


class TeamKitError(Exception):
    """The file cannot be used: wrong passphrase, not a team settings file, or damaged. The message says which."""


def suggest_passphrase() -> str:
    """Four groups of five letters/digits (~100 bits), easy to read out: "k7m2q-x9rtw-..." (no 0/o/1/l)."""
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(4))


def _key(passphrase: str, salt: bytes, kdf: dict) -> bytes:
    import hashlib
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=kdf["n"], r=kdf["r"], p=kdf["p"],
                          maxmem=128 * 1024 * 1024, dklen=32)


def collect(include_youtube_login: bool = False, include_drive_login: bool = False) -> dict:
    """What this laptop's setup would hand on (secrets included). Raises TeamKitError when there is nothing to share."""
    sl, yt, wk = config.section("slack"), config.section("youtube"), config.section("wiki")
    dr = config.section("drive")
    kit: dict = {"created": time.strftime("%Y-%m-%d %H:%M"), "from_host": socket.gethostname()}
    token = config.get_secret("slack_bot_token")
    if sl.get("enabled") and token:
        kit["slack"] = {"token": token, **{k: sl[k] for k in ("channel_id", "channel_name", "teams", "score_in_thread",
                                                               "clip_height") if k in sl}}
    client = config.get_secret("youtube_client")
    if yt.get("enabled") and client:
        y = {"client": json.loads(client), **{k: yt[k] for k in ("route", "version", "privacy", "title_format",
                                                                   "description_format", "channel_id",
                                                                   "channel_title") if k in yt}}
        # not "terms_accepted": whoever runs the next laptop agrees to YouTube's Terms themselves
        login = config.get_secret("youtube_token")
        if include_youtube_login and login:
            y["login"] = login
        kit["youtube"] = y
    if dr.get("enabled") and dr.get("folder_id") and client:
        # not the event's folder: the Drive step (or the first match) finds or makes this event's
        d = {k: dr[k] for k in DRIVE_KEYS if k in dr}
        if "youtube" not in kit:
            d["client"] = json.loads(client)        # the sign-in app Drive logs in with
        login = config.get_secret("drive_token")
        if include_drive_login and login:
            d["login"] = login
        kit["drive"] = d
    password = config.get_secret("wiki_bot_password")
    if wk.get("enabled") and password and wk.get("api_url"):
        # not the page: it belongs to an event, and the wiki step picks this event's
        kit["wiki"] = {"password": password, **{k: wk[k] for k in WIKI_KEYS if k in wk}}
    tba = config.tba_key()
    if tba:
        kit["tba_key"] = tba
    if not {"slack", "youtube", "drive", "wiki"} & set(kit):
        raise TeamKitError("Slack, YouTube, Google Drive and the team wiki are not set up on this computer yet, so "
                           "there is nothing to share.")
    return kit


def _digest(kit: dict) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(kit, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _complete(kit: dict) -> bool:
    """Whether every part apply() reads is there and of the right kind (a plain file has no tag to prove that)."""
    def text(v) -> bool:
        return isinstance(v, str) and bool(v)

    def maybe(v, kind) -> bool:
        return v is None or isinstance(v, kind)
    parts = {"slack": lambda s: text(s.get("token")),
             "youtube": lambda y: isinstance(y.get("client"), dict) and maybe(y.get("login"), str),
             "drive": lambda d: maybe(d.get("client"), dict) and maybe(d.get("login"), str),
             "wiki": lambda w: text(w.get("password"))}
    for part, ok in parts.items():
        if part in kit and not (isinstance(kit[part], dict) and ok(kit[part])):
            return False
    return maybe(kit.get("tba_key"), str)


def write(path: Path, kit: dict, passphrase: str | None = None) -> Path:
    """Write `kit` to `path` (the suffix is added when missing), readable by this user only: encrypted with
    `passphrase`, or with None as plain JSON, which anyone who gets the file can use."""
    if passphrase is not None and len(passphrase) < MIN_PASSPHRASE:
        raise TeamKitError(f"Use a passphrase of at least {MIN_PASSPHRASE} characters.")
    path = Path(path)
    if not path.name.endswith(SUFFIX):
        path = path.with_name(path.name + SUFFIX)
    if passphrase is None:
        doc = {"format": FORMAT, "version": PLAIN_VERSION, "encrypted": False, "note": PLAIN_NOTE, "kit": kit,
               "sha256": _digest(kit)}
    else:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        salt, nonce = os.urandom(16), os.urandom(12)
        header = {"format": FORMAT, "version": VERSION, "kdf": {"name": "scrypt", **_SCRYPT}}
        aad = json.dumps(header, sort_keys=True).encode()          # the header is authenticated too
        blob = AESGCM(_key(passphrase, salt, _SCRYPT)).encrypt(nonce, json.dumps(kit).encode("utf-8"), aad)
        b64 = lambda b: base64.b64encode(b).decode("ascii")        # noqa: E731
        doc = {**header, "salt": b64(salt), "nonce": b64(nonce), "data": b64(blob),
               "note": "StreamSplitter team settings. Import it with the setup (run.bat / run.sh); it needs the "
                       "passphrase."}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    try:
        config._owner_only(tmp)
    except OSError:
        pass
    os.replace(tmp, path)
    return path


def _load(path: Path) -> dict:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise TeamKitError(f"No file at {path}.") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise TeamKitError("That is not a StreamSplitter team settings file (or it is damaged).") from None
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise TeamKitError("That is not a StreamSplitter team settings file.")
    if doc.get("version") not in (VERSION, PLAIN_VERSION):
        raise TeamKitError("This team settings file was made by a newer StreamSplitter: update this copy first.")
    return doc


def needs_passphrase(path: Path) -> bool:
    """Whether the file is locked with a passphrase (False: made without one). TeamKitError when it is not a team
    settings file."""
    return _load(path)["version"] != PLAIN_VERSION


def read(path: Path, passphrase: str | None = None) -> dict:
    """The settings in a team settings file: a plain one as it is, an encrypted one decrypted with `passphrase`.
    TeamKitError on a wrong or missing passphrase, or a file that is not one (or was changed or damaged)."""
    doc = _load(path)
    if doc["version"] == PLAIN_VERSION:
        kit = doc.get("kit")
        if doc.get("encrypted") is not False or not isinstance(kit, dict) or doc.get("sha256") != _digest(kit) \
                or not _complete(kit):
            raise TeamKitError("The team settings file is damaged (or was changed after it was made).")
        return kit
    if not passphrase:
        raise TeamKitError("This team settings file is locked: it needs its passphrase.")
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        kdf = doc["kdf"]
        header = {"format": doc["format"], "version": doc["version"], "kdf": kdf}
        aad = json.dumps(header, sort_keys=True).encode()
        salt, nonce, blob = (base64.b64decode(doc[k]) for k in ("salt", "nonce", "data"))
        plain = AESGCM(_key(passphrase, salt, kdf)).decrypt(nonce, blob, aad)
    except InvalidTag:
        raise TeamKitError("Wrong passphrase (or the file was changed after it was made).") from None
    except (KeyError, TypeError, ValueError):
        raise TeamKitError("The team settings file is damaged.") from None
    return json.loads(plain.decode("utf-8"))


def summary(kit: dict) -> list[str]:
    """Plain lines saying what a kit holds (never a secret's value)."""
    out = []
    if "slack" in kit:
        s = kit["slack"]
        out.append(f"Slack: post to #{s.get('channel_name', '?')} for team(s) {', '.join(s.get('teams') or []) or '?'}")
    if "youtube" in kit:
        y = kit["youtube"]
        who = f"to {y['channel_title']}" if y.get("channel_title") else "sign-in app only"
        out.append(f"YouTube: {who}" + (", logged in" if y.get("login") else ", each laptop logs in once")
                   + f", {y.get('privacy', 'unlisted')}")
    if "drive" in kit:
        d = kit["drive"]
        h = d.get("height", 720)
        out.append(f"Google Drive: every match to the folder '{d.get('folder_title') or '?'}'"
                   + (", logged in" if d.get("login") else ", each laptop logs in once")
                   + f", {f'{h}p' if h else 'as recorded'}")
    if "wiki" in kit:
        w = kit["wiki"]
        host = urllib.parse.urlsplit(str(w.get("api_url") or "")).netloc or "?"
        out.append(f"Team wiki: every match to {host} as {w.get('username', '?')}"
                   + (", members-only" if w.get("private", True) else ""))
    if kit.get("tba_key"):
        out.append("The Blue Alliance read key")
    out.append(f"Made {kit.get('created', '?')} on {kit.get('from_host', '?')}")
    return out


def apply(kit: dict) -> list[str]:
    """Store a kit on this laptop: secrets to the credential store, choices to config.toml. Returns what was set.
    The event, stream, playlist and alert contact are left as they are."""
    done = []
    if "slack" in kit:
        s = dict(kit["slack"])
        config.set_secret("slack_bot_token", s.pop("token"))
        config.save_sections({"slack": {"enabled": True, **s}})
        done.append("Slack")
    if "youtube" in kit:
        y = dict(kit["youtube"])
        config.set_secret("youtube_client", json.dumps(y.pop("client")))
        login = y.pop("login", None)
        if login:
            config.set_secret("youtube_token", login)
        # the playlist belongs to an event: the YouTube step finds or creates this event's
        config.save_sections({"youtube": {"enabled": True, **y, "playlist_id": None, "playlist_title": None,
                                          "playlist_event": None}})
        done.append("YouTube" + (" (logged in)" if login else " (log in once with setup --section youtube)"))
    if "drive" in kit:
        d = dict(kit["drive"])
        client = d.pop("client", None)
        if client and "youtube" not in kit:
            config.set_secret("youtube_client", json.dumps(client))
        login = d.pop("login", None)
        if login:
            config.set_secret("drive_token", login)
        config.save_sections({"drive": {"enabled": True, **{k: d[k] for k in DRIVE_KEYS if k in d},
                                        **dict.fromkeys(DRIVE_EVENT_KEYS)}})
        done.append("Google Drive" + (" (logged in)" if login else " (log in once with setup --section drive)"))
    if "wiki" in kit:
        w = dict(kit["wiki"])
        config.set_secret("wiki_bot_password", w.pop("password"))
        # the page belongs to an event: the wiki step finds this event's
        config.save_sections({"wiki": {"enabled": True, **{k: w[k] for k in WIKI_KEYS if k in w}, "page_title": None,
                                       "page_event": None, "event_page": None}})
        done.append("the team wiki")
    if kit.get("tba_key"):
        config.set_secret("tba_key", kit["tba_key"])
        done.append("The Blue Alliance key")
    return done
