# StreamSplitter

Records an FRC event's live stream (YouTube or Twitch) on one ordinary computer and saves every match as its own
video about a minute after the match ends. It can post your team's matches to Slack, upload every match to YouTube
and to a Google Drive folder, and help put the links on The Blue Alliance.

It reads the match timer, title and team numbers off the picture, so it works with FMS, Cheesy Arena and home-made
overlays, in any season. A webcast with no timer on screen cannot be split.

## What it did at MadTown Throwdown 2026

- 83 of 87 matches clipped and uploaded; 4 were missed.
- Each clip was on Google Drive 1 to 1.5 minutes after its match ended, and our own matches were in Slack.
- One home PC did it, started and stopped from Slack. Nobody ran it at the venue.
- What went wrong (a glitchy stream, YouTube's 100 uploads a day) and what changed: [docs/results.md](docs/results.md).

## Start with an AI assistant

The repo is laid out so an AI coding assistant (Claude Code, Codex, Cursor) can install it and guide you without
reading everything. Tell it:

> Clone https://github.com/IanHovander/StreamSplitter, read AGENTS.md, install it, and walk me through setting it
> up for event 2027xxxx.

Use your event's code from The Blue Alliance (`thebluealliance.com/event/2026mttd` is `2026mttd`).
[AGENTS.md](AGENTS.md) is a one-page map: what to run, the rules, and which small file answers which question. You
still type the logins and keys yourself; the setup never shows them to the assistant.

## Start by hand

1. Download this repository (the green **Code** button, *Download ZIP*) into a short path that no cloud service
   syncs, for example `C:\StreamSplitter`.
2. Windows: double-click `run.bat` (in PowerShell: `.\run.bat`). macOS/Linux: `sh run.sh`. It installs
   everything inside the folder (a few minutes, about 1 GB) and starts the setup.
3. Answer the questions: about 15 minutes, 30 for the first person on a team.

The one-page guide: [START-HERE.md](START-HERE.md).

## What you need

A computer that stays on and online all event day (at home is fine), about 100 GB of free disk, and 4-6 Mbit/s of
download. For Slack, a free app slot in the workspace; for YouTube, the channel owner's Google account; for Google
Drive, about 4 GB per event. The full list and the limits:
[docs/requirements-and-limits.md](docs/requirements-and-limits.md).

**Ask the event's organizers before you upload or link clips of their webcast.** The footage is theirs. Uploads are
unlisted by default, and The Blue Alliance links are off unless you turn them on.

## Docs

| For | Read |
|---|---|
| An AI assistant | [AGENTS.md](AGENTS.md) |
| The setup, step by step | [docs/setup.md](docs/setup.md) |
| Event day, and which stream it records | [docs/event-day.md](docs/event-day.md) |
| Starting and stopping from Slack | [docs/start-from-slack.md](docs/start-from-slack.md) |
| Splitting a saved stream afterwards | [docs/saved-stream.md](docs/saved-stream.md) |
| Videos onto The Blue Alliance | [docs/tba.md](docs/tba.md) |
| Results | [docs/results.md](docs/results.md) |
| Lessons, by topic | [streams](docs/lessons/streams.md), [detection](docs/lessons/detection.md), [publishing](docs/lessons/publishing.md), [operations](docs/lessons/operations.md) |
| Changing the code | [docs/code-map.md](docs/code-map.md) |

Windows is tested at events, Linux in the tests, macOS not at all. Built by FRC Team 5026 with AI coding
assistants. License: [MIT](LICENSE); the bundled sample video and field sounds are not covered, see
[NOTICE](NOTICE).
