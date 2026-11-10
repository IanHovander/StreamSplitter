"""Guided setup: `streamsplitter setup`, which run.bat / run.sh start on a fresh computer.

    streamsplitter setup                        every step, then a summary (on a set-up computer: a menu)
    streamsplitter setup --section youtube      one step: event | tba | stream | slack | youtube | drive | wiki |
                                                notify | listen | test | team
    streamsplitter setup --install-only         only the downloads below, waiting for them

It is for team members who are not developers, on their own laptop, days before an event. Each step says what it
needs and how to find it, offers the current value as the default, checks the answer against the real service (TBA,
yt-dlp, Slack, YouTube, Google Drive, the team wiki) and saves it at once, so a setup cut short loses nothing and any
step can be run again on its own. Choices go to config.toml sections ([event], [slack], [youtube], [drive], [wiki],
[notify], [listen]: docs/publish-design.md); secrets to the credential store (config.set_secret). A secret is typed
with hidden input and never printed. The wiki step is not in the first run's list since 2026-11-05 (the user chose a
shared Google Drive folder for the videos instead); it stays in the menu and `--section wiki`. "Start from Slack"
([listen], listener.py, 2026-11-05) is asked after the alerts, off unless chosen; the menu starts and stops it. The
alerts contact is the owner in Slack: they change settings and add the other people there (slackadmin.py), so the
setup asks for nobody else and messages nobody but the contact.

Walkthroughs and the team settings file (2026-11-04, for teams other than ours)
-------------------------------------------------------------------------------
The Slack app and the Google sign-in app (OAuth client) are made once per team, in small steps: one or two lines of
what to do, the exact page opened in the browser, then Enter (or the wizard notices by itself: the client file
appearing in the real Downloads folder, the browser login coming back). Google's common login failures (the app left
in Testing, a non-Desktop client, the API not enabled, an account with no channel, the wrong channel) are recognised
and their fix opened. Every other laptop then loads a team settings file (teamkit.py) instead: asked first in a fresh
setup, made from the menu or `--section team`, and the Slack and YouTube steps then ask only this laptop's own things
(the alert contact, this event's playlist and privacy, the YouTube login if the file has none).

Downloads in the background
---------------------------
run.bat / run.sh install only the light packages the questions need (seconds) and start this. The heavy pieces
download on threads while the questions are asked; the wizard shows a line of progress between steps and waits for
them before it offers to start recording:
- detection packages: the `detect` extra in pyproject.toml (numpy, OpenCV, ONNX Runtime, RapidOCR; ~150 MB), with
  uv (STREAMSPLITTER_UV from run.bat, uv on PATH, or .tools/uv) or else pip; then a test load of the OCR engines in
  a child process, which catches a missing Microsoft C++ runtime on Windows, or a folder path too long for Windows
  to load the DLLs from (path_too_long also warns of that at the start);
- ffmpeg + ffprobe, unless both already run: gyan.dev's "release essentials" build on Windows (one of the two builds
  ffmpeg.org links for Windows; winget's Gyan.FFmpeg is the same), Martin Riedl's static release builds on macOS and
  Linux (arm64 and Intel; signed on macOS), each checked against its published SHA-256 and put in the venv's
  scripts folder;
- Deno, the JavaScript runtime yt-dlp recommends for YouTube's challenges (Deno's official wheels on PyPI; the deno
  binary lands in the venv's scripts folder too), unless a deno >= 2.3 is on PATH. Optional: live HLS records without
  it, but yt-dlp warns, and normal (not live) YouTube videos need it.
prepare_environment() (cli.main calls it for every command) puts the venv's scripts folder on PATH, so ffmpeg.find()
and yt-dlp find all three without any change to the system.

Prompts go through an IO object (ConsoleIO here; a scripted one in tests/test_wizard.py) and the outside world
through Services (TBA, yt-dlp, Slack, YouTube, Google Drive, the wiki, the live runner), which tests replace with
fakes. Output is
ASCII, and stdout replaces what a cp1252 console cannot show (names from TBA or Slack) instead of failing.
"""

from __future__ import annotations

import datetime as _dt
import getpass
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import sysconfig
import tempfile
import textwrap
import threading
import time
import tomllib
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import config, teamkit
from .publish.base import Job, Match, PermanentError, TransientError

ROOT = config.ROOT
LOG_DIR = ROOT / "logs"
SAMPLE_CLIP = Path(__file__).resolve().parent / "assets" / "sample_match_2024mttd_qm6.mp4"
# The bundled clip: Qualification 6 of the 2024 MadTown Throwdown (TBA), team 5026 on blue.
SAMPLE_MATCH = Match(key="2024mttd_qm6", name="2024mttd_qm6", event_key="2024mttd", event_name="Madtown Throwdown",
                     year=2024, comp_level="qm", set_number=1, match_number=6, red=["1323", "6619", "254B"],
                     blue=["5026", "5499", "114"], red_score=81, blue_score=63)
SECTIONS = ("event", "tba", "stream", "slack", "youtube", "drive", "wiki", "notify", "listen", "test", "team",
            "tbalinks")
DEFAULT_TEAMS: list[str] = []                   # no team or channel is assumed: a first setup asks for both
DEFAULT_CHANNEL = ""
HEARTBEAT_S, ALARM_AFTER_S = 60, 420           # [notify] defaults: the stopped-running alarm cycles every minute
OLD_NOTIFY_DEFAULTS = {(120, 600), (60, 180), (60, 600)}   # saved by earlier setups; replaced by the above when seen
_FROM_FILE = object()                           # slack_login: a team settings file was loaded instead
RESTART_WAIT_S = 180.0                          # a stopping run gets this long before a new one is started
SLACK_DOC = ROOT / "docs" / "setup-slack.md"
YOUTUBE_DOC = ROOT / "docs" / "setup-youtube.md"
WIKI_DOC = "docs/setup-wiki.md"
# A team's wiki offered as the default address, only when that team is among [slack] teams (a generic tool has no
# wiki of its own to suggest)
KNOWN_WIKIS = {"5026": "https://www.team5026.com/api.php"}
TBA_ACCOUNT_URL = "https://www.thebluealliance.com/account"
SLACK_APPS_URL = "https://api.slack.com/apps"
# Google Cloud console pages (the Google Auth Platform layout). /projectcreate, /auth/branding, /auth/audience and
# /auth/clients are the links Google's own guides use (developers.google.com/workspace/guides/create-project,
# configure-oauth-consent, create-credentials; checked 2026-11-04); the API's library page follows the console's
# /apis/library/<service> pattern. ?project=<id> picks the project when it is known.
GOOGLE_PAGES = {"project": "https://console.cloud.google.com/projectcreate",
                "api": "https://console.cloud.google.com/apis/library/youtube.googleapis.com",
                "drive_api": "https://console.cloud.google.com/apis/library/drive.googleapis.com",
                "branding": "https://console.cloud.google.com/auth/branding",
                "audience": "https://console.cloud.google.com/auth/audience",
                "clients": "https://console.cloud.google.com/auth/clients"}
PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{4,61}[a-z0-9]")
YOUTUBE_TERMS_URL = "https://www.youtube.com/t/terms"
GOOGLE_PRIVACY_URL = "https://policies.google.com/privacy"
GOOGLE_PERMISSIONS_URL = "https://myaccount.google.com/permissions"
RERUN = "run.bat (Windows) or ./run.sh (Mac/Linux)"

GYAN_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
RIEDL_URL = "https://ffmpeg.martin-riedl.de/redirect/latest/{os}/{arch}/release/{tool}.zip"
DETECT_MODULES = ("numpy", "cv2", "onnxruntime", "rapidocr")
# ... and the OCR engines themselves: `import rapidocr` loads lazily, and passed on 2026-11-06 where loading them failed
DETECT_CHECK = "from streamsplitter.detect.overlay import _engines; _engines()"
ROOT_MAX = 150                                  # Windows: a deeper folder may not load the packages' DLLs (path_too_long)
SHORT_PATH_FIX = ("Unzip StreamSplitter again into a short folder such as C:\\StreamSplitter and run run.bat there "
                  "(copy config.toml across to keep the answers given so far).")
DENO_MIN = (2, 3)                               # yt-dlp's minimum Deno (github.com/yt-dlp/yt-dlp/wiki/EJS)
FFMPEG_FIX = ("Run setup again to retry, or install ffmpeg yourself: Windows `winget install Gyan.FFmpeg`, "
              "Mac `brew install ffmpeg`, Linux `sudo apt install ffmpeg`.")


class Cancelled(Exception):
    """The person stopped the setup (Ctrl+C, or the input closed)."""


class SetupFailed(Exception):
    """A step that cannot finish; `fix` says what the person can do about it."""

    def __init__(self, message: str, fix: str = ""):
        super().__init__(message)
        self.fix = fix


# ---------------------------------------------------------------------------------------------------------------------
# Prompts

_INVISIBLE = dict.fromkeys((0xFEFF, 0x200B, 0x200C, 0x200D, 0x2060), None) | {0xA0: " "}


def _clean(text: str) -> str:
    """A typed or pasted answer without the invisible characters a paste or a piped console can carry (a byte-order
    mark, zero-width spaces, a no-break space), and without the spaces around it."""
    return (text or "").translate(_INVISIBLE).strip()


class BaseIO:
    """Questions and messages. A subclass supplies _write(text), _line(shown, question) for one typed line,
    _hidden(shown, question) for one typed secret, and _browse(url)."""

    def say(self, text: str = "") -> None:
        self._write(text)

    def ask(self, prompt: str, default: str | None = None, check=None) -> str:
        """A line of text. Enter takes `default` ("" allows an empty answer; None requires one). `check(answer)`
        returns what is wrong with it, or None."""
        while True:
            shown = f"{prompt} [{default}]: " if default else f"{prompt}: "
            answer = _clean(self._line(shown, prompt))
            if not answer and default is not None:
                answer = str(default)
            if not answer and default is None:
                self.say("  (please type an answer)")
                continue
            problem = check(answer) if check and answer else None
            if problem:
                self.say(f"  {problem}")
                continue
            return answer

    def ask_secret(self, prompt: str) -> str:
        return _clean(self._hidden(f"{prompt}: ", prompt))

    def confirm(self, prompt: str, default: bool = True) -> bool:
        while True:
            a = _clean(self._line(f"{prompt} [{'Y/n' if default else 'y/N'}]: ", prompt)).lower()
            if not a:
                return default
            if a in ("y", "yes"):
                return True
            if a in ("n", "no"):
                return False
            self.say("  (type y or n)")

    def choose(self, prompt: str, options: list[str], default: int = 0) -> int:
        """The index of the chosen option (numbered from 1 on screen)."""
        self.say(prompt)
        for i, o in enumerate(options, 1):
            self.say(f"  {i}) {o}")
        while True:
            a = _clean(self._line(f"Choose 1-{len(options)} [{default + 1}]: ", prompt))
            if not a:
                return default
            if a.isdigit() and 1 <= int(a) <= len(options):
                return int(a) - 1
            self.say(f"  (type a number from 1 to {len(options)})")

    def open_url(self, url: str) -> None:
        self.say(f"  Opening in your browser: {url}")
        if not getattr(self, "_told_browser", False):
            self.say("  (if no browser window appears, copy that address into one)")
            self._told_browser = True
        self._browse(url)

    def wait_until(self, prompt: str, check, grace_s: float = 0.0):
        """Wait for check() to return something (a downloaded file, a finished login) while the person may type a
        line instead. Returns (what check() returned, or None; the typed line, or None). This version, for tests and
        piped input, gives check() `grace_s` and then asks for one line; ConsoleIO watches both at once."""
        end = time.monotonic() + grace_s
        while True:
            found = check()
            if found:
                return found, None
            if time.monotonic() >= end:
                break
            time.sleep(0.05)
        typed = self.ask(prompt, default="")
        return check(), typed


class ConsoleIO(BaseIO):
    def __init__(self):
        for s in (sys.stdout, sys.stderr):
            try:
                s.reconfigure(errors="replace")
            except (AttributeError, ValueError):
                pass

    def _write(self, text: str) -> None:
        print(text, flush=True)

    def _line(self, shown: str, question: str) -> str:
        try:
            return input(shown)
        except (EOFError, KeyboardInterrupt):
            print()
            raise Cancelled() from None

    def _hidden(self, shown: str, question: str) -> str:
        try:
            return getpass.getpass(shown)
        except (EOFError, KeyboardInterrupt):
            print()
            raise Cancelled() from None

    def _browse(self, url: str) -> None:
        try:
            webbrowser.open(url, new=2)
        except Exception:
            pass

    def wait_until(self, prompt: str, check, grace_s: float = 0.0):
        """Polls check() and the keyboard together, so a file that appears or a login that finishes goes on by itself
        and a typed line (a path, an error code) goes on at Enter. Without a real console: the base version."""
        try:
            interactive = sys.stdin.isatty() and sys.stdout.isatty()
        except (AttributeError, ValueError):
            interactive = False
        if not interactive:
            return super().wait_until(prompt, check, grace_s)
        print(f"{prompt}: ", end="", flush=True)
        buf: list[str] = []
        try:
            while True:
                found = check()
                if found:
                    print(flush=True)
                    self._drop_typed()
                    return found, None
                line = self._poll_line(buf, 0.5)
                if line is not None:
                    return check(), _clean(line)
        except (EOFError, KeyboardInterrupt):
            print()
            raise Cancelled() from None

    @staticmethod
    def _poll_line(buf: list[str], timeout: float) -> str | None:
        """A line once Enter is pressed within `timeout`, else None (what was typed so far stays in `buf`)."""
        if os.name == "nt":
            import msvcrt
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                while msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("\r", "\n"):
                        print(flush=True)
                        line = "".join(buf)
                        buf.clear()
                        return line
                    if ch == "\x03":
                        raise KeyboardInterrupt
                    if ch in ("\x00", "\xe0"):               # an arrow or function key: its second half
                        msvcrt.getwch()
                    elif ch == "\x08":
                        if buf:
                            buf.pop()
                            print("\b \b", end="", flush=True)
                    elif ch >= " ":
                        buf.append(ch)
                        print(ch, end="", flush=True)
                time.sleep(0.05)
            return None
        import select
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return None
        line = sys.stdin.readline()
        if line == "":
            raise EOFError
        return line.rstrip("\r\n")

    @staticmethod
    def _drop_typed() -> None:
        """Forget half-typed input (a Mac/Linux terminal keeps it for the next question)."""
        if os.name == "nt":
            return
        try:
            import termios
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
        except Exception:
            pass


# ---------------------------------------------------------------------------------------------------------------------
# Where the downloaded programs live

def in_venv() -> bool:
    return sys.prefix != sys.base_prefix


def tools_bin() -> Path:
    """Where downloaded programs go: the venv's scripts folder (where pip also puts deno), else .tools/bin."""
    return Path(sysconfig.get_path("scripts")) if in_venv() else ROOT / ".tools" / "bin"


CA_BUNDLES = ("/etc/ssl/certs/ca-certificates.crt",     # Debian, Ubuntu, Arch
              "/etc/pki/tls/certs/ca-bundle.crt",       # Fedora, RHEL
              "/etc/ssl/ca-bundle.pem",                 # openSUSE
              "/etc/ssl/cert.pem")                      # macOS, Alpine


def prepare_environment() -> None:
    """For this process and its children (the recorder, ffmpeg): the installer's programs first on PATH, so
    ffmpeg.find() and yt-dlp's Deno lookup see them without a change to the system PATH. On a Mac or Linux, also
    SSL_CERT_FILE (unless set): the static ffmpeg builds look for certificates where their build machine kept them,
    so without it every HTTPS stream fails ("certificate verify failed"; checked on Debian 12, 2026-11-04), and uv's
    Python can lack a certificate file too. The system's bundle, else certifi's."""
    path = os.environ.get("PATH", "")
    have = {os.path.normcase(os.path.normpath(p)) for p in path.split(os.pathsep) if p}
    add = []
    for d in (Path(sysconfig.get_path("scripts")), ROOT / ".tools" / "bin"):
        if d.is_dir() and os.path.normcase(os.path.normpath(str(d))) not in have:
            add.append(str(d))
    if add:
        os.environ["PATH"] = os.pathsep.join(add + ([path] if path else []))
    if os.name != "nt" and not os.environ.get("SSL_CERT_FILE"):
        bundle = next((b for b in CA_BUNDLES if os.path.isfile(b)), None)
        if bundle is None:
            try:
                import certifi
                bundle = certifi.where()
            except ImportError:
                pass
        if bundle:
            os.environ["SSL_CERT_FILE"] = bundle


def find_uv() -> str | None:
    exe = "uv.exe" if os.name == "nt" else "uv"
    for c in (os.environ.get("STREAMSPLITTER_UV"), shutil.which("uv"), str(ROOT / ".tools" / "uv" / exe)):
        if c and Path(c).is_file():
            return c
    return None


def pip_command() -> list[str]:
    """The command that installs packages into this Python: uv if there is one (fast), else pip."""
    uv = find_uv()
    if uv:
        return [uv, "pip", "install", "--python", sys.executable]
    if importlib.util.find_spec("pip"):
        return [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
    raise SetupFailed("no package installer (uv or pip) was found", f"Start the setup with {RERUN}.")


def extra_requirements(extra: str) -> list[str]:
    """The requirements of one optional group: from pyproject.toml beside the package (the folder people unzip),
    else from the installed package's metadata."""
    try:
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        reqs = data["project"]["optional-dependencies"][extra]
        if reqs:
            return list(reqs)
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        pass
    from importlib import metadata
    try:
        reqs = metadata.requires("streamsplitter") or []
    except metadata.PackageNotFoundError:
        reqs = []
    pat = re.compile(rf"""extra\s*==\s*["']{re.escape(extra)}["']""")
    return [r.split(";")[0].strip() for r in reqs if pat.search(r)]


# ---------------------------------------------------------------------------------------------------------------------
# Background downloads

@dataclass
class Task:
    name: str                       # shown to the person: "detection packages"
    fn: object                      # fn(task) -> what was done ("ffmpeg 9.0.2, downloaded"); raises SetupFailed
    required: bool = True           # False: StreamSplitter works without it (Deno)
    after: "Task | None" = None     # start only once this one has finished (two installs into one venv)
    state: str = "waiting"          # waiting | running | done | failed
    working: bool = False           # set once it downloads or installs (not just checks)
    detail: str = ""                # progress: "downloading ffmpeg: 54 of 115 MB"
    result: str = ""
    error: str = ""
    fix: str = ""
    started: float = 0.0


class Background:
    """Runs the download tasks on threads; the wizard calls report() between steps and wait() at the end."""

    def __init__(self, tasks: list[Task]):
        self.tasks = list(tasks)
        self._shown = ""
        self._threads: list[threading.Thread] = []

    def start(self) -> "Background":
        for t in self.tasks:
            th = threading.Thread(target=self._run, args=(t,), name=f"setup-{t.name}", daemon=True)
            th.start()
            self._threads.append(th)
        return self

    def _run(self, t: Task) -> None:
        while t.after is not None and t.after.state in ("waiting", "running"):
            time.sleep(0.2)
        t.state, t.started = "running", time.time()
        try:
            t.result = t.fn(t) or "ready"
            t.state = "done"
        except SetupFailed as e:
            t.error, t.fix, t.state = str(e), e.fix, "failed"
        except Exception as e:                       # a bug or an odd system: say so instead of dying quietly
            t.error, t.state = f"{type(e).__name__}: {e}", "failed"
            t.fix = f"Run {RERUN} again; if it fails the same way, send the window's text to whoever set this up."

    def busy(self) -> bool:
        return any(t.state in ("waiting", "running") for t in self.tasks)

    def failures(self, required_only: bool = False) -> list[Task]:
        return [t for t in self.tasks if t.state == "failed" and (t.required or not required_only)]

    def line(self) -> str:
        parts = []
        for t in self.tasks:
            if not (t.working or t.state == "failed"):
                continue
            if t.state == "running":
                parts.append(f"{t.name}: {t.detail or 'working'} ({time.time() - t.started:.0f} s)")
            elif t.state == "done":
                parts.append(f"{t.name}: ready")
            elif t.state == "failed":
                parts.append(f"{t.name}: FAILED (see the end)")
        return ("Downloads in the background: " + "; ".join(parts)) if parts else ""

    def report(self, io: BaseIO) -> None:
        """One line of progress, if anything changed since the last."""
        line = self.line()
        key = re.sub(r"\(\d+ s\)|\d+ of", "", line)
        if line and key != self._shown:
            self._shown = key
            io.say(f"[{line}]")

    def wait(self, io: BaseIO, poll_s: float = 0.5, every_s: float = 10.0, verbose: bool = False) -> None:
        """Until every task has finished, with a progress line every `every_s`; then each result. Quick checks that
        found everything in place say nothing unless `verbose`."""
        told = False
        last = time.time()
        while self.busy():
            if not told and any(t.working for t in self.tasks):
                io.say("Waiting for the downloads to finish...")
                told = True
            if time.time() - last >= every_s:
                last = time.time()
                line = self.line()
                if line:
                    io.say(f"  {line}")
            time.sleep(poll_s)
        for th in self._threads:
            th.join(timeout=1)
        for t in self.tasks:
            if t.state == "done" and (verbose or t.working):
                io.say(f"  {t.name}: {t.result}")
            elif t.state == "failed":
                io.say(f"  {t.name}: FAILED - {t.error}" + ("" if t.required else " (optional)"))
                if t.fix:
                    io.say(f"    Fix: {t.fix}")


def _run_logged(cmd: list[str], log_path: Path, task: Task, what: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    task.working, task.detail = True, f"installing {what}"
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        log.write("$ " + " ".join(cmd) + "\n")
        log.flush()
        try:
            p = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=str(ROOT))
        except OSError as e:
            raise SetupFailed(f"could not start the installer ({e})", f"Run {RERUN} again.") from None
    if p.returncode != 0:
        tail = [ln.strip() for ln in log_path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        raise SetupFailed(f"installing {what} stopped with an error: {tail[-1][:200] if tail else p.returncode}",
                          f"Check the internet connection and run {RERUN} again; it continues where it stopped. "
                          f"Details: {log_path}")


def _import_problem(modules, check: str = "") -> str | None:
    """None when the modules import (and the `check` code runs) in a fresh Python, else the error's last line."""
    code = "import " + ", ".join(modules) + (f"; {check}" if check else "")
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)
    if r.returncode == 0:
        return None
    lines = [ln for ln in (r.stderr or "").splitlines() if ln.strip()]
    return lines[-1].strip() if lines else f"exit {r.returncode}"


def install_detection(task: Task) -> str:
    missing = [m for m in DETECT_MODULES if importlib.util.find_spec(m) is None]
    if missing:
        reqs = extra_requirements("detect")
        if not reqs:
            raise SetupFailed("the list of detection packages was not found", f"Run {RERUN} from the StreamSplitter "
                              "folder.")
        _run_logged(pip_command() + reqs, LOG_DIR / "setup_detect.log", task, "detection packages")
        importlib.invalidate_caches()
    task.detail = "checking the detection packages"
    problem = _import_problem(DETECT_MODULES, DETECT_CHECK)
    if problem:
        fix = f"Run {RERUN} again."
        if os.name == "nt" and "too long" in problem.lower():     # "The filename or extension is too long"
            fix = f"This folder's path is too long for Windows ({len(str(ROOT))} characters). {SHORT_PATH_FIX}"
        elif os.name == "nt" and "DLL" in problem:
            fix = ("Install Microsoft's Visual C++ Redistributable (https://aka.ms/vs/17/release/vc_redist.x64.exe), "
                   f"then run {RERUN} again.")
        raise SetupFailed(f"the detection packages do not load: {problem[:200]}", fix)
    return "installed" if missing else "already installed"


def _ffmpeg_version() -> str | None:
    """"9.0.2 (in <folder>)" when both ffmpeg and ffprobe are found and run, else None."""
    from . import ffmpeg as ff
    try:
        paths = [ff.find("ffmpeg"), ff.find("ffprobe")]
        runs = [subprocess.run([p, "-version"], capture_output=True, text=True, timeout=60,
                               stdin=subprocess.DEVNULL) for p in paths]
    except (OSError, subprocess.TimeoutExpired):
        return None
    if any(r.returncode for r in runs):
        return None
    m = re.search(r"version (\S+)", (runs[0].stdout or "").split("\n", 1)[0])
    return f"{m.group(1) if m else 'found'} (in {Path(paths[0]).parent})"


def ffmpeg_downloads() -> list[tuple[str, list[str]]]:
    """(zip URL, the programs to take from it) for this computer."""
    machine = platform.machine().lower()
    if os.name == "nt":
        return [(GYAN_URL, ["ffmpeg", "ffprobe"])]  # x64 build; Windows on Arm runs it emulated
    osname = "macos" if sys.platform == "darwin" else "linux" if sys.platform.startswith("linux") else None
    if osname is None or machine not in ("x86_64", "amd64", "arm64", "aarch64"):
        raise SetupFailed(f"there is no ffmpeg download for this computer ({sys.platform} {machine})", FFMPEG_FIX)
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    return [(RIEDL_URL.format(os=osname, arch=arch, tool=t), [t]) for t in ("ffmpeg", "ffprobe")]


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _fetch(url: str, dest: Path | None = None, task: Task | None = None, label: str = "") -> tuple[str, bytes]:
    """GET `url` into `dest` (progress in task.detail) or into memory. Returns (final URL after redirects, body)."""
    req = urllib.request.Request(url, headers={"User-Agent": "StreamSplitter-setup"})
    with urllib.request.urlopen(req, timeout=60, context=_ssl_context()) as r:
        if dest is None:
            return r.geturl(), r.read()
        total, done = int(r.headers.get("Content-Length") or 0), 0
        with open(dest, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if task is not None:
                    task.detail = (f"downloading {label}: {done >> 20} of {total >> 20} MB" if total
                                   else f"downloading {label}: {done >> 20} MB")
        return r.geturl(), b""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _published_sha256(urls: list[str]) -> str | None:
    """The SHA-256 the build site publishes beside the file (<url>.sha256), or None if it cannot be had."""
    for u in urls:
        try:
            _, body = _fetch(u + ".sha256")
        except Exception:
            continue
        m = re.match(r"\s*([0-9a-fA-F]{64})\b", body.decode("ascii", "replace"))
        if m:
            return m.group(1).lower()
    return None


def install_ffmpeg(task: Task) -> str:
    have = _ffmpeg_version()
    if have:
        return f"ffmpeg {have}"
    dest = tools_bin()
    dest.mkdir(parents=True, exist_ok=True)
    task.working = True
    with tempfile.TemporaryDirectory(prefix="streamsplitter-ffmpeg-") as tmp:
        for url, tools in ffmpeg_downloads():
            label = " + ".join(tools)
            zpath = Path(tmp) / f"{tools[0]}.zip"
            try:
                final, _ = _fetch(url, zpath, task, label)
            except Exception as e:
                raise SetupFailed(f"downloading {label} failed ({type(e).__name__}: {e})", FFMPEG_FIX) from None
            want = _published_sha256([final, url])
            if want and _sha256(zpath) != want:
                raise SetupFailed(f"the {label} download was damaged (its checksum does not match)", FFMPEG_FIX)
            task.detail = f"unpacking {label}"
            try:
                with zipfile.ZipFile(zpath) as z:
                    for tool in tools:
                        exe = tool + (".exe" if os.name == "nt" else "")
                        member = next((n for n in z.namelist() if n.replace("\\", "/").rsplit("/", 1)[-1] == exe),
                                      None)
                        if member is None:
                            raise SetupFailed(f"{exe} is not in the download from {url}", FFMPEG_FIX)
                        part = dest / (exe + ".part")
                        with z.open(member) as src, open(part, "wb") as out:
                            shutil.copyfileobj(src, out, 1 << 20)
                        if os.name != "nt":
                            os.chmod(part, 0o755)
                        os.replace(part, dest / exe)
            except zipfile.BadZipFile:
                raise SetupFailed(f"the {label} download is not a zip file", FFMPEG_FIX) from None
    prepare_environment()
    have = _ffmpeg_version()
    if not have:
        raise SetupFailed(f"ffmpeg was downloaded to {dest} but does not run", FFMPEG_FIX)
    return f"ffmpeg {have}, downloaded"


def _deno_version() -> str | None:
    exe = shutil.which("deno")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"deno (\d+)\.(\d+)\.(\d+)", r.stdout or "")
    if r.returncode or not m or tuple(int(x) for x in m.groups()[:2]) < DENO_MIN:
        return None
    return ".".join(m.groups())


def install_deno(task: Task) -> str:
    have = _deno_version()
    if have:
        return f"Deno {have}"
    fix = ("Optional: live streams record without it. To add it, install Deno (https://docs.deno.com/runtime/"
           f"getting_started/installation/) and run {RERUN} again.")
    try:
        _run_logged(pip_command() + (extra_requirements("deno") or ["deno>=2.3"]), LOG_DIR / "setup_deno.log", task,
                    "Deno")
    except SetupFailed as e:
        raise SetupFailed(str(e), fix) from None
    prepare_environment()
    have = _deno_version()
    if not have:
        raise SetupFailed("Deno was installed but does not run", fix)
    return f"Deno {have}, installed"


def path_too_long() -> str | None:
    """Windows: a warning when this folder's path may be too long for the detection packages' DLLs to load. Seen
    2026-11-06: shapely's GEOS DLLs, reached ~92 characters below the folder (shapely\\..\\shapely.libs\\...), did not
    load under a 175-character folder ("The filename or extension is too long"), although long paths were turned on:
    the 260-character limit still holds for DLLs."""
    if os.name != "nt" or len(str(ROOT)) <= ROOT_MAX:
        return None
    return (f"This folder's path is {len(str(ROOT))} characters long ({ROOT}); Windows may not load some of "
            f"StreamSplitter's packages from that deep. {SHORT_PATH_FIX}")


def default_background() -> Background:
    detect = Task("detection packages", install_detection)
    return Background([detect, Task("ffmpeg", install_ffmpeg), Task("Deno", install_deno, required=False,
                                                                    after=detect)])


# ---------------------------------------------------------------------------------------------------------------------
# The outside world (tests replace these)

class _QuietLog:
    def debug(self, msg):
        pass

    info = warning = error = debug


def _short(text: str) -> str:
    s = (str(text).strip().splitlines() or [""])[0]
    return re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*)?", "", s)[:300]


def probe_stream(url: str) -> dict:
    """What a stream link points at, without downloading: {"status": live | upcoming | offline | ended | video |
    error, "title", "channel", "handle", "start" (unix time), "message"}. "offline" is a channel with nothing live or
    scheduled; a channel's /live page is followed to the video it shows."""
    import yt_dlp
    params = {"quiet": True, "noplaylist": True, "skip_download": True, "socket_timeout": 20, "logger": _QuietLog(),
              "ignore_no_formats_error": True}
    try:
        with yt_dlp.YoutubeDL(params) as ydl:
            info = ydl.extract_info(url, download=False, process=False)
            for _ in range(2):
                if info.get("_type") in ("url", "url_transparent") and info.get("url"):
                    info = ydl.extract_info(info["url"], download=False, process=False)
    except Exception as e:
        msg = _short(str(e))
        low = msg.lower()
        if "not currently live" in low:
            return {"status": "offline", "message": msg}
        if "will begin" in low or "premieres in" in low:
            return {"status": "upcoming", "message": msg}
        return {"status": "error", "message": msg}
    if info.get("_type") == "playlist":
        return {"status": "error", "message": "that is a channel or playlist page, not a stream",
                "channel": info.get("channel")}
    ls = info.get("live_status") or ("is_live" if info.get("is_live") else None)
    status = {"is_live": "live", "is_upcoming": "upcoming", "was_live": "ended", "post_live": "ended",
              "not_live": "video"}.get(ls, "live" if info.get("is_live") else "video")
    handle = info.get("uploader_id") if str(info.get("uploader_id") or "").startswith("@") else None
    return {"status": status, "title": info.get("title") or "", "channel": info.get("channel") or info.get("uploader"),
            "handle": handle, "channel_url": info.get("channel_url"),
            "start": info.get("release_timestamp") or info.get("timestamp"), "message": ""}


class Services:
    """TBA, yt-dlp, Slack, YouTube, Google Drive, the team wiki and the live runner, imported only when a step needs
    them."""

    def tba_get(self, path: str, key: str):
        from . import tba
        return tba.get(path, key=key)

    def probe_stream(self, url: str) -> dict:
        return probe_stream(url)

    def slack(self):
        from .publish import slack
        return slack

    def youtube(self):
        from .publish import youtube
        return youtube

    def templates(self):
        from .publish import templates
        return templates

    def wiki(self):
        from .publish import wiki
        return wiki

    def drive(self):
        from .publish import drive
        return drive

    def live(self):
        from . import live
        return live

    def listener(self):
        """The Slack listener's module (listener.py): its log-in start, start, stop and running check."""
        from . import listener
        return listener

    def notifier_alarm_text(self) -> str:
        from .publish.notify import Notifier
        return Notifier(None, None, threaded=False).alarm_text(time.time())

    def suggest_streams(self, event_key: str):
        """streams.suggest_streams(event_key): [{"url", "label", ...}], the recommended first, the same choice
        `stream auto` makes from Slack; None when this copy has no streams.py (the wizard then asks TBA itself)."""
        if importlib.util.find_spec(f"{__package__}.streams") is None:
            return None
        from . import streams
        fn = getattr(streams, "suggest_streams", None)
        if fn is None:
            return None
        return fn(event_key, tba_get=lambda path: self.tba_get(path, config.tba_key()))

    def slack_commands_help(self, channel_name: str | None = None, slack: bool = True) -> str:
        """The commands the contact (the owner) can reply to the StreamSplitter app (publish/commands.help_text, the
        same list `help` gives in Slack): Slack formatting for a DM, plain text (slack=False) for this window; ""
        when this copy has none."""
        try:
            from .publish import commands
            wiki = bool(config.section("wiki").get("enabled"))
            drive = bool(config.section("drive").get("enabled"))
            listen = bool(config.section("listen").get("enabled"))
            return str(commands.help_text(channel_name, slack=slack, wiki=wiki, drive=drive, listen=listen)
                       or "").strip()
        except Exception:
            return ""


# ---------------------------------------------------------------------------------------------------------------------
# Helpers

def _date_range(start: str | None, end: str | None) -> str:
    try:
        a = _dt.date.fromisoformat(start)
        b = _dt.date.fromisoformat(end or start)
    except (TypeError, ValueError):
        return ""
    if a == b:
        return f"{a:%b} {a.day}, {a.year}"
    if (a.year, a.month) == (b.year, b.month):
        return f"{a:%b} {a.day}-{b.day}, {a.year}"
    return f"{a:%b} {a.day} - {b:%b} {b.day}, {b.year}"


def _event_line(e: dict) -> str:
    where = ", ".join(x for x in (e.get("city"), e.get("state_prov")) if x)
    return " - ".join(x for x in (e.get("name") or e["key"], where, _date_range(e.get("start_date"),
                                                                                e.get("end_date"))) if x) \
        + f" ({e['key']})"


def normalize_stream_url(url: str) -> str:
    """https:// added; a YouTube channel page (youtube.com/@Name, /channel/ID, .../streams) becomes its /live page,
    which follows whatever the channel streams."""
    url = url.strip().strip("\"'").strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        url = "https://" + url
    u = urllib.parse.urlsplit(url)
    host = u.netloc.lower().removeprefix("www.").removeprefix("m.")
    parts = [p for p in u.path.split("/") if p]
    if host == "youtube.com" and parts and not u.query:
        n = 1 if parts[0].startswith("@") else 2 if parts[0] in ("channel", "c", "user") else 0
        if n and len(parts) >= n and (len(parts) == n or parts[n] in ("streams", "videos", "featured", "home")):
            return "https://www.youtube.com/" + "/".join(parts[:n] + ["live"])
    return url


_GENERIC = {"event", "district", "regional", "presented", "championship", "division", "first", "robotics",
            "competition", "the", "and", "with", "frc"}


def _mentions_event(title: str, ev: dict) -> bool:
    """Whether a stream title names the event (any distinctive word of its name); True when that cannot be told."""
    words = {w for w in re.findall(r"[a-z0-9]+", str(ev.get("name") or "").lower())
             if len(w) >= 4 and w not in _GENERIC and not w.isdigit()}
    if not words:
        return True
    have = set(re.findall(r"[a-z0-9]+", title.lower()))
    return bool(words & have)


def _is_single_video(url: str) -> bool:
    u = urllib.parse.urlsplit(url)
    return "v=" in u.query or "youtu.be" in u.netloc or bool(re.search(r"/live/[\w-]{11}", u.path))


def parse_teams(text: str) -> list[str]:
    out = []
    for t in re.split(r"[\s,;]+", text.strip()):
        t = t.strip().lower().removeprefix("frc").upper()
        if t and t not in out:
            out.append(t)
    return out


def _bad_teams(text: str) -> str | None:
    teams = parse_teams(text)
    if not teams:
        return "type at least one team number"
    bad = [t for t in teams if not re.fullmatch(r"\d{1,5}[A-Z]?", t)]
    return f"not a team number: {', '.join(bad)}" if bad else None


FOLDERID_DOWNLOADS = "{374DE290-123F-4565-9164-39C4925E467B}"
FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"


def downloads_dir() -> Path:
    """Where browsers save downloads (the Google client file is looked for there). Windows: the Downloads known
    folder, which can be moved (seen 2026-11-04: D:\\Downloads, so ~/Downloads did not exist); Linux: XDG_DOWNLOAD_DIR
    from ~/.config/user-dirs.dirs; else, and as the fallback, ~/Downloads."""
    found = _windows_downloads() if os.name == "nt" else None if sys.platform == "darwin" else _xdg_downloads()
    return found or Path.home() / "Downloads"


def desktop_dir() -> Path:
    """The desktop folder (a team settings file can be saved there): Windows' Desktop known folder (OneDrive often
    moves it), Linux's XDG_DESKTOP_DIR, else ~/Desktop."""
    if os.name == "nt":
        found = _windows_known_folder(FOLDERID_DESKTOP)
    else:
        found = None if sys.platform == "darwin" else _xdg_downloads("XDG_DESKTOP_DIR")
    return found or Path.home() / "Desktop"


def _windows_downloads() -> Path | None:
    return _windows_known_folder(FOLDERID_DOWNLOADS)


def _windows_known_folder(folder_id: str) -> Path | None:
    """SHGetKnownFolderPath(folder_id); None when the shell cannot say."""
    try:
        import ctypes
        import uuid
        from ctypes import wintypes
        guid = (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(folder_id).bytes_le)
        fn = ctypes.WinDLL("shell32").SHGetKnownFolderPath
        fn.argtypes = (ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p))
        fn.restype = ctypes.HRESULT                  # a failure raises OSError
        ptr = ctypes.c_void_p()
        fn(ctypes.byref(guid), 0, None, ctypes.byref(ptr))
        try:
            return Path(ctypes.wstring_at(ptr.value)) if ptr.value else None
        finally:
            free = ctypes.WinDLL("ole32").CoTaskMemFree
            free.argtypes = (ctypes.c_void_p,)
            free(ptr)
    except (OSError, AttributeError, ValueError):
        return None


def _xdg_downloads(name: str = "XDG_DOWNLOAD_DIR") -> Path | None:
    """XDG_DOWNLOAD_DIR (or another user dir) from user-dirs.dirs (e.g. XDG_DOWNLOAD_DIR="$HOME/Downloads"), None
    without one."""
    f = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "user-dirs.dirs"
    try:
        text = f.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    m = re.search(rf'^\s*{name}\s*=\s*"?([^"\n]*)"?\s*$', text, re.M)
    value = m.group(1).replace("$HOME", str(Path.home())).strip() if m else ""
    return Path(os.path.expanduser(value)) if value else None


def find_user_file(answer: str) -> Path:
    """The file a person typed or pasted: surrounding quotes dropped (Explorer's "Copy as path" adds them), ~
    expanded; a bare name or relative path is looked for in the current folder, then in the Downloads folder, also
    with ".json" added (Windows hides extensions, so the name shown has none). The path as typed when none exists,
    for the error message."""
    p = Path(os.path.expanduser(_clean(answer).strip("\"'")))
    if p.is_absolute():
        return p
    for base in (Path.cwd(), downloads_dir()):
        for cand in (base / p, base / f"{p}.json"):
            if cand.is_file():
                return cand
    return p


def find_team_file(answer: str) -> Path:
    """A team settings file the person typed or pasted, as find_user_file, also with its suffix added and looked for
    on the desktop."""
    p = find_user_file(answer)
    if p.is_file():
        return p
    name = Path(os.path.expanduser(_clean(answer).strip("\"'")))
    tries = [p.with_name(p.name + teamkit.SUFFIX)]
    if not name.is_absolute():
        tries += [base / n for base in (downloads_dir(), desktop_dir()) for n in (name, f"{name}{teamkit.SUFFIX}")]
    return next((c for c in tries if c.is_file()), p)


def _newest(folders, pattern: str) -> list[Path]:
    """Files matching `pattern` in `folders`, newest first."""
    out = {}
    for d in folders:
        try:
            for p in Path(d).glob(pattern):
                if p.is_file():
                    out[p] = p.stat().st_mtime
        except OSError:
            continue
    return sorted(out, key=out.get, reverse=True)


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _json_file(p: Path) -> bool:
    """Whether a file is complete JSON (Firefox puts an empty file with the final name down while it downloads)."""
    try:
        json.loads(p.read_text(encoding="utf-8"))
        return True
    except (OSError, ValueError):
        return False


def google_page(name: str, project: str | None = None) -> str:
    url = GOOGLE_PAGES[name]
    return f"{url}?project={urllib.parse.quote(project)}" if project else url


def project_of(client_cfg: dict | None) -> str | None:
    """The Google Cloud project ID inside a downloaded client file ("project_id"), or None."""
    p = ((client_cfg or {}).get("installed") or {}).get("project_id")
    return p if isinstance(p, str) and PROJECT_ID.fullmatch(p) else None


def _bad_project_id(text: str) -> str | None:
    if PROJECT_ID.fullmatch(text.strip().lower()):
        return None
    return ("That does not look like a project ID (lowercase letters, digits and dashes, like streamsplitter-5026). "
            "Press Enter alone to skip it.")


# Google's login failures: (words in the error, what it means, what to fix, the page to open, what follows: "login"
# again or a new "client"). Checked in this order: org_internal and admin blocks also say "Access blocked".
LOGIN_PROBLEMS = (
    (("admin_policy_enforced", "admin needs to review", "blocked by your administrator", "institution"),
     "Your school's or company's Google admin does not allow outside apps on that account.",
     "Log in with an account that is not a school or work account and that owns or manages the team's channel.",
     None, "login"),
    (("org_internal", "within its organization"),
     "The sign-in app's audience is Internal: only accounts of one school or company may use it.",
     "On the page that opens, click Make external (under User type), then Publish app.", "audience", "login"),
    (("redirect_uri_mismatch", "web application", "invalid_client", "deleted_client", "client was not found",
      "unauthorized_client", "request is invalid"),
     "The client file is not a working Desktop app client (wrong type, or deleted in Google Cloud).",
     "Make a new one: on the page that opens, click Create client; Application type: Desktop app; Create; then "
     "Download JSON.", "clients", "client"),
    (("access_denied", "being tested", "approved testers", "access blocked", "verification process"),
     "The sign-in app is still in Testing, so Google only lets in the testers listed on it.",
     "On the page that opens, click Publish app, then Confirm, so it says In production. (If you clicked Cancel in "
     "Google's window instead, nothing needs fixing.)", "audience", "login"),
    (("in time", "timed out", "timeout"),
     "The login was not finished within 10 minutes.", "", None, "login"),
    (("did not grant", "scope"),
     "The YouTube permission was not allowed.", "Log in again and allow it (tick its box if there is one).", None,
     "login"),
)


def _gb_free(path: Path) -> float | None:
    try:
        return shutil.disk_usage(path).free / 1e9
    except OSError:
        return None


def _error_text(e: Exception) -> str:
    return str(e) or type(e).__name__


# ---------------------------------------------------------------------------------------------------------------------
# The wizard

class Wizard:
    def __init__(self, io: BaseIO, svc: Services | None = None, background: Background | None = None):
        self.io = io
        self.svc = svc or Services()
        self.bg = background
        self._slack = None              # a SlackClient once a token works
        self._yt = None                 # a YouTubeClient once logged in
        self._wiki = None               # a WikiClient once its bot login works
        self._drive = None              # a DriveClient once logged in
        self._apis_on: set[str] = set()  # Google APIs the person turned on in this run ("drive")
        self.event_changed = False      # step_event switched to another event (its stream is asked next)
        self.kit = None                 # the team settings file loaded in this run: Slack/YouTube keep what it set
        self._contact_chosen = False    # the alert contact was picked in this run: the alerts step does not re-ask

    # -- small helpers -------------------------------------------------------------------------------------------------
    def say(self, text: str = "") -> None:
        self.io.say(text)

    def heading(self, title: str) -> None:
        self.say()
        self.say(f"=== {title} " + "=" * max(3, 74 - len(title)))

    def progress(self) -> None:
        if self.bg is not None:
            self.bg.report(self.io)

    @staticmethod
    def save(name: str, values: dict) -> None:
        config.save_sections({name: values})

    @staticmethod
    def configured() -> bool:
        ev = config.section("event")
        return bool(ev.get("key") and ev.get("stream_url"))

    # -- entry ---------------------------------------------------------------------------------------------------------
    def run(self, section: str | None = None) -> int:
        if section:
            self.step(section)
            if section == "event" and self.event_changed:
                self.step("stream")
            self.progress()
            return 0
        if self.configured():
            return self.menu()
        self.say("StreamSplitter setup")
        self.say("StreamSplitter records an FRC event's stream and saves each match as its own video. It can also")
        self.say("post your team's matches to Slack, and upload every match to YouTube and to a Google Drive folder")
        self.say("shared with your team.")
        self.say("Setup takes about 15 minutes (30 the first time your team sets up Slack and YouTube). Answers are")
        self.say("saved as you go: stop any time with Ctrl+C and run it again to continue.")
        if self.bg is not None:
            self.say("While you answer, the rest of StreamSplitter downloads in the background.")
        # first: the file carries the TBA key, which the event step would otherwise have the person make
        self.offer_team_file()
        self.progress()
        # the wiki is not here since 2026-11-05 (it cannot store videos yet): setup --section wiki, or the menu
        for name in ("event", "stream", "slack", "youtube", "drive", "notify", "listen", "test"):
            self.step(name)
            self.progress()
        self.finish()
        return 0

    def step(self, name: str) -> None:
        {"event": self.step_event, "tba": lambda: self.ensure_tba_key(force=True), "stream": self.step_stream,
         "slack": self.step_slack, "youtube": self.step_youtube, "drive": self.step_drive, "wiki": self.step_wiki,
         "notify": self.step_notify, "listen": self.step_listen, "test": self.step_test,
         "team": self.step_team, "tbalinks": self.step_tbalinks}[name]()

    def menu(self) -> int:
        if self.bg is not None and self.bg.busy():
            self.bg.wait(self.io)          # the live runner imports the detection packages: not while they install
        while True:
            ev = config.section("event")
            running = self.is_running()
            self.say()
            self.say(f"StreamSplitter is set up for {ev.get('name') or ev.get('key')} ({ev.get('key')})"
                     + (": it is running now." if running else "."))
            self.progress()
            opts = ["Start recording (in the background)", "Show status", "Stop recording", "Change a setting",
                    "Send the test upload again", "Share these settings with a teammate", "Show the setup summary",
                    self.listen_menu_label(), "Exit"]
            i = self.io.choose("What would you like to do?", opts, default=1 if running else 0)
            if i == 0:
                self.start()
            elif i == 1:
                self.show_status()
            elif i == 2:
                self.stop()
            elif i == 3:
                names = ["The event", "The Blue Alliance key", "The stream link", "Slack (teams, channel, contact)",
                         "YouTube", "Google Drive", "Team website (wiki)", "Who gets alerts",
                         "Start from Slack (start and stop recordings from Slack)",
                         "The Blue Alliance video links (event organizers)",
                         "Load a team settings file from a teammate", "Back"]
                steps = ("event", "tba", "stream", "slack", "youtube", "drive", "wiki", "notify", "listen", "tbalinks")
                j = self.io.choose("Which setting?", names, default=len(names) - 1)
                if j < len(names) - 1:
                    self.event_changed = False
                    old_tag = self._tag()              # the running recording's, also when the event changes
                    if j == len(steps):
                        self.import_team_settings()
                    else:
                        self.step(steps[j])
                    if self.event_changed:
                        self.step("stream")
                    if running and steps[j:j + 1] != ("listen",):     # the listener is not the recording's
                        if self.io.confirm("StreamSplitter is running with the old settings. Restart it now so it "
                                           "uses the new ones?", True):
                            self.restart(old_tag)
                        else:
                            self.say("It keeps the old settings until it is stopped and started again.")
            elif i == 4:
                self.step_test()
            elif i == 5:
                self.share_team_settings()
            elif i == 6:
                self.summary()
            elif i == 7:
                self.listen_menu()
            else:
                return 0

    # -- event and TBA -------------------------------------------------------------------------------------------------
    def tba_check(self, key: str) -> str:
        """"ok", "bad" (TBA refused the key) or "offline"."""
        try:
            self.svc.tba_get("/status", key)
            return "ok"
        except Exception as e:
            return "bad" if re.search(r"HTTP 40[13]", str(e)) else "offline"

    def ensure_tba_key(self, force: bool = False) -> str | None:
        """A working TBA key: the saved one, or a new one the person gets now. None when they go on without."""
        key = config.tba_key()
        if key:
            state = self.tba_check(key)
            if state == "ok":
                if not force:
                    self.say("The Blue Alliance key: saved, and it works.")
                    return key
                if self.io.confirm("A working Blue Alliance key is already saved. Keep it?", True):
                    return key
            elif state == "offline":
                self.say("Could not reach The Blue Alliance to check the saved key (no internet?); using it as is.")
                if not force:
                    return key
            else:
                self.say("The Blue Alliance refused the saved key, so let's get a new one.")
        return self.new_tba_key()

    def new_tba_key(self) -> str | None:
        self.heading("The Blue Alliance key")
        self.say("StreamSplitter reads the event's match schedule from The Blue Alliance (TBA) with a free 'read")
        self.say("key'. To get one (a minute):")
        self.say("  1. Log in on the page that opens (a Google or Apple account works).")
        self.say("  2. Under 'Read API Keys', type a description (StreamSplitter) and click 'Add New Key'.")
        self.say("  3. Copy the key in the 'X-TBA-Auth-Key' column.")
        self.io.open_url(TBA_ACCOUNT_URL)
        while True:
            key = self.io.ask_secret("Paste the key (hidden as you type; Enter alone to skip)")
            if not key:
                if self.io.confirm("Go on without a TBA key? Videos are then named from the stream's overlay only",
                                   False):
                    return None
                continue
            state = self.tba_check(key)
            if state == "bad":
                self.say("The Blue Alliance did not accept that key. Copy the whole key (64 letters and digits) from")
                self.say("the X-TBA-Auth-Key column and paste it again.")
                continue
            if state == "offline" and not self.io.confirm("Could not reach The Blue Alliance to check the key (no "
                                                          "internet?). Save it anyway?", True):
                continue
            where = config.set_secret("tba_key", key)
            self.say("Saved the key " + ("in this computer's password store." if where == "keyring"
                                         else f"in {where} (not in config.toml)."))
            if os.environ.get("TBA_API_KEY"):
                self.say("Note: the TBA_API_KEY environment variable is set and is used instead of this key.")
            return key

    def step_event(self) -> None:
        self.heading("Step 1: the event")
        cur = config.section("event")
        self.say("StreamSplitter names each video from the event's schedule on The Blue Alliance (TBA). Type part")
        self.say("of the event's name or city and pick it from a list, or type its code: the end of the event's")
        self.say("TBA page address (thebluealliance.com/event/2026mttd  ->  2026mttd).")
        key = self.ensure_tba_key()
        while True:
            answer = self.io.ask("Event code, or part of its name", default=cur.get("key") or None)
            ev = self.find_event(answer, key)
            if ev:
                break
        changed, values = save_event(ev)
        if changed:
            self.event_changed = True
        self.say(f"Saved: {_event_line({**ev, **values})}")
        yt = config.section("youtube")
        if self.event_changed and yt.get("enabled"):
            self.say("The event changed: make its YouTube playlist too (setup --section youtube). Nothing is uploaded "
                     "until then.")
        wk = config.section("wiki")
        if self.event_changed and wk.get("enabled"):
            self.say("The event changed: check its wiki page too (setup --section wiki). Until then its matches go to "
                     "a page named after the event.")

    def find_event(self, answer: str, key: str | None) -> dict | None:
        """The event the person means, confirmed; None to ask again."""
        a = answer.strip().lower()
        if re.fullmatch(r"\d{4}[a-z0-9]+", a):
            if key is None:
                self.say(f"Using {a} without checking it (no TBA key). Make sure it matches thebluealliance.com/"
                         f"event/{a}.")
                return {"key": a, "name": a, "year": int(a[:4])}
            try:
                ev = self.svc.tba_get(f"/event/{a}/simple", key)
            except Exception as e:
                if "HTTP 404" in str(e):
                    self.say(f"TBA has no event with the code {a}. Check it at thebluealliance.com/event/{a}, or type")
                    self.say("part of the event's name to search.")
                    return None
                self.say(f"Could not reach The Blue Alliance ({_error_text(e)}); using {a} as typed.")
                return {"key": a, "name": a, "year": int(a[:4])}
            self.say(f"Found: {_event_line(ev)}")
            return ev if self.io.confirm("Is this your event?", True) else None
        if key is None:
            self.say("Searching needs a TBA key; type the event's code instead (thebluealliance.com/event/<code>).")
            return None
        m = re.match(r"^(\d{4})\s+(.*)$", a)
        year, words = (int(m.group(1)), m.group(2)) if m else (_dt.date.today().year, a)
        words = words.split()
        try:
            events = self.svc.tba_get(f"/events/{year}/simple", key) or []
        except Exception as e:
            self.say(f"Could not search The Blue Alliance ({_error_text(e)}). Type the event's code instead.")
            return None

        def text(e):
            return " ".join(str(e.get(k) or "") for k in ("name", "city", "state_prov", "key", "event_code")).lower()
        hits = [e for e in events if all(w in text(e) for w in words)]
        today = _dt.date.today().isoformat()
        hits.sort(key=lambda e: (str(e.get("end_date") or "") < today, str(e.get("start_date") or "")))
        if not hits:
            self.say(f"No {year} event matches '{answer}'. Try fewer words or the city, or put a year first (for "
                     f"example: {year - 1} {words[0] if words else 'madtown'}).")
            return None
        hits = hits[:9]
        i = self.io.choose(f"{year} events matching '{answer}':", [_event_line(e) for e in hits]
                           + ["None of these: search again"], default=0)
        return hits[i] if i < len(hits) else None

    # -- stream --------------------------------------------------------------------------------------------------------
    def webcast_suggestions(self, ev: dict) -> list[tuple[str, str]]:
        """(url, label) for the streams TBA lists for the event, the recommended one first: the runtime's
        suggest_streams(event_key) when this copy has it (the same choice `stream auto` makes from Slack), else
        tba_webcasts() here."""
        if not ev.get("key"):
            return []
        try:
            got = self.svc.suggest_streams(ev["key"])
        except Exception:
            got = None
        if got is None:
            return self.tba_webcasts(ev)
        out = []
        for s in got or []:
            if isinstance(s, dict):
                url = s.get("url")
                label = s.get("label") or s.get("title") or s.get("note") or url
            elif isinstance(s, (tuple, list)) and s:
                url, label = s[0], (s[1] if len(s) > 1 and s[1] else s[0])
            else:
                url = label = str(s)
            if url:
                out.append((str(url), str(label)))
        return out

    def tba_webcasts(self, ev: dict) -> list[tuple[str, str]]:
        """(url, label) for the streams TBA lists for the event, the recommended one first. With a new YouTube video
        per day, the channel's /live page, which follows from day to day; with a single video, that video, since a
        channel that streams several events (a district's, FIRST's) may show another event on its /live page."""
        key = config.tba_key()
        if not (key and ev.get("key")):
            return []
        try:
            casts = (self.svc.tba_get(f"/event/{ev['key']}", key) or {}).get("webcasts") or []
        except Exception:
            return []
        out: list[tuple[str, str]] = []
        yt = [c for c in casts if c.get("type") == "youtube" and c.get("channel")]
        videos = [(f"https://www.youtube.com/watch?v={c['channel']}", c.get("date")) for c in yt
                  if not (str(c["channel"]).startswith("UC") and len(str(c["channel"])) == 24)]
        live_page, who = None, "The event's channel"
        ids = [str(c["channel"]) for c in yt if str(c["channel"]).startswith("UC") and len(str(c["channel"])) == 24]
        if ids:
            live_page = f"https://www.youtube.com/channel/{ids[0]}/live"
        elif videos:
            info = self.svc.probe_stream(videos[0][0])
            if info.get("handle"):
                live_page = f"https://www.youtube.com/{info['handle']}/live"
                who = f"{info.get('channel') or info['handle']}"
        if live_page and len(videos) != 1:
            out.append((live_page, f"{who} on YouTube, live page (follows each day's stream"
                        + (f"; TBA lists {len(videos)} day streams" if len(videos) > 1 else "") + "; recommended)"))
        for i, (url, date) in enumerate(videos):
            label = f"The stream of {date}" if date else "The event's stream"
            if len(videos) == 1:
                out.append((url, f"{label} ({url.split('//', 1)[1]}; recommended)"))
            else:
                out.append((url, f"Only the stream of {date or 'one day'} ({url.split('//', 1)[1]})"))
        if live_page and len(videos) == 1:
            out.append((live_page, f"{who} on YouTube, live page (whatever the channel streams next; it may "
                        "stream other events too)"))
        for c in casts:
            if c.get("type") == "twitch" and c.get("channel"):
                out.append((f"https://www.twitch.tv/{c['channel']}", f"Twitch: twitch.tv/{c['channel']}"))
        return out

    def step_stream(self) -> None:
        self.heading("Step 2: the stream")
        ev = config.section("event")
        self.say("StreamSplitter records the event's live stream (YouTube or Twitch) and cuts the matches from it.")
        cur = ev.get("stream_url")
        sugg = self.webcast_suggestions(ev)
        if sugg:
            self.say(f"The Blue Alliance lists this stream for the event: {sugg[0][0]}")
        elif config.tba_key() and ev.get("key"):
            self.say("The Blue Alliance lists no stream for this event yet, so type its link. (Once TBA lists one,")
            self.say("replying 'stream auto' to the StreamSplitter app in Slack switches to it.)")
        while True:
            if sugg:
                urls = [u for u, _ in sugg]
                if len(sugg) == 1 and (not cur or cur == urls[0]):
                    url = urls[0] if self.io.confirm("Use it?", True) else self.ask_stream_link(cur)
                else:
                    opts = [label for _, label in sugg]
                    if cur and cur not in urls:
                        opts.insert(0, f"Keep the current link: {cur}")
                        urls.insert(0, cur)
                    opts.append("Type a different link")
                    i = self.io.choose("Use it, or pick another:", opts, default=urls.index(cur) if cur in urls else 0)
                    url = urls[i] if i < len(urls) else self.ask_stream_link(cur)
            else:
                url = self.ask_stream_link(cur)
            url = normalize_stream_url(url)
            if self.check_stream(url, ev):
                self.save("event", {"stream_url": url})
                self.say(f"Saved the stream link: {url}")
                self.say("To change it later: setup --section stream, or reply 'stream <link>' to the StreamSplitter "
                         "app in Slack.")
                return

    def ask_stream_link(self, cur: str | None) -> str:
        self.say("The best link is the channel's live page (youtube.com/@ChannelName/live or twitch.tv/name): it")
        self.say("keeps working when the event starts a new stream each day. Copy it from the browser's address bar.")
        return self.io.ask("Stream link (YouTube or Twitch)", default=cur or None)

    def check_stream(self, url: str, ev: dict) -> bool:
        self.say("Checking the link...")
        try:
            info = self.svc.probe_stream(url)
        except Exception as e:
            info = {"status": "error", "message": _error_text(e)}
        st, title = info.get("status"), info.get("title") or ""
        who = f" ({info['channel']})" if info.get("channel") else ""
        multi_day = bool(ev.get("start_date") and ev.get("end_date") and ev["start_date"] != ev["end_date"])
        if st in ("live", "upcoming") and _is_single_video(url) and multi_day:
            self.say("Note: this is one video. If the event streams each day as a new video, use the channel's live")
            self.say("page instead (youtube.com/@ChannelName/live), so the next days are recorded too.")
        if st in ("live", "upcoming"):
            if st == "live":
                self.say(f"Live now: {title}{who}.")
            else:
                when = ""
                if info.get("start"):
                    when = time.strftime(", starting %a %b %d at %H:%M", time.localtime(info["start"]))
                self.say(f"Scheduled: {title}{who}{when}." if title else f"Scheduled ({info.get('message')}).")
            if title and not _mentions_event(title, ev):
                self.say(f"Note: '{title}' does not look like {ev.get('name')}. A channel that streams several")
                self.say("events can show another one here; if so, use the event's own video link instead.")
                return self.io.confirm("Use this link?", True)
            return True
        if st == "offline":
            self.say("The channel is not live and has nothing scheduled: normal days before the event. StreamSplitter")
            self.say("waits for the stream to start.")
            return self.io.confirm("Use this link?", True)
        if st == "ended":
            self.say(f"This stream has ended: {title}{who}. Use the channel's live page instead.")
            return self.io.confirm("Use it anyway?", False)
        if st == "video":
            self.say(f"This is a recorded video, not a live stream: {title}{who}. It is only useful for a test.")
            return self.io.confirm("Use it anyway?", False)
        self.say(f"That link does not work: {info.get('message') or 'no stream found'}.")
        self.say("Copy the address from your browser's address bar while the channel or stream is open.")
        return self.io.confirm("Use it anyway?", False)

    # -- Slack ---------------------------------------------------------------------------------------------------------
    def step_slack(self) -> None:
        self.heading("Step 3: Slack")
        cur = config.section("slack")
        if self.kit and "slack" in self.kit and cur.get("enabled") and self.slack_from_team_file(cur):
            return
        self.say("StreamSplitter can post your team's matches to a Slack channel a minute after each one ends, and")
        self.say("send one person a direct message (DM) when something goes wrong.")
        if not self.io.confirm("Post your team's matches to Slack?", bool(cur.get("enabled", True))):
            self.save("slack", {"enabled": False})
            self.say("Slack is off. (To turn it on later: setup --section slack)")
            return
        teams = parse_teams(self.io.ask("Which teams' matches go to Slack? (team numbers, separated by commas)",
                                        default=", ".join(cur.get("teams") or DEFAULT_TEAMS) or None,
                                        check=_bad_teams))
        self.check_teams(teams)
        self.save("slack", {"teams": teams})
        client = self.slack_login()
        if client is _FROM_FILE:
            if self.slack_from_team_file(config.section("slack")):
                return
            client = self.slack_login(offer_file=False)
        if client is None:
            return self._slack_off()
        ch = self.slack_channel(client, cur.get("channel_name") or DEFAULT_CHANNEL)
        if ch is None:
            return self._slack_off()
        # the fix the Slack destination suggests when a workspace refuses large uploads
        small = self.io.choose("Video size for Slack", ["As cut (1080p, about 90-130 MB a match)",
                                                        "720p, about 40 MB (quicker to post on venue Wi-Fi and to "
                                                        "open on a phone)"],
                               default=1 if int(cur.get("clip_height") or 0) else 0) == 1
        self.save("slack", {"enabled": True, "channel_id": ch["id"], "channel_name": ch["name"],
                            "score_in_thread": bool(cur.get("score_in_thread", True)),
                            "clip_height": 720 if small else 0})
        self.say(f"Saved: matches with team{'s' if len(teams) > 1 else ''} {', '.join(teams)} go to #{ch['name']}.")
        self.pick_contact(client)

    def _slack_off(self) -> None:
        self.save("slack", {"enabled": False})
        self.say("Slack is off for now. (To set it up later: setup --section slack)")

    def slack_from_team_file(self, cur: dict) -> bool:
        """Slack as the team settings file set it up: its token and channel are checked, and only this laptop's alert
        contact is asked. False when the file's token does not work (the usual questions follow)."""
        slack = self.svc.slack()
        token = config.get_secret("slack_bot_token")
        if not token:
            return False
        client, info = slack.SlackClient(token), {}
        try:
            info = client.auth_test()
        except PermanentError as e:
            self.say(f"The Slack token in the team settings file does not work: {_error_text(e)}")
            self.say("Ask the teammate for a new file, or set Slack up here.")
            return False
        except Exception as e:
            self.say(f"Could not reach Slack to check the team settings ({_error_text(e)}); using them as they are.")
        teams = ", ".join(cur.get("teams") or DEFAULT_TEAMS) or "no team"
        where =f" in the {info['team']} workspace" if info.get("team") else ""
        self.say(f"From the team settings file: matches with {teams} go to #{cur.get('channel_name')}{where}.")
        self._slack = client
        if info:
            self.check_scopes(client, info)
            ch = self.slack_channel(client, cur.get("channel_name") or DEFAULT_CHANNEL, ask_name=False)
            if ch is None:
                self._slack_off()
                return True
            if ch["id"] != cur.get("channel_id") or ch["name"] != cur.get("channel_name"):
                self.save("slack", {"channel_id": ch["id"], "channel_name": ch["name"]})
        self.pick_contact(client)
        return True

    def check_teams(self, teams: list[str]) -> None:
        ev, key = config.section("event"), config.tba_key()
        if not (ev.get("key") and key):
            return
        try:
            listed = set(self.svc.tba_get(f"/event/{ev['key']}/teams/keys", key) or [])
        except Exception:
            return
        if not listed:
            return                                   # TBA has no team list yet
        missing = [t for t in teams if f"frc{t}" not in listed]
        if missing:
            self.say(f"Note: TBA does not list team{'s' if len(missing) > 1 else ''} {', '.join(missing)} at "
                     f"{ev.get('name') or ev['key']} (yet). Check the number{'s' if len(missing) > 1 else ''}.")

    def slack_login(self, offer_file: bool = True):
        """A SlackClient with a working bot token (saved, or pasted after the walkthrough), _FROM_FILE when a team
        settings file was loaded instead, or None if the person skips."""
        slack = self.svc.slack()
        token = config.get_secret("slack_bot_token")
        if token:
            try:
                client = slack.SlackClient(token)
                info = client.auth_test()
                self.say(f"Slack: connected to the {info.get('team')} workspace.")
                if self.io.confirm("Keep using this Slack connection?", True):
                    self.check_scopes(client, info)
                    self._slack = client
                    return client
            except PermanentError as e:
                self.say(f"The saved Slack connection no longer works: {_error_text(e)}")
            except Exception as e:
                self.say(f"Could not reach Slack to check the saved connection ({_error_text(e)}).")
                if self.io.confirm("Keep it anyway?", True):
                    self._slack = slack.SlackClient(token)
                    return self._slack
        self.say()
        self.say("Slack needs a small app for StreamSplitter to post as. One app serves the whole team: every laptop,")
        self.say("every event, all season.")
        opts = ["No: create it now (about 5 minutes)", "Yes: copy its password (token) from Slack's app page",
                "Yes: a teammate gave me a team settings file", "Skip Slack for now"]
        if not offer_file:
            opts[2] = None
        shown = [o for o in opts if o]
        i = opts.index(shown[self.io.choose("Does your team already have the StreamSplitter Slack app?", shown, 0)])
        if i == 3:
            return None
        if i == 2:
            return _FROM_FILE if self.import_team_settings(follow=False) and "slack" in (self.kit or {}) else \
                self.slack_login(offer_file=False)
        if i == 0:
            if not self.slack_new_app(slack):
                return None
        else:
            # never the create page again: a second app would take another of the workspace's slots
            self.say()
            self.say("Copy the app's password (its Bot User OAuth Token) from Slack, signed in to the team's workspace:")
            self.say("  Click StreamSplitter on the page that opens (not listed? ask whoever made it to add you under")
            self.say("  Basic Information > Collaborators), then OAuth & Permissions, and copy the Bot User OAuth Token.")
            self.io.open_url(SLACK_APPS_URL)
        while True:
            token = self.io.ask_secret("Paste the Bot User OAuth Token (hidden as you type; Enter alone to skip Slack)")
            if not token:
                if self.io.confirm("Skip Slack for now?", True):
                    return None
                continue
            if not token.startswith("xoxb-"):
                kind = {"xoxp-": "a user token", "xapp-": "an app-level token", "xoxe": "a refresh token"}
                what = next((v for k, v in kind.items() if token.startswith(k)), "not a Slack bot token")
                self.say(f"That is {what}. Copy the Bot User OAuth Token (it starts with xoxb-) from OAuth & "
                         "Permissions.")
                continue
            try:
                client = slack.SlackClient(token)
                info = client.auth_test()
            except PermanentError as e:
                self.say(f"Slack did not accept that token: {_error_text(e)}")
                continue
            except Exception as e:
                self.say(f"Could not reach Slack ({_error_text(e)}). Check the internet connection.")
                if not self.io.confirm("Try again?", True):
                    return None
                continue
            config.set_secret("slack_bot_token", token)
            self.say(f"Connected to the {info.get('team')} workspace; the token is saved.")
            self.check_scopes(client, info)
            self._slack = client
            return client

    def slack_new_app(self, slack) -> bool:
        """The three steps that make the team's Slack app; False when the person stops to wait for an admin."""
        self.say()
        self.say("Slack step 1 of 3: create the app, in the browser where you are signed in to Slack.")
        self.say("  A Slack page opens with StreamSplitter filled in: pick the team's workspace, click Next, Next,")
        self.say("  then Create. Do this once for the team: each app uses one of the workspace's app slots.")
        self.io.open_url(slack.create_app_url())
        self.io.ask("Press Enter when the app's Basic Information page shows", default="")
        self.say()
        self.say("Slack step 2 of 3: install it. On the left click Install App, then Install to <workspace>, then")
        self.say("  Allow. If the button says Request to Install, a workspace admin must approve the app: send the")
        self.say("  request; once Slack says it is approved, come back to that page and install it.")
        if self.io.ask("Press Enter when it is installed (or type later to wait for an admin)",
                       default="").lower() == "later":
            self.say("When it is approved and installed, run the setup again: setup --section slack. Answer 'Yes'")
            self.say("to having the app (do not create a second one).")
            return False
        self.say()
        self.say("Slack step 3 of 3: copy the app's password. On the left click OAuth & Permissions and copy the")
        self.say("  Bot User OAuth Token (it starts with xoxb-). Paste it only here.")
        return True

    def check_scopes(self, client, info: dict) -> dict:
        """When the app lacks permissions (an app made before StreamSplitter needed them; im:history since
        2026-11-04, to read the contact's replies), walk through adding them and check again. The token stays the
        same."""
        missing = self._missing_scopes(info)
        while missing:
            names = ", ".join(missing)
            self.say(f"The StreamSplitter Slack app lacks the permission{'s' if len(missing) > 1 else ''} {names}. "
                     "To add:")
            self.say("  1. On the page that opens, click StreamSplitter, then OAuth & Permissions on the left.")
            self.say(f"  2. Under Scopes > Bot Token Scopes, click Add an OAuth Scope and pick {names}.")
            self.say("  3. At the top of the page, click Reinstall to Workspace, then Allow. The token stays the same.")
            self.say("     (Request to Install instead? A workspace admin must approve it first.)")
            self.io.open_url(SLACK_APPS_URL)
            if self.io.ask("Press Enter when it is reinstalled (or type skip)", default="").lower() == "skip":
                self.say("Until then, the parts of StreamSplitter that need it do not work. (To check again: setup "
                         "--section slack)")
                break
            try:
                info = client.auth_test()
            except Exception as e:
                self.say(f"Could not check with Slack ({_error_text(e)}).")
                break
            missing = self._missing_scopes(info)
            if not missing:
                self.say("The Slack app has every permission it needs now.")
        return info

    def _missing_scopes(self, info: dict) -> list[str]:
        missing = list(info.get("missing_scopes") or [])
        required = getattr(self.svc.slack(), "REQUIRED_SCOPES", None)
        if required and info.get("scopes") is not None:
            missing += [s for s in required if s not in info["scopes"] and s not in missing]
        return missing

    def slack_channel(self, client, default: str, ask_name: bool = True) -> dict | None:
        """The channel for the videos, with the app in it (it joins a public channel; a private one needs /invite).
        `ask_name` False tries `default` before asking."""
        name = default
        while True:
            if ask_name or not name:
                name = self.io.ask("Slack channel for the match videos", default=name or None).strip().lstrip("#")
            ask_name = True
            try:
                ch = client.find_channel(name)
            except Exception as e:
                self.say(f"Could not look the channel up: {_error_text(e)}")
                if not self.io.confirm("Try again?", True):
                    return None
                continue
            if ch is None:
                self.say(f"No channel #{name} was found. Check the spelling. A private channel shows up only once the")
                self.say(f"app is in it: in Slack, type /invite @StreamSplitter in #{name}, then try again.")
                continue
            if ch.get("is_member"):
                return ch
            if not ch.get("is_private"):
                try:
                    client.join_channel(ch["id"])
                    self.say(f"The app joined #{ch['name']}.")
                    return ch
                except Exception as e:
                    self.say(f"The app could not join #{ch['name']}: {_error_text(e)}")
            self.say(f"In Slack, open #{ch['name']} and type /invite @StreamSplitter.")
            self.io.ask("Press Enter when that is done", default="")
            ask_name = False

    def pick_contact(self, client, ask_keep: bool = True) -> None:
        """[notify]: the person who gets DMs (errors, warnings, the stopped-running alarm), with a hello DM."""
        cur = config.section("notify")
        self.say()
        self.say("Who should get a Slack DM when something needs attention (errors, warnings, and an alarm if")
        self.say("StreamSplitter stops)? Usually the person running it, or a mentor.")
        if ask_keep and cur.get("slack_user") and self.io.confirm(
                f"Keep sending them to {cur.get('slack_user_name') or cur['slack_user']}?", True):
            self._contact_chosen = True
            return
        while True:
            q = self.io.ask("Their name or email address in Slack")
            try:
                people = client.find_users(q)
            except Exception as e:
                self.say(f"Could not search Slack: {_error_text(e)}")
                if not self.io.confirm("Try again?", True):
                    return
                continue
            if not people:
                self.say(f"Nobody in the workspace matches '{q}'. Try part of their name, or their email address.")
                continue

            def who(p):
                name = p.get("real_name") or p.get("display_name") or p.get("name") or p["id"]
                extra = ", ".join(x for x in (p.get("name") and "@" + p["name"], p.get("email")) if x)
                return f"{name} ({extra})" if extra else name
            if len(people) == 1:
                person = people[0] if self.io.confirm(f"Send them to {who(people[0])}?", True) else None
            else:
                shown = people[:9]
                i = self.io.choose("Who is it?", [who(p) for p in shown] + ["Nobody here: search again"], default=0)
                person = shown[i] if i < len(shown) else None
            if person is None:
                continue
            name = person.get("real_name") or person.get("display_name") or person.get("name") or person["id"]
            ev = config.section("event")
            text = (f"Hi {name}! StreamSplitter (the match video recorder) is being set up for "
                    f"{ev.get('name') or 'the event'}. It will message you here if anything needs attention: errors, "
                    "warnings, and an alarm if it stops running. As its owner you can also change its settings and "
                    "let others use it, from this chat.")
            help_text = self.svc.slack_commands_help(config.section("slack").get("channel_name"))
            text += f"\n\n{help_text}" if help_text else " No need to reply."
            try:
                client.dm(person["id"], text)
                self.say(f"Sent a hello DM to {name}.")
            except Exception as e:
                self.say(f"Could not DM {name}: {_error_text(e)}")
                if not self.io.confirm("Use them anyway?", False):
                    continue
            hb, alarm = int(cur.get("heartbeat_s", HEARTBEAT_S)), int(cur.get("alarm_after_s", ALARM_AFTER_S))
            if (hb, alarm) in OLD_NOTIFY_DEFAULTS:         # the old defaults (60/180: Slack posted cancelled alarms)
                hb, alarm = HEARTBEAT_S, ALARM_AFTER_S
            self.save("notify", {"slack_user": person["id"], "slack_user_name": name, "heartbeat_s": hb,
                                 "alarm_after_s": alarm})
            self._contact_chosen = True
            return

    # -- YouTube -------------------------------------------------------------------------------------------------------
    def step_youtube(self) -> None:
        self.heading("Step 4: YouTube")
        cur = config.section("youtube")
        from_file = bool(self.kit and "youtube" in self.kit and cur.get("enabled"))
        if from_file:
            self.say(f"From the team settings file: matches upload to {cur.get('channel_title') or 'the team channel'}.")
            self.say("Left to set on this laptop: this event's playlist" + ("." if config.get_secret("youtube_token")
                                                                           else ", and the YouTube login."))
        else:
            self.say("StreamSplitter can upload every match to your team's YouTube channel, into a playlist for the")
            self.say("event. Videos are unlisted (only people with the link can see them) unless you choose otherwise.")
        official = is_official_event(config.section("event"))
        if official:
            # user, 2026-11-04: FIRST posts official events' matches on its own channel, and its footage may be
            # registered for YouTube's Content ID, so copies can be claimed or blocked: off unless chosen
            self.say("This is an official FIRST event: FIRST posts every match on its own YouTube channel, and")
            self.say("copies of its webcast can be claimed or blocked by YouTube. Slack is not affected. Upload only")
            self.say("if FIRST has said teams may (ask frcteams@firstinspires.org).")
        if official or not from_file:
            default = False if official else bool(cur.get("enabled", True))
            if not self.io.confirm("Upload matches to YouTube" + (" anyway?" if official else "?"), default):
                self.save("youtube", {"enabled": False})
                self.say("YouTube is off. (To turn it on later: setup --section youtube)")
                return
        if not self.youtube_terms():
            return self._youtube_off()
        yt_mod, tpl = self.svc.youtube(), self.svc.templates()
        client_cfg = self.youtube_client_config(yt_mod, ask_keep=not from_file)
        if client_cfg is None:
            return self._youtube_off()
        yt, ch = self.youtube_login(yt_mod, client_cfg, expect=cur.get("channel_id"),
                                    expect_title=cur.get("channel_title"))
        if yt is None:
            return self._youtube_off()
        ev, sl = config.section("event"), config.section("slack")
        slack_on = bool(sl.get("enabled"))
        if from_file:
            route = cur.get("route") or "all"
        elif slack_on:
            # the team's wish (2026-11-04): its own matches go to Slack and YouTube, all others to YouTube only
            route = "all" if self.io.confirm("Upload your own team's matches to YouTube too (they also go to Slack)?",
                                             cur.get("route") != "others") else "others"
        else:
            route = "all"
            self.say("Slack is off, so every match goes to YouTube.")
        self.say()
        if not from_file:
            self.say("Names follow the FIRST Robotics Competition YouTube channel: playlists like '2026 <event name>',")
            self.say("videos like 'Qualification 12 - <event name>'.")
        default_pl = tpl.render_playlist(tpl.PLAYLIST_FORMAT, ev.get("key"), ev.get("name"), ev.get("year"))
        if cur.get("playlist_title") and cur.get("playlist_event") == ev.get("key"):
            default_pl = cur["playlist_title"]
        playlist_title = self.io.ask("Playlist name", default=default_pl,
                                     check=lambda s: self._format_problem(tpl, s, "playlist"))
        privacy = self.youtube_privacy(cur.get("privacy"))
        team = (sl.get("teams") or DEFAULT_TEAMS or [None])[0]
        default_desc =tpl.default_description(team) if hasattr(tpl, "default_description") else \
            tpl.DESCRIPTION_FORMAT
        if from_file:                                # the team's formats, as the file carried them
            title_fmt = cur.get("title_format") or tpl.TITLE_FORMAT
            desc_fmt = cur.get("description_format") or default_desc
        else:
            title_fmt = self.edit_format(tpl, "title", cur.get("title_format") or tpl.TITLE_FORMAT)
            desc_fmt = self.edit_format(tpl, "description", cur.get("description_format") or default_desc)
        pl = self.youtube_playlist(yt, playlist_title, ev, privacy)
        if pl is None:
            return self._youtube_off()
        self.save("youtube", {"enabled": True, "route": route, "version": cur.get("version") or "score",
                              "privacy": privacy, "channel_id": ch.get("id"),
                              "channel_title": ch.get("title"), "playlist_id": pl["id"],
                              "playlist_title": pl.get("title") or playlist_title, "playlist_event": ev.get("key"),
                              "title_format": title_fmt, "description_format": desc_fmt})
        self.say(f"Saved: {'every match' if route == 'all' else 'the matches without your teams'} upload"
                 f"{' (yours go to Slack too)' if route == 'all' and slack_on else ''} as {privacy} videos to "
                 f"'{playlist_title}' on {ch.get('title')}.")

    PRIVACY_CHOICES = (("unlisted", "Unlisted: only people with the link (recommended)"),
                       ("public", "Public: anyone, and it can show up in search"),
                       ("private", "Private: only you and people you invite in YouTube Studio"))

    def youtube_privacy(self, current: str | None) -> str:
        """Who can see the playlist and its videos (user, 2026-11-04): one answer for both. Default: the saved
        choice, else unlisted."""
        keys = [k for k, _ in self.PRIVACY_CHOICES]
        i = self.io.choose("Who can see the playlist and its videos?", [t for _, t in self.PRIVACY_CHOICES],
                           default=keys.index(current) if current in keys else 0)
        if keys[i] == "public":
            self.say("Note: public means every match video can be found by anyone searching YouTube.")
        return keys[i]

    def _youtube_off(self) -> None:
        self.save("youtube", {"enabled": False})
        self.say("YouTube is off for now. (To set it up later: setup --section youtube)")

    def step_tbalinks(self) -> None:
        """Each YouTube video added to its match on The Blue Alliance (user, 2026-11-05): off by default, for event
        organizers with a TBA write key, with the warning first. Not in the first-run flow (few teams run events):
        setup --section tbalinks, or the menu. The key is checked against the event with a call that changes
        nothing, and kept in the credential store."""
        from .publish import tbalink
        self.heading("The Blue Alliance video links (event organizers)")
        yt, ev = config.section("youtube"), config.section("event")
        self.say("StreamSplitter can add each match's YouTube video to the match's page on The Blue Alliance, as soon")
        self.say("as it is uploaded. That needs a TBA write key, which TBA gives to the people who run an event.")
        for line in textwrap.wrap(tbalink.WARNING, 100):
            self.say(line)
        if not yt.get("enabled"):
            self.say("YouTube is off, so there is nothing to link. (Turn it on with setup --section youtube.)")
            return
        if yt.get("privacy") == "private":
            self.say("The videos are private, so a link on The Blue Alliance would not play for anyone.")
            if yt.get("tba_links"):
                self.save("youtube", {"tba_links": False})
            return
        if not self.io.confirm("Add the videos to The Blue Alliance with a write key?", bool(yt.get("tba_links"))):
            if yt.get("tba_links"):
                self.save("youtube", {"tba_links": False})
            self.say("The Blue Alliance links are off.")
            self._tba_titles(yt)
            return
        auth_id, secret = config.get_secret("tba_write_id"), config.get_secret("tba_write_secret")
        if not (auth_id and secret and self.io.confirm("Use the TBA write key saved on this computer?", True)):
            self.say("This needs a TBA write key for the event with match video access. Organizers request one at")
            self.say(f"  {tbalink.REQUEST_URL}")
            self.say(f"Once TBA grants it, its Auth ID and Auth Secret are on your TBA account page ({TBA_ACCOUNT_URL}).")
            auth_id = self.io.ask("The key's Auth ID")
            secret = self.io.ask_secret("The key's Auth Secret (hidden as you type)")
        if not ev.get("key"):
            self.say("No event is set, so the key cannot be checked yet. The links stay off: set the event, then run")
            self.say("setup --section tbalinks again.")
            return
        problem = tbalink.TrustedClient(auth_id, secret).check(ev["key"]) if secret else "no secret was typed"
        if problem:
            self.say(f"The key did not work for {ev['key']}: {problem}")
            self.say("The Blue Alliance links are off. (To try again: setup --section tbalinks)")
            self.save("youtube", {"tba_links": False})
            return
        config.set_secret("tba_write_id", auth_id)
        config.set_secret("tba_write_secret", secret)
        self.save("youtube", {"tba_links": True})
        self.say(f"Saved: each YouTube video is added to its match on The Blue Alliance ({ev['key']}).")
        self.say("(Videos uploaded before now: streamsplitter tba)")

    def _tba_titles(self, yt: dict) -> None:
        """Without a write key: anyone with a TBA account can suggest the playlist on TBA's page afterwards
        (`streamsplitter tba` prints the links). That page reads each video's match from its title, so this offers
        the match's TBA id at the end of new titles (templates.TBA_TITLE_SUFFIX). Off unless the person says yes."""
        tpl = self.svc.templates()
        self.say()
        self.say("Without a key, anyone with a TBA account can suggest the playlist to The Blue Alliance after the")
        self.say("event, and a TBA moderator reviews it: `streamsplitter tba` prints the page and what to paste.")
        self.say("TBA's page finds each video's match by itself when the title ends with the match's TBA id, like")
        self.say("'Qualification 12 - 2026 MadTown Throwdown (qm12)'.")
        fmt = yt.get("title_format") or tpl.TITLE_FORMAT
        if "{tba_id}" in fmt:
            self.say("New video titles already carry the id.")
            return
        if self.io.confirm("Put the match's TBA id at the end of new video titles?", False):
            self.save("youtube", {"title_format": fmt + tpl.TBA_TITLE_SUFFIX})
            self.say("Saved: new video titles end with the match's TBA id. (For videos already uploaded: "
                     "streamsplitter tba --retitle)")

    def youtube_terms(self) -> bool:
        """The person's OK to YouTube's Terms of Service before anything is uploaded (asked once; the date is kept
        in [youtube] terms_accepted)."""
        cur = config.section("youtube")
        if cur.get("terms_accepted"):
            return True
        self.say()
        self.say("Uploading uses the YouTube API Services, so it needs your OK to YouTube's Terms of Service:")
        self.say(f"  {YOUTUBE_TERMS_URL}")
        self.say(f"Google's Privacy Policy covers Google's side ({GOOGLE_PRIVACY_URL}). You can remove")
        self.say(f"StreamSplitter's access any time at {GOOGLE_PERMISSIONS_URL}.")
        self.say("The videos are the event's webcast footage: check with the organisers that they are happy for you")
        self.say("to post them.")
        if not self.io.confirm("Do you agree to YouTube's Terms of Service for these uploads?", False):
            self.say("No uploads to YouTube without that.")
            return False
        self.save("youtube", {"terms_accepted": _dt.date.today().isoformat()})
        return True

    @staticmethod
    def _format_problem(tpl, fmt: str, kind: str) -> str | None:
        try:
            tpl.validate(fmt, kind)
        except ValueError as e:
            return f"That {kind} cannot be used: {e}."
        return None

    LOGIN_GRACE_S = 2.0          # a login that fails at once (a bad client file) comes back before the wait message

    def youtube_client_config(self, yt_mod, ask_keep: bool = True, service: str = "YouTube") -> dict | None:
        """The team's Google sign-in app (OAuth client JSON): the saved one, a teammate's file, or one made now in the
        Google Cloud walkthrough. None when the person skips `service` ("YouTube", or "Google Drive", which uses the
        same sign-in app)."""
        saved = config.get_secret("youtube_client")
        if saved:
            try:
                cfg = yt_mod.check_client_config(json.loads(saved))
                if not ask_keep or self.io.confirm("Use the Google sign-in app you set up before?", True):
                    return cfg
            except (ValueError, PermanentError):
                pass
        self.say()
        self.say("Uploading needs your team's own Google sign-in app for StreamSplitter (Google calls it an 'OAuth")
        self.say("client'). A team makes it once, free, in about 15 minutes; every laptop then uses the same one.")
        found = _newest([downloads_dir()], "client_secret*.json")
        i = self.io.choose("Has your team made it already?",
                           ["No: walk me through making it", "Yes: I have its client_secret...json file",
                            f"Skip {service} for now"], default=1 if found else 0)
        if i == 2:
            return None
        if i == 0:
            return self.make_google_client(yt_mod, service)
        self.say("Ask the teammate who made it for that file (or for a team settings file, which also sets up Slack).")
        if found:
            self.say(f"Found a client file: {found[0]}")
        while True:
            path = self.io.ask("Path of the client_secret...json file (or type skip)",
                               default=str(found[0]) if found else None)
            if path.strip().strip("\"'").lower() == "skip":
                return None
            try:
                cfg = yt_mod.read_client_config(str(find_user_file(path)))
            except PermanentError as e:
                self.say(f"That file cannot be used: {_error_text(e)}")
                continue
            return self._save_client(cfg)

    def _save_client(self, cfg: dict) -> dict:
        try:
            old = (json.loads(config.get_secret("youtube_client") or "{}").get("installed") or {}).get("client_id")
        except (ValueError, AttributeError):
            old = None
        config.set_secret("youtube_client", json.dumps(cfg))
        config.set_secret("youtube_token", None)    # a login belongs to the client that made it
        if old != (cfg.get("installed") or {}).get("client_id"):
            config.set_secret("drive_token", None)  # (the Drive login too, when the client is another one)
        self.say("Saved the Google sign-in app.")
        return cfg

    def make_google_client(self, yt_mod, service: str = "YouTube") -> dict | None:
        """The Google Cloud walkthrough, one page at a time: project, API (YouTube's, or Drive's for `service`
        "Google Drive"), branding, audience (published), Desktop client; then the downloaded client file is picked up
        from the Downloads folder."""
        drive = service != "YouTube"
        self.say()
        if drive:
            self.say("Use the team's Google account if it has one (the picture at the top right of each Google page")
            self.say("shows which account is in use: click it to switch).")
        else:
            self.say("Use the Google account that owns the team's YouTube channel, or an Owner or Manager of its Brand")
            self.say("Account (someone only invited in YouTube Studio cannot do this). The picture at the top right of")
            self.say("each Google page shows which account is in use: click it to switch.")
        self.say()
        self.say("Google step 1 of 5: create a project (a free folder for the sign-in app).")
        self.say("  Name it, for example StreamSplitter 5026. Note the Project ID shown under the name (like")
        self.say("  streamsplitter-5026), then click Create. No billing or free trial is needed; if Google asks for")
        self.say("  your country and to accept its terms, do that.")
        self.io.open_url(google_page("project"))
        project = self.io.ask("When it is created, type its Project ID (Enter to skip)", default="",
                              check=_bad_project_id).lower() or None
        check_picker = "" if project else " Check that the project picker at the top shows the new project."
        self.say()
        if drive:
            self.say("Google step 2 of 5: turn on the Google Drive API (what StreamSplitter uploads through).")
            self._apis_on.add("drive")
        else:
            self.say("Google step 2 of 5: turn on the YouTube Data API v3 (what StreamSplitter uploads through).")
        self.say("  Click Enable. (It says Manage instead? It is on already.)" + check_picker)
        self.io.open_url(google_page("drive_api" if drive else "api", project))
        self.io.ask("Press Enter when it is enabled", default="")
        self.say()
        self.say("Google step 3 of 5: describe the sign-in app. Click Get started, then: App name StreamSplitter,")
        self.say("  support email yours, Next; Audience External, Next; contact email yours, Next; tick the agreement,")
        self.say("  Continue, Create. (The page shows the app's details already? Then it is done.)" + check_picker)
        self.io.open_url(google_page("branding", project))
        self.io.ask("Press Enter when it is created", default="")
        self.say()
        self.say("Google step 4 of 5: publish it. Under Publishing status, click Publish app, then Confirm: it should")
        self.say("  say In production. (In Testing, Google blocks every account not on a tester list, and logins stop")
        self.say("  after 7 days.)")
        self.io.open_url(google_page("audience", project))
        self.io.ask("Press Enter when it says In production", default="")
        self.say()
        self.say("Google step 5 of 5: get the sign-in app's file. Click Create client; Application type: Desktop app;")
        self.say("  Name: StreamSplitter; Create. Then click Download JSON in the window that shows (it is offered")
        self.say("  only then: if you closed it, create another client).")
        self.io.open_url(google_page("clients", project))
        return self.wait_for_client_file(yt_mod, project)

    def wait_for_client_file(self, yt_mod, project: str | None) -> dict | None:
        """The client file downloaded from here on, picked up from the Downloads folder as soon as it is complete, or
        the path the person types. A Web-application client is refused with the fix. None on skip."""
        dl = downloads_dir()
        seen = {p: _mtime(p) for p in _newest([dl], "client_secret*.json")}

        def new_file():
            for p in _newest([dl], "client_secret*.json"):
                if (p not in seen or _mtime(p) > seen[p]) and _json_file(p):
                    return p
            return None
        while True:
            found, typed = self.io.wait_until(f"Waiting for the downloaded file in {dl} (saved it somewhere else? "
                                              "type its path and press Enter; skip to stop)", new_file)
            if typed and typed.strip("\"'").lower() == "skip":
                return None
            path = find_user_file(typed) if typed else found
            if path is None:
                self.say(f"  No new client_secret...json file in {dl} yet.")
                continue
            try:
                cfg = yt_mod.read_client_config(str(path))
            except PermanentError as e:
                seen[path] = _mtime(path)
                if "web application" in _error_text(e).lower():
                    self.say(f"{path.name} is from a 'Web application' client, which StreamSplitter cannot use. Click")
                    self.say("Create client again with Application type: Desktop app, then Download JSON.")
                    self.io.open_url(google_page("clients", project))
                else:
                    self.say(f"That file cannot be used: {_error_text(e)}")
                continue
            self.say(f"Got the client file: {path.name}")
            return self._save_client(cfg)

    def youtube_login(self, yt_mod, client_cfg: dict, expect: str | None = None, expect_title: str | None = None):
        """(YouTubeClient, channel dict) for the team's channel, or (None, None). `expect` is the team's channel id when
        known (an earlier setup, the team settings file): a login to another channel is caught without asking."""
        def keep(new_json):
            config.set_secret("youtube_token", new_json)
        token = config.get_secret("youtube_token")
        while True:
            project = project_of(client_cfg)
            if not token:
                self.login_help()
                ok, value = self.browser_login(yt_mod, client_cfg)
                if not ok:
                    then = self.explain_login_problem(value, project)
                    if then == "client":
                        client_cfg = self.wait_for_client_file(yt_mod, project)
                        if client_cfg is None:
                            return None, None
                    elif then == "stop":
                        return None, None
                    continue
                token = value
                config.set_secret("youtube_token", token)
            try:
                yt = yt_mod.YouTubeClient(token, client_cfg, on_token=keep)
                ch = yt.channel()
            except PermanentError as e:
                text = _error_text(e)
                if "not enabled" in text:
                    self.say("The YouTube Data API v3 is not turned on in the Google project. On the page that opens,")
                    self.say("click Enable, then wait a minute.")
                    self.io.open_url(google_page("api", project))
                    if self.io.ask("Press Enter when it is enabled (or type skip)", default="").lower() == "skip":
                        return None, None
                    continue                         # the login itself is fine
                config.set_secret("youtube_token", None)
                token = None
                if "no YouTube channel" in text:
                    self.say("That Google account has no YouTube channel. Log in again and pick the team's channel: a")
                    self.say("Brand Account channel is its own entry on Google's account page. (No team channel yet?")
                    self.say("Create one at youtube.com first.)")
                else:
                    self.say(f"YouTube did not accept the login: {text}")
                if self.io.confirm("Log in again?", True):
                    continue
                return None, None
            except Exception as e:
                self.say(f"Could not reach YouTube ({_error_text(e)}).")
                if self.io.confirm("Try again?", True):
                    continue
                return None, None
            self.say(f"Logged in to the YouTube channel '{ch.get('title')}' ({ch.get('url')}).")
            if expect and ch.get("id") == expect:
                use = True
            elif expect:
                self.say(f"That is not the team's channel ({expect_title or expect}). Log in again and pick it on")
                self.say("Google's 'Choose an account' page.")
                use = not self.io.confirm("Log in again?", True)
            else:
                use = self.io.confirm("Is that the team's channel, the one to upload to?", True)
                if not use:
                    self.say("Then log in again and pick the team's channel on Google's 'Choose an account' page.")
            if use:
                self._yt = yt
                return yt, ch
            config.set_secret("youtube_token", None)
            token = None

    def login_help(self) -> None:
        self.say()
        if getattr(self, "_login_helped", False):
            self.say("Logging in to YouTube again: a browser tab opens. Pick the team's account and channel.")
            return
        self._login_helped = True
        self.say("Log in to YouTube in the browser tab that opens:")
        self.say("  - Pick the team's Google account, then the team's channel if Google asks which one.")
        self.say("  - 'Google hasn't verified this app' is expected (it is your team's own app): click Advanced, then")
        self.say("    Go to StreamSplitter (unsafe), then Continue to allow the YouTube permission.")

    def browser_login(self, yt_mod, client_cfg: dict) -> tuple[bool, str]:
        """(True, token JSON) or (False, what went wrong). The login runs on a thread, so an error that Google shows
        only in the browser (it never comes back to StreamSplitter: the app left in Testing, a redirect mismatch) can
        be typed here instead of waiting 10 minutes for the login to time out."""
        box: dict = {}
        done = threading.Event()

        def run():
            try:
                box["token"] = yt_mod.login(client_cfg)
            except Exception as e:
                box["error"] = _error_text(e)
            finally:
                done.set()
        threading.Thread(target=run, name="youtube-login", daemon=True).start()
        while True:
            _, typed = self.io.wait_until("Waiting for the login to finish in the browser (Google shows an error page "
                                          "instead? type its error, e.g. access_denied, and press Enter)",
                                          done.is_set, grace_s=self.LOGIN_GRACE_S)
            if done.is_set():
                return ("token" in box), box.get("token") or box.get("error") or "the login stopped"
            if typed:
                return False, typed
            self.say("  Not finished yet: finish it in the browser tab (no tab? open the link shown above).")

    def explain_login_problem(self, text: str, project: str | None, service: str = "YouTube") -> str:
        """Say what a login failure means and open its fix. Returns what to do next: "login" again, make a new
        "client" (wait for its file), or "stop". `service`: "YouTube" or "Google Drive" (the same sign-in app)."""
        self.say(f"The {service} login did not work: {text}")
        low = text.lower()
        for words, meaning, fix, page, then in LOGIN_PROBLEMS:
            if not any(w in low for w in words):
                continue
            if service != "YouTube":
                meaning = meaning.replace("YouTube", service)
                fix = fix.replace(" and that owns or manages the team's channel", "")
            self.say(meaning)
            if fix:
                for line in textwrap.wrap(f"Fix: {fix}", width=104, subsequent_indent="  "):
                    self.say(line)
            if page:
                self.io.open_url(google_page(page, project))
            if then == "client":
                return "client"
            if page:
                a = self.io.ask("Press Enter when that is done, to log in again (or type skip)", default="")
                return "stop" if a.lower() == "skip" else "login"
            return "login" if self.io.confirm("Log in again?", True) else "stop"
        doc = "docs/setup-youtube.md" if service == "YouTube" else "docs/setup-drive.md"
        self.say(f"See 'When something goes wrong' in {doc}.")
        return "login" if self.io.confirm("Log in again?", True) else "stop"

    def edit_format(self, tpl, kind: str, fmt: str) -> str:
        """Show `fmt` filled in for a sample match; let the person keep it, add a line (descriptions) or replace it."""
        while True:
            self.say()
            self.say(f"Video {kind}, shown for a sample match:")
            for line in (tpl.example(fmt).splitlines() or [""]):
                self.say(f"    {line}")
            opts = [f"Use this {kind}", f"Write my own {kind} format"]
            if kind == "description":
                opts.insert(1, "Add a line at the end (for example: Recorded by Team 5026)")
            i = self.io.choose(f"The {kind}:", opts, default=0)
            if i == 0:
                return fmt
            fields = ", ".join(f"{{{k}}}" for k in getattr(tpl, "FIELDS", {}))
            if kind == "description" and i == 1:
                self.say(f"You can use these values in it: {fields}")
                extra = self.io.ask("The line to add", default="")
                new = fmt.rstrip("\n") + "\n\n" + extra if extra else fmt
            else:
                self.say(f"Write {{name}} for a value: {fields}")
                if kind == "description":
                    self.say("The current format is:")
                    for line in fmt.splitlines():
                        self.say(f"    {line}")
                    self.say("Type the new description line by line; a line with just END finishes it.")
                    lines = []
                    while True:
                        ln = self.io.ask(f"Line {len(lines) + 1}", default="")
                        if ln.strip() == "END":
                            break
                        lines.append(ln)
                    new = "\n".join(lines).strip("\n")
                else:
                    new = self.io.ask(f"New {kind} format", default=fmt)
            problem = self._format_problem(tpl, new, kind)
            if problem:
                self.say(problem)
                continue
            fmt = new

    def youtube_playlist(self, yt, title: str, ev: dict, privacy: str = "unlisted") -> dict | None:
        """The event's playlist: the channel's own with this title (its privacy changed to `privacy` when it
        differs), else a new one."""
        try:
            pl = yt.find_playlist(title)
            if pl:
                self.say(f"Using your playlist '{pl.get('title')}' ({pl.get('privacy') or 'privacy unknown'}): "
                         f"{pl.get('url')}")
                if pl.get("privacy") != privacy:
                    old = pl.get("privacy") or "unknown"
                    pl = {**pl, **(yt.set_playlist_privacy(pl["id"], privacy) or {}), "privacy": privacy}
                    self.say(f"Changed the playlist from {old} to {privacy}, as chosen.")
                return pl
            pl = yt.create_playlist(title, f"Matches from the {ev.get('year') or ''} {ev.get('name') or ''}, "
                                    "recorded and cut by StreamSplitter.".replace("  ", " "), privacy)
            self.say(f"Made the {privacy} playlist '{pl.get('title') or title}': {pl.get('url')}")
            return pl
        except Exception as e:
            self.say(f"Could not find or make the playlist: {_error_text(e)}")
            return None

    # -- Google Drive --------------------------------------------------------------------------------------------------
    DRIVE_HEIGHTS = ((720, "720p: about 40 MB a match (recommended)"),
                     (540, "540p: about 27 MB a match, a little less sharp"),
                     (0, "As recorded (1080p): about 130 MB a match, 3 times the space"))

    def step_drive(self) -> None:
        """[drive]: every match to a folder in a Google Drive that a person shares with the team (user, 2026-11-05).
        StreamSplitter makes the folder itself: with the drive.file permission it sees only what it made."""
        self.heading("Step 5: Google Drive")
        cur = config.section("drive")
        dr = self.svc.drive()
        from_file = bool(self.kit and "drive" in self.kit and cur.get("enabled"))
        if from_file:
            self.say(f"From the team settings file: every match goes to the Google Drive folder "
                     f"'{cur.get('folder_title') or dr.TOP_TITLE}'.")
        else:
            self.say("StreamSplitter can upload every match to a Google Drive folder that you share with the team.")
            self.say("Members watch the videos in the browser or the Drive phone app; only people you share it with")
            self.say("can see them.")
            self.say(f"A free Google account has {dr.FREE_GB} GB, shared with its Gmail and Photos. At 720p a match is "
                     "about 40 MB, so")
            self.say("a 100-match event takes about 4 GB: room for about 3 events. The videos use the space of the "
                     "account")
            self.say("that logs in, so a team Google account is best.")
            if not self.io.confirm("Upload every match to Google Drive?", bool(cur.get("enabled", True))):
                self.save("drive", {"enabled": False})
                self.say("Google Drive is off. (To turn it on later: setup --section drive)")
                return
        client_cfg = self.drive_client_config(dr)
        if client_cfg is None:
            return self._drive_off()
        client, _info = self.drive_login(dr, client_cfg)
        if client is None:
            return self._drive_off()
        folder, new = self.drive_folder(client, dr, cur)
        if folder is None:
            return self._drive_off()
        values = {"enabled": True, "version": cur.get("version") or "score", "folder_id": folder["id"],
                  "folder_url": folder["url"], "folder_title": folder["name"]}
        if cur.get("folder_id") != folder["id"]:
            values.update(event_folder_id=None, event_folder_event=None, event_folder_title=None,
                          event_folder_url=None)
        self.save("drive", values)
        event_folder = self.drive_event_folder(client, dr, folder, config.section("event"))
        self.drive_share(folder, new)
        if from_file:
            height = dr.DEFAULT_HEIGHT if cur.get("height") is None else int(cur["height"])
            fmt = cur.get("name_format") or dr.NAME_FORMAT
        else:
            height = self.drive_height(dr, cur)
            fmt = self.drive_name_format(dr, cur, config.section("event"))
        self.save("drive", {"height": height, "name_format": fmt})
        where = f"'{folder['name']}'" + (f" (this event: '{event_folder['name']}')" if event_folder else "")
        self.say(f"Saved: every match goes to {where}, " + (f"{height}p." if height else "as recorded."))

    def _drive_off(self) -> None:
        self.save("drive", {"enabled": False})
        self.say("Google Drive is off for now. (To set it up later: setup --section drive)")

    def drive_client_config(self, dr) -> dict | None:
        """The team's Google sign-in app: the one YouTube uses when it is set up, else the Google Cloud walkthrough
        with the Drive API (or a teammate's client file). None when the person skips Drive."""
        saved = config.get_secret("youtube_client")
        if saved:
            try:
                return dr.check_client_config(json.loads(saved))
            except (ValueError, PermanentError):
                pass
        self.say()
        self.say("Google Drive uses the same Google sign-in app as YouTube, and YouTube is not set up on this laptop.")
        return self.youtube_client_config(dr, ask_keep=False, service="Google Drive")

    def drive_enable_api(self, project: str | None) -> bool:
        """Open the Drive API's page in the project the sign-in app belongs to; False when the person types skip."""
        self.say()
        self.say("Turn on the Google Drive API in your team's Google Cloud project. On the page that opens, check that")
        self.say(f"the project picker at the top shows {project or 'the project your team made for StreamSplitter'}, "
                 "then click Enable.")
        self.say("(It says Manage instead? It is on already.)")
        self.io.open_url(google_page("drive_api", project))
        self._apis_on.add("drive")
        return self.io.ask("Press Enter when it is enabled (or type skip)", default="").lower() != "skip"

    def drive_login_help(self) -> None:
        self.say()
        self.say("Log in to Google Drive in the browser tab that opens:")
        self.say("  - Pick the account whose storage should hold the videos (a team account is best).")
        self.say("  - If Google says it hasn't verified this app, that is expected (it is your team's own app): click")
        self.say("    Advanced, then Go to StreamSplitter (unsafe).")
        self.say("  - Allow the Google Drive permission. StreamSplitter sees only the files and folders it makes, nothing")
        self.say("    else in that Drive.")

    def drive_login(self, dr, client_cfg: dict):
        """(DriveClient, about) for the account whose Drive holds the videos, or (None, None)."""
        def keep(new_json):
            config.set_secret("drive_token", new_json)
        token = config.get_secret("drive_token")
        asked_api = bool(token) or "drive" in self._apis_on
        while True:
            project = project_of(client_cfg)
            if not token:
                if not asked_api:
                    asked_api = True
                    if not self.drive_enable_api(project):
                        return None, None
                self.drive_login_help()
                ok, value = self.browser_login(dr, client_cfg)
                if not ok:
                    then = self.explain_login_problem(value, project, service="Google Drive")
                    if then == "client":
                        client_cfg = self.wait_for_client_file(dr, project)
                        if client_cfg is None:
                            return None, None
                    elif then == "stop":
                        return None, None
                    continue
                token = value
                config.set_secret("drive_token", token)
            try:
                client = dr.DriveClient(token, client_cfg, on_token=keep)
                info = client.about()
            except PermanentError as e:
                text = _error_text(e)
                if "not enabled" in text:
                    self.say("The Google Drive API is not turned on in the Google project yet.")
                    if not self.drive_enable_api(project):
                        return None, None
                    continue                         # the login itself is fine
                config.set_secret("drive_token", None)
                token = None
                self.say(f"Google Drive did not accept the login: {text}")
                if self.io.confirm("Log in again?", True):
                    continue
                return None, None
            except Exception as e:
                self.say(f"Could not reach Google Drive ({_error_text(e)}).")
                if self.io.confirm("Try again?", True):
                    continue
                return None, None
            who = info.get("email") or info.get("name") or "a Google account"
            space = (f"{dr.gb(info.get('free'))} GB free of {dr.gb(info['limit'])} GB" if info.get("limit")
                     else "no storage limit")
            self.say(f"Logged in to Google Drive as {who} ({space}).")
            low = dr.EVENT_MATCHES * dr.MB_PER_MATCH[dr.DEFAULT_HEIGHT] * 1e6
            if info.get("free") is not None and info.get("limit") and info["free"] < low:
                self.say("That is less than one event at 720p: free some space there, or log in with another account.")
            if self.io.confirm("Keep the videos in this account's Drive?", True):
                self._drive = client
                return client, info
            config.set_secret("drive_token", None)
            token = None
            self.say("Then log in again and pick the other account in Google's account chooser.")

    def drive_folder(self, client, dr, cur: dict):
        """(the top folder, made now?) for the logged-in account: the saved one when this account can see it, else
        one made now (or one StreamSplitter made earlier with that name). (None, False) to stop."""
        fid, title = cur.get("folder_id"), cur.get("folder_title") or dr.TOP_TITLE
        if fid:
            try:
                f = client.get(fid)
            except Exception as e:
                self.say(f"Could not check the folder '{title}' ({_error_text(e)}); keeping it.")
                return {"id": fid, "name": title, "url": cur.get("folder_url") or dr.folder_url(fid)}, False
        try:
            if fid:
                if f is not None and not f["trashed"]:
                    self.say(f"Using the folder '{f['name']}': {f['url']}")
                    return f, False
                if f is not None:
                    self.say(f"The folder '{title}' is in that Drive's trash.")
                    if self.io.confirm("Take it out of the trash and use it again?", True):
                        f = client.update(fid, {"trashed": False}, what="restore")
                        self.say(f"Restored it: {f['url']}")
                        return f, False
                else:
                    self.say(f"This Google account cannot see the folder '{title}': another account made it, or it was")
                    self.say("deleted. StreamSplitter can only upload into a folder it made with the account that is")
                    self.say("logged in.")
                    if not self.io.confirm("Make a new folder in this account's Drive?", True):
                        self.say("Then log in with the account that made it: setup --section drive")
                        return None, False
            name = self.io.ask("Name of the folder for the match videos", default=title,
                               check=lambda s: None if dr.clean_name(s, "") else "type a name")
            f = dr.find_top_folder(client, name)
            if f is not None:
                self.say(f"Using the folder '{f['name']}' that StreamSplitter made before in this Drive: {f['url']}")
                return f, False
            f = dr.create_top_folder(client, name)
            self.say(f"Made the folder '{f['name']}' in My Drive: {f['url']}")
            return f, True
        except Exception as e:
            self.say(f"Could not find or make the folder: {_error_text(e)}")
            return None, False

    def drive_event_folder(self, client, dr, folder: dict, ev: dict) -> dict | None:
        """This event's folder inside the top folder, found or made now (else it is made with the first match)."""
        if not ev.get("key"):
            return None
        try:
            f = dr.ensure_event_folder(client, folder["id"], ev["key"],
                                       dr.event_title(ev["key"], ev.get("name"), ev.get("year")))
        except Exception as e:
            self.say(f"Could not make this event's folder yet ({_error_text(e)}); it is made with the first match.")
            return None
        self.save("drive", {"event_folder_id": f["id"], "event_folder_event": ev["key"],
                            "event_folder_title": f["name"], "event_folder_url": f["url"]})
        return f

    def drive_share(self, folder: dict, new: bool) -> None:
        """How to share the folder with the team: a person does it in Drive (StreamSplitter never shares)."""
        if not new and not self.io.confirm("Open the folder to check who it is shared with?", False):
            return
        self.say()
        self.say("Share the folder with the team. On the page that opens:")
        self.say("  1. Click the folder's name at the top, then Share.")
        self.say("  2. Add your team's Google Group, or the members' emails, as Viewer, and click Send.")
        self.say("  Or, under General access, pick 'Anyone with the link' (Viewer) and post the link in Slack.")
        self.io.open_url(folder["url"])
        self.io.ask("Press Enter when it is shared (or do it later)", default="")

    def drive_height(self, dr, cur: dict) -> int:
        keys = [h for h, _ in self.DRIVE_HEIGHTS]
        now = dr.DEFAULT_HEIGHT if cur.get("height") is None else int(cur["height"])
        i = self.io.choose("Video size in Google Drive:", [t for _, t in self.DRIVE_HEIGHTS],
                           default=keys.index(now) if now in keys else 0)
        return keys[i]

    def drive_examples(self, dr, ev: dict) -> tuple[int | None, list]:
        """(the event's last qualification number, sample matches) for showing the file names: the event's first
        qualification match from TBA's schedule when it is out, and a playoff match."""
        tpl = self.svc.templates()
        key, name, year = ev.get("key"), ev.get("name"), ev.get("year")
        quals, first = None, None
        if key and config.tba_key():
            try:
                rows = self.svc.tba_get(f"/event/{key}/matches/simple", config.tba_key()) or []
                qms = sorted((r for r in rows if r.get("comp_level") == "qm" and isinstance(r.get("match_number"),
                                                                                             int)),
                             key=lambda r: r["match_number"])
                if qms:
                    quals, first = qms[-1]["match_number"], qms[0]
            except Exception:
                pass
        if first is not None:
            al = first.get("alliances") or {}
            teams = {c: [str(t).removeprefix("frc") for t in (al.get(c) or {}).get("team_keys") or []]
                     for c in ("red", "blue")}
            qual = Match(key=first.get("key"), name=first.get("key") or "", event_key=key, event_name=name, year=year,
                         comp_level="qm", set_number=1, match_number=first["match_number"], red=teams["red"],
                         blue=teams["blue"])
        else:
            qual = tpl.SAMPLE
        k = key or tpl.SAMPLE.event_key
        playoff = Match(key=f"{k}_sf3m1", name=f"{k}_sf3m1", event_key=k, event_name=name, year=year or 2026,
                        comp_level="sf", set_number=3, match_number=1)
        return quals, [qual, playoff]

    def drive_name_format(self, dr, cur: dict, ev: dict) -> str:
        """The file names, shown for this event's matches, approved or typed."""
        fmt = cur.get("name_format") or dr.NAME_FORMAT
        quals, samples = self.drive_examples(dr, ev)
        self.say()
        self.say("Files are named so they sort in play order (Q = qualification, P = playoff, PF = final,")
        self.say("PR = practice), for example:")
        for m in samples:
            self.say(f"    {dr.render_name(fmt, m, quals=quals)}.mp4")
        if self.io.confirm("Name the files like this?", True):
            return fmt
        self.say("Write {name} for a value: " + ", ".join(f"{{{k}}}" for k in dr.NAME_FIELDS))
        self.say("A [part in brackets] is left out until its values are known (the teams, before the schedule is out).")
        while True:
            new = self.io.ask("File name format", default=fmt, check=dr.name_format_problem)
            for m in samples:
                self.say(f"    {dr.render_name(new, m, quals=quals)}.mp4")
            if self.io.confirm("Use this?", True):
                return new

    def drive_client(self):
        """The DriveClient for the saved login (None without one)."""
        if self._drive is None:
            token, cfg = config.get_secret("drive_token"), config.get_secret("youtube_client")
            if token and cfg:
                try:
                    self._drive = self.svc.drive().DriveClient(
                        token, json.loads(cfg), on_token=lambda j: config.set_secret("drive_token", j))
                except Exception as e:
                    self.say(f"Google Drive is not available: {_error_text(e)}")
        return self._drive

    def test_drive(self, client, cfg: dict) -> dict | None:
        """The sample clip into the top folder (as cut: quick, and no ffmpeg needed): the destination's result, None
        when it failed."""
        dr, tpl = self.svc.drive(), self.svc.templates()
        shown = [-1]

        def progress(job, sent, total):
            pct = int(100 * sent / total) if total else 100
            if pct // 25 > shown[0]:
                shown[0] = pct // 25
                self.say(f"  {pct} %")
        team = (config.section("slack").get("teams") or DEFAULT_TEAMS or [None])[0]
        desc = config.section("youtube").get("description_format") or tpl.default_description(team)
        try:
            dest = dr.DriveDestination(client, cfg["folder_id"], folder_title=cfg.get("folder_title"),
                                       name_format=cfg.get("name_format") or dr.NAME_FORMAT, description_format=desc,
                                       progress=progress, log=lambda s: None)
        except Exception as e:
            self.say(f"Google Drive: {_error_text(e)}")
            return None
        job = Job(id="setup-test:test:drive", destination="drive", kind="test", path=str(SAMPLE_CLIP),
                  match=SAMPLE_MATCH)
        self.say(f"Uploading the test video to the Google Drive folder '{cfg.get('folder_title') or dr.TOP_TITLE}'...")
        r = None
        for attempt in range(3):
            try:
                r = dest.send(job)
                break
            except TransientError as e:
                self.say(f"  Google Drive: {_error_text(e)}" + ("; trying again" if attempt < 2 else ""))
                time.sleep(3 * (attempt + 1))
            except Exception as e:
                self.say(f"Google Drive: the test upload failed: {_error_text(e)}")
                return None
        if r is None:
            self.say("Google Drive: the test upload did not finish (no internet?). Run the test again later.")
            return None
        self.say(f"Google Drive: uploaded '{r.get('name')}'.")
        return r

    # -- team wiki -----------------------------------------------------------------------------------------------------
    def step_wiki(self) -> None:
        """[wiki]: every match uploaded to the team's own MediaWiki (user, 2026-11-04), on a members-only page per
        event. The bot password is made by the person at Special:BotPasswords and typed here (hidden). Not in the
        first run's steps since 2026-11-05 (the videos go to Google Drive instead): setup --section wiki."""
        self.heading("Team website (wiki)")
        cur = config.section("wiki")
        wk = self.svc.wiki()
        from_file = bool(self.kit and "wiki" in self.kit and cur.get("enabled"))
        if from_file:
            self.say(f"From the team settings file: every match goes to the team wiki ({_host(cur.get('api_url'))}) "
                     f"as {cur.get('username')}. Left to set on this laptop: this event's page.")
        else:
            self.say("StreamSplitter can also upload every match video to your team's own wiki (a MediaWiki site), on")
            self.say("a members-only page per event with a section per match. The wiki's admin must allow video")
            self.say(f"uploads first ({WIKI_DOC}).")
            if not self.io.confirm("Upload every match to your team's wiki?", bool(cur.get("enabled", False))):
                self.save("wiki", {"enabled": False})
                self.say("The wiki is off. (To turn it on later: setup --section wiki)")
                return
        client = self.wiki_login(wk, cur, ask_keep=not from_file)
        if client is None:
            return self._wiki_off()
        ev = config.section("event")
        page, event_page = self.wiki_page(client, wk, ev, cur)
        markup = cur.get("private_markup") or wk.PRIVATE_MARKUP
        if from_file:
            private, embed = bool(cur.get("private", True)), cur.get("embed_format") or wk.EMBED_FORMAT
        else:
            private = self.io.confirm(f"Make the page and its videos members-only (puts {markup} at the top of "
                                      "each)?", bool(cur.get("private", True)))
            embed = self.wiki_embed(wk, cur)
        self.save("wiki", {"enabled": True, "api_url": client.api_url, "username": client.username,
                           "version": cur.get("version") or "score", "private": private, "embed_format": embed,
                           "page_format": cur.get("page_format") or wk.PAGE_FORMAT, "page_title": page,
                           "page_event": ev.get("key") if page else None, "event_page": event_page})
        where = f"the page '{page}'" if page else "a page named after the event"
        self.say(f"Saved: every match is uploaded to {where} on the wiki" + (", members-only." if private else "."))

    def _wiki_off(self) -> None:
        self.save("wiki", {"enabled": False})
        self.say("The wiki is off for now. (To set it up later: setup --section wiki)")

    @staticmethod
    def _known_wiki() -> str | None:
        for t in config.section("slack").get("teams") or []:
            if str(t).upper() in KNOWN_WIKIS:
                return KNOWN_WIKIS[str(t).upper()]
        return None

    def wiki_login(self, wk, cur: dict, ask_keep: bool = True):
        """A logged-in WikiClient: the saved bot login when it works (and the person keeps it), else the wiki's
        address and a new bot password after the walkthrough. None when the person skips the wiki."""
        password = config.get_secret("wiki_bot_password")
        if cur.get("api_url") and cur.get("username") and password:
            client = wk.WikiClient(cur["api_url"], cur["username"], password)
            try:
                who = (client.login() or {}).get("user") or cur["username"]
                self.say(f"The wiki: logged in to {_host(cur['api_url'])} as {who}.")
                if not ask_keep or self.io.confirm("Keep using this wiki login?", True):
                    self.check_wiki_rights(client, wk)
                    self._wiki = client
                    return client
            except PermanentError as e:
                self.say(f"The saved wiki login no longer works: {_error_text(e)}")
            except Exception as e:
                self.say(f"Could not reach the wiki to check the saved login ({_error_text(e)}).")
                if self.io.confirm("Keep it anyway?", True):
                    self._wiki = client
                    return client
        found = self.wiki_address(wk, cur)
        if found is None:
            return None
        api, probe = found
        self.wiki_walkthrough(wk, probe)
        user = cur.get("username") if cur.get("api_url") == api else None
        while True:
            user = self.io.ask(f"The bot's user name, as the wiki shows it (like YourName@{wk.BOT_NAME})",
                               default=user or None, check=_bad_bot_user)
            password = self.io.ask_secret("Paste the bot password (hidden as you type; Enter alone to skip the wiki)")
            if not password:
                if self.io.confirm("Skip the wiki for now?", True):
                    return None
                continue
            client = wk.WikiClient(api, user, password)
            try:
                who = (client.login() or {}).get("user") or user
            except PermanentError as e:
                self.say(f"The wiki did not accept that: {_error_text(e)}")
                self.say("Check the user name (all of Name@..., as the page showed it) and copy the password again.")
                self.say(f"Lost the password? On Special:BotPasswords click {wk.BOT_NAME}, then Reset password.")
                continue
            except Exception as e:
                self.say(f"Could not reach the wiki ({_error_text(e)}). Check the internet connection.")
                if not self.io.confirm("Try again?", True):
                    return None
                continue
            config.set_secret("wiki_bot_password", password)
            self.save("wiki", {"api_url": api, "username": user})
            self.say(f"Logged in to the wiki as {who}; the bot password is saved.")
            self.check_wiki_rights(client, wk)
            self._wiki = client
            return client

    def wiki_address(self, wk, cur: dict):
        """(api.php address, a client that read the wiki's site info) for the team's wiki; None to skip it."""
        default = cur.get("api_url") or self._known_wiki()
        self.say("Type your wiki's address: its api.php is best (usually the site's address with /api.php or")
        self.say("/w/api.php after it, for example https://wiki.example.org/w/api.php), but the wiki's own address")
        self.say("works too.")
        while True:
            answer = self.io.ask("Wiki address", default=default or None)
            last = None
            for url in wk.api_candidates(answer):
                probe = wk.WikiClient(url)
                try:
                    site = probe.site_info()
                except Exception as e:
                    last = e
                    continue
                self.say(f"Found {site.get('sitename') or 'a wiki'} ({site.get('generator') or 'MediaWiki'}) at "
                         f"{url}.")
                self.wiki_site_notes(site)
                return url, probe
            self.say(f"No wiki answered at {answer}" + (f" ({_error_text(last)})" if last else "") + ". Open the wiki")
            self.say("in a browser and copy its address from there.")
            if not self.io.confirm("Try another address?", True):
                return None
            default = None

    def wiki_site_notes(self, site: dict) -> None:
        """What the wiki's own settings say about uploading match videos (its admin fixes these)."""
        exts = site.get("fileextensions") or []
        if exts and "mp4" not in exts:
            self.say("Note: this wiki does not accept .mp4 files yet: its admin must add mp4 to $wgFileExtensions")
            self.say(f"({WIKI_DOC}), or every upload is refused.")
        top = site.get("maxuploadsize")
        if top and top < 150e6:
            self.say(f"Note: this wiki takes files up to {top / 1e6:.0f} MB, and a match video is 90-130 MB: its "
                     f"admin must raise $wgMaxUploadSize ({WIKI_DOC}).")

    def wiki_walkthrough(self, wk, probe) -> None:
        self.say()
        self.say("StreamSplitter logs in to the wiki with a bot password: a separate password that can do only what")
        self.say("it needs. To make one (a minute):")
        self.say("  1. On the page that opens, log in as the wiki account the videos should show as uploaded by (a")
        self.say("     bot account your wiki admin made, or your own). It must be able to see members-only pages.")
        self.say(f"  2. Under 'Create a new bot password', type the bot name {wk.BOT_NAME} and click Create.")
        self.say(f"     ({wk.BOT_NAME} listed already? Click it, then Reset password.)")
        self.say("  3. Tick these four boxes, so it can make the event page, add each match and upload the videos:")
        for g in wk.GRANTS:
            self.say(f"       - {g}")
        self.say("     Leave the rest as it is and click Create.")
        self.say(f"  4. The wiki shows the user name to use (like YourName@{wk.BOT_NAME}) and the password. Copy the")
        self.say("     password now: the wiki shows it only once.")
        self.io.open_url(probe.page_url("Special:BotPasswords"))

    def check_wiki_rights(self, client, wk) -> None:
        """When the bot password lacks a grant StreamSplitter needs, walk through ticking it (the password stays)."""
        for _ in range(3):
            try:
                missing = wk.missing_rights(client.user_info().get("rights") or [])
            except Exception as e:
                self.say(f"Could not check what the bot may do on the wiki ({_error_text(e)}).")
                return
            if not missing:
                return
            self.say(f"The bot password may not: {', '.join(missing)}. On the page that opens, tick "
                     f"{'it' if len(missing) == 1 else 'them'} and click Update (the password stays the same). Ticked")
            self.say("already? Then the wiki account itself may not do it: ask the wiki admin.")
            self.io.open_url(client.page_url(f"Special:BotPasswords/{wk.BOT_NAME}"))
            if self.io.ask("Press Enter when it is updated (or type skip)", default="").lower() == "skip":
                self.say("Until then, uploads to the wiki fail and the alert contact is told why.")
                return
            try:
                client.login()                  # the grants a session has are fixed when it logs in
            except Exception as e:
                self.say(f"Could not log in again ({_error_text(e)}).")
                return

    def wiki_page(self, client, wk, ev: dict, cur: dict):
        """(the page for this event's match videos, the event's own wiki page or None): the event's page is looked
        for on the wiki (its spelling may differ from TBA's: "Madtown" against "MadTown"), then the title is confirmed
        or typed. (None, None) without an event."""
        if not ev.get("key"):
            self.say("Set the event first (setup --section event), then pick its wiki page: setup --section wiki.")
            return None, None
        fmt = cur.get("page_format") or wk.PAGE_FORMAT
        name, year = ev.get("name") or ev["key"], ev.get("year")
        event_page = None
        if cur.get("page_title") and cur.get("page_event") == ev["key"]:
            default, event_page = cur["page_title"], cur.get("event_page")
        else:
            try:
                titles = client.search_pages(name)
            except Exception as e:
                self.say(f"Could not search the wiki ({_error_text(e)}).")
                titles = []
            found = wk.event_page_candidates(titles, name, year)[:6]
            if len(found) == 1:
                if self.io.confirm(f"Is '{found[0]}' the wiki's page for {name}?", True):
                    event_page = found[0]
            elif found:
                i = self.io.choose(f"Which is the wiki's page for {name}?", found + ["None of these"], default=0)
                event_page = found[i] if i < len(found) else None
            else:
                self.say(f"No page for {name} was found on the wiki.")
            default = wk.render_page_title(fmt, wk.wiki_event_name(event_page) or name, year, ev["key"])
        self.say("The match videos go on a page of their own. Wiki event pages are usually named 'Name (year)': check")
        self.say("the spelling, which can differ from The Blue Alliance's.")
        page = self.io.ask("Page for the match videos", default=default, check=wk.title_problem)
        try:
            sections = client.sections(page)
            if sections is None:
                self.say("That page does not exist yet: it is made with the first match.")
            else:
                n = len(sections)
                self.say(f"That page exists ({n} section{'' if n == 1 else 's'}): each match is added at its end, and "
                         "nothing already on it is changed.")
        except Exception as e:
            self.say(f"Could not look at the page ({_error_text(e)}).")
        return page, event_page

    def wiki_embed(self, wk, cur: dict) -> str:
        self.say("How each video shows on the page, as wiki markup with {file} where the file's name goes:")
        self.say(f"{wk.EMBED_FORMAT} plays it on a wiki with the TimedMediaHandler extension; your wiki's admin may")
        self.say("give you another (for example <video>File:{file}</video>). Enter keeps the one shown.")
        return self.io.ask("Video markup", default=cur.get("embed_format") or wk.EMBED_FORMAT, check=wk.embed_problem)

    def wiki_client(self):
        """The WikiClient for the saved bot login (None without one)."""
        if self._wiki is None:
            cfg, password = config.section("wiki"), config.get_secret("wiki_bot_password")
            if cfg.get("api_url") and cfg.get("username") and password:
                try:
                    self._wiki = self.svc.wiki().WikiClient(cfg["api_url"], cfg["username"], password)
                except Exception as e:
                    self.say(f"The wiki is not available: {_error_text(e)}")
        return self._wiki

    def test_wiki(self, wiki, cfg: dict) -> dict | None:
        """The sample clip on the wiki's test page (members-only when the matches are): the destination's result
        plus "can_delete" (the bot may delete it afterwards), None when it failed."""
        wk = self.svc.wiki()
        shown = [-1]

        def progress(job, sent, total):
            pct = int(100 * sent / total) if total else 100
            if pct // 25 > shown[0]:
                shown[0] = pct // 25
                self.say(f"  {pct} %")
        try:
            dest = wk.WikiDestination(wiki, private=bool(cfg.get("private", True)),
                                      private_markup=cfg.get("private_markup") or wk.PRIVATE_MARKUP,
                                      embed_format=cfg.get("embed_format") or wk.EMBED_FORMAT, progress=progress)
        except Exception as e:
            self.say(f"The wiki: {_error_text(e)}")
            return None
        job = Job(id="setup-test:test:wiki", destination="wiki", kind="test", path=str(SAMPLE_CLIP),
                  match=SAMPLE_MATCH)
        self.say(f"Uploading the test video to the wiki page '{wk.TEST_PAGE}' (in pieces)...")
        r = None
        for attempt in range(3):
            try:
                r = dest.send(job)
                break
            except TransientError as e:
                self.say(f"  The wiki: {_error_text(e)}" + ("; trying again" if attempt < 2 else ""))
                time.sleep(3 * (attempt + 1))
            except Exception as e:
                self.say(f"The wiki: the test upload failed: {_error_text(e)}")
                return None
        if r is None:
            self.say("The wiki: the test upload did not finish (no internet?). Run the test again later.")
            return None
        again = " (it was there from an earlier test)" if job.state.get("existed") else ""
        self.say(f"The wiki: uploaded {r.get('file')} to the page '{r.get('page')}'{again}.")
        try:
            rights = wiki.user_info().get("rights") or []
        except Exception:
            rights = []
        return {**r, "can_delete": "delete" in rights}

    def wiki_test_cleanup(self, wiki, res: dict) -> None:
        """Delete the wiki test page and file when the bot may (delete rights are not required), else say where."""
        if res.get("can_delete") and self.io.confirm("Delete the wiki test page and its file now?", True):
            try:
                wiki.delete(res["page"], "StreamSplitter setup test")
                wiki.delete(f"File:{res['file']}", "StreamSplitter setup test")
                self.say("Deleted the wiki test page and its file.")
                return
            except Exception as e:
                self.say(f"Could not delete them ({_error_text(e)}).")
        self.say("When you have looked, delete the wiki test page and its file (with the wiki's Delete action;")
        self.say("leaving them does no harm):")
        for url in (res.get("url"), res.get("file_page")):
            if url:
                self.say(f"  {url}")

    # -- team settings file --------------------------------------------------------------------------------------------
    def offer_team_file(self) -> None:
        """The first question of a fresh setup: a teammate's file skips the Slack app and the Google project (and
        carries the TBA key the event step needs)."""
        self.heading("Team settings file")
        self.say("A teammate who already set up StreamSplitter can give you a team settings file (and its passphrase,")
        self.say("if it has one).")
        self.say("It sets up Slack, YouTube and Google Drive for you: no Slack app or Google project to make.")
        if self.io.confirm("Do you have a team settings file from a teammate?", False):
            self.import_team_settings(follow=False)

    def step_team(self) -> None:
        """`setup --section team`: make a file for a teammate, or load one."""
        try:
            teamkit.collect()
            can_share = True
        except teamkit.TeamKitError:
            can_share = False
        if can_share:
            i = self.io.choose("Team settings file:", ["Make one for a teammate (from this laptop's Slack, YouTube "
                                                       "and Google Drive)",
                                                       "Load one a teammate gave me", "Back"], default=0)
            if i == 0:
                self.share_team_settings()
            elif i == 1:
                self.import_team_settings()
            return
        self.heading("Team settings file")
        self.import_team_settings()

    def import_team_settings(self, follow: bool = True) -> bool:
        """Find, unlock and apply a team settings file. With `follow`, the Slack and YouTube steps run next, asking
        only this laptop's own things (the first run asks them anyway). True when applied."""
        found = _newest([downloads_dir(), desktop_dir()], "*" + teamkit.SUFFIX)
        if found:
            self.say(f"Found a team settings file: {found[0]}")
        else:
            self.say("Save the file your teammate sent (in Downloads, for example), then say where it is.")
        kit = None
        while kit is None:
            answer = self.io.ask("Where is the team settings file? (type or paste its path, or skip)",
                                 default=str(found[0]) if found else None)
            if answer.strip("\"'").lower() == "skip":
                return False
            path = find_team_file(answer)
            if not path.is_file():
                self.say(f"No file at {path}.")
                continue
            try:
                if not teamkit.needs_passphrase(path):      # made without one: nothing to ask
                    kit = teamkit.read(path)
                    continue
            except teamkit.TeamKitError as e:
                self.say(str(e))
                continue
            while True:
                passphrase = self.io.ask_secret("Its passphrase (hidden as you type; Enter alone to go back)")
                if not passphrase:
                    break
                try:
                    kit = teamkit.read(path, passphrase)
                    break
                except teamkit.TeamKitError as e:
                    self.say(str(e))
                    if "passphrase" not in str(e).lower():
                        break
        self.say("The file sets up:")
        for line in teamkit.summary(kit):
            self.say(f"  - {line}")
        if not self.io.confirm("Use these settings on this laptop?", True):
            return False
        teamkit.apply(kit)
        self.kit = kit
        done = []
        if "slack" in kit:
            done.append("Slack")
        if "youtube" in kit:
            done.append("YouTube" + (" (logged in)" if kit["youtube"].get("login") else ""))
        if "drive" in kit:
            done.append("Google Drive" + (" (logged in)" if kit["drive"].get("login") else ""))
        if "wiki" in kit:
            done.append("the team wiki")
        if kit.get("tba_key"):
            done.append("the Blue Alliance key")
        self.say("Set up from the file: " + ", ".join(done) + ".")
        self.say("Only one laptop per event should run StreamSplitter, or each match is posted and uploaded twice.")
        if follow:
            if "slack" in kit:
                self.step_slack()
            if "youtube" in kit:
                if config.section("event").get("key"):
                    self.step_youtube()
                else:
                    self.say("Set the event (setup --section event), then make its playlist: setup --section youtube.")
            if "drive" in kit:
                self.step_drive()
            if "wiki" in kit:
                self.step_wiki()
        return True

    def share_team_settings(self) -> None:
        """Make a team settings file from this laptop's Slack and YouTube setup, for a teammate's laptop."""
        self.heading("Share these settings with a teammate")
        try:
            preview = teamkit.collect()
        except teamkit.TeamKitError as e:
            self.say(str(e))
            return
        self.say("This makes a team settings file. On a teammate's laptop it sets up Slack, YouTube and Google Drive,")
        self.say("so they do not make another Slack app or Google project. It holds:")
        for line in teamkit.summary(preview)[:-1]:
            self.say(f"  - {line}")
        # first, so the login questions can say what including one means without a passphrase
        passphrase = self.new_passphrase()
        self.say("Anyone with the file" + ("" if passphrase is None else " and its passphrase")
                 + " can post to your Slack and upload to your YouTube channel"
                 + (" and your Google Drive folder" if "drive" in preview else "")
                 + (" and your wiki" if "wiki" in preview else "") + ":")
        self.say("give it only to team members who run StreamSplitter.")
        login = drive_login = False
        if "youtube" in preview and config.get_secret("youtube_token"):
            self.say("It can also carry this laptop's YouTube login. Then the teammate does not need the channel")
            self.say("owner's Google account, but can upload to and edit the channel's videos with StreamSplitter.")
            if passphrase is None:
                self.say("With no passphrase, anyone who gets the file could upload to or delete the channel's videos.")
            login = self.io.confirm("Include the YouTube login?", False)
        if "drive" in preview and config.get_secret("drive_token"):
            self.say("It can also carry this laptop's Google Drive login. Then the teammate's laptop uploads into the")
            self.say("same folder without that account's password (StreamSplitter can only touch what it made there).")
            if passphrase is None:
                self.say("With no passphrase, anyone who gets the file could upload to that folder or delete the videos")
                self.say("StreamSplitter put there.")
            drive_login = self.io.confirm("Include the Google Drive login?", False)
        kit = teamkit.collect(include_youtube_login=login, include_drive_login=drive_login)
        folder = self.pick_folder()
        team = (config.section("slack").get("teams") or [""])[0]
        name = f"StreamSplitter-team{'-' + team if team else ''}-{_dt.date.today().isoformat()}"
        try:
            path = teamkit.write(folder / name, kit, passphrase)
        except (OSError, teamkit.TeamKitError) as e:
            self.say(f"Could not save the file: {_error_text(e)}")
            return
        self.say(f"Saved: {path}")
        if passphrase is None:
            self.say("It has no passphrase, so it can be sent as is (Slack, email or a USB stick): only to team members")
            self.say("who run StreamSplitter.")
        else:
            self.say("Send the file and the passphrase by different routes: for example the file by email or USB "
                     "stick,")
            self.say("and the passphrase by voice or in a password manager. Never both in one message.")
        self.say("On the teammate's laptop: start the setup (run.bat or run.sh) and answer yes to 'Do you have a team")
        self.say("settings file from a teammate?' (or run.bat --section team).")

    def new_passphrase(self) -> str | None:
        """The file's passphrase, or None for a file without one (user, 2026-11-06: the keys are easy to replace)."""
        suggested = teamkit.suggest_passphrase()
        n = teamkit.MIN_PASSPHRASE
        i = self.io.choose("Passphrase for the file:", ["Make one up for me", f"I will type my own (at least {n} "
                                                                            "characters)",
                                                        "No passphrase (anyone who gets the file can use these keys)"],
                           default=0)
        if i == 2:
            self.say("No passphrase: the file is plain text, and whoever gets it can use the keys in it.")
            return None
        if i == 0:
            self.say(f"The passphrase is:  {suggested}")
            self.say("Write it down or put it in a password manager now: it is not saved anywhere, and the file")
            self.say("cannot be opened without it.")
            self.io.ask("Press Enter when you have it", default="")
            return suggested
        while True:
            p = self.io.ask_secret(f"Type a passphrase (at least {n} characters; hidden as you type)")
            if len(p) < n:
                self.say(f"That is {len(p)} characters; use at least {n}.")
                continue
            if self.io.ask_secret("Type it again") != p:
                self.say("The two do not match; try again.")
                continue
            return p

    def pick_folder(self) -> Path:
        dl, desk = downloads_dir(), desktop_dir()
        options = [(f"Downloads ({dl})", dl)]
        if desk.is_dir() and desk != dl:
            options.append((f"Desktop ({desk})", desk))
        if len(options) == 1:
            return dl
        return options[self.io.choose("Save the file in:", [label for label, _ in options], default=0)][1]

    # -- notifications -------------------------------------------------------------------------------------------------
    def slack_client(self):
        """The SlackClient for the saved token (None without Slack)."""
        if self._slack is None:
            token = config.get_secret("slack_bot_token")
            if token and config.section("slack").get("enabled"):
                try:
                    self._slack = self.svc.slack().SlackClient(token)
                except Exception as e:
                    self.say(f"Slack is not available: {_error_text(e)}")
        return self._slack

    def step_notify(self) -> None:
        self.heading("Step 6: alerts")
        client = self.slack_client()
        if client is None:
            self.say("Slack is off, so warnings and errors only show in StreamSplitter's window and log, and nobody is")
            self.say("told if it stops. Turning Slack on (setup --section slack) adds DMs and a stopped-running alarm.")
            return
        nt = config.section("notify")
        if not nt.get("slack_user"):
            self.pick_contact(client, ask_keep=False)
            nt = config.section("notify")
            if not nt.get("slack_user"):
                return
        elif not self._contact_chosen and self.io.confirm(
                f"Alerts go to {nt.get('slack_user_name') or nt['slack_user']} by Slack DM. Change that?", False):
            self.pick_contact(client, ask_keep=False)
            nt = config.section("notify")
        name = nt.get("slack_user_name") or nt["slack_user"]
        mins = int(nt.get("alarm_after_s", ALARM_AFTER_S)) // 60
        self.say(f"While StreamSplitter runs, {name} gets a Slack DM for:")
        self.say("  - problems a person must fix: an expired login, a missing channel or playlist, a full disk, a")
        self.say("    stream link that stopped working;")
        self.say("  - uploads failing for 10 minutes (the venue's internet is down, say). The videos wait and go out")
        self.say("    when it is back, and a second DM says so;")
        self.say(f"  - an alarm if StreamSplitter stops. Slack holds the alarm {mins} minutes ahead and StreamSplitter")
        self.say("    keeps pushing it back, so if the laptop sleeps, crashes or loses power or internet, Slack")
        self.say("    still delivers it.")
        if not self.io.confirm(f"Send {name} a sample alert now, so you know what one looks like?", True):
            return
        try:
            r = client.dm(nt["slack_user"], "[Sample alert from the StreamSplitter setup] Warning: YouTube uploads "
                          "have failed for 10 minutes (no internet?). The clips are kept and will upload when it is "
                          "back. This is only a sample of what an alert looks like.")
            self.say(f"Sent a sample alert to {name}.")
        except Exception as e:
            self.say(f"Could not send it: {_error_text(e)}")
            return
        if self.io.confirm("Also send a sample stopped-running alarm? Slack delivers it by itself in about 2 "
                           "minutes, as it would if this computer died", True):
            try:
                text = self.svc.notifier_alarm_text()
            except Exception:
                text = "StreamSplitter has not checked in for a while; it may have stopped."
            try:
                client.schedule_message((r or {}).get("channel") or nt["slack_user"],
                                        "[Sample alarm from the StreamSplitter setup] " + text,
                                        int(time.time()) + 90)
                self.say("Scheduled: it arrives in about 2 minutes, even if you close this window.")
            except Exception as e:
                self.say(f"Could not schedule it: {_error_text(e)}")
        if not self.io.confirm(f"Did {name} get the sample alert?", True):
            self.say(f"Check that {name} is in the workspace and has not muted the StreamSplitter app (in Slack,")
            self.say("open the app's Messages tab). To pick someone else: setup --section notify.")

    # -- start from Slack (listener.py) ---------------------------------------------------------------------------------
    LISTEN_MODES = (("background", "In the background: it starts by itself when you log in (a computer left on at "
                                   "home; recommended)"),
                    ("window", "Only while it is open in a window here (a laptop someone sits at)"),
                    (None, "Off"))

    def step_listen(self) -> None:
        """[listen]: the Slack listener on this computer (user, 2026-11-05: start and stop recordings from Slack),
        in the background from each log-in or only while open in a window (mode). Per computer: not in the team
        settings file. Nobody besides the alerts contact is asked for here (user, 2026-11-05: the setup once DMed
        12 added teammates): the contact adds people later from Slack (add @name, slackadmin.py), and [listen] users
        / user_names saved by an earlier setup are kept."""
        self.heading("Start from Slack (optional)")
        cur, nt = config.section("listen"), config.section("notify")
        ln = self.svc.listener()
        client = self.slack_client()
        if client is None or not nt.get("slack_user"):
            self.say("Starting recordings from Slack needs Slack and an alerts contact: set them up first (setup "
                     "--section slack).")
            return
        self.say("Slack can start a recording on this computer: reply split 2026mttd to the StreamSplitter app, and")
        self.say("stop to stop it. The alerts contact can also change settings and let others do it from Slack. In")
        self.say("the background, the computer must stay on and logged in all event weekend (locked is fine); it")
        self.say("does not sleep while it waits. Set Windows Update's active hours, or pause updates, for event")
        self.say("weekends: after an update restart it waits until someone logs in.")
        modes = [m for m, _ in self.LISTEN_MODES]
        now = (("window" if cur.get("mode") == "window" else "background") if cur.get("enabled") else None)
        mode = modes[self.io.choose("How should Slack start recordings on this computer?",
                                    [label for _, label in self.LISTEN_MODES], default=modes.index(now))]
        if mode is None:
            if cur.get("enabled") or ln.login_start_installed() or ln.is_running():
                self.listen_off(ln)
            return
        try:
            info = client.auth_test()
        except Exception as e:
            info = None
            self.say(f"Could not check the Slack app's permissions ({_error_text(e)}); going on.")
        if info is not None:
            info = self.check_scopes(client, info)
            scopes = info.get("scopes")
            if "im:history" in self._missing_scopes(info) or (scopes is not None and "im:history" not in scopes):
                self.say("Start from Slack reads your replies, which needs the app's im:history permission (above).")
                self.say("Run setup --section listen again once it is added.")
                return
        self.save("listen", {"enabled": True, "mode": mode})
        contact = nt.get("slack_user_name") or nt["slack_user"]
        people = list(cur.get("user_names") or cur.get("users") or [])
        self.say(f"{contact} can let others start and stop recordings later, from Slack: message the app add @name "
                 f"(help lists every command). Adding someone sends them nothing unless {contact} says so."
                 + (f" Already allowed: {_and(people)}." if people else ""))
        if mode == "window":
            # nothing at log-in and nothing in the background: it listens only while its window is open
            try:
                ln.remove_login_start()
                if ln.is_running():
                    ln.request_stop()
                    ln.wait_stopped(30.0)
            except Exception as e:
                self.say(f"Could not remove the background listener ({_error_text(e)}).")
            self.say(f"To listen: run {RERUN} and choose {getattr(ln, 'MENU_WINDOW', 'Listen for Slack commands')},")
            self.say("or type `streamsplitter listen`. Slack can start recordings only while that window is open;")
            self.say("closing it stops listening, and a recording keeps going.")
            return
        try:
            ln.install_login_start()
            self.say("It starts by itself, hidden, each time you log in to this computer.")
        except Exception as e:
            self.say(f"Could not make it start at log-in ({_error_text(e)}). After each log-in, start it from the "
                     f"menu: {ln.MENU_START}.")
        if not self.listen_start(ln, restart=True):
            return
        key = config.section("event").get("key") or "2026mttd"
        text = (f"Listening on {platform.node() or 'this computer'}: reply split <event> to start a recording (e.g. "
                f"split {key}). Reply help for every command; add @name lets someone else start and stop recordings "
                f"too.")
        # the contact only (user, 2026-11-05): nobody else is messaged unasked
        try:
            client.dm(nt["slack_user"], text.replace("<", "&lt;").replace(">", "&gt;"))
            self.say(f"Sent you: \"{text}\"")
        except Exception as e:
            self.say(f"Could not DM {nt.get('slack_user_name') or nt['slack_user']}: {_error_text(e)}")

    def listen_start(self, ln, restart: bool = False) -> bool:
        """Start the listener in the background (restarted when `restart`, so it reads the new settings)."""
        try:
            if ln.is_running():
                if not restart:
                    self.say("The Slack listener is running already.")
                    return True
                ln.request_stop()
                if not ln.wait_stopped(30.0):
                    self.say("The running listener has not stopped yet; it uses the new settings after its next start.")
                    return True
            pid = ln.start_detached()
        except Exception as e:
            self.say(f"Could not start the Slack listener: {_error_text(e)}")
            return False
        self.say(f"The Slack listener is running in the background (process {pid}). You can close this window.")
        return True

    def listen_off(self, ln) -> None:
        """Start from Slack off: no log-in start, the listener stopped; recordings keep running."""
        self.save("listen", {"enabled": False})
        try:
            ln.remove_login_start()
        except Exception as e:
            self.say(f"Could not remove the log-in start ({_error_text(e)}).")
        try:
            if ln.is_running():
                ln.request_stop()
                ln.wait_stopped(30.0)
        except Exception as e:
            self.say(f"Could not stop the Slack listener ({_error_text(e)}).")
        self.say("Start from Slack is off on this computer. A recording that runs keeps running.")

    def listen_menu_label(self) -> str:
        cur = config.section("listen")
        if not cur.get("enabled"):
            return "Start from Slack: start and stop recordings from Slack"
        ln = self.svc.listener()
        if cur.get("mode") == "window":
            return getattr(ln, "MENU_WINDOW", "Listen for Slack commands in this window")
        try:
            running = bool(ln.is_running())
        except Exception:
            running = False
        return getattr(ln, "MENU_STOP" if running else "MENU_START", "Start from Slack")

    def listen_menu(self) -> None:
        ln = self.svc.listener()
        cur = config.section("listen")
        if not cur.get("enabled"):
            self.step_listen()
            return
        if cur.get("mode") == "window":
            # in this very window, until it is closed or Ctrl+C; a recording it starts runs detached
            if ln.run() == 1:
                self.say("Another Slack listener runs on this computer already (in the background?). To listen here "
                         "instead: Change a setting, Start from Slack.")
            return
        if not ln.is_running():
            self.listen_start(ln)
            return
        i = self.io.choose("The Slack listener is running.", ["Stop it until the next log-in",
                                                              "Turn Start from Slack off on this computer", "Back"], 2)
        if i == 0:
            ln.request_stop()
            self.say("Asked it to stop." if ln.wait_stopped(30.0) else "Asked it to stop; it has not yet.")
            self.say("Recordings keep running. It starts again at the next log-in, or from this menu.")
        elif i == 1:
            self.listen_off(ln)

    # -- test upload ---------------------------------------------------------------------------------------------------
    def step_test(self) -> None:
        self.heading("Step 7: test upload")
        sl, yt_cfg, wk_cfg = config.section("slack"), config.section("youtube"), config.section("wiki")
        client = self.slack_client() if sl.get("enabled") and sl.get("channel_id") else None
        yt = self.youtube_client() if yt_cfg.get("enabled") and yt_cfg.get("playlist_id") else None
        if yt is not None and not self.youtube_terms():
            yt = None
        wiki = self.wiki_client() if wk_cfg.get("enabled") else None
        dr_cfg = config.section("drive")
        drive = self.drive_client() if dr_cfg.get("enabled") and dr_cfg.get("folder_id") else None
        where = [w for w, c in (("Slack", client), ("YouTube", yt), ("Google Drive", drive), ("the wiki", wiki))
                 if c is not None]
        if not where:
            self.say("Nothing to test: Slack, YouTube, Google Drive and the wiki are all off.")
            return
        if not SAMPLE_CLIP.is_file():
            self.say(f"The sample clip is missing ({SAMPLE_CLIP}); skipping the test.")
            return
        size = SAMPLE_CLIP.stat().st_size >> 20
        if not self.io.confirm(f"Send a test video (a {size} MB match from MadTown 2024) to {_and(where)}, "
                               "to check that everything is hooked up?", True):
            return
        got: dict[str, str] = {}
        failed = []
        if client is not None:
            link = self.test_slack(client, sl)
            got["Slack"] = link if link else ""
            if link is None:
                failed.append("Slack")
        video = None
        if yt is not None:
            video = self.test_youtube(yt, yt_cfg)
            if video is None or video.get("locked"):
                failed.append("YouTube")
            if video is not None:
                got["YouTube"] = video["url"]
        drive_res = None
        if drive is not None:
            drive_res = self.test_drive(drive, dr_cfg)
            if drive_res is None:
                failed.append("Google Drive")
            else:
                got["Google Drive"] = drive_res.get("url") or ""
        wiki_res = None
        if wiki is not None:
            wiki_res = self.test_wiki(wiki, wk_cfg)
            if wiki_res is None:
                failed.append("the wiki")
            else:
                got["the wiki"] = wiki_res.get("url") or ""
        shown = [w for w in where if w in got]
        if shown:
            self.say()
            for w in shown:
                where_slack = getattr(self, "_slack_where", f"#{sl.get('channel_name')}")
                self.say(f"  {w}: {got[w] or 'posted in ' + where_slack}")
            check = " (in YouTube Studio > Content it should say Unlisted)" if "YouTube" in shown else ""
            places = {"Slack": "in Slack", "YouTube": "on YouTube", "Google Drive": "in Google Drive",
                      "the wiki": "on the wiki"}
            seen = self.io.confirm(f"Open {'that link' if len(shown) == 1 else 'those links'}: can you see the test "
                                   f"video {_and([places.get(w, w) for w in shown])}{check}?", True)
            ok = [w for w in shown if w not in failed]
            if seen and ok:
                self.say("The test worked: " + _and(ok) + (" are" if len(ok) > 1 else " is") + " hooked up.")
            elif not seen:
                self.say("If the YouTube video says it is still processing, give it a minute or two. In Slack, look in")
                self.say(f"{getattr(self, '_slack_where', '#' + str(sl.get('channel_name')))}. Run the test again "
                         "with: setup --section test")
                if "Google Drive" in shown:
                    self.say("Google Drive can also take a few minutes before a new video plays.")
                failed += [w for w in shown if w not in failed]
        if video is not None and self.io.confirm("Delete the YouTube test video now?", True):
            try:
                yt.delete_video(video["video_id"])
                self.say("Deleted the YouTube test video.")
            except Exception as e:
                self.say(f"Could not delete it ({_error_text(e)}); delete it in YouTube Studio.")
        if drive_res is not None and self.io.confirm("Move the Google Drive test video to the trash now?", True):
            try:
                drive.trash(drive_res["file_id"])
                self.say("Moved it to the trash (Google Drive empties the trash after 30 days).")
            except Exception as e:
                self.say(f"Could not move it ({_error_text(e)}); delete it in Google Drive.")
        if wiki_res is not None:
            self.wiki_test_cleanup(wiki, wiki_res)
        if failed:
            self.say("Not working yet: " + ", ".join(failed) + ". The messages above say what to fix; then run the "
                     "test again with: setup --section test")

    def youtube_client(self):
        if self._yt is None:
            token, cfg = config.get_secret("youtube_token"), config.get_secret("youtube_client")
            if token and cfg:
                try:
                    yt_mod = self.svc.youtube()
                    self._yt = yt_mod.YouTubeClient(token, json.loads(cfg),
                                                    on_token=lambda j: config.set_secret("youtube_token", j))
                except Exception as e:
                    self.say(f"YouTube is not available: {_error_text(e)}")
        return self._yt

    def test_slack(self, client, sl: dict) -> str | None:
        """The test post's link ("" when Slack gave none), or None when it failed. It can go to the contact by direct
        message instead of the channel, so a test does not post to a channel the whole team reads (user, 11-04)."""
        target, self._slack_where = sl["channel_id"], f"#{sl.get('channel_name')}"
        nt = config.section("notify")
        if nt.get("slack_user"):
            name = nt.get("slack_user_name") or "your contact"
            if self.io.choose("Where should the Slack test go?",
                              [f"A direct message to {name} (nobody else sees it)",
                               f"The channel {self._slack_where} (the whole channel sees it)"], 0) == 0:
                try:
                    target, self._slack_where = client.open_dm(nt["slack_user"]), f"the direct messages with {name}"
                except Exception as e:
                    self.say(f"Slack: could not open a direct message: {_error_text(e)}")
                    return None
        self.say(f"Posting the test video to {self._slack_where} (this can take half a minute)...")
        slack = self.svc.slack()
        comment = getattr(slack, "TEST_FORMAT", "StreamSplitter setup test.")
        try:
            r = client.upload_file(target, str(SAMPLE_CLIP), "StreamSplitter test: 2024 MadTown "
                                   "Qualification 6", comment)
        except Exception as e:
            self.say(f"Slack: the test post failed: {_error_text(e)}")
            return None
        self.say("Slack: posted.")
        return (r or {}).get("permalink") or ""

    def test_youtube(self, yt, cfg: dict) -> dict | None:
        tpl, yt_mod = self.svc.templates(), self.svc.youtube()
        prefix = getattr(yt_mod, "TEST_TITLE", "StreamSplitter setup test")
        title = f"{prefix}: {tpl.render(cfg.get('title_format') or tpl.TITLE_FORMAT, SAMPLE_MATCH)}"[:100]
        desc = getattr(yt_mod, "TEST_DESCRIPTION", "A test upload from the StreamSplitter setup.") + "\n\n" + \
            tpl.render(cfg.get("description_format") or tpl.DESCRIPTION_FORMAT, SAMPLE_MATCH)
        privacy = "unlisted"          # whatever the matches get: the test is deleted after, nobody needs to find it
        self.say("Uploading the test video to YouTube...")
        shown = [-1]

        def progress(sent, total):
            pct = int(100 * sent / total) if total else 100
            if pct // 25 > shown[0]:
                shown[0] = pct // 25
                self.say(f"  {pct} %")
        state: dict = {}
        r = None
        for attempt in range(3):
            try:
                r = yt.upload(str(SAMPLE_CLIP), title, desc, privacy, state, progress=progress)
                break
            except TransientError as e:
                self.say(f"  YouTube: {_error_text(e)}" + ("; trying again" if attempt < 2 else ""))
                time.sleep(3 * (attempt + 1))
            except Exception as e:
                self.say(f"YouTube: the test upload failed: {_error_text(e)}")
                return None
        if r is None:
            self.say("YouTube: the test upload did not finish (no internet?). Run the test again later.")
            return None
        try:
            yt.add_to_playlist(cfg["playlist_id"], r["video_id"])
            self.say(f"YouTube: uploaded and added to the playlist '{cfg.get('playlist_title')}'.")
        except Exception as e:
            self.say(f"YouTube: uploaded, but adding it to the playlist failed: {_error_text(e)}")
        # YouTube lifted its lock on uploads from unaudited projects on 2026-11-02; ask what it made of this one
        actual = None
        try:
            actual = (yt.video_status(r["video_id"]) or {}).get("privacyStatus")
        except Exception as e:
            self.say(f"  (could not ask YouTube for the video's status: {_error_text(e)})")
        actual = actual or r.get("privacy")
        r["privacy_checked"] = actual
        if actual == privacy and not r.get("locked_private"):
            self.say(f"YouTube says the test video is {actual}, as it should be (not locked private).")
        elif actual == "private" or r.get("locked_private"):
            r["locked"] = True
            self.say(f"YouTube made the test video PRIVATE instead of {privacy}: uploads from this Google project are")
            self.say("locked private, so the match videos would be too. Until that changes, upload the clips by hand")
            self.say("in YouTube Studio (15 at a time; see 'Locked as private' in docs/setup-youtube.md).")
        else:
            self.say(f"YouTube shows the test video as {actual or 'unknown'}; check it in YouTube Studio > Content.")
        return r

    # -- summary, start, status, stop ----------------------------------------------------------------------------------
    def finish(self) -> None:
        if self.bg is not None:
            self.heading("Step 8: the downloads")
            self.bg.wait(self.io, verbose=True)
        self.summary()
        ev = config.section("event")
        if self.bg is not None and self.bg.failures(required_only=True):
            self.say("Recording cannot start until the downloads above work: fix them and run setup again.")
            return
        if not ev.get("stream_url"):
            return
        today = _dt.date.today().isoformat()
        during = bool(ev.get("start_date") and ev["start_date"] <= today <= (ev.get("end_date") or ev["start_date"]))
        if not during and ev.get("start_date"):
            self.say(f"The event starts {_date_range(ev['start_date'], ev['start_date'])}. Starting now is fine too:")
            self.say("StreamSplitter waits for the stream to go live (and keeps this computer awake).")
        if self.io.confirm("Start recording now, in the background?", during):
            self.start()
        else:
            self.say("To start it later, run the setup again (run.bat or ./run.sh) and pick 'Start recording'.")

    def summary(self) -> None:
        self.heading("Summary")
        ev, sl, yt, nt = (config.section(s) for s in ("event", "slack", "youtube", "notify"))
        missing = []
        self.say(f"  Event:    {_event_line(ev) if ev.get('key') else 'not set'}")
        self.say(f"  Stream:   {ev.get('stream_url') or 'not set'}")
        if not ev.get("key"):
            missing.append("the event (setup --section event)")
        if not ev.get("stream_url"):
            missing.append("the stream link (setup --section stream)")
        has_key = bool(config.tba_key())
        self.say(f"  TBA key:  {'saved' if has_key else 'none: clips are named from the overlay only'}")
        if not has_key:
            missing.append("a TBA key (setup --section tba)")
        if sl.get("enabled"):
            teams = ", ".join(sl.get("teams") or DEFAULT_TEAMS) or "no team"
            both =" (and go to YouTube too)" if yt.get("enabled") and yt.get("route") != "others" else ""
            self.say(f"  Slack:    matches with {teams} are posted to #{sl.get('channel_name')}{both}")
        else:
            self.say("  Slack:    off")
        if yt.get("enabled"):
            what = "every match" if yt.get("route") != "others" or not sl.get("enabled")                 else "the matches without your teams"
            self.say(f"  YouTube:  {what}, {yt.get('privacy') or 'unlisted'}, to '{yt.get('playlist_title')}' on "
                     f"{yt.get('channel_title')}")
        else:
            self.say("  YouTube:  off")
        dr = config.section("drive")
        if dr.get("enabled"):
            h = dr.get("height", 720)
            self.say(f"  Drive:    every match, {f'{h}p' if h else 'as recorded'}, to the folder "
                     f"'{dr.get('folder_title') or 'not made yet'}'" + (f": {dr['folder_url']}" if dr.get("folder_url")
                                                                         else ""))
            if not (dr.get("folder_id") and config.get_secret("drive_token")):
                missing.append("the Google Drive login and folder (setup --section drive)")
        else:
            self.say("  Drive:    off")
        wk = config.section("wiki")
        if wk.get("enabled"):                      # not shown when off: it is not one of the first run's steps
            page = wk.get("page_title") if wk.get("page_event") == ev.get("key") else None
            vis = "members-only" if wk.get("private", True) else "for every reader"
            self.say(f"  Wiki:     every match, {vis}, to " + (f"'{page}'" if page else "a page named after the event")
                     + f" on {_host(wk.get('api_url'))}")
            if not (wk.get("api_url") and wk.get("username") and config.get_secret("wiki_bot_password")):
                missing.append("the wiki's bot login (setup --section wiki)")
        if sl.get("enabled") and nt.get("slack_user"):
            self.say(f"  Alerts:   Slack DMs to {nt.get('slack_user_name') or nt['slack_user']}: errors, warnings, "
                     "and an alarm if it stops")
            plain = self.svc.slack_commands_help(sl.get("channel_name"), slack=False).splitlines()
            rows = [ln.strip() for ln in plain if ln.startswith(" ")] or [ln.strip() for ln in plain]
            if rows:
                self.say(f"  Commands: {nt.get('slack_user_name') or 'the contact'} can reply to the StreamSplitter "
                         "app in Slack with:")
                for line in rows:
                    self.say(f"    {line}")
        else:
            self.say("  Alerts:   in StreamSplitter's window and log only (no Slack contact)")
            if sl.get("enabled"):
                missing.append("a Slack contact for alerts (setup --section notify)")
        ls = config.section("listen")
        if ls.get("enabled"):                      # not shown when off: most computers are not left on at home
            try:
                running = bool(self.svc.listener().is_running())
            except Exception:
                running = False
            who = _and([nt.get("slack_user_name") or "the contact"] + list(ls.get("user_names") or []))
            how = ("only while its window is open" if ls.get("mode") == "window" else "in the background from each "
                   "log-in")
            self.say(f"  Slack starts: {who} can start and stop recordings here from Slack (split <event>, stop), "
                     f"{how}; " + ("listening now" if running else "not listening now"))
        clips = config.data_root() / "clips" / (ev.get("tag") or ev.get("key") or "<event>")
        free = _gb_free(config.data_root())
        self.say(f"  Clips:    {clips}" + (f" ({free:.0f} GB free)" if free is not None else ""))
        if config.deliver_dir():
            self.say(f"  Copies:   {Path(config.deliver_dir()) / (ev.get('tag') or ev.get('key') or '')}")
        if free is not None and free < 100:
            self.say("  Warning: start an event with about 100 GB free (the recording and the videos). Free some "
                     f"space, or set data_root in {config.config_path()} to a bigger disk.")
        if missing:
            self.say("  Not set up yet: " + "; ".join(missing))

    def _tag(self) -> str | None:
        ev = config.section("event")
        return ev.get("tag") or ev.get("key")

    def _status(self, tag: str | None = None) -> dict:
        tag = tag or self._tag()
        if not tag:
            return {}
        try:
            return self.svc.live().read_status(tag) or {}
        except Exception:
            return {}

    def is_running(self) -> bool:
        return bool(self._status().get("running"))

    def _wait_stopped(self, tag: str | None) -> bool:
        """Wait for a stopping run to end (its lock released), so two runs never send the same clip."""
        wait = getattr(self.svc.live(), "wait_stopped", None)
        if wait is None:
            return not self._status(tag).get("running")
        try:
            return bool(wait(tag or self._tag(), RESTART_WAIT_S))
        except Exception:
            return False

    def start(self) -> bool:
        ev = config.section("event")
        if not (ev.get("stream_url") and ev.get("key")):
            self.say("Set the event and the stream link first (setup --section event / --section stream).")
            return False
        if self.bg is not None and self.bg.busy():
            self.bg.wait(self.io)
        if self.bg is not None and self.bg.failures(required_only=True):
            self.say("Recording cannot start until the downloads work (see above).")
            return False
        st = self._status()
        if st.get("stopping"):
            self.say("The last run is still stopping (it finishes an upload in progress first); waiting for it...")
            if not self._wait_stopped(self._tag()):
                self.say("It has not stopped yet. Pick 'Start recording' again once 'Show status' says it is not "
                         "running.")
                return False
        elif st.get("running"):
            self.say("StreamSplitter is already running.")
            return True
        try:
            pid = self.svc.live().start_detached(live_args())
        except Exception as e:
            self.say(f"Could not start it: {_error_text(e)}")
            return False
        self.say(f"StreamSplitter is running in the background (process {pid}). It records the stream, cuts each")
        self.say("match, and posts or uploads it. You can close this window. Its log: "
                 f"{config.data_root() / 'logs' / f'live_{self._tag()}.log'}")
        self.say("To see how it is doing or to stop it: run the setup again (run.bat or ./run.sh), or")
        self.say(f"  {_cli()} status        {_cli()} stop")
        if config.section("slack").get("enabled") and config.section("notify").get("slack_user"):
            self.say("In Slack, reply 'status' to the StreamSplitter app.")
        return True

    def show_status(self) -> None:
        tag = self._tag()
        try:
            st = self.svc.live().read_status(tag) or {}
        except Exception as e:
            self.say(f"No status available: {_error_text(e)}")
            return
        if not st:
            self.say("StreamSplitter has not run for this event yet.")
            return
        for line in format_status(st):
            self.say(line)

    def stop(self) -> None:
        tag = self._tag()
        if not self.is_running():
            self.say("StreamSplitter is not running.")
            return
        try:
            self.svc.live().request_stop(tag)
        except Exception as e:
            self.say(f"Could not ask it to stop: {_error_text(e)}")
            return
        self.say("Asked StreamSplitter to stop; it finishes the clip it is on and stops within a minute.")

    def restart(self, tag: str | None = None) -> bool:
        """Stop the run of `tag` (the recording before a settings change: the event, and so the tag, may have
        changed), wait until it has really stopped, then start with the saved settings."""
        tag = tag or self._tag()
        try:
            self.svc.live().request_stop(tag)
        except Exception as e:
            self.say(f"Could not ask it to stop: {_error_text(e)}")
            return False
        self.say(f"Asked StreamSplitter to stop; waiting for it to finish what it is doing (up to "
                 f"{int(RESTART_WAIT_S // 60)} minutes)...")
        if not self._wait_stopped(tag):
            self.say("It has not stopped yet (an upload may be finishing). Pick 'Start recording' once 'Show "
                     "status' says it is not running.")
            return False
        return self.start()


def _and(items: list[str]) -> str:
    """"a", "a and b", "a, b and c"."""
    items = [str(x) for x in items]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


def _host(url: str | None) -> str:
    return urllib.parse.urlsplit(url or "").netloc or (url or "the wiki")


def _bad_bot_user(text: str) -> str | None:
    if "@" not in text or text.startswith("@") or text.endswith("@"):
        return ("type the user name the bot password page showed, with the @ in it (like YourName@StreamSplitter), "
                "not the account's own name")
    return None


OFFSEASON_TYPES = {99, 100}         # TBA event_type: 99 offseason, 100 preseason; 0-7 are official FIRST events


def is_official_event(ev: dict) -> bool:
    """An official FIRST event (regional, district, championship...), by TBA's event_type; unknown counts as not."""
    t = ev.get("event_type")
    return t is not None and int(t) not in OFFSEASON_TYPES


def save_event(ev: dict) -> tuple[bool, dict]:
    """Save TBA's event record `ev` as [event] (code, name, year, dates, type; the tag kept for the same event) and,
    when it is another event than the one saved, forget the old event's stream link, YouTube playlist, Google Drive
    event folder and wiki page, so none of them gets this event's matches. Returns (changed, the [event] values
    saved). The event step and the Slack listener's split (listener.py) both use it."""
    cur = config.section("event")
    values = {"key": ev["key"], "name": ev.get("name") or ev["key"], "year": int(ev.get("year") or ev["key"][:4]),
              "start_date": ev.get("start_date"), "end_date": ev.get("end_date"), "event_type": ev.get("event_type")}
    same = cur.get("key") == ev["key"]
    values["tag"] = cur["tag"] if same and cur.get("tag") else ev["key"]
    changed = bool(cur.get("key")) and not same
    if changed:
        values["stream_url"] = None                  # the old event's stream: asked again next
    config.save_sections({"event": values})
    if changed:
        yt, dr, wk = config.section("youtube"), config.section("drive"), config.section("wiki")
        if any(yt.get(k) for k in ("playlist_id", "playlist_title", "playlist_event")):
            # the old event's playlist must not get this event's matches (live.build_publishing checks it too)
            config.save_sections({"youtube": {"playlist_id": None, "playlist_title": None, "playlist_event": None}})
        if any(dr.get(k) for k in ("event_folder_id", "event_folder_event")):
            # the new event's folder is found or made in the top folder with its first match
            config.save_sections({"drive": {"event_folder_id": None, "event_folder_event": None,
                                            "event_folder_title": None, "event_folder_url": None}})
        if any(wk.get(k) for k in ("page_title", "page_event", "event_page")):
            config.save_sections({"wiki": {"page_title": None, "page_event": None, "event_page": None}})
    return changed, values


def live_args() -> list[str]:
    """The `streamsplitter live` arguments for the configured event."""
    ev = config.section("event")
    # no link yet: `live` looks the stream up on TBA, DMs how to set one, and checks again every 10 min
    args = ([ev["stream_url"]] if ev.get("stream_url") else []) + ["--event", ev["key"]]
    if ev.get("tag") and ev["tag"] != ev["key"]:
        args += ["--tag", ev["tag"]]
    return args


def _cli() -> str:
    return r".venv\Scripts\streamsplitter" if os.name == "nt" else ".venv/bin/streamsplitter"


def format_status(st: dict, indent: str = "  ") -> list[str]:
    """status.json as readable lines (nested dicts indented one level)."""
    out = []
    for k, v in st.items():
        label = str(k).replace("_", " ")
        if isinstance(v, dict):
            out.append(f"{indent}{label}:")
            out += format_status(v, indent + "  ")
        elif isinstance(v, float) and v > 1e9:            # a unix time
            out.append(f"{indent}{label}: {time.strftime('%a %H:%M:%S', time.localtime(v))}")
        elif isinstance(v, list):
            out.append(f"{indent}{label}: {', '.join(str(x) for x in v[-5:])}" + (" ..." if len(v) > 5 else ""))
        else:
            out.append(f"{indent}{label}: {v}")
    return out


def main(section: str | None = None, install_only: bool = False, io: BaseIO | None = None,
         svc: Services | None = None, background: Background | None = None) -> int:
    io = io or ConsoleIO()
    prepare_environment()
    deep = path_too_long()
    if deep:
        io.say(f"Warning: {deep}")
    if install_only:
        bg = (background or default_background()).start()
        io.say("Checking and downloading what StreamSplitter needs...")
        try:
            bg.wait(io, verbose=True)
        except KeyboardInterrupt:
            return 130
        return 1 if bg.failures(required_only=True) else 0
    bg = background if background is not None else (default_background() if section is None else None)
    if bg is not None and not bg._threads:
        bg.start()
    try:
        return Wizard(io, svc, bg).run(section)
    except (Cancelled, KeyboardInterrupt):
        io.say()
        io.say("Setup stopped. Everything you finished is saved; run it again to continue.")
        return 130
