# StreamSplitter: map for AI agents

Read this file, then open only the file your task needs (table at the end). Do not read the whole repo: the docs
are about 70k tokens, more than half of it history and research. Installing and guiding the setup needs this file
and [docs/setup.md](docs/setup.md): about 3k tokens.

StreamSplitter records an FRC event's live stream (YouTube or Twitch) on one ordinary computer and saves every
match as its own clip about a minute after the match ends. It can post a team's matches to Slack, upload every
match to YouTube and Google Drive, and put the YouTube links on The Blue Alliance (TBA). Windows is tested at
events, Linux in tests, macOS not at all.

## Install (asks nothing, changes nothing outside the folder)

```
git clone https://github.com/IanHovander/StreamSplitter C:\StreamSplitter
cd C:\StreamSplitter
.\run.bat --install-only
```

A bare `run.bat` is not found in PowerShell or in most agent shells: type `.\run.bat` (Git Bash: `./run.bat`).
The docs write `run.bat` for short. macOS/Linux: `sh run.sh --install-only`. No git: unzip
`https://github.com/IanHovander/StreamSplitter/archive/refs/heads/main.zip`. Use a short path that no cloud
service syncs (not Documents, Desktop, OneDrive, Dropbox). It takes a few minutes and about 1 GB: uv, Python
3.12, the packages, ffmpeg and Deno, all inside the folder. Check: `.venv\Scripts\streamsplitter`
(`.venv/bin/streamsplitter`) prints its version and "not set up yet".

## Set up: the person runs it, you guide

The setup is a question-and-answer wizard that opens browser logins and takes keys, so it needs the person's own
terminal: `run.bat` (`sh run.sh`). About 15 minutes; 30 for the first person on a team. Say what each step wants
from [docs/setup.md](docs/setup.md). One step alone: `run.bat --section NAME` with NAME one of event, tba, stream,
slack, youtube, drive, wiki, notify, listen, test, team, tbalinks.

## Rules

- Never ask for a token, key, Google client file or passphrase in chat, and never print `config.toml`,
  `secrets.json` or a team settings file. The wizard takes secrets itself and keeps them in the computer's
  password store.
- It uploads to the person's accounts: start a recording, a `vod` run, or `tba` changes only when they ask.
- Recording someone else's webcast needs the event organizer's OK. Say so once, before the first upload.
- One computer per event, or every match is posted twice.
- A recording or listener that is running keeps its old code until it is restarted; restart only when asked.
- Never commit `config.toml`, `secrets.json` or `logs/`.

## Commands

`run.bat X` is `.venv\Scripts\streamsplitter X` with the installer's tools on PATH.

| Command | Does |
|---|---|
| `run.bat` | the setup; once set up, a menu (start, status, stop, settings, test upload) |
| `run.bat start` / `status` / `stop` | the set-up event's recording, in the background |
| `run.bat listen start` / `stop` / `status` | the Slack listener: teammates start and stop recordings from Slack |
| `run.bat live URL --event KEY` | record and cut in this window |
| `run.bat vod URL --event KEY` | split a saved stream (Twitch VOD, past YouTube stream) |
| `run.bat tba` | what TBA has, what was uploaded, and how to add the rest |

## Read only what the task needs

| Task | File | Tokens |
|---|---|---|
| What a team needs first; what it cannot do | [docs/requirements-and-limits.md](docs/requirements-and-limits.md) | ~3.9k |
| Walk a person through the setup | [docs/setup.md](docs/setup.md) | ~1.5k |
| Make the Slack app | [docs/setup-slack.md](docs/setup-slack.md) | ~2.1k |
| Make the Google sign-in app, YouTube uploads | [docs/setup-youtube.md](docs/setup-youtube.md) | ~3.8k |
| Google Drive uploads | [docs/setup-drive.md](docs/setup-drive.md) | ~2.6k |
| MediaWiki uploads (optional, rarely wanted) | [docs/setup-wiki.md](docs/setup-wiki.md) | ~2.4k |
| Run it on event day; which stream it records | [docs/event-day.md](docs/event-day.md) | ~1.3k |
| Slack commands, roles, how fast it answers | [docs/start-from-slack.md](docs/start-from-slack.md) | ~4.5k |
| Split a saved stream after the event | [docs/saved-stream.md](docs/saved-stream.md) | ~0.3k |
| Put the videos on The Blue Alliance | [docs/tba.md](docs/tba.md) | ~0.6k |
| What it did at MadTown 2026 (numbers) | [docs/results.md](docs/results.md) | ~0.6k |
| Lessons: finding and recording the stream | [docs/lessons/streams.md](docs/lessons/streams.md) | ~0.7k |
| Lessons: finding, cutting and naming matches | [docs/lessons/detection.md](docs/lessons/detection.md) | ~0.8k |
| Lessons: YouTube, Google, Slack, TBA | [docs/lessons/publishing.md](docs/lessons/publishing.md) | ~0.9k |
| Lessons: the computer and the people | [docs/lessons/operations.md](docs/lessons/operations.md) | ~0.6k |
| Change the code, run the tests | [docs/code-map.md](docs/code-map.md) | ~1.4k |

History and research. Skip these unless you are changing that part of the code:
[docs/publish-design.md](docs/publish-design.md) (~5k),
[docs/research/youtube.md](docs/research/youtube.md) (~10k),
[docs/research/cloud-server.md](docs/research/cloud-server.md) (~12k),
[docs/history/PROPOSAL.md](docs/history/PROPOSAL.md) (~5k),
[phase0/PLAN.md](phase0/PLAN.md) (~6k), [phase0/notes_clock.md](phase0/notes_clock.md) (~4k).
[START-HERE.md](START-HERE.md) is the one-page quick start for a person (~1.4k), and [README.md](README.md) the
front page (~0.9k); neither adds anything to this file and docs/setup.md.
