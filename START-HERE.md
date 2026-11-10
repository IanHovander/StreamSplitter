# StreamSplitter: start here

Working with an AI assistant (Claude Code, Codex, Cursor)? Tell it: *"Read AGENTS.md in this folder, install
StreamSplitter, and walk me through the setup."* The rest of this page is the same thing by hand.

StreamSplitter records an FRC event's live stream and saves each match as its own video, a minute or so after the
match ends. It can post your team's matches to Slack and upload every match to YouTube and to a Google Drive folder
shared with your team.

## What you need

A computer that stays on, plugged in and online all event day, about 100 GB free disk, and a good internet
connection. For Slack: a free app slot in the team's workspace. For YouTube: the channel owner's Google account. For
Google Drive: a Google account with about 4 GB free per event (a team account is best). The full list, and what
StreamSplitter cannot do: [docs/requirements-and-limits.md](docs/requirements-and-limits.md).

## 1. Get the folder

Download the zip from GitHub (the green **Code** button, *Download ZIP*) or from a teammate, or `git clone` the
repository. Put the StreamSplitter folder somewhere that is **not** synced by OneDrive, Google Drive or Dropbox, for
example `C:\StreamSplitter` (on Windows, the Documents and Desktop folders are often synced). On Windows: right-click
the zip, *Extract All*. Keep the folder's path short (the Downloads folder is fine): from a folder nested very deep, Windows
cannot load some of StreamSplitter's parts, and the setup says so.

## 2. Start the setup

- **Windows:** double-click `run.bat`. If Windows says "Windows protected your PC", click **More info**, then **Run
  anyway**.
- **Mac / Linux:** open a terminal in the folder and type `sh run.sh`. (macOS is untested.)

The first start installs what StreamSplitter needs, inside the folder only, then asks its questions. Answers are
saved as you go: press Ctrl+C to stop, and start it again to continue.

## 3. Answer the questions

About 15 minutes; 30 if you are the first on your team to set up Slack and YouTube. In order:

1. **Team settings file.** If a teammate already set StreamSplitter up, ask them for the team settings file (they
   make it in the menu: *Share these settings with a teammate*), and its passphrase if they gave it one; a file made
   with *No passphrase* needs none. It fills in Slack, YouTube, Google Drive and the Blue Alliance key, so the rest
   goes quickly.
2. **The event:** type part of its name and pick it from the list.
3. **The stream:** usually just press Enter for the one The Blue Alliance lists.
4. **Slack** (optional): the first person on the team makes the Slack app, one for the whole team
   ([docs/setup-slack.md](docs/setup-slack.md)). Then the channel, and who gets the alerts.
5. **YouTube** (optional): the first person on the team makes the Google sign-in app
   ([docs/setup-youtube.md](docs/setup-youtube.md)); every laptop then logs in once.
6. **Google Drive** (optional): every match into a folder StreamSplitter makes, which you share with the team. Turn
   on the Google Drive API in the same Google project, log in with the account whose storage holds the videos
   ([docs/setup-drive.md](docs/setup-drive.md)).
7. **Alerts**, then **Start from Slack** (optional: start and stop recordings by messaging the StreamSplitter app,
   [docs/start-from-slack.md](docs/start-from-slack.md)), a **test upload**, and an offer to **start recording**.

A team wiki (MediaWiki) can get every match too, but is not asked for: `run.bat --section wiki`
([docs/setup-wiki.md](docs/setup-wiki.md)).

Each step opens the right web page for you and tells you what to click.

## On event day

Only one computer per event should run StreamSplitter, or each match is posted and uploaded twice.

| To | Do |
|---|---|
| Start recording | double-click `run.bat`, pick **Start recording** (or type `run.bat start` in a terminal). It waits for the stream to go live. You can close the window. |
| See how it is doing | `run.bat`, **Show status** (or `run.bat status`), or reply `status` to the StreamSplitter app in Slack |
| Record a different stream | an admin replies `set stream <link>` to the StreamSplitter app in Slack, or `run.bat --section stream`. It also looks for a better YouTube stream by itself every 5 minutes |
| Stop | `run.bat`, **Stop recording** (or `run.bat stop`) |
| Start or stop from your phone | with Start from Slack on: message the StreamSplitter app `split 2026mttd`, or `stop` ([docs/start-from-slack.md](docs/start-from-slack.md)) |
| Change a setting from your phone | an admin (the alerts contact is the first one) messages the app `settings`: every setting with the command that changes it. `help` lists the commands |
| Let a teammate start and stop it | an admin messages the app `add @name` (`add admin @name` for another admin); they get no message unless the admin says yes to the how-to note |
| Put the videos on The Blue Alliance | after the event: `run.bat tba` ([docs/tba.md](docs/tba.md)) |

On a Mac or Linux, use `sh run.sh` instead of `run.bat`. Keep the computer plugged in, set closing the lid to do
nothing, and pause Windows updates for the weekend.

## Help

- Ask the teammate who gave you the folder, or your team's StreamSplitter lead.
- Most messages say what to do. The guides have a "When something goes wrong" table:
  [Slack](docs/setup-slack.md), [YouTube](docs/setup-youtube.md), [Google Drive](docs/setup-drive.md).
- When asking for help, send the text in the window and the log files (the `logs` folder in the StreamSplitter
  folder, and the one in the data folder the setup's summary shows). Never send the Slack token, the Google client
  file or a passphrase: StreamSplitter never shows them, and nobody helping needs them.
