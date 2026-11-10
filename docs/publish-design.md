# Publishing design: Slack, YouTube, notifications, setup wizard

Status: in progress (2026-11-04), for MadTown Throwdown 2026 (2026mttd, Nov 6-8). Shared contract for the people
and agents building it. Read this before editing anything under `streamsplitter/publish/` or the wizard.

## What the team asked for (user, 2026-11-04)

- Every clip with one of the team's own matches (default team 5026) is posted to the team's Slack channel
  (`#errata`) automatically while `streamsplitter live` runs.
- Every other match is uploaded to the team's YouTube channel as an **unlisted** video in a playlist for the event,
  which the script creates itself (no manual playlist).
- Spotty internet must not lose anything: uploads queue and retry every so often, for Slack and YouTube, across
  restarts.
- A Slack contact gets a DM with info, warnings and errors, and an alarm if the script stops running.
- A guided setup: started once, it downloads what it needs in the background while it asks for: the event code
  (teaching how to find it), a TBA read key (teaching how to get one), the stream link, the teams whose matches go to
  Slack, Slack yes/no (log in, channel, DM contact), YouTube yes/no (log in in the browser, channel, approve the
  playlist name, title format and description, following the FIRST Robotics Competition channel's conventions),
  creates the playlist, offers a test upload of a bundled clip to Slack and YouTube, and confirms that everything
  is hooked up and that failures will be reported.

## Defaults (changeable in the wizard and config.toml)

- Routing (user, 2026-11-04): a match whose teams include one of `slack.teams` goes to Slack **and** YouTube; every
  other match goes to YouTube only (`youtube.route = "all"`; `"others"` would keep the team's own matches off
  YouTube).
- Slack gets the **match clip** the moment it is cut (drive team needs it within 4 minutes), and the **score
  version** (match + results screen) as a reply in the same thread when it is ready (`slack.score_in_thread = true`).
- YouTube gets the **score version** when it is settled, or the match clip when no results screen was found
  (`youtube.version = "score"`); the description carries TBA's final score once posted (filled at upload time, or
  updated later).
- Notifications go to one Slack user by DM (`notify.slack_user`); without Slack, to the console/log only.
- Google Drive (user, 2026-11-05: the wiki is no place to store videos; a shared Drive folder is free, members-only
  and plays in the browser and the Drive app): every match, at official events too, as a 720p copy of the score
  version (`drive.version = "score"`, `drive.height = 720`), into a folder per event inside a top folder the setup
  makes and a person shares. The wiki step left the first run's list (`setup --section wiki` keeps it).

## config.toml sections (written by the wizard via `config.save_sections`, read via `config.section`)

```toml
[event]
key = "2026mttd"                 # TBA event key
name = "Madtown Throwdown"       # TBA's event name (for titles)
year = 2026
stream_url = "https://www.youtube.com/@.../live"
tag = "2026mttd"                 # folder name for chunks and clips

[slack]
enabled = true
teams = ["5026"]
channel_id = "C0123"             # resolved by the wizard from "#errata"
channel_name = "errata"
score_in_thread = true
clip_height = 0                  # 0 = as cut (1080p); 720 = a smaller copy for phones

[youtube]
enabled = true
route = "all"                    # "all" (own matches on Slack and YouTube) | "others"
version = "score"                # "score" | "match"
privacy = "unlisted"
channel_id = "UC..."
channel_title = "..."
playlist_id = "PL..."
playlist_title = "2026 Madtown Throwdown"
title_format = "..."             # see publish/templates.py
description_format = "..."

[drive]                          # a Google Drive folder shared with the team: every match (docs/setup-drive.md)
enabled = true
version = "score"                # "score" | "match", as for YouTube
height = 720                     # 720 | 540 | 0 (as cut); the copy is made in the Drive job
name_format = "{code} {match_title}[ - {teams}]"   # Q07 Qualification 7 - 5026 254 1678 vs 971 8 649.mp4
folder_id = "..."                # the top folder the setup made (drive.file: the app sees only what it made)
folder_url = "https://drive.google.com/drive/folders/..."
folder_title = "FRC match videos"
event_folder_id = "..."          # "2026 MadTown Throwdown" inside it, found or made for event_folder_event
event_folder_event = "2026mttd"
# description_format: optional; else [youtube] description_format, else the default. paused = true: held

[wiki]                           # the team's MediaWiki: every match (docs/setup-wiki.md)
enabled = true
api_url = "https://www.team5026.com/api.php"
username = "Ian@StreamSplitter"  # the bot password's user name
private = true                   # {{Private}} (private_markup) atop the event page and each file page
embed_format = "[[File:{file}|800px]]"           # {file}, {title}; plain replacement, so {{...}} is fine
page_format = "{event_name} ({year}) match videos"
page_title = "Madtown Throwdown (2026) match videos"   # confirmed in the setup, for page_event
page_event = "2026mttd"
version = "score"                # "score" | "match", as for YouTube

[notify]
slack_user = "U0123"             # DM target
slack_user_name = "..."
heartbeat_s = 60                 # how often the stopped-running alarm is pushed back
alarm_after_s = 420              # silence after which the alarm DM fires (>= 420: Slack posts alarms deleted late)

[listen]                         # Start from Slack (listener.py, docs/start-from-slack.md); per computer, not in the
enabled = true                   # team settings file
mode = "background"              # "background": hidden from each log-in | "window": only while open in a window
users = ["U0456"]                # who may command it besides [notify] slack_user
user_names = ["..."]
# alarm: its own stopped-running alarm (default true in background, false in window); keep_awake (default true);
# youtube_off_for = "<key>": the listener turned YouTube off for an official event; the next event allowing it
# turns it back on
```

Secrets (`config.get_secret` / `config.set_secret`; never in config.toml, never printed or logged):
`tba_key`, `slack_bot_token`, `youtube_token` (OAuth token JSON incl. refresh token), `youtube_client` (the OAuth
client JSON; Google Drive uses it too), `drive_token` (the Google Drive login, scope drive.file; its account may differ
from the YouTube channel's), `wiki_bot_password` (the MediaWiki bot password; the user types it into the setup).

## Modules and owners

| File | Owner | What |
|---|---|---|
| `publish/base.py` | lead (done) | `Job`, `Match`, `Destination`, `TransientError`, `PermanentError` |
| `config.py`, `pyproject.toml` | lead | sections, secrets, dependencies (ask the lead for a new dependency) |
| `publish/slack.py`, `publish/slack_manifest.json`, `tests/test_slack.py`, `docs/setup-slack.md` | Slack agent | Slack client, destination, app manifest |
| `publish/youtube.py`, `publish/templates.py`, `tests/test_youtube.py`, `tests/test_templates.py`, `docs/setup-youtube.md` | YouTube agent | OAuth, playlist, resumable upload, titles/descriptions |
| `publish/outbox.py`, `publish/notify.py`, `publish/router.py`, `publish/publisher.py`, `live.py`, `tests/test_outbox.py`, `tests/test_publisher.py` | Outbox agent | durable retries, DMs and the alarm, routing, the live hook, start/stop/status |
| `setup_wizard.py`, `cli.py` (`setup` + start/stop/status commands), `run.bat`, `run.sh`, `tests/test_wizard.py`, README "Setup" section | Wizard agent | guided setup, installer, background downloads, test upload |
| `publish/wiki.py`, `tests/test_wiki.py`, `tests/fake_mediawiki.py`, `docs/setup-wiki.md` (2026-11-04) | Wiki agent | MediaWiki bot login, chunked resumable upload, event page + sections; its hooks in router, publisher, live, commands, the wizard's wiki step and teamkit |
| `publish/drive.py`, `tests/test_drive.py`, `tests/fake_drive.py`, `docs/setup-drive.md` (2026-11-05) | Drive agent | Drive login (drive.file), folders, smaller copy, resumable upload, names; its hooks in router, publisher, live, commands, the wizard's Drive step and teamkit |
| `listener.py`, `tests/test_listener.py`, `docs/start-from-slack.md` (2026-11-05) | Listener agent | Start from Slack: split / yes / no / stop / list from the DMs, restarts and the auto-stop, its alarm, the log-in start; its hooks in commands (listener_command, listener_on, help), notify (text hooks), cli (`listen`) and the wizard's listen step and menu |

Do not edit a file another owner holds; if you need a change there, say so in your final report.

## Interfaces

### Slack (`publish/slack.py`)

```python
class SlackClient:                       # thin wrapper over slack_sdk.WebClient; maps errors to base errors
    def __init__(self, token: str, timeout: float = 30.0): ...
    def auth_test(self) -> dict          # {"team", "team_id", "bot_user_id", "url"}
    def find_channel(self, name: str) -> dict | None      # {"id", "name", "is_private", "is_member"}
    def join_channel(self, channel_id: str) -> None       # public channels; private ones need /invite
    def find_users(self, query: str) -> list[dict]        # email (exact) or name (contains): {"id","name","real_name","email"}
    def upload_file(self, channel_id: str, path: str, title: str, comment: str, thread_ts: str | None = None) -> dict
                                         # {"file_id", "permalink", "ts"}  (ts of the message, for threading)
    def post_message(self, channel_id: str, text: str, thread_ts: str | None = None) -> dict   # {"ts", "channel"}
    def dm(self, user_id: str, text: str) -> dict
    def schedule_message(self, channel_id: str, text: str, post_at: int) -> str    # scheduled_message_id
    def delete_scheduled(self, channel_id: str, scheduled_message_id: str) -> None
def create_app_url() -> str              # Slack's "create an app from this manifest" link, manifest prefilled
class SlackDestination:                  # Destination; name = "slack"
    def __init__(self, client: SlackClient, channel_id: str, *, clip_height: int = 0): ...
    def send(self, job: Job) -> dict     # clip: new message with the video; score: reply in the clip's thread
                                         # (the thread ts comes from job.state["thread_ts"], set by the publisher)
```

### YouTube (`publish/youtube.py`, `publish/templates.py`)

```python
def login(client_config: dict) -> str                    # browser OAuth (installed app, loopback); token JSON
class YouTubeClient:
    def __init__(self, token_json: str, client_config: dict, on_token=None): ...   # on_token(new_json) after refresh
    def channel(self) -> dict                            # {"id", "title", "url"}
    def find_playlist(self, title: str) -> dict | None   # {"id", "title", "url", "privacy"}
    def create_playlist(self, title: str, description: str, privacy: str = "unlisted") -> dict
    def upload(self, path: str, title: str, description: str, privacy: str, state: dict, progress=None) -> dict
                                                         # resumable; state["upload_uri"] etc. survive restarts
                                                         # -> {"video_id", "url"}
    def add_to_playlist(self, playlist_id: str, video_id: str) -> None
    def set_description(self, video_id: str, title: str, description: str) -> None
    def delete_video(self, video_id: str) -> None
class YouTubeDestination:                # Destination; name = "youtube"; upload, then playlist; steps idempotent
    def __init__(self, client: YouTubeClient, playlist_id: str, title_format: str, description_format: str,
                 privacy: str = "unlisted"): ...
    def send(self, job: Job) -> dict

# templates.py
PLAYLIST_FORMAT, TITLE_FORMAT, DESCRIPTION_FORMAT     # defaults following the FIRST Robotics Competition channel
def render(fmt: str, match: Match) -> str             # fields documented in the module
```

### Team wiki (`publish/wiki.py`, added 2026-11-04)

```python
class WikiClient:                        # MediaWiki Action API (api.php) with requests; paced MIN_INTERVAL_S apart
    def __init__(self, api_url: str, username: str | None = None, password: str | None = None, *, session=None): ...
    def login(self) -> dict              # bot password (User@botname) -> {"user"}; CSRF token fetched on first write
    def site_info(self) / user_info(self) / page_url(self, title)
    def upload(self, path, filename, comment, text, state, *, on_state=None, progress=None) -> dict
                                         # chunked (stash, 10 MB pieces), resumable from state["filekey"/"offset"]
    def create_page(self, title, text, summary) -> bool      # createonly; False when it existed
    def add_section(self, title, heading, text, summary) -> dict   # section=new, nocreate -> {"newrevid"}
    def page_text(self, title) / sections(self, title) / search_pages(self, query) / file_info(self, name) / delete(...)
class WikiDestination:                   # Destination; name = "wiki"; upload, page once, section once (job.state)
    def __init__(self, client, *, page_title=None, page_event=None, page_format=PAGE_FORMAT,
                 embed_format=EMBED_FORMAT, private=True, private_markup="{{Private}}", on_state=None, warn=None): ...
```

route() sends every match to the wiki when [wiki] enabled (independent of YouTube and of official events); the
Publisher queues it like YouTube (score version from on_score, partial clips from on_clip).

### Google Drive (`publish/drive.py`, added 2026-11-05)

```python
def login(client_config: dict) -> str     # browser OAuth with the YouTube client, scope drive.file; token JSON
class DriveClient:                        # Drive API v3 with requests; errors mapped as for YouTube
    def __init__(self, token_json: str, client_config: dict, on_token=None, *, session=None): ...
    def about(self) -> dict               # account and storage: {"name", "email", "limit", "usage", "free", ...}
    def get(self, file_id) / find(self, query) / create_folder(self, name, parent, props) / update(...) / trash(...)
    def upload(self, path, metadata, state, progress=None, on_state=None) -> dict
                                          # resumable (8 MiB pieces); state["upload_uri"] etc. survive restarts
def find_top_folder(client, title) / create_top_folder(client, title) / ensure_event_folder(client, top, key, title)
def render_name(fmt, match, alliances=8, quals=None) -> str      # "Q07 Qualification 7 - 5026 254 1678 vs 971 8 649"
class DriveDestination:                   # Destination; name = "drive"
    def __init__(self, client, folder_id, *, event=None, event_folder_id=None, event_folder_event=None, height=720,
                 name_format=NAME_FORMAT, description_format=..., on_state=None, on_folder=None, on_full=None,
                 warn=None): ...
    def send(self, job: Job) -> dict      # smaller copy, event folder, look for the match (appProperties), upload;
                                          # a "describe" job (late score) updates the description
    def storage_status(self) -> dict | None   # for status.json and the status reply, no network
```

route() sends every match to Drive when [drive] enabled (order: slack, youtube, drive, wiki). The Publisher queues it
like YouTube, with the clip's cut time in job.state["played"] (the file's modifiedTime), and queues a background
"describe" job when the score came after the upload. A full Drive pauses the Drive queue (live.\_drive_full: [drive]
paused, one DM); `resume drive` sends the held jobs at once.

### Outbox, notifications, routing, the live hook

```python
class Outbox:                            # sqlite (WAL) at <data_root>/outbox/<tag>.sqlite
    def __init__(self, path: Path, destinations: dict[str, Destination], notifier: "Notifier"): ...
    def add(self, job: Job) -> bool      # False when the id is already queued or done (idempotent)
    def start(self) -> None              # one worker thread per destination; never blocks the follower
    def stop(self, timeout: float = 10.0) -> None
    def status(self) -> dict             # per destination: queued, done, failing_since, last_error
# Transient: retry forever, backoff 15 s doubling to 10 min (retry_after wins); warn the contact once a destination
# has failed for 10 min, and again when it recovers. Permanent: mark failed, tell the contact what to fix.

class Notifier:
    def info(self, text: str) / warn(self, text: str, key: str | None = None) / error(...)   # key: one DM per 30 min
    def heartbeat(self) -> None          # push the stopped-running alarm (a scheduled DM) alarm_after_s ahead
    def stopped(self, reason: str) -> None   # clean stop: cancel the alarm, DM why

def route(teams: set[str], cfg_slack: dict, cfg_youtube: dict, cfg_wiki=None, cfg_drive=None) -> list[str]
                                         # any of "slack", "youtube", "drive", "wiki", in that order
class Publisher:                         # built by live.run_from_args when Slack or YouTube is enabled
    def on_clip(self, row: dict) -> None     # pipeline row of a match clip (row["naming"], row["clip"]["path"], ...)
    def on_score(self, row: dict) -> None    # row settled by the pipeline's score search (score_clip or not)

# live.py (Outbox agent): start_detached(args) -> pid, request_stop(tag) (a stop file, never a console signal),
# read_status(tag) -> dict (status.json: running, last chunk, clips, outbox status, last warning).
```

### Start from Slack (`listener.py`, added 2026-11-05)

```python
class Listener:                          # one commands.CommandPoller per allowed person, its _answer replaced
    def handle(self, poller, message) -> str | None   # the reply; None: silent (the running recording answers)
    def tick(self) -> None               # stops under way, the restart (3 an hour), the auto-stop (06:00 after the end)
def serve(listener, notifier, folder, *, window, alarm, ...) -> int   # the loop, heartbeat, keep-awake, clean exit
class ListenerNotifier(Notifier)         # notify.py's DMs and alarm in the listener's words (Notifier text hooks)
def install_login_start() / remove_login_start()      # Startup-folder shortcut to pythonw.exe; LaunchAgent; autostart
# commands.py: listener_command(text), listener_on() (a listener's lock is held), help_text(listen=True)
# setup_wizard.save_event(ev): the event step's saving, shared with split
```

A running recording leaves split, stop, list, yes and no to the listener only while one is alive (its lock held);
with none alive it answers them as before (UNKNOWN, and STOP_HOW for stop). The listener starts a recording as
`streamsplitter start` does (live.start_detached with setup_wizard.live_args()), so a recording never depends on the
listener's process or window.

## Rules for everyone

- Python 3.12 venv at `.venv` (`.venv\Scripts\python.exe -m pytest -q`); Windows first, but nothing Windows-only
  without a mac/Linux path.
- Never call the real Slack or YouTube APIs, and never log in anywhere: tests mock HTTP. The lead runs the real tests
  with the user's accounts.
- Never print, log or commit a token or key. Do not commit at all.
- Keep the follower fast: uploads run on their own threads; nothing in the publish path may block chunk processing.
- One heavy CPU job at a time on this machine; long jobs via `tools\launch_detached.ps1`.
- Match the surrounding code: comment density, naming, short docstrings that say why.
