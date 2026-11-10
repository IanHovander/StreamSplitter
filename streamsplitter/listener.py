"""Start from Slack: a small program that starts and stops recordings when the owner (the alerts contact), or a
person the owner added, messages the StreamSplitter app in Slack; the owner also changes settings and adds people
there (slackadmin.py).

    streamsplitter listen            listen in this window until it is closed (the same as `listen run`)
    streamsplitter listen start      listen in the background, hidden (log: <data_root>/logs/listener.log)
    streamsplitter listen stop       ask the background one to stop (a stop file, as `streamsplitter stop` uses)
    streamsplitter listen status     whether it runs, and the event it keeps going

Two ways to listen ([listen] mode, user 2026-11-05; the setup asks):
- "background", for a computer left on at home: it starts hidden at each log-in (install_login_start) and runs until
  stopped, with its own stopped-running alarm ([listen] alarm, on by default here).
- "window", for a laptop someone sits at: nothing at log-in and nothing in the background. It listens only while
  `streamsplitter listen` (or the run.bat / run.sh menu's "Listen for Slack commands in this window") is open in a
  visible window; `listen start` refuses. Closing that window or Ctrl+C stops listening only: a recording that split
  started runs detached, as `streamsplitter start` starts one, with its own hidden console, so closing the window
  (which once ended every job) cannot reach it. It DMs "Listening ..." when it opens and "No longer listening ..."
  on a clean exit (Ctrl+C; closing the window too: Windows gives a closing console a few seconds, which serve() uses
  through its own console handler). No alarm by default here: closing the window is deliberate.
Restarts, the auto-stop and keeping the computer awake happen only while a listener runs, in either mode.

What it answers (user, 2026-11-05: leave the home computer on and start and stop recordings from Slack; no server and
no open ports, only calls out to Slack, as the recording's own commands make, publish/commands.py). Every message of
an allowed person gets exactly one answer, from here (user, 2026-11-09: two programs answering one message, sometimes
contradicting each other; publish/commands.py says who reads what):
    split <event>   a TBA event code (2026mttd), its TBA link, or part of its name, short name or city, searched among
                    this year's events (several hits are listed with their codes); start / record <event> too. With the
                    code (or link) of an event on today or tomorrow (LEAD_DAYS) it starts at once and the reply sums up
                    the event, the stream (TBA's, picked as the setup picks it) and where the clips go, ending "Reply
                    stop to stop it" (user, 2026-11-09: the yes it used to wait for was missed twice and nothing was
                    recorded). Found by name, a bare `split` (the event set up), or an event not on now: it asks first
                    and says why; split <event> yes starts without asking. The question waits QUESTION_S, for the
                    person who asked only; after REMIND_S it reminds them once, and when it expires it says so.
    yes / no        yes saves the event in config.toml as the setup's event step does (setup_wizard.save_event: the
                    old event's playlist, Drive event folder and wiki page are forgotten), makes the YouTube playlist
                    when YouTube is on and allowed (off at official FIRST events, as in the setup; when it cannot be
                    made, this event records without YouTube rather than not at all), then starts the recording as
                    `streamsplitter start` does. The recording's own "StreamSplitter started" DM follows. ok and sure
                    count as yes; with no question waiting, ok and thanks get a thumbs-up.
    stop            asks "Stop recording <event>? Reply yes"; yes stops it (request_stop) and says when it has. stop yes,
                    or stop with the running event's code, stops without asking.
    status          (list, info) whether a recording runs and how (its own status, through its mailbox), and whether
                    this listener runs and how quickly it reads the asker's chat.
    help            a short first screen for the asker and the state (commands.first_help); help all, help <topic>.
    set event <code>  the same summary as split, then yes saves the event without starting it (admins).
    settings, set, people, add, remove, set owner, stop listener: the admins' (slackadmin.Admin; people: anyone's).
    set stream, set teams, pause, resume (the running recording's, admins): passed to the running recording through
                    its mailbox (publish/commands.Mailbox), whose reply is posted here (deliver); while nothing
                    records, saved for the next start (Admin.offline).
    anything else   what it probably meant (commands.unknown_reply), else the few commands that matter now.
One recording per computer: split while another event records says to stop that one first. A recording of an older
version (no mailbox) answers its owner's DM as before; this program then stays silent there for what that recording
answers (commands.legacy_listener_command), and says so to an admin whose command it cannot pass on.

Roles (user, 2026-11-09; slackadmin.py). Admins (the owner, [notify] slack_user, who gets the alerts, and [listen]
admins) change settings, people and the running recording. People ([listen] users / user_names) are added and removed
from Slack by an admin (add @name; the setup no longer asks for them) and may use help, status, split, yes, no and
stop. Anything else gets a no that names the admins and says what to do. A message that looks like a token or key is
answered with what to revoke and is not used (slackadmin.secret_reply).

How it reads the DMs: one publish/commands.CommandPoller per person (the owner first), each reading that person's
DM with the app with its checks (im:history, only that person's own messages, the 15-minute restart grace, one note
about older messages not done) and its saved position, <data_root>/listen/commands_<user>.json, which a recording
reading the owner's DM while no listener runs keeps up to date too, so no message is handled twice across a restart
or a hand-over. The handled timestamps are kept too, and a yes older than its question does nothing. Each message
gets a :eyes: reaction as it is read when the app may (the optional reactions:write). People added or removed (from
Slack, or by the setup) are picked up at once or within a tick (reload_people). status.json says "protocol":
commands.PROTOCOL, which tells a recording that this listener reads its owner's DM (it reads none then).

Polling budget. conversations.history is Tier 3 for this app (about 50 calls a minute, per app and method). The
listener reads: the owner and the other admins every 20 s, and anyone who wrote in the last ACTIVE_S (10 minutes) or
has a question waiting, at most ACTIVE_MAX (5) of them with the owner (18 a minute); everyone else in turn, sharing
OTHERS_PER_MIN (20) reads a minute: every max(20 s, 3 s x their number), e.g. 36 s for 12 people, 60 s for 20.
After someone writes (user, 2026-11-09: replies took 10-20 s), their chat is read every commands.FAST_S (3 s) for
commands.HOT_S (2 minutes), at most HOT_MAX chats at once sharing HOT_PER_MIN reads a minute; whatever happens, at
most READS_PER_MIN reads go out in any minute (the most urgent first: those chats, then the owner and admins, then the
rest). A recording reads no DM while this listener runs. The first reads after a start are spread STAGGER_S apart. A
`ratelimited` answer holds every read back for as long as Slack asks (at least a minute).

Keeping it going. An event confirmed from Slack is "active" until `stop` from Slack, a new split, or the auto-stop.
- Restart: when its recording is not running (its lock is free) and did not stop on purpose, it is started again and
  the contact told. On purpose means request_stop's stop file ended it: Slack's stop, `streamsplitter stop`, the
  run.bat menu and the setup's restart all use it, and live.py then writes stopped_reason "stop requested ..." as its
  last status; a crash, a killed process, a power cut or a Windows restart leave another reason or none. At most 3
  restarts in an hour, then one DM and no more tries until the next split. After a reboot and log-in this program
  starts and restarts the active event the same way. Never while another recording runs, and only while config.toml's
  event is still that event (the setup may have changed it).
- Auto-stop: `live` never ends by itself (it waits for each day's stream, user 2026-11-04), so at 06:00 (this
  computer's time) the morning after the event's last day the active recording is stopped and the contact gets one
  line: clips made, uploads still waiting. An event that had already ended when it was split records until `stop`.
- Its own stopped-running alarm in the contact's DM (notify.py's mechanism, in its own words; [listen] alarm, on by
  default in the background mode, off in the window mode). With a recording's alarm, about six messages at most are
  scheduled in that DM at once, minutes apart; Slack allows 30 within any 5 minutes per channel (notify.py).
- Sleep: blocked while it runs ([listen] keep_awake, on by default), as during a recording; the screen may still turn
  off. It cannot stop a Windows Update restart: after one, it starts again only once someone logs in.

Starting at log-in (install_login_start; the background mode only), without admin rights and with no window a
person could close (a closed console window once ended every job): Windows, a shortcut in the user's Startup folder
to the venv's pythonw.exe (a program with no console) running `listen start`, which starts `listen run` with no
window, as start_detached starts a recording; macOS, a LaunchAgent; Linux, an XDG autostart entry.

Files, in <data_root>/listen/: listen.lock (one listener per computer), status.json, state.json (the active event,
questions waiting, restarts, handled messages, commands passed to a recording), stop, alarm.json,
commands_<user>.json (deleted when that person is removed), replies/ (a recording's replies to pass on); the log is
<data_root>/logs/listener.log. Nothing of it goes in the team settings file: it belongs to this computer.

Optional, not built: Slack's Socket Mode (an app-level xapp- token, Socket Mode and the message.im event turned on in
the app) would let Slack push each message here at once instead of being asked; slack_sdk, already a dependency, has a
client for it (slack_sdk.socket_mode.builtin). The polling above would stay as the fallback.
"""

from __future__ import annotations

import collections
import datetime as _dt
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from . import config, slackadmin, streams
from .publish import commands
from .publish.notify import Notifier, alarm_timing, redact

LOCK_FILE = "listen.lock"
STATUS_FILE = "status.json"
STATE_FILE = "state.json"
STOP_FILE = "stop"
ALARM_FILE = "alarm.json"
LOCK_WAIT_S = 3.0          # a starting listener tries its lock this long (a status check holds it for an instant)
TICK_S = 5.0               # the restart / auto-stop / stopping checks
CONFIRM_S = slackadmin.QUESTION_S   # a question waits this long for its yes (by Slack's message times)
CONFIRM_MIN = int(CONFIRM_S // 60)
REMIND_S = 300.0           # a split question still waiting this long gets one reminder
LEAD_DAYS = 1              # split <code> starts at once from this many days before the event's first day
ACTIVE_S = 600.0           # someone who wrote this recently, or has a question waiting, is read as often as the owner
ACTIVE_MAX = 5             # ... at most this many at once (the owner and the admins first)
OTHERS_PER_MIN = 20.0      # everyone else's reads share this many conversations.history calls a minute
HOT_MAX = 3                # chats read every commands.FAST_S after their person wrote, at most this many at once
HOT_PER_MIN = 24.0         # ... sharing this many reads a minute (one chat: every 3 s; two: every 5 s)
READS_PER_MIN = 45         # all of the listener's conversations.history calls in any minute (Slack allows about 50)
STAGGER_S = 60.0 / OTHERS_PER_MIN   # the first reads after a start, this far apart (3 s): everyone else's reads
#                                     then stay spread out at their own pace, never a burst of one read a second
FORWARD_WAIT_S = 20.0      # a command passed to the recording and not taken this long is given up (nothing done)
FORWARD_GIVEUP_S = 180.0   # ... taken but not answered this long: given up too (it may still happen)
PENDING_KEEP_S = 86400.0   # ... and is kept a day, so a late yes is told it expired
STOP_WAIT_S = 180.0        # a stopping recording gets this long before "it has not stopped yet"
RESTART_MAX = 3            # restarts in RESTART_WINDOW_S, then no more until the next split
RESTART_WINDOW_S = 3600.0
START_GRACE_S = 60.0       # a recording just started is left alone this long (it takes its lock within seconds)
AUTO_STOP_HOUR = 6         # the morning after the event's last day, this computer's time
HUNG_S = 300.0             # the heartbeat stops pushing the alarm back when the main loop is stuck this long
HEARTBEAT_RETRY_S = 15.0
HANDLED_KEEP = 300         # handled message timestamps kept
LIST_MAX = 9               # events listed for a name with several hits
CLOSE_WAIT_S = 4.0         # a closing console window: how long the clean exit (its DM) may take; Windows allows ~5 s
MENU_START = "Start listening for Slack commands"     # the run.bat / run.sh menu items (setup_wizard.menu)
MENU_STOP = "Stop listening for Slack commands"
MENU_WINDOW = "Listen for Slack commands in this window"
BACKGROUND, WINDOW = "background", "window"           # [listen] mode
SHORTCUT_NAME = "StreamSplitter listener.lnk"
AGENT_LABEL = "com.streamsplitter.listener"
AUTOSTART_NAME = "streamsplitter-listener.desktop"

UNKNOWN = commands.UNKNOWN
NOTHING_TO_CONFIRM = commands.NOTHING_TO_CONFIRM
SPLIT_HOW = ("split needs the event: its code (split 2026mttd, the end of its thebluealliance.com/event/... link), "
             "that link, or part of its name (split madtown).")
_TBA_LINK = re.compile(r"(?:https?://)?(?:www\.)?thebluealliance\.com/event/(\d{4}[a-z0-9]+)", re.I)
_KEY = re.compile(r"\d{4}[a-z0-9]{1,16}")
_RECORDING_COMMANDS = commands.RECORDING_COMMANDS
PAUSED = " Paused: the uploads wait until you reply resume {dest}."   # a split summary's YouTube / Drive line
REPLIES = commands.REPLIES_DIR


def _clock(t) -> str:
    return time.strftime("%a %H:%M", time.localtime(float(t))) if t else "?"


def _ts(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------------------------------------------
# Where its files are; one listener per computer
# ---------------------------------------------------------------------------------------------------------------

def listen_folder() -> Path:
    return config.data_root() / "listen"


def log_path() -> Path:
    return config.data_root() / "logs" / "listener.log"


class AlreadyRunning(RuntimeError):
    """The listener holds its lock: it runs (or is just stopping)."""


class _Lock:
    """An OS lock on a file, held while the listener runs and released by the OS however it ends (as live.RunLock;
    a copy, so the listener and the setup need not load live.py and the detection packages)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._f = None

    def acquire(self, wait_s: float = 0.0) -> bool:
        deadline = time.monotonic() + wait_s
        while not self._try():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)
        return True

    def _try(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self._f = f
        return True

    def release(self) -> None:
        f, self._f = self._f, None
        if f is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            f.close()


def is_running() -> bool | None:
    """Whether a listener holds its lock on this computer (None: the lock cannot be tried). Trying takes the lock for
    an instant, which a starting listener waits out (LOCK_WAIT_S)."""
    path = listen_folder() / LOCK_FILE
    if not path.is_file():
        return False
    probe = _Lock(path)
    try:
        if probe._try():
            probe.release()
            return False
    except OSError:
        return None
    return True


def read_status() -> dict:
    """status.json, with "running" from the lock (a crashed listener cannot correct its own file)."""
    try:
        data = json.loads((listen_folder() / STATUS_FILE).read_text(encoding="utf-8"))
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    data["running"] = bool(is_running())
    return data


def request_stop() -> Path:
    """Ask the running listener to stop: a stop file it checks every second (never a signal; it has no console)."""
    path = listen_folder() / STOP_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"stop requested {time.strftime('%Y-%m-%d %H:%M:%S')}\n", encoding="utf-8")
    return path


def wait_stopped(timeout_s: float = 30.0, sleep=time.sleep) -> bool:
    deadline = time.monotonic() + timeout_s
    while is_running():
        if time.monotonic() >= deadline:
            return False
        sleep(0.5)
    return True


def _console_python() -> str:
    """The venv's python.exe, also when this runs under pythonw.exe (the log-in shortcut): `listen run` runs with a
    hidden console, which the recordings it starts share, as a recording's own children do."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").is_file():
        return str(exe.with_name("python.exe"))
    return str(exe)


def start_detached() -> int:
    """Start `streamsplitter listen run` in the background, as live.start_detached starts a recording: Windows, no
    console window (CREATE_NO_WINDOW: a hidden one nobody can close), its own process group and out of the caller's
    job object when allowed; mac/Linux, a new session with stdin from /dev/null. Logs to listener.log (appended).
    Returns the pid; AlreadyRunning while one runs."""
    if is_running():
        raise AlreadyRunning("The Slack listener is already running on this computer.")
    return _spawn([_console_python(), "-u", "-m", "streamsplitter.cli", "listen", "run"], log_path())


def _spawn(cmd: list[str], log: Path) -> int:
    """Start `cmd` with no window and detached (start_detached), its output appended to `log`."""
    log.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    with open(log, "ab") as out:
        out.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} start: {' '.join(cmd[1:])}\n".encode("utf-8"))
        out.flush()
        kw = dict(cwd=str(config.ROOT), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=env,
                  close_fds=True)
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            try:
                p = subprocess.Popen(cmd, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kw)
            except OSError:                       # the caller's job object forbids breaking away
                p = subprocess.Popen(cmd, creationflags=flags, **kw)
        else:
            p = subprocess.Popen(cmd, start_new_session=True, **kw)
    return p.pid


def mode_of(cfg: dict | None = None) -> str:
    """[listen] mode: "background" (the default) or "window"."""
    cfg = config.section("listen") if cfg is None else cfg
    return WINDOW if str(cfg.get("mode") or "").lower() == WINDOW else BACKGROUND


def allowed_users(cfg: dict | None = None, nt: dict | None = None) -> list[str]:
    """Who may command it: the [notify] contact first, then [listen] users (Slack user ids)."""
    cfg = config.section("listen") if cfg is None else cfg
    nt = config.section("notify") if nt is None else nt
    out = [nt["slack_user"]] if nt.get("slack_user") else []
    for u in cfg.get("users") or []:
        if isinstance(u, str) and u.strip() and u.strip() not in out:
            out.append(u.strip())
    return out


def setup_problem(cfg: dict | None = None, nt: dict | None = None) -> str | None:
    """What stops the listener from running here, with the fix; None when nothing does."""
    cfg = config.section("listen") if cfg is None else cfg
    nt = config.section("notify") if nt is None else nt
    if not cfg.get("enabled"):
        return ("Start from Slack is off on this computer. To turn it on: run.bat (Mac/Linux: sh run.sh), Change a "
                "setting, Start from Slack; or `streamsplitter setup --section listen`.")
    if not config.get_secret("slack_bot_token"):
        return "No Slack token is stored on this computer: run `streamsplitter setup --section slack` first."
    if not nt.get("slack_user"):
        return "No Slack contact is set ([notify] slack_user): run `streamsplitter setup --section notify` first."
    return None


def keep_awake():
    """No system sleep while the listener runs (as live.keep_awake; the screen may still turn off). Windows: the
    thread execution state, which lasts while this thread runs; macOS: caffeinate tied to this process; Linux:
    systemd-inhibit, when there is one (the returned process is ended on the way out)."""
    if os.name == "nt":
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        return None
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        return subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    if shutil.which("systemd-inhibit"):
        return subprocess.Popen(["systemd-inhibit", "--what=sleep", "--why=StreamSplitter listener", "sleep",
                                 "infinity"])
    return None


# ---------------------------------------------------------------------------------------------------------------
# Its DMs and its alarm, in its own words
# ---------------------------------------------------------------------------------------------------------------

class ListenerNotifier(Notifier):
    """notify.Notifier (DMs to the contact, retried; the stopped-running alarm, a scheduled DM pushed back every
    minute and swept after a restart by its first words), saying it is the listener. `window`: the window mode
    (its stop DM says it no longer listens; a recording keeps going)."""

    def __init__(self, *a, window: bool = False, **kw):
        self.window = window
        super().__init__(*a, **kw)

    def signature(self) -> str:
        return f"StreamSplitter listener on {self.host}"

    def _alarm_marker(self) -> str:
        return f"The StreamSplitter listener on {self.host} stopped at"

    def alarm_text(self, checked_in: float) -> str:
        how = (f"open run.bat (Mac/Linux: sh run.sh) on it and choose {MENU_WINDOW}" if self.window else
               f"log in to the computer, or double-click run.bat (Mac/Linux: sh run.sh) and choose {MENU_START}")
        return (f"{self._alarm_marker()} {_clock(checked_in)}. Slack can't start or restart recordings until it runs "
                f"again: {how}.")

    def false_alarm_text(self, last: float) -> str:
        return (f"False alarm: the StreamSplitter listener on {self.host} is still running. It could not push its "
                f"alarm back after {_clock(last)} (no internet, or Slack unreachable).")

    def running_again_text(self) -> str:
        return (f"The StreamSplitter listener on {self.host} is running again (since {_clock(self._t_started)}). "
                f"Replies to your messages come within about {commands.POLL_S:.0f} s.")

    def restarted(self) -> None:
        """The run before stopped cleanly and DMed so: say this one runs (user, 2026-11-07: a restart for an update
        left "stopped ... until it runs again" as the last word, and it looked as if MadTown had stopped)."""
        self._enqueue(self.running_again_text())

    def stopped_text(self, reason: str) -> str:
        if self.window:
            return (f"No longer listening for Slack commands on {self.host} ({reason}). A recording that runs keeps "
                    f"going.")
        return (f"The StreamSplitter listener on {self.host} stopped: {reason}. Slack can't start or restart "
                f"recordings until it runs again.")


class _Heartbeat:
    """Pushes the listener's alarm back every `every_s` while its main loop shows life (as live.Heartbeat)."""

    def __init__(self, notifier, listener, every_s: float):
        self.notifier, self.listener, self.every_s = notifier, listener, every_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="listener-heartbeat", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(5.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            if time.time() - self.listener.alive >= HUNG_S:
                ok = True                           # stuck: let the alarm come due
            else:
                try:
                    ok = self.notifier.heartbeat()
                except Exception as e:  # noqa: BLE001 - never let the heartbeat thread die
                    print(f"heartbeat: {type(e).__name__}: {redact(e)}", flush=True)
                    ok = False
            every = self.every_s if ok else min(HEARTBEAT_RETRY_S, self.every_s)
            self._stop.wait(max(1.0, every - (time.monotonic() - t0)))


# ---------------------------------------------------------------------------------------------------------------
# The recordings on this computer (live.py, loaded when first needed)
# ---------------------------------------------------------------------------------------------------------------

class Runner:
    """Starting, stopping and reading the recordings through live.py, as `streamsplitter start/stop/status` do.
    live.py loads the detection packages, so it is imported only when first needed. Tests pass a fake."""

    def __init__(self):
        self._live = None

    def live(self):
        if self._live is None:
            from . import live
            self._live = live
        return self._live

    def recordings(self) -> list[dict]:
        """read_status() of every recording on this computer that is running or stopping, with "tag"."""
        try:
            live = self.live()
        except ImportError:                       # not installed yet: nothing can be recording
            return []
        out = []
        for lock in sorted((config.work_root() / "live").glob(f"*/{live.LOCK_FILE}")):
            st = live.read_status(lock.parent.name) or {}
            if st.get("running"):
                out.append({**st, "tag": lock.parent.name})
        return out

    def status(self, tag: str) -> dict:
        try:
            return self.live().read_status(tag) or {}
        except ImportError:
            return {}

    def stop_waiting(self, tag: str) -> bool:
        """A stop file not yet taken: someone asked this recording to stop."""
        live = self.live()
        return (live.live_folder(tag) / live.STOP_FILE).is_file()

    def mailbox(self, tag: str) -> Path | None:
        """The running recording's mailbox folder (publish/commands.Mailbox), when it has one open: this version's
        protocol, advertised by the process that writes its status. None for a recording of an older version."""
        try:
            live = self.live()
        except ImportError:
            return None
        folder = live.live_folder(tag)
        ad = commands.advertised(folder)
        if not ad or int(ad.get("protocol") or 0) < commands.PROTOCOL:
            return None
        st = live.read_status(tag) or {}
        if not st.get("running") or (st.get("pid") is not None and st.get("pid") != ad.get("pid")):
            return None
        return folder / commands.INBOX_DIR

    def start(self) -> int:
        """Start the event config.toml has, in the background, as `streamsplitter start` does (cli._start). Returns
        the pid; raises with what is wrong."""
        from . import setup_wizard
        try:
            live = self.live()
        except ImportError as e:
            raise RuntimeError(f"StreamSplitter is not fully installed on this computer ({e}): run run.bat (Mac/"
                               f"Linux: sh run.sh) once") from None
        ev = config.section("event")
        if not ev.get("key"):
            raise RuntimeError("no event is set up in config.toml")
        st = live.read_status(ev.get("tag") or ev["key"]) or {}
        if st.get("stopping"):
            raise live.AlreadyRunning("the last run is still stopping (it finishes an upload first)")
        if st.get("running"):
            raise live.AlreadyRunning("it is already running")
        return live.start_detached(setup_wizard.live_args())

    def request_stop(self, tag: str, by: str = "streamsplitter stop") -> None:
        self.live().request_stop(tag, by=by)

    def clips(self, tag: str) -> int:
        """Match clips (not score versions) in the clip folder."""
        try:
            return sum(1 for p in (config.data_root() / "clips" / tag).glob("*.mp4") if not p.stem.endswith("_score"))
        except OSError:
            return 0

    def event_name(self, tag: str) -> str | None:
        """The event's name from the TBA record the recording kept (tba_event.json), if any."""
        try:
            return json.loads((config.work_root() / "live" / tag / "tba_event.json").read_text(encoding="utf-8"))\
                .get("name")
        except (OSError, ValueError, AttributeError):
            return None


def _tba_get(path: str):
    from . import tba
    return tba.get(path)


def _youtube_client():
    """The YouTube client of this computer's login, as live.py makes it for uploads."""
    from .publish import youtube
    token, client_json = config.get_secret("youtube_token"), config.get_secret("youtube_client")
    if not (token and client_json):
        raise RuntimeError("this computer is not logged in to YouTube")
    return youtube.YouTubeClient(token, json.loads(client_json),
                                 on_token=lambda j: config.set_secret("youtube_token", j))


def event_target(text: str) -> tuple[str, str]:
    """("key", "2026mttd") for a code or a TBA link (with or without https or www); else ("name", the words)."""
    s = streams.unwrap_slack(text or "").strip().strip("\"'`<>")
    m = _TBA_LINK.search(s)
    if m:
        return "key", m.group(1).lower()
    if _KEY.fullmatch(s.lower()):
        return "key", s.lower()
    return "name", " ".join(s.split())


def stop_time(ev: dict) -> float | None:
    """AUTO_STOP_HOUR (this computer's time) the morning after the event's last day; None when TBA gave no dates."""
    try:
        last = _dt.date.fromisoformat(str(ev.get("end_date") or ev.get("start_date")))
    except (TypeError, ValueError):
        return None
    return _dt.datetime.combine(last + _dt.timedelta(days=1), _dt.time(AUTO_STOP_HOUR)).timestamp()


class LookupProblem(Exception):
    """The event could not be looked up; the message is the reply."""


# ---------------------------------------------------------------------------------------------------------------
# The listener
# ---------------------------------------------------------------------------------------------------------------

class _Poller(commands.CommandPoller):
    """CommandPoller for one allowed person's DM: its reads, scope check, restart grace and saved position; each
    message goes to Listener.handle (None: no reply) instead of the recording's dispatch(). It always reads (it is the
    listener), and how often is the listener's (Listener.interval)."""

    def __init__(self, listener: "Listener", client, user_id: str, **kw):
        super().__init__(client, user_id, None, **kw)
        self.listener = listener

    @property
    def channel(self) -> str | None:
        return self._channel

    def mode(self) -> str:
        return commands.LISTENER

    def next_wait(self) -> float:
        return commands.POLL_S

    def poll(self) -> float:
        if self.user not in self.listener.users:
            return commands.POLL_S              # removed in this round: not read (nor its position saved) again
        return super().poll()

    def _answer(self, m: dict) -> None:
        try:
            reply = self.listener.handle(self, m)
        except Exception as e:  # noqa: BLE001 - say so rather than nothing
            self.log(f"Slack listener: {type(e).__name__}: {redact(e)}\n{traceback.format_exc()}")
            reply = (f"That did not work ({type(e).__name__}: {redact(e)}). The listener's log on "
                     f"{self.listener.host} says more ({log_path()}).")
        if reply:
            self._reply(m, reply)


class Listener:
    """The commands and the keeping-going (module docstring). handle() answers one message; tick() runs every
    TICK_S; loop() runs both until stop() says why to stop. Everything outside (Slack, TBA, YouTube, the recordings)
    is passed in, so tests run it with fakes."""

    def __init__(self, client, contact: str, users: list[str] | None = None, *, contact_name: str | None = None,
                 user_names: list[str] | None = None, runner=None, notifier=None, folder: Path | None = None,
                 tba_get=None, channel_live=streams.channel_live_page, youtube=None, clock=time.time, log=print,
                 host: str | None = None, window: bool = False):
        """`users`: everyone allowed (the contact first); `user_names`: the others' names, in the same order (their
        Slack ids when missing). `window`: the window mode (it keeps an event going only while its window is
        open)."""
        self.window = window
        self.client = client
        self.runner = runner if runner is not None else Runner()
        self.notifier = notifier if notifier is not None else Notifier(log=log)
        self.folder = Path(folder) if folder else listen_folder()
        self.tba_get = tba_get or _tba_get
        self.channel_live = channel_live
        self.youtube = youtube or _youtube_client
        self.clock, self.log = clock, log
        self.host = host or socket.gethostname()
        self.started = self.alive = clock()
        self._lock = threading.RLock()
        self.state = self._load()
        self.quit: str | None = None            # why to stop, said from Slack (set listener off)
        self.heard: dict[str, float] = {}       # when each person last wrote (this computer's clock)
        self.monotonic = time.monotonic         # the loop's timer (tests replace it)
        self.pollers: list[_Poller] = []
        self.admins: list[str] = []             # slackadmin.admin_ids(), read with the people
        self._set_people(contact, contact_name, users, user_names)
        self._read_admins()
        self.admin = slackadmin.Admin(client, host=self.host, log=log, clock=clock, listener=self,
                                      recording=lambda: bool(self.runner.recordings()), youtube=self.youtube)

    # people ---------------------------------------------------------------------------------------------------------
    @staticmethod
    def _people_of(contact: str, contact_name: str | None, users, user_names) -> tuple[list[str], dict[str, str]]:
        """([the owner, then the people], {id: name}); `user_names` go with `users` without the owner, in order."""
        name = contact_name or "the alerts contact"
        others = [u for u in dict.fromkeys(users or []) if u and u != contact]
        names = list(user_names or [])
        return [contact] + others, {contact: name, **{u: (names[i] if i < len(names) and names[i] else u)
                                                      for i, u in enumerate(others)}}

    def _set_people(self, contact: str, contact_name: str | None, users, user_names) -> None:
        """The owner and the people, and a poller for each (the owner's first; one already reading keeps going)."""
        self.contact = contact
        self.contact_name = contact_name or "the alerts contact"
        self.users, self.names = self._people_of(contact, contact_name, users, user_names)
        have = {p.user: p for p in self.pollers}
        self.pollers = [have.get(u) or _Poller(self, self.client, u, notifier=self.notifier,
                                               state_path=self.folder / f"commands_{u}.json", log=self.log,
                                               clock=self.clock) for u in self.users]

    def reload_people(self, removed: str | None = None) -> bool:
        """Read the owner and the people from config.toml again (changed from Slack or by the setup); True when they
        changed. A removed person's question and saved position are dropped."""
        with self._lock:
            nt, (users, names) = config.section("notify"), slackadmin.people()
            contact = nt.get("slack_user")
            if not contact:
                return False
            self._read_admins()
            name = nt.get("slack_user_name")
            pairs = [(u, n) for u, n in zip(users, names) if u != contact]
            users, names = [u for u, _ in pairs], [n for _, n in pairs]
            if self._people_of(contact, name, users, names) == (self.users, self.names) and removed is None:
                return False
            new_users = self._people_of(contact, name, users, names)[0]
            gone = [u for u in self.users if u not in new_users] + ([removed] if removed else [])
            self._set_people(contact, name, users, names)
            for u in dict.fromkeys(gone):
                if u in self.users:
                    continue
                if self.state["pending"].pop(u, None) is not None:
                    self._save()
                self.heard.pop(u, None)
                try:
                    (self.folder / f"commands_{u}.json").unlink(missing_ok=True)
                except OSError:
                    pass
            self.log(f"listening in {self.people()}")
            return True

    def _read_admins(self) -> None:
        try:
            self.admins = slackadmin.admin_ids()
        except Exception as e:  # noqa: BLE001 - an unreadable config.toml: keep them as they are
            self.log(f"could not read the admins ({type(e).__name__})")

    def read_every(self, now: float | None = None) -> float:
        """Seconds between reads of someone not active (the polling budget, module docstring)."""
        now = self.clock() if now is None else now
        idle = len(self.users) - 1 - len(self._active(now))
        return max(commands.POLL_S, 60.0 * idle / OTHERS_PER_MIN)

    def _active(self, now: float) -> list[str]:
        """The people (not the owner) read as often as the owner: the admins, then a question waiting or wrote in the
        last ACTIVE_S, the most recent first; at most ACTIVE_MAX."""
        admins = [u for u in self.users[1:] if u in self.admins]
        recent = [(self.heard.get(u, 0.0), u) for u in self.users[1:] if u not in admins and
                  (u in self.state["pending"] or now - self.heard.get(u, -1e18) < ACTIVE_S)]
        return (admins + [u for _, u in sorted(recent, reverse=True)])[:ACTIVE_MAX]

    def _hot(self, now: float) -> list[str]:
        """The chats read every commands.FAST_S: their person wrote in the last commands.HOT_S (or a command of theirs
        waits for the recording's answer), the most recent first, at most HOT_MAX."""
        waiting = {f.get("user") for f in self.state["forwards"].values()}
        recent = [(self.heard.get(u, 0.0), u) for u in self.users
                  if now - self.heard.get(u, -1e18) < commands.HOT_S or u in waiting]
        return [u for _, u in sorted(recent, reverse=True)[:HOT_MAX]]

    def interval(self, user: str, now: float | None = None) -> float:
        """Seconds until `user`'s DM is read again (the polling budget, module docstring)."""
        now = self.clock() if now is None else now
        hot = self._hot(now)
        if user in hot:
            return max(commands.FAST_S, 60.0 * len(hot) / HOT_PER_MIN)
        return self.usual_interval(user, now)

    def usual_interval(self, user: str, now: float | None = None) -> float:
        """How often `user`'s DM is read while they are not writing (help and status say so)."""
        now = self.clock() if now is None else now
        if user == self.contact or user in self._active(now):
            return commands.POLL_S
        return self.read_every(now)

    # state ----------------------------------------------------------------------------------------------------------
    def _load(self) -> dict:
        try:
            data = json.loads((self.folder / STATE_FILE).read_text(encoding="utf-8"))
            data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            data = {}
        return {"active": data.get("active") or None, "pending": dict(data.get("pending") or {}),
                "stopping": list(data.get("stopping") or []), "restarts": list(data.get("restarts") or []),
                "gave_up": bool(data.get("gave_up")), "next_check": float(data.get("next_check") or 0.0),
                "handled": list(data.get("handled") or []), "forwards": dict(data.get("forwards") or {})}

    def _save(self) -> None:
        try:
            _write_json(self.folder / STATE_FILE, self.state)
        except OSError as e:
            self.log(f"could not save {self.folder / STATE_FILE} ({e})")

    def write_status(self, final: bool = False, reason: str | None = None) -> None:
        a = self.state.get("active")
        data = {"running": not final, "pid": os.getpid(), "host": self.host, "started": self.started,
                "protocol": commands.PROTOCOL,    # a recording reads no DM while this listener runs (commands.py)
                "updated": self.clock(), "users": self.users, "active": a, "gave_up": self.state.get("gave_up"),
                "restarts_last_hour": len([t for t in self.state["restarts"] if self.clock() - t < RESTART_WINDOW_S]),
                "stopped_reason": reason}
        try:
            _write_json(self.folder / STATUS_FILE, data)
        except OSError as e:
            self.log(f"could not write {self.folder / STATUS_FILE} ({e})")

    # names ----------------------------------------------------------------------------------------------------------
    def _name(self, tag: str) -> str:
        """"MadTown Throwdown (2026mttd)" for a recording's tag."""
        a = self.state.get("active")
        if a and a.get("tag") == tag:
            return f"{a.get('name') or a['key']} ({a['key']})"
        ev = config.section("event")
        if tag in (ev.get("tag"), ev.get("key")) and ev.get("name"):
            return f"{ev['name']} ({ev['key']})"
        name = self.runner.event_name(tag)
        return f"{name} ({tag})" if name else tag

    def _key_of(self, tag: str) -> str:
        a = self.state.get("active")
        if a and a.get("tag") == tag:
            return a["key"]
        ev = config.section("event")
        return ev["key"] if ev.get("key") and tag == (ev.get("tag") or ev["key"]) else tag

    # one message ----------------------------------------------------------------------------------------------------
    def handle(self, poller, m: dict) -> str | None:
        """The reply to one message of an allowed person (None: no reply now), after doing what it asks. Every message
        gets exactly one answer: this reply; the recording's, passed on later (forward); a thumbs-up; or, for a
        recording of an older version in its owner's DM, that recording's own (module docstring)."""
        with self._lock:
            ts, user = str(m.get("ts") or ""), poller.user
            mark = f"{user}:{ts}"
            if mark in self.state["handled"]:
                return None
            self.state["handled"] = (self.state["handled"] + [mark])[-HANDLED_KEEP:]
            self._save()                        # before acting: a crash midway never acts twice
            text = str(m.get("text") or "")
            secret = slackadmin.secret_reply(text)
            cmd = None if secret is not None else commands.parse(text)
            rec = self.runner.recordings()
            box = self._mailbox(rec)
            if rec and box is None and user == self.contact and secret is None and \
                    not commands.legacy_listener_command(text):
                self.log(f"{self.names.get(user, user)}: {cmd.name} -> left to the recording (an older version)")
                return None
            self.heard[user] = self.clock()
            ack = cmd is not None and (cmd.name == "ack" or (cmd.name == "yes" and cmd.arg in commands.SOFT_YES
                                                             and user not in self.state["pending"]))
            reacted = poller.react(m, commands.THANKS if ack else commands.SEEN)
            if secret is not None:
                reply, word = secret, "(looked like a password or key: not used)"
            elif ack:
                reply, word = (None if reacted else ":+1:"), "thanks"
            else:
                word = cmd.name
                reply = self.route(poller, m, cmd, rec, box)
            if cmd is None or cmd.name not in ("split", "set_event"):
                reply = self._still_waiting(user, ts, reply)     # a new split is about another question
            # one short line per message, for the window or the log: the command word and the answer's first line
            # (the answer never repeats what was typed)
            said = (reply or "").splitlines()[0][:110] if reply else "(answered later, or a reaction)"
            self.log(f"{self.names.get(user, user)}: {word} -> {said}")
            return reply

    def route(self, poller, m: dict, cmd, rec: list[dict], box: Path | None) -> str | None:
        """The reply to one parsed command (None: the recording's reply follows, passed on by deliver)."""
        user, ts, name = poller.user, str(m.get("ts") or ""), cmd.name
        admin = user in self.admins
        if name == "help":
            return self.help_text(user, cmd.arg, poller)
        if name == "status":
            return self.status_reply(poller, m, cmd, rec, box)
        if name == "split":
            return self.split(user, ts, cmd.arg or "", confirm=cmd.confirm, channel=poller.channel)
        if name == "set_event":
            if not admin:
                return self.admin.refuse(user, "change the event")
            return self.split(user, ts, cmd.arg or "", start=False, confirm=cmd.confirm, channel=poller.channel)
        if name == "yes":
            return self.yes(user, ts, poller)
        if name == "no":
            return self.no(user, ts)
        if name == "number":
            return self.number(user, ts, str(cmd.arg))
        if name == "stop":
            return self.ask_stop(user, ts, cmd, poller)
        if name == "admin":
            reply, q = self.admin.handle(user, cmd.slack)
            if q is not None:
                self.ask(user, ts, q)
            return reply
        if name in _RECORDING_COMMANDS:
            if not admin:
                return self.admin.refuse(user, "change the running recording (the stream, the teams, the uploads)")
            if cmd.error:
                return cmd.error
            if rec and box is not None:
                self.forward(poller, m, cmd, box)
                return None
            if rec:
                return (f"The recording running now started before an update, so it takes this command only from "
                        f"{self.contact_name}, in their own chat with the StreamSplitter app. From its next start, "
                        f"every admin can use it.")
            return self.admin.offline(cmd)      # nothing records: saved for the next start
        return commands.unknown_reply(str(m.get("text") or ""), self.help_context(user, poller))

    def _mailbox(self, rec: list[dict]) -> Path | None:
        """The running recording's mailbox (Runner.mailbox), None for none or one of an older version."""
        if not rec:
            return None
        fn = getattr(self.runner, "mailbox", None)
        try:
            return fn(rec[0]["tag"]) if fn is not None else None
        except Exception as e:  # noqa: BLE001 - as for a recording of an older version
            self.log(f"could not look for the recording's mailbox ({type(e).__name__})")
            return None

    def _still_waiting(self, user: str, ts: str, reply: str | None) -> str | None:
        """`reply`, and a word on `user`'s split (or set event) question while it waits for its yes; once it has
        expired, the next reply says it never started (once). User, 2026-11-07: split 2026mttd answered with pause
        youtube, resume drive and teams instead of yes was never started, nothing said so, and list later looked as
        if the event had been lost."""
        p = self.state["pending"].get(user)
        if not reply or not p or p.get("kind") not in ("split", "event") or p.get("ts") == ts or p.get("told"):
            return reply
        ev = p.get("event") or {}
        name = f"{ev.get('name') or ev.get('key')} ({ev.get('key')})"
        start = p["kind"] == "split"
        if _ts(ts) - _ts(p.get("ts")) <= CONFIRM_S:
            return reply + (f"\n({name} has not started yet: reply yes to start it, or no.)" if start else
                            f"\n({name} is not saved as the event yet: reply yes to save it, or no.)")
        p["told"] = True
        self._save()
        return reply + f"\n({self._expired_text(p)})"

    def _expired_text(self, p: dict) -> str:
        """What happened to a split (or set event) question that was not answered in time."""
        ev = p.get("event") or {}
        name = f"{ev.get('name') or ev.get('key')} ({ev.get('key')})"
        if p.get("kind") == "split":
            return (f"split {ev.get('key')} at {_clock(p.get('asked'))} was not answered yes within {CONFIRM_MIN} "
                    f"minutes, so {name} was not started. Reply split {ev.get('key')} yes to start it now.")
        return (f"set event {ev.get('key')} at {_clock(p.get('asked'))} was not answered yes within {CONFIRM_MIN} "
                f"minutes, so {name} was not saved. Reply set event {ev.get('key')} yes to save it now.")

    def people(self) -> str:
        """"Ian H's DM" / "the DMs of Ian H and Sam": where it listens."""
        names = [self.names.get(u, u) for u in self.users]
        return f"{names[0]}'s DM" if len(names) == 1 else f"the DMs of {_and(names)}"

    def ask(self, user: str, ts: str, q: dict) -> None:
        """Keep the question `q` (slackadmin.Admin's) for `user`'s answer: one per person, a new one replaces it."""
        self.state["pending"][user] = {"kind": "admin", "ts": ts, "asked": self.clock(), "q": q}
        self._save()

    def number(self, user: str, ts: str, word: str) -> str | None:
        """A number: the answer to "Several people match. Reply with the number ..." (slackadmin)."""
        p = self.state["pending"].get(user)
        if p is None or p.get("kind") != "admin" or (p.get("q") or {}).get("action") != "pick":
            return commands.NOTHING_TO_PICK
        return self._admin_answer(user, ts, word)

    def _admin_answer(self, user: str, ts: str, word: str) -> str | None:
        p = self.state["pending"].get(user)
        if _ts(ts) <= _ts(p.get("ts")):
            return None                          # older than its question (a message handled again after a restart)
        del self.state["pending"][user]
        self._save()
        if _ts(ts) - _ts(p.get("ts")) > CONFIRM_S:
            again = (p.get("q") or {}).get("again")
            return (f"That question expired after {CONFIRM_MIN} minutes, so nothing was done."
                    + (f" Reply {again} to ask again." if again else ""))
        reply, q = self.admin.confirm(user, p["q"], word)
        if q is not None:
            self.ask(user, ts, q)
        return reply

    # split -----------------------------------------------------------------------------------------------------------
    def split(self, user: str, ts: str, arg: str, start: bool = True, confirm: bool = False,
              channel: str | None = None) -> str:
        """split <event> (start=True), or set event <code> (start=False: yes saves it without starting). It starts
        at once when nothing is in doubt (_why_ask); else it asks, and `confirm` (split <event> yes) is the yes."""
        word = "split" if start else "set event"
        arg = arg.strip()
        bare = not arg
        if bare:
            key = config.section("event").get("key") if start else None
            if not key:
                return SPLIT_HOW if start else SPLIT_HOW.replace("split", "set event")
            arg = key                            # the event set up: asked first (it was not named)
        kind, what = event_target(arg)
        rec = self.runner.recordings()
        if kind == "key" and rec:
            return self._busy(rec, what, word)
        try:
            found = self.find_event(kind, what)
        except LookupProblem as e:
            return str(e).replace("Reply split", f"Reply {word}").replace("(for example split", f"(for example {word}")
        if isinstance(found, list):
            year = found[0].get("year") or str(found[0]["key"])[:4]
            lines = [f"Several {year} events match. Reply {word} with the code of yours:"]
            lines += [f"  {word} {e['key']}   ({_event_words(e)})" for e in found[:LIST_MAX]]
            return "\n".join(lines)
        if rec:
            return self._busy(rec, found["key"], word)
        why = None if confirm else self._why_ask(found, kind, bare, start)
        plan = self.plan(found, asking=why is not None)
        p = {"kind": "split" if start else "event", "ts": ts, "asked": self.clock(), "event": plan["event"],
             "channel": channel, "plan": {k: v for k, v in plan.items() if k not in ("event", "text")}}
        if why is None:
            self.state["pending"].pop(user, None)
            return self.start_event(p, user, ts, start=start, summary=plan["text"])
        self.state["pending"][user] = p
        self._save()
        key = found["key"]
        if start:
            return (f"{plan['text']}\n*Reply yes to start recording, or no.* Nothing records until you do. (Asking "
                    f"because {why}. Next time, split {key} yes starts without asking.)")
        return (f"{plan['text']}\n*Reply yes to save it as the event* (nothing starts recording), or no. (Next time, "
                f"set event {key} yes saves it without asking.)")

    def _why_ask(self, ev: dict, kind: str, bare: bool, start: bool) -> str | None:
        """Why a split must ask before it starts (None: it need not): only when it might be the wrong event, so a
        stray message cannot start uploads to the wrong place (user, 2026-11-09: the yes was missed twice and nothing
        was recorded). set event always asks: it replaces the event set up."""
        if not start:
            return "set event replaces the event set up"
        if bare:
            return "no event was named, and this is the event set up on this computer"
        if kind != "key":
            return "it was found by its name"
        days = streams.event_days(ev)
        if not days:
            return "The Blue Alliance gives no dates for it"
        try:
            today = streams.event_today(ev, self.clock())
        except Exception:  # noqa: BLE001 - no time zone database: this computer's date
            today = _dt.datetime.fromtimestamp(self.clock()).date()
        if days[0] - _dt.timedelta(days=LEAD_DAYS) <= today <= days[1]:
            return None
        return f"it is not on today or tomorrow ({_event_words(ev).rsplit(' - ', 1)[-1]})"

    def _busy(self, rec: list[dict], key: str, word: str = "split") -> str:
        same = next((r for r in rec if key in (r["tag"], self._key_of(r["tag"]))), None)
        if same is not None and word == "split":
            if same.get("stopping"):
                return (f"{self._name(same['tag'])} is still stopping on {self.host} (it finishes an upload first). "
                        f"Try again in a minute.")
            return f"{self._name(same['tag'])} is already recording on {self.host}. Reply status to see how it is going."
        names = _and([self._name(r["tag"]) for r in rec])
        return (f"{self.host} is recording {names}. One recording per computer: reply stop first, then {word} "
                f"again.")

    def find_event(self, kind: str, what: str):
        """TBA's event record for a code, or for a name with one hit; a list for several hits; LookupProblem with
        the reply otherwise."""
        if kind == "name":
            m = re.match(r"^(\d{4})\s+(.+)$", what)
            year = int(m.group(1)) if m else _dt.datetime.fromtimestamp(self.clock()).year
            words = [w for w in re.split(r"[\s,]+", (m.group(2) if m else what).lower()) if w]
            try:
                events = self.tba_get(f"/events/{year}") or []
            except Exception as e:  # noqa: BLE001
                raise LookupProblem(self._tba_down(e)) from None

            def text(e):
                return " ".join(str(e.get(k) or "") for k in ("name", "short_name", "city", "key", "event_code"))\
                    .lower()
            hits = [e for e in events if isinstance(e, dict) and e.get("key") and all(w in text(e) for w in words)]
            today = _dt.datetime.fromtimestamp(self.clock()).date().isoformat()
            hits.sort(key=lambda e: (str(e.get("end_date") or "") < today, str(e.get("start_date") or "")))
            if not hits:
                raise LookupProblem(f"No {year} event matches that. Reply split with its code from thebluealliance.com "
                                    f"(for example split {year}mttd), part of its name, or its city.")
            if len(hits) > 1:
                return hits
            what = hits[0]["key"]
        try:
            ev = self.tba_get(f"/event/{what}")
        except Exception as e:  # noqa: BLE001
            if "HTTP 404" in str(e):
                raise LookupProblem(f"The Blue Alliance has no event {what}. Check its code on thebluealliance.com, "
                                    f"or reply split with part of its name.") from None
            raise LookupProblem(self._tba_down(e)) from None
        if not isinstance(ev, dict) or not ev.get("key"):
            raise LookupProblem(f"The Blue Alliance has no event {what}.")
        return ev

    def _tba_down(self, e: Exception) -> str:
        if not config.tba_key():
            return (f"There is no Blue Alliance key on {self.host}, so events cannot be looked up: run setup --section "
                    f"tba on it.")
        return f"Could not reach The Blue Alliance ({redact(e)}). Try again in a minute."

    def plan(self, ev: dict, asking: bool = True) -> dict:
        """What a start of `ev` does: the summary text, the stream link, the YouTube plan (`asking`: a yes starts
        it; else it starts now)."""
        from . import setup_wizard
        key = ev["key"]
        cur = config.section("event")
        keep_link = cur.get("key") == key and bool(cur.get("stream_url"))
        lines = [setup_wizard._event_line(ev)]
        days = streams.event_days(ev)
        today = streams.event_today(ev, self.clock())
        if keep_link:
            stream = cur["stream_url"]
            lines.append(f"Stream: {stream} (the link set before).")
        else:
            try:
                p = streams.pick(ev, today if days and days[0] <= today <= days[1] else None, self.channel_live)
            except Exception:  # noqa: BLE001 - no suggestion: the recorder asks TBA again
                p = None
            stream = p["url"] if p else None
            lines.append(f"Stream: {stream} (from The Blue Alliance)." if stream else
                         "Stream: The Blue Alliance lists none yet. The recorder keeps checking; once it runs, reply "
                         "stream <link> to set one.")
        try:
            also = streams.alternates(ev, today, stream) if stream else []
        except Exception:  # noqa: BLE001 - only the summary's line; the recording asks again at each start
            also = []
        if also:                                 # what live passes the recorder as --also (user, 2026-11-07)
            lines.append(f"If that is not live, it tries {_and(also)}.")
        sl = config.section("slack")
        if sl.get("enabled") and sl.get("channel_name"):
            teams = ", ".join(str(t) for t in sl.get("teams") or [])
            lines.append(f"Slack: your team's matches ({teams}) go to #{sl['channel_name']}." if teams else
                         f"Slack: no team is set, so no match goes to #{sl['channel_name']}.")
        youtube, line = self._youtube_plan(ev, asking)
        lines.append(line)
        dr = config.section("drive")
        if dr.get("enabled"):
            lines.append(f"Google Drive: every match, to the folder \"{_drive_folder(dr, ev)}\"."
                         + (PAUSED.format(dest="drive") if dr.get("paused") else ""))
        if config.section("wiki").get("enabled"):
            lines.append("Team wiki: every match.")
        stop_at = stop_time(ev)
        if stop_at is not None and stop_at <= self.clock():
            stop_at = None
            lines.append("That event is over: it records until you reply stop.")
        small = {k: ev.get(k) for k in ("key", "name", "short_name", "year", "start_date", "end_date", "event_type",
                                        "city", "state_prov", "timezone") if ev.get(k) is not None}
        return {"event": small, "stream": stream, "keep_link": keep_link, "youtube": youtube, "stop_at": stop_at,
                "text": "\n".join(lines)}

    def _youtube_plan(self, ev: dict, asking: bool = True) -> tuple[dict, str]:
        """{"mode": "off" | "official" | "nologin" | "on", ...} and its summary line. YouTube counts as on when the
        setup turned it on, or when the listener turned it off for an earlier event that did not allow it."""
        from . import setup_wizard
        from .publish import templates
        yt, lc = config.section("youtube"), config.section("listen")
        if not (yt.get("enabled") or lc.get("youtube_off_for")):
            return {"mode": "off"}, "YouTube: off."
        if setup_wizard.is_official_event(ev):
            return {"mode": "official"}, "YouTube: off (official FIRST event)."
        if not (config.get_secret("youtube_token") and config.get_secret("youtube_client")):
            return {"mode": "nologin"}, ("YouTube: off for this event: this computer is not logged in to YouTube (run "
                                         "setup --section youtube on it).")
        same = yt.get("playlist_event") == ev["key"] and yt.get("playlist_id")
        title = (yt.get("playlist_title") if same and yt.get("playlist_title") else
                 templates.render_playlist(templates.PLAYLIST_FORMAT, ev["key"], ev.get("name"), ev.get("year")))
        privacy = yt.get("privacy") or "unlisted"
        what = "every match" if yt.get("route", "all") == "all" or not config.section("slack").get("enabled") \
            else "the other teams' matches"
        made = "" if same or not asking else " (made when you reply yes)"
        return ({"mode": "on", "title": title, "privacy": privacy, "existing": yt.get("playlist_id") if same else None},
                f"YouTube: {what}, {privacy}, to the playlist \"{title}\"{made}."
                + (PAUSED.format(dest="youtube") if yt.get("paused") else ""))

    # yes / no --------------------------------------------------------------------------------------------------------
    def yes(self, user: str, ts: str, poller) -> str | None:
        p = self.state["pending"].get(user)
        if p is None:
            return NOTHING_TO_CONFIRM
        if p.get("kind") == "admin":
            return self._admin_answer(user, ts, "yes")
        if _ts(ts) <= _ts(p.get("ts")):
            return None                          # older than its question (a message handled again after a restart)
        del self.state["pending"][user]
        self._save()
        if _ts(ts) - _ts(p.get("ts")) > CONFIRM_S:
            key = p.get("event", {}).get("key")
            again = {"split": f"split {key} yes to start it now", "event": f"set event {key} yes to save it now"}.get(
                p["kind"], "stop to ask again")
            return f"That question expired after {CONFIRM_MIN} minutes, so nothing was done. Reply {again}."
        if p["kind"] in ("split", "event"):
            return self.start_event(p, user, ts, start=p["kind"] == "split")
        return self.stop_now(p, poller)

    def no(self, user: str, ts: str) -> str | None:
        p = self.state["pending"].get(user)
        if p is None:
            return NOTHING_TO_CONFIRM
        if p.get("kind") == "admin":
            return self._admin_answer(user, ts, "no")
        if _ts(ts) <= _ts(p.get("ts")):
            return None
        del self.state["pending"][user]
        self._save()
        return {"split": "OK, nothing started.", "event": "OK, the event stays as it was."}.get(
            p["kind"], "OK, it keeps recording.")

    def start_event(self, p: dict, user: str, ts: str, start: bool = True, summary: str | None = None) -> str:
        """The yes to a split, or a split that need not ask (`summary`: its plan's text, said first): save the event,
        the stream and the playlist, then start the recording. start=False (set event): save them only."""
        from . import setup_wizard
        ev, plan = p["event"], p["plan"]
        rec = self.runner.recordings()
        if rec:
            return self._busy(rec, ev["key"], "split" if start else "set event")
        setup_wizard.save_event(ev)
        if not plan.get("keep_link"):
            config.save_sections({"event": {"stream_url": plan.get("stream")}})
        note = self._youtube_apply(ev, plan.get("youtube") or {"mode": "off"})
        if not start:
            self.log(f"saved {ev['key']} as the event, asked by {self.names.get(user, user)}")
            return (f"Saved {ev.get('name') or ev['key']} ({ev['key']}) as the event on {self.host}; nothing records "
                    f"yet. Reply split {ev['key']} to start it.{note}")
        cur = config.section("event")
        tag = cur.get("tag") or ev["key"]
        self._hand_over(tag, ts if user == self.contact else None)
        try:
            pid = self.runner.start()
        except Exception as e:  # noqa: BLE001 - say what is wrong
            self.log(f"could not start {ev['key']}: {type(e).__name__}: {redact(e)}")
            return f"Could not start the recording: {redact(e)}.{note}"
        now = self.clock()
        self.state.update(active={"key": ev["key"], "tag": tag, "name": ev.get("name") or ev["key"],
                                  "end_date": ev.get("end_date") or ev.get("start_date"), "since": now, "by": user,
                                  "stop_at": plan.get("stop_at")},
                          restarts=[], gave_up=False, next_check=now + START_GRACE_S)
        self._save()
        self.log(f"started recording {ev['key']} (process {pid}), asked by {self.names.get(user, user)}")
        name = f"{ev.get('name') or ev['key']} ({ev['key']})"
        who = ("its \"StreamSplitter started\" message comes here in about 30 s" if user == self.contact else
               f"{self.contact_name} gets its messages")
        if summary is None:                      # the yes to its summary
            return f"Starting {name} on {self.host}; {who}.{note}"
        lines = summary.splitlines()[1:]
        if note.strip().startswith("YouTube:"):  # the playlist could not be made: that replaces the YouTube line
            lines = [note.strip() if ln.startswith("YouTube:") else ln for ln in lines]
            note = ""
        return "\n".join([f"Starting {name} on {self.host} now; {who}.{note}"] + lines
                         + ["Wrong event or setting? Reply stop to stop it."])

    def _youtube_apply(self, ev: dict, y: dict) -> str:
        """Make the plan's YouTube true in config.toml; a sentence for the reply when something is worth saying."""
        key, mode = ev["key"], y.get("mode")
        yt = config.section("youtube")
        if mode == "off":
            return ""
        if mode in ("official", "nologin"):
            if yt.get("enabled"):
                # off for this event only: the next event that allows YouTube turns it back on (youtube_off_for)
                config.save_sections({"youtube": {"enabled": False}, "listen": {"youtube_off_for": key}})
            return ""
        if y.get("existing"):
            if not yt.get("enabled"):
                config.save_sections({"youtube": {"enabled": True}, "listen": {"youtube_off_for": None}})
            return ""
        title, privacy = y["title"], y.get("privacy") or "unlisted"
        try:
            pl = slackadmin.make_playlist(self.youtube(), title, ev, privacy)
        except Exception as e:  # noqa: BLE001 - record without YouTube rather than not at all
            self.log(f"YouTube playlist for {key}: {type(e).__name__}: {redact(e)}")
            config.save_sections({"youtube": {"enabled": False}, "listen": {"youtube_off_for": key}})
            return (f" YouTube: the playlist could not be made ({redact(e)}), so this event records without "
                    f"YouTube. To add it: run setup --section youtube on {self.host}, then reply stop and split {key} "
                    f"again.")
        config.save_sections({"youtube": {"enabled": True, "playlist_id": pl["id"],
                                          "playlist_title": pl.get("title") or title, "playlist_event": key},
                              "listen": {"youtube_off_for": None}})
        return f" YouTube playlist: {pl.get('url')}" if pl.get("url") else ""

    def _hand_over(self, tag: str, ts: str | None = None) -> None:
        """Move the recording's saved command position (publish/commands.py) past what the listener answered in the
        contact's DM, so the recording does not answer those messages again once it starts."""
        last = max(_ts(self.pollers[0].last_ts), _ts(ts))
        if not last:
            return
        path = config.data_root() / "outbox" / f"{tag}.commands.json"
        try:
            saved = _ts(json.loads(path.read_text(encoding="utf-8")).get("last_ts"))
        except (OSError, ValueError, AttributeError):
            saved = 0.0
        if saved >= last:
            return
        try:
            _write_json(path, {"last_ts": f"{last:.6f}"})
        except OSError as e:
            self.log(f"could not write {path} ({e})")

    # stop and list ---------------------------------------------------------------------------------------------------
    def ask_stop(self, user: str, ts: str, cmd=None, poller=None) -> str:
        """stop: asks first, so a stray message cannot end a recording; stop yes, or stop with the code of the event
        recording, stops at once."""
        rec = self.runner.recordings()
        if not rec:
            return "Nothing is recording." + (self._hint() or self._set_up(user))
        tags = [r["tag"] for r in rec]
        names = _and([self._name(t) for t in tags])
        p = {"kind": "stop", "ts": ts, "asked": self.clock(), "tags": tags, "names": names}
        code = getattr(cmd, "arg", None)
        if code and not any(code in (t, self._key_of(t)) for t in tags):
            return f"That is not what is recording: {self.host} is recording {names}. Reply stop to stop it."
        if code or getattr(cmd, "confirm", False):
            self.state["pending"].pop(user, None)
            return self.stop_now(p, poller)
        self.state["pending"][user] = p
        self._save()
        return f"Stop recording {names}? Reply yes (or no to keep it recording)."

    def stop_now(self, p: dict, poller) -> str:
        running = {r["tag"] for r in self.runner.recordings()}
        tags = [t for t in p["tags"] if t in running]
        if not tags:
            return f"{p['names']} is not recording any more."
        who = self.names.get(getattr(poller, "user", None)) or "someone"
        for t in tags:
            self.runner.request_stop(t, by=f"from Slack by {who}")
        a = self.state.get("active")
        if a and a.get("tag") in tags:
            self.state["active"] = None          # stopped from Slack: not restarted
        names = _and([self._name(t) for t in tags])
        self.state["stopping"].append({"tags": tags, "names": names, "channel": getattr(poller, "channel", None),
                                       "since": self.clock(), "auto": False})
        self._save()
        return (f"Stopping {names}: it finishes what it is doing first (an upload can take a minute). I'll say here "
                f"when it has stopped.")

    def list_text(self, user: str | None = None) -> str:
        rec = self.runner.recordings()
        if not rec:
            return "Nothing is recording." + (self._hint() or self._set_up(user))
        return "\n".join(self._recording_line(r) for r in rec)

    def _set_up(self, user: str | None) -> str:
        """The event config.toml keeps, for a list while nothing records and nothing is kept going: the event outlives
        a restart or an update, the recording only runs after split's yes (user, 2026-11-07). Not while `user`'s own
        split question waits (_still_waiting says that)."""
        ev = config.section("event")
        p = self.state["pending"].get(user) if user else None
        if not ev.get("key") or (p and p.get("kind") in ("split", "event") and not p.get("told")):
            return ""
        return (f" The event set up on {self.host} is {ev.get('name') or ev['key']} ({ev['key']}): reply split "
                f"{ev['key']} to start recording it.")

    def _recording_line(self, r: dict) -> str:
        name = self._name(r["tag"])
        if r.get("stopping"):
            return f"{name} is stopping on {self.host} (it finishes what it was doing first)."
        s = r.get("stream") or {}
        if s.get("live"):
            stream = f"live since {_clock(s.get('live_since'))}" + (f": {s['title']}" if s.get("title") else "")
        elif s.get("url"):
            stream = f"waiting for it to start (checking {s['url']})"
        else:
            stream = "no link yet"
        line = (f"Recording {name} on {self.host} since {_clock(r.get('started'))}. Stream: {stream}. Clips cut "
                f"since then: {int(r.get('clips_cut') or 0)}" + (f" (last: {r['last_clip']})." if r.get("last_clip")
                                                                 else "."))
        a = self.state.get("active")
        if a and a.get("tag") == r["tag"] and a.get("stop_at"):
            line += (f" Started from Slack: restarted if it stops, and stops by itself {_clock(a['stop_at'])}"
                     + (", while the listener's window stays open." if self.window else "."))
        return line

    def _hint(self) -> str:
        a = self.state.get("active")
        if not a:
            return ""
        name = f"{a.get('name') or a['key']} ({a['key']})"
        if self.state.get("gave_up"):
            return (f" {name} stopped {RESTART_MAX} times within an hour, so it is not restarted any more: reply split "
                    f"{a['key']} to try again.")
        st = self.runner.status(a["tag"])
        if self._on_purpose(a["tag"], st):
            return f" {name} was stopped on the computer; reply split {a['key']} to start it again."
        return f" {name} stopped; it is started again within a minute."

    def listener_line(self, user: str | None = None) -> str:
        """status's last line: this listener runs, and how quickly it reads `user`'s chat."""
        every = self.usual_interval(user) if user else commands.POLL_S
        return (f"Slack listener: running on {self.host} since {_clock(self.started)}. It reads your messages every "
                f"{every:.0f} s, and every {commands.FAST_S:.0f} s for {commands.HOT_S / 60:.0f} minutes after you "
                f"write.")

    def status_text(self, user: str | None = None) -> str:
        """status while nothing records."""
        try:
            free = f" {shutil.disk_usage(config.data_root()).free / 1e9:.0f} GB free for recordings."
        except OSError:
            free = ""
        extra = self._hint() or self._set_up(user) or " Reply split <event> to start one, e.g. split 2026mttd."
        return f"Recording: nothing is recording on {self.host}.{free}{extra}\n{self.listener_line(user)}"

    def status_reply(self, poller, m: dict, cmd, rec: list[dict], box: Path | None) -> str | None:
        """status: the recording's own (through its mailbox), else its line from its status file; and the listener."""
        user = poller.user
        if rec and box is not None:
            self.forward(poller, m, cmd, box, append=self.listener_line(user))
            return None
        if rec:
            return "\n".join(self._recording_line(r) for r in rec) + "\n" + self.listener_line(user)
        return self.status_text(user)

    def help_context(self, user: str, poller=None) -> commands.HelpContext:
        rec = self.runner.recordings()
        ev = config.section("event")
        r = rec[0] if rec else None
        return commands.HelpContext(
            host=self.host, admin=user in self.admins, listener=True,
            recording=self._name(r["tag"]) if r else None, since=r.get("started") if r else None,
            event=ev.get("key"), event_name=ev.get("name"), drive=bool(config.section("drive").get("enabled")),
            wiki=bool(config.section("wiki").get("enabled")), admins=tuple(self.names.get(a, a) for a in self.admins),
            every_s=self.usual_interval(user), react=bool(getattr(poller, "can_react", False)))

    def help_text(self, user: str, topic: str | None = None, poller=None) -> str:
        """help: a short first screen for `user` now (commands.first_help); help all: every command they may use
        (commands.help_text); help <topic>: one explained."""
        if not topic:
            return commands.first_help(self.help_context(user, poller))
        sl = config.section("slack")
        return commands.help_reply(topic, channel_name=sl.get("channel_name") if sl.get("enabled") else None,
                                   wiki=bool(config.section("wiki").get("enabled")),
                                   drive=bool(config.section("drive").get("enabled")), listen=True,
                                   owner=user in self.admins,
                                   owner_name=_and([self.names.get(a, a) for a in self.admins]))

    # commands passed to the running recording (publish/commands.Mailbox) ----------------------------------------------
    def forward(self, poller, m: dict, cmd, box: Path, append: str | None = None) -> None:
        """Pass `cmd` to the running recording's mailbox; its reply is posted by deliver(), or a word that it did not
        come by expire_forwards()."""
        ts, user = str(m.get("ts") or ""), poller.user
        rid = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{ts}-{user}")
        thread = m.get("thread_ts") if m.get("thread_ts") and m.get("thread_ts") != m.get("ts") else None
        req = {"id": rid, "user": user, "name": self.names.get(user, user), "text": cmd.slack, "channel": poller.channel,
               "thread": thread, "asked": self.clock(), "append": append}
        try:
            commands._write_json(Path(box) / f"{rid}.json", req)
        except OSError as e:
            self.log(f"could not pass a command to the recording ({e})")
            commands.post_reply(self.client, poller.channel, f"Could not pass that to the recording ({e}). Try again "
                                f"in a minute.", thread, log=self.log)
            return
        self.state["forwards"][rid] = {"box": str(box), "user": user, "channel": poller.channel, "thread": thread,
                                       "asked": self.clock()}
        self._save()

    def deliver(self) -> int:
        """Post the recordings' replies waiting in the replies folder (each once); how many."""
        folder = self.folder / REPLIES
        try:
            ready = sorted(folder.glob("*.json"))
        except OSError:
            return 0
        n = 0
        for path in ready:
            try:
                rep = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            try:
                path.unlink()
            except OSError:
                continue                          # another try next time
            with self._lock:
                f = self.state["forwards"].pop(str(rep.get("id")), None)
                if f is not None:
                    self._save()
            channel = (f or {}).get("channel") or rep.get("channel")
            if channel:
                commands.post_reply(self.client, channel, str(rep.get("text") or ""),
                                    (f or {}).get("thread") or rep.get("thread"), log=self.log)
                n += 1
        return n

    def expire_forwards(self, now: float | None = None) -> None:
        """A command the recording did not take within FORWARD_WAIT_S (it may be stuck, or stopping) is taken back,
        and its person told nothing was done; one taken but not answered within FORWARD_GIVEUP_S is given up."""
        now = self.clock() if now is None else now
        for rid, f in list(self.state["forwards"].items()):
            age = now - float(f.get("asked") or 0.0)
            box = Path(f.get("box") or ".")
            text = None
            if age >= FORWARD_WAIT_S and commands.claim(box / f"{rid}.json", ".expired") is not None:
                (box / f"{rid}.expired").unlink(missing_ok=True)
                text = (f"The recording on {self.host} did not take that within {FORWARD_WAIT_S:.0f} s, so nothing was "
                        f"done (it may be busy or stopping). Reply status to see how it is doing, then try again.")
            elif age >= FORWARD_GIVEUP_S and commands.claim(box / f"{rid}.taken", ".gone") is not None:
                (box / f"{rid}.gone").unlink(missing_ok=True)
                text = (f"The recording on {self.host} did not answer that within {FORWARD_GIVEUP_S / 60:.0f} minutes; "
                        f"it may still happen. Reply status to check.")
            elif age < FORWARD_GIVEUP_S + 60.0:
                continue
            del self.state["forwards"][rid]
            self._save()
            if text and f.get("channel"):
                commands.post_reply(self.client, f["channel"], text, f.get("thread"), log=self.log)

    # keeping it going ------------------------------------------------------------------------------------------------
    def tick(self) -> None:
        """Every TICK_S: people changed in config.toml, stops under way, questions too old to keep, the auto-stop and
        the restart."""
        with self._lock:
            now = self.clock()
            self.alive = now
            try:
                self.reload_people()
            except Exception as e:  # noqa: BLE001 - an unreadable config.toml: keep the people as they are
                self.log(f"could not read the people from config.toml ({type(e).__name__}: {redact(e)})")
            self._check_stopping(now)
            self._remind(now)
            self.expire_forwards(now)
            old = [u for u, p in self.state["pending"].items() if now - float(p.get("asked") or 0) > PENDING_KEEP_S]
            for u in old:
                del self.state["pending"][u]
            if old:
                self._save()
            a = self.state.get("active")
            if a and a.get("stop_at") and now >= float(a["stop_at"]):
                self._auto_stop(a, now)
            elif a and now >= self.state.get("next_check", 0.0):
                self._keep_going(a, now)
            self.write_status()

    def _remind(self, now: float) -> None:
        """A split question still waiting after REMIND_S: one reminder in the asker's chat; once it expires, one line
        that nothing started (user, 2026-11-09: a split asked twice was never answered yes, and nothing said so)."""
        changed = False
        for user, p in self.state["pending"].items():
            if p.get("kind") not in ("split", "event") or p.get("told") or not p.get("channel"):
                continue
            ev = p.get("event") or {}
            name = f"{ev.get('name') or ev.get('key')} ({ev.get('key')})"
            age = now - float(p.get("asked") or now)
            if age > CONFIRM_S:
                p["told"] = changed = True
                text = self._expired_text(p)
            elif age >= REMIND_S and not p.get("reminded"):
                p["reminded"] = changed = True
                ends = _clock(float(p.get("asked") or now) + CONFIRM_S)
                text = (f"Still waiting for your yes: reply yes to start {name}, or no. Nothing records until you do; "
                        f"the question ends at {ends}." if p["kind"] == "split" else
                        f"Still waiting: reply yes to save {name} as the event, or no (until {ends}).")
            else:
                continue
            try:
                self.client.post_message(p["channel"], commands._esc(text))
            except Exception as e:  # noqa: BLE001 - only a reminder
                self.log(f"could not remind {self.names.get(user, user)} ({type(e).__name__})")
        if changed:
            self._save()

    def _on_purpose(self, tag: str, st: dict) -> bool:
        """Stopped on purpose: request_stop's stop file ended it, or waits for it (module docstring)."""
        if str(st.get("stopped_reason") or "").startswith("stop requested"):
            return True
        try:
            return bool(self.runner.stop_waiting(tag))
        except Exception:  # noqa: BLE001
            return False

    def _keep_going(self, a: dict, now: float) -> None:
        st = self.runner.status(a["tag"])
        if st.get("running") or self._on_purpose(a["tag"], st) or self.state.get("gave_up"):
            return
        name = f"{a.get('name') or a['key']} ({a['key']})"
        reason = st.get("reason") or st.get("stopped_reason") or "no status"
        ev = config.section("event")
        if ev.get("key") != a["key"]:
            self.state["active"] = None
            self._save()
            self.notifier.info(f"The recording of {name} stopped ({reason}). The event set up on {self.host} is now "
                               f"{ev.get('key') or 'none'}, so it was not restarted. Reply split {a['key']} to record "
                               f"it again.")
            return
        if self.runner.recordings():
            return                               # one recording per computer: another one runs
        recent = [t for t in self.state["restarts"] if now - float(t) < RESTART_WINDOW_S]
        if len(recent) >= RESTART_MAX:
            self.state["gave_up"] = True
            self._save()
            self.notifier.warn(f"The recording of {name} on {self.host} stopped again ({reason}). It was restarted "
                               f"{RESTART_MAX} times in the last hour, so the listener stops trying. Its log on the "
                               f"computer says why ({config.data_root() / 'logs' / ('live_' + a['tag'] + '.log')}). "
                               f"Reply split {a['key']} to start it again.")
            return
        self._hand_over(a["tag"])
        self.state["restarts"], self.state["next_check"] = recent + [now], now + START_GRACE_S
        self._save()
        try:
            self.runner.start()
        except Exception as e:  # noqa: BLE001 - counted as a try; said once per half hour
            self.notifier.warn(f"Could not restart the recording of {name}: {redact(e)}. Trying again in a minute.",
                               key="listener_restart")
            return
        self.log(f"restarted {a['key']} ({reason})")
        self.notifier.info(f"Restarted the recording of {name} on {self.host}: it had stopped ({reason}).")

    def _auto_stop(self, a: dict, now: float) -> None:
        self.state["active"] = None
        st = self.runner.status(a["tag"])
        if st.get("running") and not st.get("stopping"):
            self.runner.request_stop(a["tag"], by="the event ended")
            self.state["stopping"].append({"tags": [a["tag"]], "names": f"{a.get('name') or a['key']} ({a['key']})",
                                           "channel": None, "since": now, "auto": True, "ended": a.get("end_date")})
            self.log(f"auto-stop of {a['key']}: the event ended {a.get('end_date')}")
        self._save()

    def _check_stopping(self, now: float) -> None:
        left, done = [], False
        for s in self.state["stopping"]:
            if not [t for t in s["tags"] if self.runner.status(t).get("running")]:
                line = self._stopped_line(s)
                self.log(line)
                self._say(s, line)
                done = True
            elif now - float(s["since"]) > STOP_WAIT_S:
                self._say(s, f"{s['names']} has not stopped after {STOP_WAIT_S / 60:.0f} minutes (an upload may be "
                             f"finishing). It stops by itself once that is done; reply status to check.")
                done = True
            else:
                left.append(s)
        if done:
            self.state["stopping"] = left
            self._save()

    def _stopped_line(self, s: dict) -> str:
        clips = sum(self.runner.clips(t) for t in s["tags"])
        waiting = 0
        for t in s["tags"]:
            for d in ((self.runner.status(t) or {}).get("outbox") or {}).values():
                if isinstance(d, dict):
                    waiting += int(d.get("queued") or 0) + int(d.get("running") or 0)
        keys = _and([self._key_of(t) for t in s["tags"]])
        ups = ("no uploads waiting" if not waiting else
               f"{waiting} upload{'s' if waiting != 1 else ''} still waiting (they go out the next time it records: "
               f"split {keys})")
        why = f": the event ended ({s['ended']})" if s.get("auto") and s.get("ended") else ""
        return f"Stopped recording {s['names']}{why}. {clips} clip{'s' if clips != 1 else ''} made; {ups}."

    def _say(self, s: dict, text: str) -> None:
        """A late reply: in the DM the stop was asked in, else (the auto-stop) to the contact."""
        if s.get("channel"):
            try:
                self.client.post_message(s["channel"], commands._esc(text))
                return
            except Exception as e:  # noqa: BLE001 - the notifier keeps it and retries
                self.log(f"could not reply ({type(e).__name__}); queued as a DM to the contact")
        self.notifier.info(text)

    # the loop -------------------------------------------------------------------------------------------------------
    def loop(self, stop, sleep=time.sleep) -> str:
        """Read the DMs when due (the polling budget, module docstring) and tick, until stop() returns why, or a
        Slack command turned the listener off (self.quit)."""
        due: dict[str, float] = {}
        next_tick = 0.0
        mono = self.monotonic
        reads: collections.deque = collections.deque()          # when each read of the last minute went out
        while True:
            why = stop() or self.quit
            if why:
                return why
            start = mono()
            while reads and start - reads[0] >= 60.0:
                reads.popleft()
            now = self.clock()
            hot, warm = set(self._hot(now)), set(self._active(now)) | {self.contact}
            order = sorted(enumerate(list(self.pollers)),
                           key=lambda ip: (0 if ip[1].user in hot else 1 if ip[1].user in warm else 2, ip[0]))
            for i, p in order:
                if p.user not in due:
                    due[p.user] = start + i * STAGGER_S          # the first reads, spread out
                if mono() < due[p.user]:
                    continue
                if len(reads) >= READS_PER_MIN:
                    break                                         # the most urgent went first; the rest wait
                before = p.reads
                try:
                    wait = p.poll()
                except Exception as e:  # noqa: BLE001 - never stop listening
                    self.log(f"Slack listener: {type(e).__name__}: {redact(e)}")
                    wait = 60.0
                if p.reads > before:
                    reads.append(mono())
                wait = float(wait)
                if wait == commands.POLL_S:                       # read normally: by the budget
                    wait = self.interval(p.user)
                due[p.user] = mono() + max(1.0, wait)
                if getattr(p, "limited", 0.0):                    # Slack said slow down: every read waits
                    hold = mono() + float(p.limited)
                    p.limited = 0.0
                    for u in due:
                        due[u] = max(due[u], hold)
                    self.log(f"Slack asked the listener to slow down: reading again in {hold - mono():.0f} s")
            try:
                self.deliver()                                    # the recording's replies, within a second
            except Exception as e:  # noqa: BLE001 - never stop listening
                self.log(f"Slack listener: {type(e).__name__}: {redact(e)}")
            if mono() >= next_tick:
                try:
                    self.tick()
                except Exception as e:  # noqa: BLE001
                    self.log(f"Slack listener: {type(e).__name__}: {redact(e)}\n{traceback.format_exc()}")
                next_tick = mono() + TICK_S
            self.alive = self.clock()
            sleep(1.0)


def _and(items: list[str]) -> str:
    items = [str(x) for x in items]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


def _event_words(e: dict) -> str:
    """"MadTown Throwdown - Madera, CA - Nov 6-8, 2026" (setup_wizard._event_line without the code)."""
    from . import setup_wizard
    return setup_wizard._event_line(e).rsplit(" (", 1)[0]


def _drive_folder(dr: dict, ev: dict) -> str:
    """The Drive folder an event's matches go to: "<top folder>/<event folder>" (made with its first match)."""
    top = dr.get("folder_title") or "FRC match videos"
    if dr.get("event_folder_event") == ev["key"] and dr.get("event_folder_title"):
        return f"{top}/{dr['event_folder_title']}"
    try:
        from .publish import drive
        return f"{top}/{drive.event_title(ev['key'], ev.get('name'), ev.get('year'))}"
    except Exception:  # noqa: BLE001 - the name is only shown
        return top


# ---------------------------------------------------------------------------------------------------------------
# streamsplitter listen run | start | stop | status
# ---------------------------------------------------------------------------------------------------------------

def _say(s: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {s}", flush=True)


def _say_window(s: str) -> None:
    print(f"{time.strftime('%H:%M')} {s}", flush=True)


def run() -> int:
    """`listen` / `listen run`: listen here until a stop file, Ctrl+C, or its window closing (or Windows shutting
    down or logging off). The background mode's hidden process runs this too."""
    folder = listen_folder()
    folder.mkdir(parents=True, exist_ok=True)
    lock = _Lock(folder / LOCK_FILE)
    if not lock.acquire(wait_s=LOCK_WAIT_S):
        print("The Slack listener is already running on this computer (`streamsplitter listen status`).", flush=True)
        return 1
    try:
        return _run(folder)
    finally:
        lock.release()


def _run(folder: Path) -> int:
    (folder / STOP_FILE).unlink(missing_ok=True)          # a stop asked of an earlier run
    cfg, nt = config.section("listen"), config.section("notify")
    problem = setup_problem(cfg, nt)
    if problem:
        print(problem, flush=True)
        return 2
    window = mode_of(cfg) == WINDOW
    say = _say_window if window else _say
    from .publish import slack
    client = slack.SlackClient(config.get_secret("slack_bot_token"))
    heartbeat_s, alarm_s, note = alarm_timing(nt.get("heartbeat_s"), nt.get("alarm_after_s"))
    if note:
        say(note)
    notifier = ListenerNotifier(client, nt["slack_user"], alarm_after_s=alarm_s, state_path=folder / ALARM_FILE,
                                log=say, window=window)
    listener = Listener(client, nt["slack_user"], allowed_users(cfg, nt), contact_name=nt.get("slack_user_name"),
                        user_names=cfg.get("user_names"), notifier=notifier, folder=folder, log=say, window=window)
    return serve(listener, notifier, folder, window=window, alarm=bool(cfg.get("alarm", not window)),
                 heartbeat_s=heartbeat_s, awake=bool(cfg.get("keep_awake", True)))


def serve(listener: Listener, notifier, folder: Path, *, window: bool = False, alarm: bool = True,
          heartbeat_s: float = 60.0, awake: bool = True, sleep=time.sleep, signals: bool = True) -> int:
    """Listen until stopped: the loop, the heartbeat (`alarm`), keeping the computer awake (`awake`), and a clean
    exit that DMs why (notifier.stopped). Stops on the stop file, Ctrl+C, or (Windows) the window closing or Windows
    logging off or shutting down: a console handler holds the closing process up to CLOSE_WAIT_S for that DM.
    signals=False (tests) leaves the process's signal and console handlers alone."""
    stopping: dict = {"why": None, "closing": False}
    done = threading.Event()
    ctrl_c = "Ctrl+C in its window" if window else "stopped from the console (Ctrl+C)"

    def on_signal(*_):
        stopping["why"] = stopping["why"] or ctrl_c
    old: dict = {}
    console = None
    if signals:
        for name in ("SIGINT", "SIGBREAK"):
            if hasattr(signal, name):
                old[name] = signal.signal(getattr(signal, name), on_signal)
        console = _register_console_handler(console_close_handler(stopping, done, window))

    def stop() -> str | None:
        if stopping["why"]:
            return stopping["why"]
        if (folder / STOP_FILE).is_file():
            (folder / STOP_FILE).unlink(missing_ok=True)
            return "stop requested (the setup, or streamsplitter listen stop)"
        return None
    keep = keep_awake() if awake else None
    heart = _Heartbeat(notifier, listener, heartbeat_s) if alarm else None
    a = listener.state.get("active")
    try:
        before = json.loads((folder / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        before = {}
    if window:
        listener.log(f"Listening for `split <event>` in {listener.people()}. Close this window to stop listening (a "
                     f"recording keeps going).")
        notifier.info(f"Listening for Slack commands while its window is open on {listener.host}: reply split "
                      f"<event> to start a recording (e.g. split 2026mttd), or help. Replies come within about "
                      f"{commands.POLL_S:.0f} s.")
    else:
        listener.log(f"listening in the background on {listener.host} for Slack commands in {listener.people()}")
        # the run before ended through serve's exit, so its DM said it stopped (a killed process or a power cut leaves
        # running true and no DM: its alarm, once it goes off, is followed by the notifier's own "running again")
        if isinstance(before, dict) and before.get("running") is False and before.get("stopped_reason") and \
                callable(getattr(notifier, "restarted", None)):
            notifier.restarted()
    if a:
        listener.log(f"keeping {a.get('name') or a['key']} ({a['key']}) going")
    reason = "stopped"
    try:
        listener.write_status()
        if heart is not None:
            heart.start()
        reason = listener.loop(stop, sleep=sleep)
    except BaseException as e:
        reason = ctrl_c if isinstance(e, KeyboardInterrupt) else f"crashed: {type(e).__name__}: {redact(e)}"
        raise
    finally:
        if heart is not None:
            heart.stop()
        listener.write_status(final=True, reason=reason)
        notifier.stopped(reason, timeout=CLOSE_WAIT_S - 1.0 if stopping["closing"] else 15.0)
        if keep is not None:
            keep.terminate()
        if window:
            listener.log("No longer listening for Slack commands. A recording that runs keeps going.")
        if not stopping["closing"]:                  # closing: the process ends as the handler returns
            for name, handler in old.items():
                signal.signal(getattr(signal, name), handler)
            _unregister_console_handler(console)
        done.set()
    return 0


def console_close_handler(stopping: dict, done: threading.Event, window: bool):
    """The Python side of a Windows console control handler. The window closing (CTRL_CLOSE_EVENT), or Windows
    logging off or shutting down: ask the loop to stop and wait (up to CLOSE_WAIT_S; Windows ends a closing console's
    processes about 5 s after asking) for the clean exit and its DM. Ctrl+C and Ctrl+Break: False, so Python's own
    handler (on_signal in serve) takes them."""
    def handler(event: int) -> bool:
        if event not in (2, 5, 6):                  # CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT
            return False
        stopping["closing"] = True
        stopping["why"] = stopping["why"] or ("its window was closed" if window and event == 2
                                              else "Windows is logging off or shutting down")
        done.wait(CLOSE_WAIT_S)
        return True
    return handler


def _register_console_handler(handler):
    """SetConsoleCtrlHandler(handler) on Windows (else nothing); the ctypes callback, kept while registered."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    fn = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)(lambda event: bool(handler(event)))
    return fn if ctypes.windll.kernel32.SetConsoleCtrlHandler(fn, True) else None


def _unregister_console_handler(fn) -> None:
    if fn is not None and os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetConsoleCtrlHandler(fn, False)


def start() -> int:
    """`listen start`: the listener in the background (the log-in shortcut runs this through pythonw.exe). Refused in
    the window mode, which never listens in the background."""
    problem = setup_problem()
    if problem:
        print(problem, flush=True)
        return 2
    if mode_of() == WINDOW:
        print(f"Start from Slack is set to listen only while its window is open, never in the background: run "
              f"`streamsplitter listen` in a window (or run.bat, {MENU_WINDOW}).", flush=True)
        return 2
    try:
        pid = start_detached()
    except AlreadyRunning as e:
        print(e, flush=True)
        return 0
    print(f"The Slack listener runs in the background (process {pid}). Its log: {log_path()}", flush=True)
    return 0


def stop(timeout_s: float = 30.0) -> int:
    """`listen stop`: ask it to stop and wait for it."""
    if not is_running():
        print("The Slack listener is not running.", flush=True)
        return 0
    request_stop()
    if wait_stopped(timeout_s):
        again = (" It starts again at the next log-in." if config.section("listen").get("enabled")
                 and mode_of() == BACKGROUND and login_start_installed() else "")
        print(f"The Slack listener has stopped. Recordings keep running.{again}", flush=True)
        return 0
    print(f"Asked the Slack listener to stop; it has not yet (its log: {log_path()}).", flush=True)
    return 1


def status() -> int:
    st = read_status()
    cfg = config.section("listen")
    how = ("off" if not cfg.get("enabled") else "in the background" if mode_of(cfg) == BACKGROUND
           else "only while its window is open")
    print(f"Start from Slack on this computer: {how}; the listener is "
          f"{'running' if st['running'] else 'not running'}" + (f" (since {_clock(st.get('started'))})."
                                                                  if st["running"] and st.get("started") else "."))
    a = st.get("active")
    if a:
        print(f"Keeping {a.get('name') or a['key']} ({a['key']}) going until {_clock(a.get('stop_at'))}"
              if a.get("stop_at") else f"Keeping {a.get('name') or a['key']} ({a['key']}) going until stop")
    if not st["running"] and st.get("stopped_reason"):
        print(f"It stopped: {st['stopped_reason']}")
    print(f"Log-in start: {'installed' if login_start_installed() else 'not installed'}. Log: {log_path()}")
    return 0


def main(action: str = "run") -> int:
    return {"run": run, "start": start, "stop": stop, "status": status}[action]()


# ---------------------------------------------------------------------------------------------------------------
# Starting at log-in
# ---------------------------------------------------------------------------------------------------------------

FOLDERID_STARTUP = "{B97D20BB-F46A-4C97-BA10-5E3608430854}"


def startup_folder() -> Path:
    """Windows: the user's Startup folder (shell:startup), which Windows runs at log-in; no admin rights needed."""
    from . import setup_wizard
    found = setup_wizard._windows_known_folder(FOLDERID_STARTUP)
    if found:
        return found
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Microsoft" / "Windows" / \
        "Start Menu" / "Programs" / "Startup"


def launch_agents_folder() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def autostart_folder() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart"


def _platform(platform: str | None) -> str:
    p = platform or sys.platform
    return "win32" if p.startswith("win") else "darwin" if p == "darwin" else "linux"


def login_start_path(platform: str | None = None) -> Path:
    p = _platform(platform)
    if p == "win32":
        return startup_folder() / SHORTCUT_NAME
    if p == "darwin":
        return launch_agents_folder() / f"{AGENT_LABEL}.plist"
    return autostart_folder() / AUTOSTART_NAME


def login_start_installed(platform: str | None = None) -> bool:
    return login_start_path(platform).is_file()


def _venv_python(windowless: bool = False) -> str:
    exe = Path(sys.executable)
    if windowless and os.name == "nt":
        w = exe.with_name("pythonw.exe")
        if not w.is_file():
            raise RuntimeError(f"pythonw.exe is missing next to {exe}, so the listener cannot start without a "
                               f"window. Run run.bat again to repair the install.")
        return str(w)
    return str(exe)


def install_login_start(platform: str | None = None, *, python: str | None = None) -> Path:
    """Start the listener at each log-in of this user (module docstring). Returns the file written. `python`: the
    interpreter to run (default: this venv's; pythonw.exe on Windows)."""
    p = _platform(platform)
    path = login_start_path(p)
    path.parent.mkdir(parents=True, exist_ok=True)
    root = str(config.ROOT)
    if p == "win32":
        exe = python or _venv_python(windowless=True)
        _write_shortcut(path, exe, "-m streamsplitter.cli listen start", root,
                        "Starts the StreamSplitter Slack listener, hidden (Start from Slack)")
    elif p == "darwin":
        import plistlib
        log = str(log_path())
        data = {"Label": AGENT_LABEL, "ProgramArguments": [python or _venv_python(), "-m", "streamsplitter.cli",
                                                           "listen", "run"],
                "WorkingDirectory": root, "RunAtLoad": True, "ProcessType": "Background", "AbandonProcessGroup": True,
                "StandardOutPath": log, "StandardErrorPath": log}
        path.write_bytes(plistlib.dumps(data))
    else:
        exe = python or _venv_python()
        path.write_text("[Desktop Entry]\nType=Application\nName=StreamSplitter listener\n"
                        "Comment=Starts and stops StreamSplitter recordings from Slack\n"
                        f"Exec={_desktop_quote(exe)} -m streamsplitter.cli listen start\nPath={root}\n"
                        "Terminal=false\nNoDisplay=true\nX-GNOME-Autostart-enabled=true\n", encoding="utf-8")
    return path


def remove_login_start(platform: str | None = None) -> bool:
    """Remove the log-in start; False when there was none."""
    path = login_start_path(platform)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def _desktop_quote(arg: str) -> str:
    """One Exec argument of a desktop entry (freedesktop's quoting, then its own backslash escape)."""
    if not re.search(r"[\s\"'\\$`<>|;&*?#()]", arg):
        return arg
    inner = re.sub(r'(["`$\\])', r"\\\1", arg)
    return '"' + inner.replace("\\", "\\\\") + '"'


def _com_method(obj, index: int, restype, *argtypes):
    import ctypes
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    fn = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])
    return lambda *a: fn(obj, *a)


def _shell_link(action):
    """Run action(link, persist) on a new Windows shell link (IShellLinkW, IPersistFile), with COM set up."""
    import ctypes
    from ctypes import wintypes
    ole32 = ctypes.OleDLL("ole32")

    class GUID(ctypes.Structure):
        _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]

    def guid(s):
        g = GUID()
        ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g))
        return g
    clsid_link = guid("{00021401-0000-0000-C000-000000000046}")
    iid_link = guid("{000214F9-0000-0000-C000-000000000046}")
    iid_persist = guid("{0000010B-0000-0000-C000-000000000046}")
    try:
        ole32.CoInitializeEx(None, 2)            # apartment-threaded; S_FALSE when this thread had it already
        uninit = True
    except OSError:                              # RPC_E_CHANGED_MODE: set up another way, which works too
        uninit = False
    link, persist = ctypes.c_void_p(), ctypes.c_void_p()
    try:
        ole32.CoCreateInstance(ctypes.byref(clsid_link), None, 1, ctypes.byref(iid_link), ctypes.byref(link))
        _com_method(link, 0, ctypes.HRESULT, ctypes.c_void_p, ctypes.c_void_p)(ctypes.byref(iid_persist),
                                                                              ctypes.byref(persist))
        return action(link, persist)
    finally:
        for obj in (persist, link):
            if obj.value:
                _com_method(obj, 2, wintypes.ULONG)()
        if uninit:
            ole32.CoUninitialize()


def _write_shortcut(path: Path, target: str, args: str, workdir: str, description: str) -> None:
    """A Windows shortcut (.lnk) through the shell's own IShellLinkW, as an installer makes one."""
    import ctypes
    w = ctypes.c_wchar_p

    def make(link, persist):
        _com_method(link, 20, ctypes.HRESULT, w)(w(target))                  # SetPath
        _com_method(link, 11, ctypes.HRESULT, w)(w(args))                    # SetArguments
        _com_method(link, 9, ctypes.HRESULT, w)(w(workdir))                  # SetWorkingDirectory
        _com_method(link, 7, ctypes.HRESULT, w)(w(description))              # SetDescription
        _com_method(link, 15, ctypes.HRESULT, ctypes.c_int)(7)               # SetShowCmd: SW_SHOWMINNOACTIVE
        _com_method(persist, 6, ctypes.HRESULT, w, ctypes.c_int)(w(str(path)), 1)   # IPersistFile.Save
    _shell_link(make)


def read_shortcut(path: Path) -> dict:
    """{"target", "arguments", "workdir"} of a Windows shortcut (to check what install_login_start wrote)."""
    import ctypes

    def read(link, persist):
        _com_method(persist, 5, ctypes.HRESULT, ctypes.c_wchar_p, ctypes.c_int)(ctypes.c_wchar_p(str(path)), 0)
        out = {}
        for name, index, extra in (("target", 3, True), ("arguments", 10, False), ("workdir", 8, False)):
            buf = ctypes.create_unicode_buffer(1024)
            if extra:                            # GetPath(buffer, size, WIN32_FIND_DATAW*, SLGP_RAWPATH)
                _com_method(link, index, ctypes.HRESULT, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p,
                            ctypes.c_uint)(buf, 1024, None, 4)
            else:
                _com_method(link, index, ctypes.HRESULT, ctypes.c_wchar_p, ctypes.c_int)(buf, 1024)
            out[name] = buf.value
        return out
    return _shell_link(read)
