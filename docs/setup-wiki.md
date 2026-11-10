# Setting up the team wiki

StreamSplitter can upload every match video to your team's own wiki (a [MediaWiki](https://www.mediawiki.org)
site): all matches, your team's and everyone else's, at offseason and official events alike. Each event gets one
members-only page with one section per match, so the page's contents list jumps straight to a match.

`run.bat --section wiki` (or the setup menu's *Team website (wiki)*) walks you through this; this page is the same
steps with more detail, plus what your wiki's admin must set up first. Before you start, see
[requirements-and-limits.md](requirements-and-limits.md). Since 2026-11-05 a first setup does not ask about the wiki:
the team keeps the match videos in a shared Google Drive folder instead ([setup-drive.md](setup-drive.md)).

## What StreamSplitter does on the wiki

- **Uploads each match as a file**, named after the match: `2026mttd_qm12.mp4`, a replay `2026mttd_qm12_r2.mp4`.
  The file's page starts with `{{Private}}` and says which match it is.
- **Makes the event's page once**, for example *Madtown Throwdown (2026) match videos*. It starts with
  `{{Private}}` and one line saying what the page is.
- **Adds one section per match at the end of that page**: a heading such as `== Qualification 12 ==` (or *Match 7
  (R2)*, *Final 2*, *Practice 3*, with *(Replay)* or *(Partial)* when that applies), the video, and one line with the
  teams and, when The Blue Alliance has posted it, the score:
  `Red 1323, 6619, 254B - 81 | Blue 5026, 5499, 114 - 63`. Team numbers only, never names.

It never changes what people wrote on the page, never overwrites a file someone else uploaded, and never deletes
anything. If the same video is already on the wiki, it uses that file instead of uploading a second copy.

## What the wiki's admin sets up first (once)

| Setting | Why |
|---|---|
| Uploads on: `$wgEnableUploads = true;` | StreamSplitter uploads files. |
| MP4 allowed: `$wgFileExtensions[] = 'mp4';` | The videos are MP4 files. |
| `$wgMaxUploadSize` above the biggest clip: 300 MB is safe | A match video is 90-130 MB; a long one can reach about 260 MB. |
| PHP `upload_max_filesize` and `post_max_size` at least 16M, and the web server's request limit too (nginx: `client_max_body_size 16m;`) | Videos go up in 10 MB pieces, one request each. StreamSplitter halves the pieces (down to 1 MB) if they are refused, but that is slower. |
| Chunked uploads through the API | MediaWiki supports them as standard; they use the upload stash, so the wiki's temporary file storage must work. |
| In-page playback | Without a video player (TimedMediaHandler, or the wiki's own), the page shows a link to the file instead of a player. |
| A file gate for members-only files | `{{Private}}` hides a page, but a file's direct address can still be opened unless the wiki protects its files (for example `img_auth.php` with a private upload folder). |
| Disk space | About 10 GB per event on the wiki's server (80 matches at 90-130 MB each). |
| Cloudflare (if the wiki is behind it): let `/api.php` through without a browser check | A program cannot pass Cloudflare's browser check or Bot Fight Mode challenge. Cloudflare's 100 MB request limit is not a problem: the pieces are 10 MB. |
| An account for the uploads | A bot account (for example *StreamSplitterBot*) or a person's account. It must be able to see members-only pages, edit, create pages and upload. In the wiki's *bot* group it is not slowed by the wiki's own rate limits. |

## 1. Make a bot password

StreamSplitter logs in with a **bot password**: a separate password for one account that can only do what you
allow. Your own account password is never used.

1. The setup opens the wiki's **Special:BotPasswords** page. Log in there as the account the videos should show as
   uploaded by.
2. Under **Create a new bot password**, type the bot name `StreamSplitter` and click **Create**. (Already listed?
   Click it, then **Reset password**.)
3. Tick these four boxes, then click **Create**:

   | Box | Why |
   |---|---|
   | Edit existing pages | add each match's section to the event page |
   | Create, edit, and move pages | make the event page the first time |
   | Upload new files | upload the match videos |
   | Upload, replace, and move files | upload a new version of a video StreamSplitter uploaded itself (a re-cut match) |

   Leave the rest as it is. StreamSplitter does not need delete rights; if you give them anyway, the setup offers to
   delete its test page afterwards.
4. The wiki shows the **user name** to use (like `YourName@StreamSplitter`) and the **password**. Copy the password
   now: the wiki shows it only once.
5. In the setup, type the user name and paste the password (it stays hidden). The setup logs in to check it and
   tells you if a box is missing.

StreamSplitter keeps the password in the computer's password store (or, where there is none, a file only your user
can read), never in `config.toml`, and never shows it. To set up another laptop, use the team settings file (it
carries the bot password, encrypted). If the password leaks, open Special:BotPasswords, click **StreamSplitter**
and **Delete** or **Reset password**.

## 2. The wiki's address

The setup asks for the wiki's `api.php` address, for example `https://wiki.example.org/w/api.php`. The wiki's own
address works too: the setup looks for `api.php` in the usual places and checks that a MediaWiki answers.

## 3. The event's page

The page for the match videos is named from The Blue Alliance's event name, as *Name (year) match videos*. TBA's
spelling can differ from the wiki's (*MadTown* against *Madtown*), so the setup searches the wiki for the event's own
page (*Madtown Throwdown (2026)*), uses its spelling, and lets you confirm or change the title. When the event
changes, run `run.bat --section wiki` again to pick the new event's page; until then matches go to a page named from
TBA's name.

## 4. Members-only and the video markup

- **Members-only** (the default): `{{Private}}` goes at the top of the event page and of every file page. A wiki
  that uses another template can set `private_markup` in `config.toml`. If a person made the event page first
  without it, StreamSplitter leaves the page alone and tells the alert contact.
- **Video markup**: how each video shows on the page, with `{file}` where the file name goes (and `{title}` for the
  match's title, if wanted). The default `[[File:{file}|800px]]` plays the video on a wiki with TimedMediaHandler.
  Your wiki's admin may give you another, for example `<video>File:{file}</video>`.

## The test upload

The setup's test upload puts the bundled sample match on a page called *StreamSplitter setup test* (members-only,
like the matches). Look at it, then delete the page and its file with the wiki's **Delete** action (the setup does
it for you only if the bot password may delete). Leaving them does no harm.

## config.toml: [wiki]

| Key | Meaning |
|---|---|
| `enabled` | upload every match to the wiki |
| `api_url` | the wiki's api.php address |
| `username` | the bot password's user name (`YourName@StreamSplitter`) |
| `private` | `{{Private}}` at the top of the page and the file pages (default true) |
| `private_markup` | the members-only markup, if not `{{Private}}` |
| `embed_format` | the video markup, with `{file}` (and `{title}`) |
| `page_format` | the page title from the event: `{event_name}`, `{year}`, `{event_key}` (default `{event_name} ({year}) match videos`) |
| `page_title`, `page_event`, `event_page` | this event's page as confirmed in the setup, the event it is for, and the wiki's own event page |
| `version` | `"score"` (the match with its results screen, or the plain match when there is none) or `"match"` |
| `paused` | set by `pause wiki` in Slack; `resume wiki` clears it |

The bot password itself is stored as the secret `wiki_bot_password`.

## While it runs

Uploads queue on the computer and are retried when the internet or the wiki is down, and an upload interrupted part
way continues where it stopped, also after a restart. The alert contact can reply `pause wiki` or `resume wiki` to
the StreamSplitter app in Slack.

## When StreamSplitter reports a wiki problem

| Message says | What to do |
|---|---|
| "the wiki refused the bot login" | The user name or password is wrong, or the bot password was deleted: step 1, then `run.bat --section wiki`. |
| "wants a bot password, not the account's own password" | Use the `Name@StreamSplitter` user name and the bot password from step 1. |
| "the bot password lacks a permission it needs" | On Special:BotPasswords click **StreamSplitter**, tick the boxes in step 1, click **Update**. If they are ticked, the account itself may not do it: ask the wiki's admin. |
| "does not accept .mp4 files", "uploads are switched off", "refuses even 1 MB pieces" | The wiki's admin: see *What the wiki's admin sets up first*. |
| "Cloudflare ... asks StreamSplitter for a browser check" | The wiki's admin lets `/api.php` through in Cloudflare. |
| "a different file called ... uploaded by ..." | Someone uploaded another file with that match's name. Rename or delete it on the wiki, then restart StreamSplitter. |
| "has no {{Private}} at the top" | Someone made the event page without it. Add `{{Private}}` on its first line if it should be members-only. |
| "no connection", "the wiki asks to wait", "trying again later" | Nothing to do: the video waits and goes up when the wiki can be reached. You get a DM if it keeps failing for 10 minutes. |
