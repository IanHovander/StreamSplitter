"""Command-line entry point.

    streamsplitter                 settings summary (data folder, TBA key, the event set up)
    streamsplitter setup           guided setup (streamsplitter/setup_wizard.py); --section NAME for one step
    streamsplitter setup --section team    make a team settings file for a teammate, or load one
    streamsplitter start           start recording the set-up event in the background
    streamsplitter status          how the background recording is doing
    streamsplitter stop            ask the background recording to stop
    streamsplitter listen          listen for Slack commands (split <event>, stop, status; the admins' settings
                                   and people, slackadmin.py) in this window until it is closed; listen
                                   start|stop|status: the background listener (listener.py; turned on with setup
                                   --section listen)
    streamsplitter record URL ...  record a live stream into chunks (streamsplitter/record.py; --help for options)
    streamsplitter live URL ...    record it and cut every match as it ends (streamsplitter/live.py)
    streamsplitter vod URL --event KEY [--to drive]   split a saved stream (a Twitch VOD, a past YouTube stream)
                                   like a live one and publish its matches (streamsplitter/vod.py)
    streamsplitter tba [--event KEY] [--retitle]   the uploaded YouTube videos onto their match pages on The Blue
                                   Alliance: adds the links with an organizer's write key, else prints TBA's
                                   playlist page and what to paste there (streamsplitter/tbavideos.py)

start/status/stop use the event and stream link the setup saved. `live` imports the detection packages, which the
setup installs in the background, so only `live`, start, status and stop load them: `setup` works before they are
there. Every command first puts the installer's ffmpeg, ffprobe and deno on PATH (setup_wizard.prepare_environment).
The phase 0 tools live in phase0/. Docs: AGENTS.md is the map.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__, config

DETECT_MISSING = ("The detection packages are not installed yet. Run the setup (run.bat on Windows, ./run.sh on "
                  "Mac/Linux); for a developer setup: docs/code-map.md")


def _summary() -> int:
    print(f"streamsplitter {__version__}")
    print(f"data folder: {config.data_root()}")
    print(f"TBA key configured: {'yes' if config.tba_key() else 'no'}")
    ev = config.section("event")
    if ev.get("key"):
        print(f"event: {ev.get('name') or ev['key']} ({ev['key']}); stream: {ev.get('stream_url') or 'not set'}")
    else:
        print("not set up yet: run `streamsplitter setup` (or run.bat / run.sh)")
    return 0


def _live():
    """streamsplitter.live, or None (with the reason printed) when the detection packages are missing."""
    try:
        from . import live
    except ImportError as e:
        print(f"{DETECT_MISSING} ({e})")
        return None
    return live


def _tag() -> str | None:
    ev = config.section("event")
    return ev.get("tag") or ev.get("key")


def _start() -> int:
    from . import setup_wizard
    ev = config.section("event")
    if not ev.get("key"):
        print("No event is set up yet: run `streamsplitter setup` first.")
        return 1
    live = _live()
    if live is None:
        return 1
    st = live.read_status(_tag()) or {}
    if st.get("stopping"):
        print("StreamSplitter is still stopping (it finishes an upload in progress first). Run `streamsplitter start` "
              "again once `streamsplitter status` says it is not running.")
        return 1
    if st.get("running"):
        print("StreamSplitter is already running for this event (`streamsplitter status` shows how it is doing).")
        return 0
    try:
        pid = live.start_detached(setup_wizard.live_args())
    except RuntimeError as e:                     # live.AlreadyRunning: it started (or is stopping) meanwhile
        print(e)
        return 1
    print(f"started in the background (process {pid}): "
          f"{ev.get('stream_url') or 'the stream TBA lists (none set yet)'} for {ev['key']}")
    print("`streamsplitter status` shows how it is doing; `streamsplitter stop` stops it.")
    return 0


def _status() -> int:
    from . import setup_wizard
    tag = _tag()
    if not tag:
        print("Not set up yet: run `streamsplitter setup` first.")
        return 1
    live = _live()
    if live is None:
        return 1
    st = live.read_status(tag) or {}
    if not st:
        print(f"StreamSplitter has not run for {tag} yet.")
        return 0
    print(f"{config.section('event').get('name') or tag} ({tag}):")
    for line in setup_wizard.format_status(st):
        print(line)
    return 0


def _stop() -> int:
    tag = _tag()
    if not tag:
        print("Not set up yet: nothing to stop.")
        return 1
    live = _live()
    if live is None:
        return 1
    if not (live.read_status(tag) or {}).get("running"):
        print("StreamSplitter is not running.")
        return 0
    live.request_stop(tag)
    print("asked StreamSplitter to stop; it finishes what it is doing and stops within a minute")
    return 0


def main(argv: list[str] | None = None) -> int:
    """A mistake in config.toml is said in one line (which line, what to do), not as a traceback."""
    try:
        return _main(argv)
    except config.ConfigError as e:
        print(f"StreamSplitter cannot start: {e}", file=sys.stderr)
        return 2


def _main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    from . import setup_wizard
    setup_wizard.prepare_environment()
    if not argv:
        return _summary()
    ap = argparse.ArgumentParser(prog="streamsplitter")
    sub = ap.add_subparsers(dest="cmd", required=True)
    from . import record
    rp = sub.add_parser("record", help="record a live stream into ~20 s chunks + index.json",
                        description="Record a YouTube or Twitch live stream into ~20 s MPEG-TS chunks.")
    record.add_arguments(rp)
    lp = sub.add_parser("live", help="record a live stream and cut each match into its own clip as it ends",
                        description="Record an event stream and cut each match as it ends (any event software, any game).")
    vp = sub.add_parser("vod", help="split a saved stream (a Twitch VOD, a past YouTube stream) like a live one",
                        description="Copy a saved stream into chunks and cut and publish every match in it.")
    live = vod = None
    if argv[0] in ("live", "vod"):              # loads the detection packages: only when asked for
        live = _live()
        if live is None:
            return 1
        live.add_arguments(lp)
        from . import vod
        vod.add_arguments(vp)
    sp = sub.add_parser("setup", help="guided setup: event, stream, Slack, YouTube, alerts, test upload",
                        description="Guided setup. Without --section: every step (once set up: a menu).")
    sp.add_argument("--section", choices=setup_wizard.SECTIONS,
                    help="run one step of the setup on its own (team: make or load a team settings file)")
    sp.add_argument("--install-only", action="store_true",
                    help="only download what StreamSplitter needs (detection packages, ffmpeg, Deno), then exit")
    sub.add_parser("start", help="start recording the set-up event in the background")
    sub.add_parser("status", help="show how the background recording is doing")
    sub.add_parser("stop", help="ask the background recording to stop")
    lsp = sub.add_parser("listen", help="the Slack listener: start and stop recordings from Slack",
                         description="Start from Slack: answers split <event>, stop and list in Slack. Turn it on "
                                     "with: streamsplitter setup --section listen")
    lsp.add_argument("action", nargs="?", default="run", choices=("run", "start", "stop", "status"),
                     help="run (default): listen in this window until it is closed; start: in the background, "
                          "hidden; stop: stop the background one; status")
    from . import tbavideos
    tbavideos.add_arguments(sub.add_parser(
        "tba", help="put the uploaded YouTube videos on their match pages on The Blue Alliance",
        description="What The Blue Alliance has for the event, what this computer uploaded to YouTube, and the way "
                    "to add the rest: with an organizer's write key the links are added; without one it prints "
                    "TBA's playlist page and the playlist link to paste there."))
    a = ap.parse_args(argv)
    if a.cmd == "tba":
        return tbavideos.run_from_args(a)
    if a.cmd == "record":
        return record.run_from_args(a)
    if a.cmd == "live":
        return live.run_from_args(a)
    if a.cmd == "vod":
        return vod.run_from_args(a)
    if a.cmd == "setup":
        return setup_wizard.main(section=a.section, install_only=a.install_only)
    if a.cmd == "start":
        return _start()
    if a.cmd == "status":
        return _status()
    if a.cmd == "stop":
        return _stop()
    if a.cmd == "listen":
        from . import listener
        return listener.main(a.action)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
