# Setting up YouTube uploads

StreamSplitter can upload every match, your team's own too, to your team's YouTube channel as an **unlisted** video
(only people with the link can see it), in a playlist for the event. You can keep your own matches off YouTube, or
pick public or private, in the setup.

Uploading needs your team's own **Google sign-in app for StreamSplitter**. Google calls it an *OAuth client*: a small
registration in Google Cloud that lets StreamSplitter ask your permission to upload. A team makes it once (about 15
minutes, free); every laptop then uses the same one. The setup (`run.bat`, or `run.bat --section youtube`) walks you
through it one page at a time and opens each page for you. This page is the same steps with more detail. Before you
start, see [requirements-and-limits.md](requirements-and-limits.md).

Google renames buttons now and then; if a name below does not match, look for the closest one. Facts about Google and
YouTube come from [research/youtube.md](research/youtube.md) (checked 2026-11-04); *(unconfirmed)* marks what we
could not check.

## Already set up by a teammate?

Ask them for a **team settings file** (they make it in the setup menu: *Share these settings with a teammate*) and
its passphrase, if it has one. Answer yes when the setup asks "Do you have a team settings file from a teammate?". You then only
log in to YouTube once (unless the file carries the login) and name this event's playlist. Without a file, they can
send you the `client_secret_....json` file from step 5: answer "Yes: I have its client_secret...json file".

## What you need

- **The right Google account.** The account that owns the team's YouTube channel, or an Owner or Manager of its
  Brand Account. People only invited in YouTube Studio ("channel permissions") **cannot** do this: YouTube does not
  let invited users use the YouTube API.
- **Not a school account, usually.** A school (Google Workspace) admin may block outside apps; Advanced Protection
  accounts are blocked too.
- **One project per team.** Other teams should make their own: Google's rules forbid sharing these credentials with
  other groups.

The picture at the top right of each Google page shows which account is in use; click it to switch.

## Step 1: Create a project

A project is a free folder in Google Cloud that holds the sign-in app. Page:
<https://console.cloud.google.com/projectcreate>

1. Name it, for example `StreamSplitter 5026`. Under the name Google shows the **Project ID** (like
   `streamsplitter-5026`). Note it: the setup asks for it, so the next pages open in this project.
2. Click **Create**. No billing account or free trial is needed. On your first visit Google may ask for your
   country and to accept its terms.

If you skip the Project ID, check on each of the next pages that the project picker at the top shows the new
project.

## Step 2: Turn on the YouTube Data API v3

This is how StreamSplitter uploads. Page: <https://console.cloud.google.com/apis/library/youtube.googleapis.com>

Click **Enable**. If it says **Manage**, it is on already.

## Step 3: Describe the sign-in app (Branding)

Page: <https://console.cloud.google.com/auth/branding>

Click **Get started**, then:

1. **App name:** `StreamSplitter`. **User support email:** yours. **Next**.
2. **Audience:** **External**. **Next**.
3. **Contact information:** your email. **Next**.
4. Tick the agreement, **Continue**, then **Create**.

You do not need to add scopes ("Data access"). StreamSplitter asks for one permission when you log in, "Manage your
YouTube account" (`https://www.googleapis.com/auth/youtube`): the smallest one that can upload videos and manage
playlists.

## Step 4: Publish it (Audience)

Page: <https://console.cloud.google.com/auth/audience>

Under **Publishing status**, click **Publish app**, then **Confirm**. It should now say **In production**.

Why: while the app is in **Testing**, Google blocks every account that is not on its tester list ("Error 403:
access_denied ... The app is currently being tested"), and a login stops working 7 days after it was made, which can
be in the middle of an event. You do not need Google's verification: at login you see a "Google hasn't verified this
app" warning instead (step 6). An unverified app can be used by 100 different accounts over the project's life,
plenty for one team.

## Step 5: Create the client and download its file (Clients)

Page: <https://console.cloud.google.com/auth/clients>

1. Click **Create client**.
2. **Application type: Desktop app.** A "Web application" client does not work; the setup tells you if you picked
   it.
3. **Name:** `StreamSplitter`. Click **Create**.
4. In the window that shows, click **Download JSON**. Google offers the file only then: if you closed the window,
   create another client.

The file is named like `client_secret_1234-abc.apps.googleusercontent.com.json`. The setup watches your Downloads
folder and picks it up by itself; if your browser saved it elsewhere, type or paste its path.

Keep this file private: do not post it, email it to other teams, or put it in a shared code repository. StreamSplitter
keeps it in your computer's password store, not in `config.toml`.

## Step 6: Log in to YouTube

The setup opens Google's login page in your browser.

1. **Pick the account** from step 1. Google always shows the account chooser.
2. **Pick the channel.** If the account manages several channels (for example your own and the team's Brand
   Account), pick the **team's channel**. The login belongs to the channel you pick here *(the exact wording of
   this page is unconfirmed)*.
3. **"Google hasn't verified this app"** is expected: it is your team's own app. Click **Advanced**, then **Go to
   StreamSplitter (unsafe)**.
4. Allow the YouTube permission and click **Continue**. The tab says "StreamSplitter is logged in to YouTube".
5. The setup shows the channel it will upload to. Check that it is the team's.

If Google shows an error page instead, the setup cannot see it: type the error (for example `access_denied`) at the
setup's prompt and it shows the fix. StreamSplitter keeps the login in the password store and renews it by itself. It
never stores your Google password.

## Step 7: Playlist, titles and descriptions

The defaults follow the official *FIRST* Robotics Competition channel:

- **Playlist:** `2026 MadTown Throwdown`, created by StreamSplitter with the privacy you choose (unlisted by
  default). Keep it unlisted with unlisted videos: an unlisted video in a *public* playlist can show up in search.
- **Titles:** `Qualification 12 - 2026 MadTown Throwdown`, `Match 7 (R2) - 2026 MadTown Throwdown` (a
  double-elimination playoff match and its round), `Final 1`, `Final 2`, `Final Tiebreaker`, `Practice 3`. A
  replayed match gets `(Replay)`.
- **Description:**

  ```
  Qualification 12 - 2026 MadTown Throwdown
  Red (Teams 5026, 254, 1678) - 112
  Blue (Teams 604, 8033, 199) - 98
  https://www.thebluealliance.com/match/2026mttd_qm12

  Recorded from the event webcast by FRC Team 5026.
  ```

  If the score is not posted yet when the video goes up, ` - 112` is left out and filled in later, once The Blue
  Alliance has it. FIRST's own "Uploaded by MatchLIVE" and copyright lines are left out on purpose: copying them
  would suggest the video is FIRST's.

To change them, write `{field}` where a value goes:

| Field | Example |
|---|---|
| `{year}` | 2026 |
| `{event_name}` | MadTown Throwdown |
| `{event_key}` | 2026mttd |
| `{match_title}` | Qualification 12, Match 7 (R2), Final Tiebreaker, Practice 3 |
| `{level}` | Qualification, Playoff, Final, Practice |
| `{number}` | 12 |
| `{replay}` | Replay (only for a replayed match) |
| `{red}`, `{blue}` | 5026, 254, 1678 |
| `{red_score}`, `{blue_score}` | 112 (once The Blue Alliance posts it) |
| `{match_key}` | 2026mttd_qm12 |
| `{tba_url}` | the match's page on The Blue Alliance |
| `{event_url}` | the event's page on The Blue Alliance |

- **Brackets hide a part until its value is known.** `Red (Teams {red})[ - {red_score}]` reads
  `Red (Teams 5026, 254, 1678)` until the score is posted. Brackets with no `{field}` inside are printed as typed.
- **A line whose value is unknown is left out**, for example the `{tba_url}` line of a practice match.
- **No `<` or `>`.** Titles are at most 100 characters, descriptions 5000 bytes. The setup checks your formats and
  shows a sample before saving.

## Step 8: Test upload

The setup offers to upload a short test video ("StreamSplitter setup test") into the playlist. Then:

1. Open YouTube Studio, **Content**. The test video should say **Unlisted**, not "Private" and not "Locked".
2. The setup deletes the test video.

Do this before the event (see "Locked as private" below).

## Optional: add the videos to The Blue Alliance (event organizers)

After the event, with or without a write key: [tba.md](tba.md). This section is the automatic way, during the
event.

StreamSplitter can add each match's YouTube video to that match's page on The Blue Alliance, a few minutes after the
upload. It is off unless you turn it on: `run.bat --section tbalinks` (Mac/Linux: `sh run.sh --section tbalinks`), or
the menu's Change a setting, "The Blue Alliance video links".

**Only turn it on if you run the event, or its organizers said yes in writing.** Uploading an event's footage to
YouTube before its organizers do can take away their control of how it is used (for example, YouTube's copy detection
may then flag their own uploads), and a link on The Blue Alliance makes your copy the one everyone finds.

- **You need a TBA write key** for the event with match video access. Organizers request one at
  [thebluealliance.com/request/apiwrite](https://www.thebluealliance.com/request/apiwrite); once granted, its Auth ID and
  Auth Secret are on your [TBA account page](https://www.thebluealliance.com/account). The setup checks the key
  against the event (a request that changes nothing) and keeps it in the computer's password store.
- **What gets linked:** every uploaded match TBA has a page for. Not partial clips (perhaps an aborted attempt), not
  practice matches. A playoff match TBA has not listed yet is tried again every 10 minutes for 12 hours. A replayed
  match ends up with both videos on its page. StreamSplitter never removes a link.
- **Private videos** cannot be linked (nobody could watch them), so the option is not offered for them.
- **Per event:** the key is not in the team settings file; set it on each computer, for each event.

## Limits, and what they mean at the event

**"Locked as private": gone since 2026-11-02, but test it.** From 2020 until 2 November 2026, YouTube locked every
video uploaded from a new, unaudited project as private. YouTube has dropped that rule, so unlisted uploads should
work without an audit. The change is new, so the test upload is how you find out. If the test video comes back
private or locked, StreamSplitter says so; then upload the clips by hand in YouTube Studio (15 files at a time,
Unlisted and the playlist set in the upload dialog). Locked videos cannot be unlocked, only uploaded again.

**Daily API quota: about 99 matches a day.** Google gives each project 100 uploads a day, plus 10,000 "units" a day
for everything else (adding a video to the playlist costs 50, filling in the score later 50, small lookups 1). A
match uses 1 upload and about 100 units. An event day has about 30-35 matches.

- The quota is **per project, not per laptop**: laptops using the same project share it. Run only one uploading
  laptop per event, or each match goes up twice.
- It resets at **midnight Pacific time**.
- If it runs out, StreamSplitter tells your contact, keeps every clip queued, and goes on after midnight Pacific.
- `streamsplitter status` shows StreamSplitter's own estimate. Google's count: Cloud console, **APIs & Services**,
  **YouTube Data API v3**, **Quotas**.

**The channel's own daily upload limit (number not published).** Separate from the quota; it varies by channel
*(unconfirmed)*. Verifying the channel by phone at <https://www.youtube.com/verify> raises it: do that before the
event. If the channel hits it, StreamSplitter waits until midnight Pacific and goes on.

**How long the login lasts.** In production: until someone removes it, or 6 months unused. In Testing: 7 days (step
4). When it stops, StreamSplitter says "log in to YouTube again: streamsplitter setup --section youtube"; queued clips
upload once you have.

**Upload size.** A match is 90-130 MB at 1080p, depending on the stream: about 3-4.5 GB on a 35-match day. A
dropped connection or a restart resumes the upload where it stopped.

**Video category.** *Science & Technology* (YouTube's category 28 *(unconfirmed)*), since *FIRST* is a STEM
program.

**The footage belongs to the event.** The clips are the event's webcast. Ask the organisers (for MadTown Throwdown,
team 1323) whether they are happy for you to post them. YouTube may raise copyright (Content ID) claims on venue
music.

## Your data, and YouTube's terms

- StreamSplitter uses the YouTube API Services. By uploading with it you agree to YouTube's Terms of Service
  (<https://www.youtube.com/t/terms>). Google's Privacy Policy covers Google's side:
  <https://policies.google.com/privacy>.
- StreamSplitter stores two things, both on your computer in the password store: the login (a token) and the client
  file from step 5. It sends nothing anywhere except to Google.
- Remove StreamSplitter's access any time at <https://myaccount.google.com/permissions>. To remove its copy of the
  login, run the setup again or delete it from the password store.

## When something goes wrong

| You see | What to do |
|---|---|
| Google: "Error 403: access_denied ... The app is currently being tested" | The app is still in Testing: step 4, **Publish app**. Then log in again. |
| Google: "Error 400: redirect_uri_mismatch", or the setup says the client is a 'Web application' | Create a **Desktop app** client (step 5) and use its file. |
| Google: "Error 401: invalid_client" or "deleted_client" | The client was deleted: create a new one (step 5). |
| Google: "Error 403: org_internal" | The audience is Internal: on the Audience page click **Make external**, then **Publish app**. |
| Google: "Error 400: admin_policy_enforced" | A school or work account's admin blocks outside apps: log in with another account that owns or manages the channel. |
| the YouTube Data API v3 is not enabled | Step 2, in the same project. Wait a minute, then try again. |
| the Google account that logged in has no YouTube channel | You picked an account without a channel: log in again and pick the team's channel (step 6). |
| the logged-in account may not do this on the channel | Log in as the channel's owner or a Brand Account Owner/Manager. A Studio invite is not enough. |
| log in to YouTube again: streamsplitter setup --section youtube | The login expired or was removed: run that command. |
| YouTube's daily API quota ran out | Nothing to do: uploads continue after midnight Pacific. |
| the channel reached its daily upload limit | Nothing to do now: uploads continue after midnight Pacific. Phone-verify the channel for next time. |
| the event playlist is gone | Someone deleted it: run `run.bat --section youtube` to make a new one. |
