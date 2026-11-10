# Setup

A person runs the setup in their own terminal: it asks questions, opens browser logins and takes keys. An AI agent
can install, explain each step and check the result; it cannot answer for the person. First check
[requirements-and-limits.md](requirements-and-limits.md): about 100 GB free disk, 4-6 Mbit/s download all day, the
channel owner's Google account for YouTube, a Google account with about 4 GB free per event for Google Drive, and
one free Slack app slot.

## Install

Put the folder on a short path that no cloud service syncs, such as `C:\StreamSplitter` (not OneDrive, Google
Drive, Dropbox, or a synced Documents or Desktop). The installed packages would sync too, and a path longer than
about 150 characters can stop the text reader (OCR) from loading on Windows.

- **Windows:** double-click `run.bat`. If Windows says "Windows protected your PC", click *More info*, then *Run
  anyway*.
- **macOS / Linux:** `sh run.sh` in a terminal in that folder.

The first run installs uv (astral.sh's installer, into `.tools\uv` unless uv is already installed), uv's own
Python 3.12 and the light packages into `.venv` (under a minute), then starts the setup. While the setup asks its
questions, the rest downloads in the background: the detection packages (~150 MB), ffmpeg and ffprobe if the
computer has none (checked against their published SHA-256), and Deno, the JavaScript runtime yt-dlp recommends
for YouTube. About 700 MB and 5 minutes in all. Nothing outside the folder is changed (no system settings, no
PATH); delete the folder to remove everything.

`run.bat --install-only` does only this part and asks nothing: for a laptop prepared at home, or an AI agent.

## The steps

The setup saves each answer as it goes: press Ctrl+C any time and run it again to continue. Only the event and the
stream are required. `run.bat --section NAME` runs one step again.

| Step (`--section`) | What it asks | What the person needs |
|---|---|---|
| Team settings file (`team`) | A file from a teammate who has set StreamSplitter up (and its passphrase, if it has one). It carries Slack, YouTube, Google Drive and the TBA key, so the later steps ask only this computer's own things. | the file, or nothing |
| Event (`event`, `tba`) | Search by name or city, or type the TBA code (`thebluealliance.com/event/2026mttd` is `2026mttd`). | a free TBA read key; the setup opens thebluealliance.com/account and shows how |
| Stream (`stream`) | Proposes the streams TBA lists (a YouTube channel's `/live` page first: it follows each day's stream), or takes any YouTube or Twitch link, and checks it. | nothing, or the link |
| Slack (`slack`) | Which teams' matches to post, the Slack app (made once per team in three small steps, each page opened for you), the channel. | the right to install a Slack app: [setup-slack.md](setup-slack.md) |
| YouTube (`youtube`) | YouTube's Terms, the team's Google sign-in app (made once in five small Google Cloud steps), the browser login, the channel, the playlist (unlisted unless you choose otherwise), titles and description. Every match is uploaded. | the channel owner's Google account: [setup-youtube.md](setup-youtube.md) |
| Google Drive (`drive`) | The Drive API in the same Google project, the login of the account whose storage holds the videos, the folder it makes (one folder per event inside), the video size (720p, about 40 MB a match, by default), the file names. Every match is uploaded. | about 4 GB free per event: [setup-drive.md](setup-drive.md) |
| Alerts (`notify`) | Who gets the direct messages: errors, warnings, and a stopped-running alarm that Slack delivers even if the computer dies. | Slack set up |
| Start from Slack (`listen`) | Whether this computer takes commands from Slack (`split 2026mttd`, `stop`, `status`): in the background from each log-in, or only while a window is open. | [start-from-slack.md](start-from-slack.md) |
| Test upload (`test`) | Sends the bundled clip (MadTown 2024 qm6) to Slack, YouTube and Google Drive, checks that YouTube kept it unlisted, asks you to confirm, then offers to delete the copies. | 2 minutes |
| Summary | Then an offer to start recording in the background. | |

Two steps are not part of the first run: `wiki` (every match to a team's own MediaWiki,
[setup-wiki.md](setup-wiki.md)) and `tbalinks` (an event organizer's TBA write key, [tba.md](tba.md)).

## Where the answers are kept

- Choices go to `config.toml` beside `run.bat`: `[event]`, `[slack]`, `[youtube]`, `[drive]`, `[wiki]`,
  `[notify]`, `[listen]`. Nothing in it is secret, but it names the team's channel, playlist and folder.
- Keys, tokens and logins go to the computer's password store (or `secrets.json` beside `config.toml` where there
  is none). They never go to `config.toml` or the screen.
- Neither file is ever committed or shared. To set up a second computer, use the team settings file.

## After the setup

`run.bat` (`sh run.sh`) now shows a menu: start recording in the background, status, stop, change a setting, test
upload again, start or stop listening for Slack commands, and *Share these settings with a teammate*. That last one
makes the team settings file, saved in Downloads or on the desktop: encrypted with a passphrase (send the file and
the passphrase by different routes), or plain with *No passphrase*. Whoever can open it can post to your Slack and
upload to your channel and Drive folder.

```
run.bat                        # the setup, or the menu once set up (sh run.sh on macOS/Linux)
run.bat --section youtube      # one step again
run.bat --section team         # make a team settings file for a teammate, or load one
run.bat --install-only         # only install and download
run.bat start                  # start, status, stop: the set-up event, in the background
run.bat listen start           # the Slack listener in the background; listen stop, listen status
```

Next: [event-day.md](event-day.md).
