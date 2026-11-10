# StreamSplitter: requirements and limits

For FRC students and mentors deciding whether StreamSplitter fits their team, and what to sort out first. It records
an event's webcast on a computer you leave running and saves each match as its own video about 30 s after it ends.
It can also post those videos to Slack, YouTube, a Google Drive folder shared with your team, and your team's own
wiki. Checked 2026-11-04 (Google Drive 2026-11-05); "not measured" means nobody has tested it yet.

## Checklist

- [ ] A computer that stays on, awake, plugged in and online for every event day (logged in, for Start from Slack)
- [ ] About 100 GB free disk space
- [ ] Internet: 4-6 Mbit/s download all day, plus upload for the clips
- [ ] A free [The Blue Alliance](https://www.thebluealliance.com/account) read key
- [ ] Slack (optional): one free app slot, and permission to install the app
- [ ] YouTube (optional): the channel owner's Google account, and a Google Cloud project
- [ ] Google Drive (optional): a Google account (a team account is best) with about 4 GB free per event, and the same
  Google Cloud project with the Drive API on
- [ ] Team wiki (optional): a MediaWiki whose admin allows video uploads, and a bot password
- [ ] A webcast with an on-screen match timer, or audible field sounds
- [ ] The organisers' OK to post their footage

## The computer

- **System.** Tested on Windows 10/11 and on Linux under WSL. macOS is untested.
- **Stays on for the whole event.** StreamSplitter stops the computer from going to sleep when idle. It cannot stop
  a closed lid or a Windows Update restart. Keep it plugged in, set closing the lid to do nothing, and pause updates.
- **It does not have to be at the venue.** It records the webcast, so a computer at home with good internet works.
  With [Start from Slack](start-from-slack.md) in the background, recordings start and stop from Slack: that
  computer must stay on and logged in all event weekend (locked is fine), on home internet with about 6 Mbit/s
  download all event day plus upload for the clips. After a Windows Update restart nothing listens until someone
  logs in: set Windows Update's active hours, or pause updates, for event weekends.
- **One event at a time per computer.** Start from Slack refuses a second event while one records (`stop` first).
- **Processor.** The test PC (16-core desktop) handles each 20 s piece of stream in 3-7 s. A slower laptop must
  stay under 20 s, or the clips fall behind. Low-end laptops and RAM use are not measured. No graphics card needed.
- **Install.** `run.bat` / `sh run.sh` installs everything inside its folder (about 0.7 GB, no admin rights; not a
  OneDrive or Google Drive folder). On a network that blocks downloads, install at home: `run.bat --install-only`.

## Internet

| Direction | Amount |
|---|---|
| Download (the webcast, all day) | 4-6 Mbit/s measured at 1080p. That is 2-3 GB an hour, or 20-30 GB an event day. Watch hotspot data caps. |
| Upload to YouTube (every match, or only other teams' matches) | 90-130 MB per match, about 3-4.5 GB on a 35-match day |
| Upload to Slack (your matches: clip, then score version) | Same sizes. The 720p option is about 3x smaller (126 MB became 40 MB). |
| Upload to Google Drive (every match) | about 40 MB per match at 720p (the default; 27 MB at 540p, 90-130 MB as recorded): about 16 s at 20 Mbit/s, about 1 min at 5 Mbit/s |
| Upload to the team wiki (every match) | 90-130 MB per match again, in 10 MB pieces |

- **Delivery within 4 minutes** needs about 6 Mbit/s of spare upload at 1080p, or 2 Mbit/s at 720p. That is
  arithmetic, not a measurement. YouTube uploads share the connection.
- **Internet drops.** Uploads queue, retry and resume where they stopped, even after a restart. A short recording
  gap is refilled from YouTube's replay window (about 1 h). Twitch keeps about 10 s, so a Twitch drop leaves a hole.

## Disk space

| What | Size | How estimated |
|---|---|---|
| Software | under 1 GB | the test install is 0.64 GB |
| Raw recording (last 28 h only) | 2-3 GB per stream hour: about 40 GB after two 10-11 h days, 76 GB if the stream never stops | 6 Mbit/s (the highest measured) = 2.7 GB/h; written only while live |
| Clips (match + score version) | 180-260 MB per match, 20-25 GB per 100-match weekend | measured pairs: 90 + 94 MB, 127 + 132 MB; never deleted automatically |

Start with about 100 GB free. StreamSplitter logs a warning at start if there is under 30 GB, and sends a DM if free
space drops under 10 GB.

## Accounts and keys

- **The Blue Alliance read key:** free; the setup shows how to get one.
- **Slack** ([guide](setup-slack.md)): you need these four things. Without Slack there are no DMs and no "stopped
  running" alarm, and problems show only in the log.
  - a workspace with a free app slot
  - permission to install apps, or an admin's approval (ask days ahead)
  - the channel, already created
  - a contact person with Slack notifications on
- **YouTube** ([guide](setup-youtube.md)): you need these three things.
  - The Google account that owns the channel, or an Owner/Manager of its Brand Account. People invited in YouTube
    Studio [cannot use the API](https://support.google.com/youtube/answer/9481328). School accounts may be blocked
    by their admin.
  - A free Google Cloud project with an OAuth client (about 20 minutes).
  - Recommended: phone verification at [youtube.com/verify](https://www.youtube.com/verify), which
    [raises the daily upload limit](https://support.google.com/youtube/answer/9891124).
- **Google Drive** ([guide](setup-drive.md)): you need these two things.
  - A Google account whose storage holds the videos: a team account is best, since the videos use the space of the
    account that logs in, not of the people the folder is shared with. School accounts may be blocked by their admin.
  - The team's Google Cloud project (the one for YouTube, or one made in the setup) with the Google Drive API turned
    on: <https://console.cloud.google.com/apis/library/drive.googleapis.com>.
- **Team wiki** ([guide](setup-wiki.md)): a MediaWiki site whose admin has allowed MP4 uploads big enough for a match
  video, and an account (ideally a bot account) for which you make a bot password at Special:BotPasswords.

## What the webcast must have

- **A YouTube or Twitch live stream.**
- **A way to find matches:** an on-screen match timer in the overlay (any style; it is read as text), or audible
  field sounds (the start sound and the end buzzer).
- **Names:** the overlay title ("Qualification 12"), or The Blue Alliance's schedule plus the team numbers on screen.
  Without either, a clip is named by its stream time. Offseason B teams shown as stand-in numbers (9993 = 254B) are
  handled.
- **Score version:** needs the results screen shown on air. Otherwise YouTube gets the plain match clip.
- **Drops and cutaways:** if the stream drops or the broadcast cuts away mid-match, the clip may be marked
  "(Partial)".
- **It cannot split** a webcast that has neither an overlay timer nor audible field sounds.

Tests on saved webcasts ([phase0/PLAN.md](../phase0/PLAN.md)):

| Webcast | Result |
|---|---|
| Chezy Champs 2026, Saturday | 56 of 56 qualification matches found and named |
| Eagan Week Zero 2025 (home-made overlay) | 11 of 11, 2 of them with no overlay, cut from the field sounds |
| MadTown 2025 (final test) | right on the first match of day 2 and on 4 practice matches |
| MadTown 2024 at live speed | clips 27-33 s after the match end, named right |
| Kettering Week 0 2025 | **failed:** no overlay, field sounds inaudible |

## How fast clips arrive

| Step | Time |
|---|---|
| Match end → clip on the computer | about 30 s (27-33 s measured), about a minute at worst |
| The webcast's own delay | not measured; add it |
| Upload of 100 MB | about 40 s at 20 Mbit/s, about 2.7 min at 5 Mbit/s |
| Score version | 46-270 s after the end (Chezy Champs 2026) |

The target is to have the clip with the drive team within 4 minutes.

## YouTube limits

| Limit | Number | What happens |
|---|---|---|
| Uploads | **100 a day per Google Cloud project**, shared by every computer using it ([quota](https://developers.google.com/youtube/v3/determine_quota_cost)) | Clips wait in the queue until just after midnight US Pacific; the contact gets a DM |
| Other API units | 10,000 a day (playlist add 50, score update 50): about 99 matches | same |
| Channel's own daily limit | not published; phone verification raises it | waits until midnight Pacific |
| Login in "Testing" mode | [expires after 7 days](https://developers.google.com/identity/protocols/oauth2) | log in again shortly before each event |
| Login "In production", unverified | no expiry (ends after 6 months unused); [100 users over the project's life](https://support.google.com/cloud/answer/15549945) | you see "Google hasn't verified this app": click Advanced, then Go to StreamSplitter |

YouTube [stopped locking new projects' uploads as private](https://developers.google.com/youtube/v3/revision_history)
on 2026-11-02. The setup's test upload checks this. If the test upload comes back locked, upload by hand in YouTube
Studio.

## Google Drive limits

| Limit | Number | What happens |
|---|---|---|
| Storage | 15 GB per free Google account, shared with its Gmail and Google Photos; counted against the account that logs in | Under about one event left: a DM. Full: Drive uploads pause, the contact gets a DM, and `resume drive` sends them once there is room |
| Size per match | about 40 MB at 720p (a 130 MB 1080p clip measured 39.7 MB), 27 MB at 540p | about 4 GB per 100-match event at 720p: about 3 events on a free account |
| What StreamSplitter can see | only the files and folders it made (the `drive.file` permission) | It makes its own folder; it cannot upload into a folder a person made, and another account cannot see its folder |
| Where the files go | the logged-in account's My Drive | Google Workspace shared drives are not used |
| Uploads per account | [750 GB a day](https://developers.google.com/workspace/drive/api/guides/limits) | far above an event |
| Who can watch | whoever you share the folder with (a Google Group, emails, or anyone with the link) | StreamSplitter never shares anything itself |

## Team wiki limits

| Limit | Number | What happens |
|---|---|---|
| Cloudflare's request size (free plan) | 100 MB per request | Handled: videos go up in 10 MB pieces (chunked uploads) and are put together on the wiki |
| Cloudflare's request rate (free plan) | 300 requests per 10 s per address | StreamSplitter sends at most 2-3 a second |
| The wiki's own upload size (`$wgMaxUploadSize`, PHP and web server limits) | set by the wiki's admin | Too small: the uploads are refused and the contact gets a DM saying what the admin must change ([guide](setup-wiki.md)) |
| Disk on the wiki's server | 90-130 MB per match, about 10 GB per event | The wiki's admin must have room |
| Who can watch | members only, if the wiki has a file gate | `{{Private}}` hides the pages; the video files themselves are members-only only if the wiki protects its files (ask its admin) |

## Slack limits (free plan)

| Limit | Free plan |
|---|---|
| Apps | [up to 10 third-party or custom apps](https://slack.com/help/articles/115002422943-Message-file-and-app-limits-on-the-free-version-of-Slack); StreamSplitter uses 1 |
| History | [last 90 days visible; data over a year old deleted](https://slack.com/help/articles/115002422943-Message-file-and-app-limits-on-the-free-version-of-Slack) |
| File size | [1 GB per file](https://slack.com/help/articles/201330736-Add-files-to-Slack); clips are 40-260 MB |
| Installing apps | [any member by default](https://slack.com/help/articles/202035138-Add-apps-to-your-Slack-workspace); if owners [turned on app approval](https://slack.com/help/articles/222386767-Manage-app-installation-settings-for-your-workspace), "Request to Install" and wait |

Slack is not an archive: clips vanish from view after 90 days, so keep them on the computer or YouTube. One
StreamSplitter app serves every computer and event all season. Share it with the team settings file instead of
making a new app per laptop.

## Running more than one computer

- **One publishing computer per event.** Two computers publishing the same event upload every match twice (to the
  wiki too) and share YouTube's 100 a day. Google Drive is the exception when both are logged in to the same
  account: the second skips matches already in the event folder.
- **A backup computer** can record with Slack and YouTube off; its clips stay on its disk.
- **Several events at once** (one computer each) can share one Slack app and one Google project, within 100 uploads
  a day in total. Three 35-match days would not fit.

## Footage, rules and secrets

- **The footage belongs to the event.** Ask the organisers before posting it. Content ID claims are possible, for
  example on venue music. Uploading means agreeing to the [YouTube Terms](https://www.youtube.com/t/terms).
- **The Blue Alliance video links are for event organizers only** (off by default; a TBA write key for the event is
  needed). Uploading an event's footage before its organizers do, and linking it on TBA, can take away their control
  of how it is used: not recommended otherwise ([setup-youtube.md](setup-youtube.md)).
- **One Google Cloud project per team.** Google's
  [developer policies](https://developers.google.com/youtube/terms/developer-policies) forbid sharing API
  credentials with other groups.
- **Keep the Slack token (`xoxb-...`) and the Google client file private.** Never put them in Slack, email, a shared
  drive, a screenshot or a code repository. To set up another laptop, use the team settings file. With a
  passphrase it is encrypted; send the passphrase separately, through a password manager. Made with *No passphrase*
  it is plain, and anyone who gets it can use the keys in it (replace them if it goes astray: a new Slack token, a new
  Google client). If the file includes the YouTube login, anyone who can open it can upload to and delete your
  channel's videos.

## What is stored where

| What | Where |
|---|---|
| Event, stream, channel, playlist, titles, contact, Drive folder and video size, wiki address and bot user name, Start from Slack (mode, who may use it) | `config.toml`: plain text, nothing secret |
| Start from Slack's log-in start (background mode) | Windows: a shortcut in your Startup folder; macOS: a LaunchAgent; Linux: an autostart entry |
| TBA key, TBA write key (organizers), Slack token, YouTube login, Google Drive login, Google client file, wiki bot password | the system's password store (Windows Credential Manager, macOS Keychain, Linux Secret Service); without one, `secrets.json` beside `config.toml` |
| Recording, clips, upload queue | the data folder on the computer |
| Team settings file | wherever you save it; encrypted (AES-256-GCM) |

StreamSplitter talks only to the webcast site, The Blue Alliance, Slack, Google, your team's wiki (when set up), and
the download sites during install. It never stores your Google password. Remove its Google access at
[myaccount.google.com/permissions](https://myaccount.google.com/permissions); revoke the Slack token on the app's
OAuth & Permissions page.

**Not measured yet:**

- macOS
- low-end laptops
- two streams on one computer
- the webcast's own delay
- a whole event recorded live (MadTown 2026, 6-8 November, is the first)
