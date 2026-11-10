# Setting up Google Drive uploads

StreamSplitter can upload every match, your team's own and everyone else's, into a **Google Drive folder that you
share with the team**. Members watch the videos in the browser or the Google Drive phone app; only people you share
the folder with can see them. It is free, and it works at official *FIRST* events too (nothing is public).

The setup (`run.bat`, or `run.bat --section drive`) does it in a few minutes and opens each page for you. This page is
the same steps with more detail. Before you start, see [requirements-and-limits.md](requirements-and-limits.md).

## How much space it takes

| Video size | One match | A 100-match event | Free account (15 GB) |
|---|---|---|---|
| **720p** (default) | about 40 MB | about 4 GB | about 3 events |
| 540p | about 27 MB | about 2.7 GB | about 5 events |
| As recorded (1080p) | 90-130 MB | about 13 GB | about 1 event |

- A free Google account has **15 GB**, shared with its Gmail and Google Photos. More space is a paid Google One plan
  (<https://one.google.com/storage> shows what is used).
- **The videos use the space of the account that logs in** in step 3, not of the people you share with. A team
  Google account is best, so nobody's personal Drive fills up.
- StreamSplitter makes the smaller copy itself (about 9 seconds per match, at low priority so recording is not
  slowed) and deletes it once uploaded. The full-size clips stay on the computer.
- When less than about one event's worth is left, the alert contact gets a Slack message. When the Drive is full,
  Drive uploads pause and the contact is told (see "At the event").

## Already set up by a teammate?

Ask them for a **team settings file** and its passphrase, if it has one (setup menu: *Share these settings with a
teammate*). Answer
yes when the setup asks "Do you have a team settings file from a teammate?". The file carries the folder, so you only
log in (unless the file carries the login too). **Log in with the same Google account** that made the folder:
StreamSplitter cannot see a folder another account made, so with a different account the setup offers to make a new
folder in that account's Drive.

## Step 1: The Google sign-in app

Google Drive uses your team's **Google sign-in app** (an *OAuth client* in Google Cloud), the same one YouTube uses.

- **YouTube is set up already?** Nothing to do here: go to step 2.
- **No YouTube?** Make the sign-in app as [setup-youtube.md](setup-youtube.md) explains (steps 1, 3, 4 and 5: project,
  branding, publish, client file). In its step 2, turn on the **Google Drive API** instead of the YouTube one (or as
  well). The setup walks you through it either way.

## Step 2: Turn on the Google Drive API

Page: <https://console.cloud.google.com/apis/library/drive.googleapis.com>

Check that the project picker at the top shows the **same project** as the sign-in app (the setup opens the page in
that project when it knows it), then click **Enable**. If it says **Manage**, it is on already.

## Step 3: Log in to Google Drive

The setup opens Google's login page in your browser.

1. **Pick the account whose storage should hold the videos.** Google always shows the account chooser. This can be a
   different account from the YouTube channel's.
2. If Google says it **hasn't verified this app**, that is expected: it is your team's own app. Click **Advanced**,
   then **Go to StreamSplitter (unsafe)**.
3. Allow the Google Drive permission and click **Continue**. The tab says "StreamSplitter is logged in to Google
   Drive".
4. The setup shows the account and its free space. Check that it is the right account.

The permission is Google's narrowest one for Drive (`drive.file`): StreamSplitter can see and change **only the
files and folders it made itself**, nothing else in that Drive. StreamSplitter keeps the login in the computer's
password store and renews it by itself. It never stores your Google password.

## Step 4: The folder

The setup asks for a name (default **FRC match videos**) and makes the folder at the top of that account's
**My Drive**. Each event gets its own folder inside it, named like the YouTube playlist: `2026 MadTown Throwdown`.

- **Let StreamSplitter make the folder.** It cannot upload into a folder a person made (that is the narrow permission
  at work), so there is no "pick an existing folder".
- You may rename or move the folder inside My Drive, and put other files in it. Do not delete it: if you do (or
  someone logs in with another account), uploads stop and the contact is told to run `run.bat --section drive`, which
  makes a new one.
- Google Workspace **shared drives** are not used: the videos always go to the logged-in account's My Drive.

## Step 5: Share it with the team

StreamSplitter never shares anything itself. The setup opens the folder; then:

1. Click the folder's name at the top, then **Share**.
2. Add your team's Google Group, or the members' email addresses, as **Viewer**, and click **Send**.
3. Or, under **General access**, pick **Anyone with the link** (Viewer) and post the link in Slack.

Sharing the top folder shares every event folder inside it, including future ones.

## Step 6: Video size and file names

Pick 720p (recommended), 540p, or as recorded. Then approve the file names. They sort in play order within each
stage:

| Code | Means | Example |
|---|---|---|
| `Q07` | qualification 7 (zero-padded to the event's last qualification, at least two digits) | `Q07 Qualification 7 - 5026 254 1678 vs 971 8 649.mp4` |
| `P03` | double-elimination playoff match 3 | `P03 Match 3 (R1) - 1323 6619 254 vs 5026 5499 114.mp4` |
| `PF1`-`PF3` | the finals | `PF3 Final Tiebreaker - ....mp4` |
| `PR02` | practice match 2 | `PR02 Practice 2 - ....mp4` |
| `QF2-1`, `SF1-2`, `F1` | events before 2023 (set-match) | `QF2-1 Quarterfinal 2 Match 1 - ....mp4` |

Red teams come before "vs", blue after; they are left out until the schedule is known. A replay keeps YouTube's
wording, `(Replay)`; a clip StreamSplitter could not name keeps its file name. Sorted by **Name**, playoffs come
above qualifications. Sorted by **Last modified**, everything is in play order: each video's date is set to when the
match was played.

Each video's description is the YouTube one (the teams, the score, The Blue Alliance link, "Recorded from the event
webcast by FRC Team 5026."). If The Blue Alliance posts the score after the upload, the description is updated.

To change the names, answer no and type a format with `{code}`, `{match_title}`, `{teams}` and the fields listed in
[setup-youtube.md](setup-youtube.md) (step 7). `[brackets]` hide a part until its values are known.

## Step 7: Test upload

The setup offers to upload the bundled test video (2024 MadTown Qualification 6, 12 MB) to the top folder and shows
its link. Open it: a new video can take a few minutes before it plays. Then the setup offers to move it to the
**trash** (Drive empties the trash after 30 days; StreamSplitter never deletes anything for good).

## At the event

| To | Do |
|---|---|
| See uploads and free space | reply `status` to the StreamSplitter app in Slack, or `run.bat status` |
| Hold the Drive uploads (they wait on the computer) | reply `pause drive` |
| Send them again | reply `resume drive` |

- **Drive full.** Uploads pause by themselves and the contact gets one message: "Google Drive is full ... free space
  or log in with another account, then reply `resume drive`". Free space (empty the Drive trash too:
  <https://drive.google.com/drive/trash>), then reply `resume drive`. Nothing is lost meanwhile. To use another
  account instead, run `run.bat --section drive`, log in with it and make a new folder.
- **No internet.** Uploads wait and resume where they stopped, also after a restart.
- **One uploading computer per event.** A second computer logged in to the same account skips matches that are
  already in the event folder (it finds them by the clip's name), but Slack and YouTube would get every match twice.

## Limits

| Limit | Number | What happens |
|---|---|---|
| Storage | 15 GB free per Google account, shared with Gmail and Photos | Low: a message. Full: Drive uploads pause, the contact is told |
| Uploads per account | [750 GB a day](https://developers.google.com/workspace/drive/api/guides/limits) | Far above an event (4-13 GB) |
| Upload per match | 40 MB at 720p: about 16 s at 20 Mbit/s, about 1 min at 5 Mbit/s | Shares the venue's upload with Slack and YouTube |
| File size | 5 TB | - |
| API requests | Google Cloud's per-project limits (far above StreamSplitter's few calls a match) | Waited out and retried |

## Your data

- StreamSplitter stores the Drive login (a token) and uses the sign-in app's file, both in the computer's password
  store, never in `config.toml`. It sends nothing anywhere but Google.
- With `drive.file` it sees only what it made. It never shares, and it never deletes for good.
- Remove its access any time at <https://myaccount.google.com/permissions>. The videos stay in the Drive.

## When something goes wrong

| You see | What to do |
|---|---|
| the Google Drive API is not enabled | Step 2, in the sign-in app's project. Wait a minute, then start StreamSplitter again. |
| Google: "Error 403: access_denied ... The app is currently being tested" | The sign-in app is still in Testing: [setup-youtube.md](setup-youtube.md) step 4, **Publish app**. Then log in again. |
| Google: "Error 400: admin_policy_enforced" | A school or work account's admin blocks outside apps: log in with another account. |
| log in to Google Drive again: streamsplitter setup --section drive | The login expired or was removed: run `run.bat --section drive`. |
| Google Drive cannot find the folder ... | Deleted, or this computer logged in with another account: `run.bat --section drive` makes a new folder (or log in with the account that made it). |
| the Google Drive folder ... is in the trash | Restore it from the Drive trash, or make a new one with `run.bat --section drive`. |
| Google Drive is full | Free space (and empty the trash), then reply `resume drive`. |
| the 720p copies cannot be made | ffmpeg is missing or broken: run the setup again (it installs it). Meanwhile the videos go up as recorded, 3x larger. |

Checked against Google's Drive API documentation on 2026-11-05 (uploads, the `drive.file` permission, storage,
errors, limits). The exact wording of Google's login pages may differ.
