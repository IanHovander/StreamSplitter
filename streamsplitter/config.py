"""Settings: data folder, API keys, and the setup wizard's choices.

Order for every setting: environment variable, then `config.toml` next to the package (or the file named by
STREAMSPLITTER_CONFIG), then the default. Keys are never printed or logged.

The wizard (`streamsplitter setup`) writes its choices as sections of config.toml ([event], [slack], [youtube],
[drive], [wiki], [notify]; see docs/publish-design.md) with save_sections(). Secrets (tokens, keys, the wiki's bot
password) never go in config.toml: they go to the operating system's credential store (Windows Credential Manager,
macOS Keychain, Secret Service on Linux) or, where there is none, to secrets.json beside config.toml readable by this
user only. youtube_client (the team's Google sign-in app) serves YouTube and Google Drive; each has its own login
(youtube_token, drive_token), so they can be different Google accounts.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ("tba_key", "slack_bot_token", "youtube_token", "youtube_client", "wiki_bot_password", "drive_token",
           "tba_write_id", "tba_write_secret")
_KEYRING_SERVICE = "streamsplitter"


class ConfigError(Exception):
    """config.toml cannot be read (a typo after editing it by hand). The message says where and what to do; cli.main
    prints it without a traceback."""


def config_path() -> Path:
    return Path(os.environ.get("STREAMSPLITTER_CONFIG", ROOT / "config.toml"))


def _config() -> dict:
    path = config_path()
    if not path.is_file():
        return {}
    fix = "Fix that line, or rename the file and run `streamsplitter setup` again."
    try:
        text = path.read_text(encoding="utf-8-sig")       # -sig: Notepad may have saved it with a byte-order mark
    except UnicodeDecodeError:
        raise ConfigError(f"config.toml ({path}) is not plain UTF-8 text. Save it as UTF-8, or rename the file and "
                          f"run `streamsplitter setup` again.") from None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        # Python 3.12's message ends in "(at line N, column M)" or "(at end of document)"
        m = re.search(r"\s*\(at line (\d+), column \d+\)\s*$", str(e))
        line = int(m.group(1)) if m else max(1, text.count("\n") + (0 if text.endswith("\n") else 1))
        reason = re.sub(r"\s*\(at (line \d+, column \d+|end of document)\)\s*$", "", str(e)) or "not valid TOML"
        raise ConfigError(f"config.toml ({path}) has a mistake on line {line}: {reason}. {fix}") from None


def section(name: str) -> dict:
    """One wizard section of config.toml ({} when absent)."""
    return dict(_config().get(name) or {})


def save_sections(updates: dict[str, dict]) -> None:
    """Merge {section: {key: value}} into config.toml (other keys are kept; comments are not). None removes a key."""
    import tomli_w
    cfg = _config()
    for name, values in updates.items():
        sec = dict(cfg.get(name) or {})
        for k, v in values.items():
            if v is None:
                sec.pop(k, None)
            else:
                sec[k] = v
        cfg[name] = sec
    path = config_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(tomli_w.dumps(cfg), encoding="utf-8")
    os.replace(tmp, path)


def _secrets_file() -> Path:
    return config_path().with_name("secrets.json")


def _keyring():
    try:
        import keyring
        from keyring.backends import fail
        kr = keyring.get_keyring()
        return None if isinstance(kr, fail.Keyring) else keyring
    except Exception:
        return None


def get_secret(name: str) -> str | None:
    """A secret by name (SECRETS): env STREAMSPLITTER_<NAME>, then the credential store, then secrets.json."""
    v = os.environ.get(f"STREAMSPLITTER_{name.upper()}", "").strip()
    if v:
        return v
    kr = _keyring()
    if kr is not None:
        try:
            v = kr.get_password(_KEYRING_SERVICE, name)
            if v:
                return v
        except Exception:
            pass
    f = _secrets_file()
    if f.is_file():
        try:
            return json.loads(f.read_text(encoding="utf-8")).get(name) or None
        except (OSError, json.JSONDecodeError):
            return None
    return None


def set_secret(name: str, value: str | None) -> str:
    """Store (or with None, delete) a secret. Returns where it went: "keyring" or the secrets file's path."""
    if name not in SECRETS:
        raise ValueError(f"unknown secret {name}")
    kr = _keyring()
    if kr is not None:
        try:
            if value is None:
                try:
                    kr.delete_password(_KEYRING_SERVICE, name)
                except Exception:
                    pass
            else:
                kr.set_password(_KEYRING_SERVICE, name, value)
                return "keyring"
        except Exception:        # e.g. Windows' 2.5 kB limit per credential, or a locked keyring: use the file
            pass
    f = _secrets_file()
    data = {}
    if f.is_file():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    if value is None:
        data.pop(name, None)
    else:
        data[name] = value
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    _owner_only(tmp)
    os.replace(tmp, f)
    return str(f)


def _owner_only(path: Path) -> None:
    """Readable by this user only. On Windows chmod only sets the read-only flag, so the access list is set instead:
    inheritance off, full control for this user alone (best effort; the credential store is used first anyway)."""
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    if os.name == "nt":
        import subprocess
        user = os.environ.get("USERNAME")
        if user:
            subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def data_root() -> Path:
    """Folder for recordings, chunks and clips (created if missing)."""
    root = os.environ.get("STREAMSPLITTER_DATA") or _config().get("data_root") or str(Path.home() / "StreamSplitterData")
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    return path


def work_root() -> Path:
    """Folder for the recorded chunks the detectors read (the rolling raw buffer): a fast disk if there is one, since
    detectors and cutter read every chunk again and a busy hard disk becomes the bottleneck. Defaults to data_root()."""
    root = os.environ.get("STREAMSPLITTER_WORK") or _config().get("work_root")
    if not root:
        return data_root()
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    return path


def deliver_dir() -> Path | None:
    """Folder that also gets a copy of every clip (e.g. a OneDrive / Google Drive folder shared with drive team), or
    None. Clips land there complete: they are copied under a temporary name and renamed."""
    root = os.environ.get("STREAMSPLITTER_DELIVER") or _config().get("deliver_dir")
    return Path(root) if root else None


def tba_key() -> str | None:
    """The Blue Alliance read key, or None when none is configured."""
    key = os.environ.get("TBA_API_KEY", "").strip() or get_secret("tba_key")
    if key:
        return key
    cfg = _config()
    if cfg.get("tba_key"):
        return str(cfg["tba_key"]).strip()
    key_file = cfg.get("tba_key_file")
    if key_file and Path(key_file).is_file():
        return Path(key_file).read_text(encoding="utf-8").strip() or None
    return None
