# Code map: where things are, how to test

For changing the code. Every module opens with a docstring that says what it does and why; read that before the
code. Sizes are source size: open the big files by function (search), not whole.

## Flow

`record.py` writes the stream as ~20 s MPEG-TS chunks and an index. `pipeline.py` takes one chunk at a time:
`detect/overlay.py` reads the timer, title and team numbers, `timeline.py` turns countdowns into matches,
`namer.py` names them from the schedule, `cutter.py` writes the clip. `live.py` runs both and hands each clip to
`publish/publisher.py`, which queues jobs in `publish/outbox.py`; the outbox sends them to Slack, YouTube, Drive.

## Modules (`streamsplitter/`)

| File | kB | What |
|---|---|---|
| `cli.py` | 9 | the commands: setup, start, status, stop, listen, record, live, vod, tba |
| `config.py` | 7 | reads `config.toml`; data folder, work folder, keys |
| `setup_wizard.py` | 212 | the setup and the menu: one `step_<name>` per step, installs, team settings file |
| `teamkit.py` | 15 | the team settings file (encrypted or plain) |
| `record.py` | 65 | the recorder: stream -> chunks + `index.json`; reconnects, refills, moves to a better stream |
| `streams.py` | 14 | which links to try: TBA's webcasts, typed links, their order |
| `scout.py` | 11 | finds a YouTube live stream of the event that nobody listed |
| `live.py` | 122 | record + follow + publish for one event; `start`/`status`/`stop` in the background |
| `vod.py` | 19 | a saved stream split like a live one |
| `pipeline.py` | 21 | chunk -> overlay reader -> timeline -> namer -> cutter |
| `timeline.py` | 12 | countdown episodes -> matches, any game and display |
| `namer.py` | 14 | match -> schedule key (qm12, sf11m1, f1m2, p3) |
| `cutter.py` | 6 | clip from chunks by stream copy |
| `tba.py` | 0 | TBA read API client |
| `tbavideos.py` | 16 | `streamsplitter tba`: report, retitle, add links after the event |
| `listener.py` | 115 | the Slack listener: starts, stops and restarts recordings; background service |
| `slackadmin.py` | 84 | settings and people from Slack (admins) |
| `media.py`, `ffmpeg.py` | 1 | ffmpeg/ffprobe helpers |
| `detect/overlay.py` | 20 | OCR of timer, title, team numbers: any event |
| `detect/results.py` | 6 | when the results screen is on air |
| `detect/audio.py`, `detect/clock.py`, `matcher.py` | 7, 93, 8 | 2026-FMS-only detectors (field sounds, FIRST's scoreboard clock); kept as a fallback and for comparison |
| `publish/commands.py` | 64 | Slack command words, help texts, who may type what |
| `publish/slack.py` | 41 | Slack posts, threads, direct messages |
| `publish/youtube.py` | 47 | login, unlisted upload, playlist, quota |
| `publish/drive.py` | 56 | Google Drive upload |
| `publish/wiki.py` | 59 | MediaWiki upload |
| `publish/tbalink.py` | 10 | adds a YouTube link to its match on TBA (write key) |
| `publish/outbox.py` | 28 | the on-disk queue (sqlite per tag): retries, resume, nothing twice |
| `publish/publisher.py`, `router.py` | 18, 5 | which clip goes where, and when |
| `publish/templates.py` | 14 | YouTube titles, descriptions, playlist names |
| `publish/notify.py` | 30 | alerts and the stopped-running alarm |
| `seasons/2026/` | | field-sound templates and results-screen references for the 2026 game |
| `assets/` | | the test-upload clip (2024 MadTown qm6, 13 MB) |

Other folders: `tests/` (one `test_<module>.py` per area; `fake_drive.py`, `fake_mediawiki.py` stand in for the
services), `tools/package.py` (builds the zip for a teammate), `tools/launch_detached.ps1` (long jobs on Windows),
`phase0/` (the first offline proof on saved webcasts: download, fake-live chunking, scoring; record in
`phase0/PLAN.md`; not needed to run an event).

## Folders on disk

- `data_root` (config.toml): `clips/<tag>/`, `outbox/<tag>.sqlite`, downloads. Clips are never deleted.
- `work_root` (optional, an SSD): `live/<tag>/` chunks and `index.json`. Chunks older than 28 hours are deleted.
- `logs/` in the repo folder: one log per background program.
- `config.toml`, `secrets.json`: settings and the fallback key store. Never read them aloud, never commit them.

## Developer install and tests

The venv has no pip; the installer's uv does it:

```
.tools\uv\uv.exe pip install --python .venv\Scripts\python.exe -e ".[detect,dev]"
.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests -q
```

About 700 tests, 2 to 3 minutes. They use no network and no account. Run them with a time limit of 10 minutes and
kill the run if it passes it: a hung run once grew to 31 GB of memory. Run one file while working
(`tests\test_record.py`), all of them before a commit.

## The recorder alone

```
.venv\Scripts\streamsplitter record https://www.youtube.com/@SomeChannel/live --also twitch.tv/somechannel --tag t
```

It writes chunks (stream copy, 1080p H.264 + AAC when offered) and `index.json` to `<work_root>/live/<tag>/`. A
chunk is listed only once its file is complete and the index is replaced atomically, so a reader can poll it.
While nothing is live it checks every 45 s. Started again with the same tag, it continues the same index. Index
fields, timing and gap rules: the docstring of `record.py`.

## Rules for changes

- A running recording or listener keeps the old code. Do not restart one that is recording unless asked.
- Never message a Slack user who did not ask ([lessons/publishing.md](lessons/publishing.md)).
- Every upload is a job in the outbox with a stable name; keep it that way so nothing is sent twice.
- Tests never touch the real accounts. Keep fakes for new services.
