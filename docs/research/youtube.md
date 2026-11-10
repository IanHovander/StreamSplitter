# YouTube uploads for StreamSplitter: API feasibility and FRC channel conventions

Researched 2026-11-04 (Wednesday) for MadTown Throwdown 2026 (2026mttd, Fri-Sun 2026-11-06..08). Primary pages were
fetched live on 2026-11-04; most Google pages carry "Last updated 2026-11-03 UTC". Google developer-site text
(developers.google.com) is quoted verbatim under its CC BY 4.0 licence; Help Center and YouTube Terms text is
paraphrased with short quotes only. Anything I could not confirm from a primary source is marked **unconfirmed**.

## Bottom line for Friday

**Automated unlisted uploads can work by Friday.** Two days ago (2026-11-02) YouTube reversed the 2020 rule that
locked uploads from unaudited API projects to private. The live `videos.insert` reference now says: "Videos uploaded
from unverified API projects are not restricted to private viewing mode." So no audit is needed, and the default
quota (100 uploads per project per day) covers the ~30-35 matches a day the event needs.

What has to be true, and what to do now:

1. **Smoke-test the reversal on Wednesday or Thursday, before the event.** The change is two days old. Search
   engines and third-party guides still describe the old lock (a web search on 2026-11-04 returned only the old
   rule). Upload one short test clip with `status.privacyStatus = "unlisted"` from the real project to the real
   channel. Then check that `videos.list(part=status)` returns `"unlisted"` and that Studio shows Unlisted with no
   "Locked" or "Private" restriction. If the clip comes back locked, use the fallback below.
2. **Always send `privacyStatus` explicitly.** Since the change, an upload with no privacy status "defaults to public".
   Create the playlist as unlisted too: an unlisted video in a *public* playlist can show up in search (YouTube Help,
   privacy settings).
3. **OAuth:** use a Desktop-app client in one Google Cloud project owned by team 5026, External user type. Pick one
   publishing status:
   - **In production, unverified (recommended).** Users click through a "Google hasn't verified this app" warning.
     There is a lifetime cap of 100 users, and refresh tokens are *not* subject to the 7-day expiry.
   - **Testing.** Every account must be on the test-user list (100 max), and **tokens die 7 days after consent**. A
     login on Thursday lasts through Sunday; a login from last week dies mid-event.
4. **Who logs in:** the Google account that owns the team channel, or an Owner/Manager of its Brand Account. People
   invited only through Studio "channel permissions" cannot use the YouTube APIs.

**If the smoke test shows locked-private videos, the best legitimate fallback** is to keep StreamSplitter
recording, cutting and queueing the clips, with titles and descriptions pre-rendered beside each file, and have a
person upload them in YouTube Studio's web uploader. It takes up to 15 files at a time, and visibility and playlist
can be set in the upload dialog. An audit cannot be done by Friday (no published turnaround). Videos that are
already locked cannot be appealed or bulk-switched in Studio; they must be re-uploaded. Driving Studio with a browser
robot is against the YouTube Terms of Service (see A.7).

Other risks to handle (not blockers):

- **Channel daily upload limit.** This is separate from API quota and not published. Phone-verify the channel,
  handle `uploadLimitExceeded`, and space the uploads out.
- **Footage rights.** The clips are the event broadcaster's footage. Ask MadTown/1323 for permission; Content ID
  claims are possible. Not researched further.
- **Venue upload bandwidth.** About 90 MB × 35 ≈ 3.2 GB a day, roughly 9-10 GB for the weekend.

## Key numbers

| Item | Value | Source |
|---|---|---|
| Private lock for unaudited projects | **Lifted 2026-11-02** ("no longer restricted to private viewing mode") | revision history, videos.insert |
| Default privacy if unset | **public** | videos.insert `status.privacyStatus` |
| `videos.insert` cost | **1 unit in its own "Video Uploads" bucket, 100 calls/day/project** (since 2026-06-01; was ~1600 units, then ~100) | quota calculator, revision history |
| Uploads/day on default quota | **100 per project** (all laptops sharing the project share it) | quota calculator |
| General bucket | 10,000 units/day for every other method; resets at midnight Pacific | quota calculator |
| `playlists.insert` / `playlistItems.insert` / `videos.update` / `videos.delete` | 50 / 50 / 50 / 50 units | quota calculator |
| `videos.list`, `playlists.list`, `playlistItems.list`, `channels.list` | 1 unit each | quota calculator |
| `thumbnails.set` | 50 units | quota calculator |
| Per uploaded match (insert + playlist add + one later description update + status check) | 1 upload call + ~101 general units, so the general bucket allows ~99/day and the upload bucket 100/day | derived |
| Testing-mode refresh token | **expires 7 days** after consent | OAuth 2.0 overview; Cloud "Manage app audience" |
| Testing-mode test users | max 100 | Cloud "Manage app audience" |
| Unverified In-production cap | 100 new users over the project's lifetime | Cloud "Manage app audience" |
| Refresh tokens per Google Account per client ID | 100 (the 101st silently kills the oldest) | OAuth 2.0 overview |
| Unused refresh token | dies after 6 months unused | OAuth 2.0 overview |
| Max upload | 256 GB, `video/*` or `application/octet-stream` | videos.insert |
| Title / description limits | 100 characters / 5000 bytes, no `<` `>` | Required Minimum Functionality |
| Video length without phone verification | 15 minutes (clips are ~160 s, fine) | YouTube Help 71673 |

---

## A. YouTube Data API v3 feasibility

### A.1 The private lock: current wording, unlisted, existing videos, exemptions

**The current state (2026-11-02 entry, verbatim, YouTube Data API revision history):**

> Videos uploaded through the videos.insert method from unverified API projects are no longer restricted to
> private viewing mode. This change reverses the policy announced on July 28, 2020, which restricted videos uploaded
> from unverified API projects created after that date to private viewing mode until the project completed an audit.
>
> If you don't specify a value for the status.privacyStatus property in a videos.insert request, the uploaded
> video's privacy status defaults to public. Certain uploads might default to private based on creator account
> settings, as explained in the YouTube Help Center.

The `videos.insert` reference (Last updated 2026-11-03) has the same rule in the `status.privacyStatus` property:
"If you don't specify a value for this property, the uploaded video's privacy status defaults to public. Certain
uploads might default to private based on creator account settings, as explained in the YouTube Help Center. Videos
uploaded from unverified API projects are not restricted to private viewing mode."

**The old rule, for context (2020-07-28 entry, verbatim):** "All videos uploaded via the videos.insert endpoint from
unverified API projects created after 28 July 2020 will be restricted to private viewing mode. To lift this
restriction, each project must undergo an audit to verify compliance with the Terms of Service."

Answers to the specific questions:

- **Is unlisted also blocked?** It is no longer blocked. Under the old rule "private viewing mode" meant the video was
  locked private, so unlisted was impossible too. Now `unlisted` is a normal value; the uploader guide lists the valid
  values as `public`, `private` and `unlisted`.
- **"Unverified" here means unaudited.** This is the YouTube API compliance audit, which is not the same as Google
  OAuth app verification. The two are separate processes; an OAuth-unverified app is a different matter (A.4).
- **Can the lock be lifted on videos already uploaded and locked?** The 2026-11-02 entry does not say, so that is
  **unconfirmed**. YouTube Help "Videos locked as private" (answer 7300965) says such videos cannot be appealed: they
  must be re-uploaded through a verified API service or the YouTube app/site. Plan on re-uploading. This only
  matters if the smoke test fails.
- **Exemptions under the old rule?** None was documented. The 2020 rule covered "all videos" from post-2020-07-28
  unaudited projects, with no carve-out for a project owned by the channel's own account, for Testing mode, or for
  small personal use. Only projects created before 2020-07-28 were spared. This no longer matters.
- **The "creator account settings" default (YouTube Help 157177):** creators aged 13-17 default to private, and adults
  default to public; anyone can change the setting. If the channel owner is a minor, set `unlisted` explicitly (we do
  anyway). Whether a 13-17 owner can set unlisted through the API is **unconfirmed**; the Help page says everyone can
  change the setting.

### A.2 The API compliance audit

- **What it is for now.** Per "Quota and Compliance Audits" (Last updated 2026-11-03), you must complete an audit
  before you can get more than the default quota. The private-lock reason is gone. Verbatim: "If you would like to
  request additional quota beyond the default allocation, you must first complete an audit to show that your project
  is in compliance with the YouTube API Services Terms of Service."
- **How to apply:** submit the "YouTube API Services - Audit and Quota Extension Form",
  https://support.google.com/youtube/contact/yt_api_form. Projects audited in the last 12 months use the same form for
  further extensions. There are also an Appeals form, a Periodic Audit form and a Change of Control form.
- **What the form asks for (read 2026-11-04):**
  - organisation legal name, address and contacts, or "self" for an individual
  - business model and monetisation
  - API client name, access URL, privacy-policy URL and demo credentials
  - project numbers and use case ("Video Uploading & Account Management")
  - required evidence: privacy-policy screenshots (YouTube sections, Google Privacy Policy link, deletion policy), a
    homepage screenshot, and Terms of Service documentation
  - for upload tools, "a complete, working upload script (not a code snippet)" or an end-to-end screencast of the
    upload flow
  - quota per endpoint; `videos.insert` and `search.list` are requested separately from the general bucket
- **Turnaround:** not published ("A member of YouTube's API Services team will contact you as soon as possible").
  Third-party reports range from days to months (**unconfirmed**).
- **By Friday?** No realistic way, and it is not needed: the lock is gone and the default quota is enough.

### A.3 Quota

Default allocation, verbatim from the Quota Calculator and Getting Started pages: "Projects that enable the YouTube
Data API have a default quota allocation of 100 search.list calls, 100 videos.insert calls, and 10,000 units per day
combined for all other endpoints. ... Daily quotas reset at midnight Pacific Time (PT)." Also: "The search.list and
videos.insert methods have their own quota buckets. Each of these methods has a default daily limit of 100 per day.
The quota cost is 1 per call."

How the cost of an upload changed:

- 2025-12-04: "a change in the quota cost of a video upload from approximately 1600 units to approximately 100 units"
- 2026-06-01: "API calls to the videos.insert and search.list methods will be charged to their own respective quota
  buckets."

Costs for the methods StreamSplitter needs (Quota Calculator, 2026-11-03):

| Method | Cost |
|---|---|
| videos.insert | 1 per call, own bucket, 100/day |
| videos.update | 50 |
| videos.delete | 50 |
| videos.list | 1 |
| playlists.insert | 50 |
| playlists.list | 1 |
| playlistItems.insert | 50 |
| playlistItems.list | 1 |
| channels.list | 1 |
| thumbnails.set | 50 |

**Uploads per day on the default quota: 100 per project.** The general bucket is not the binding limit. Each match
needs `playlistItems.insert` (50) plus an optional later `videos.update` to add the TBA score (50) plus a few
1-unit reads, so 10,000 units covers ~99 matches with updates or ~196 without. The event needs ~27-35 a day.

Notes for the design:

- **Quota is per Cloud project.** Every laptop using the team's project draws from the same 100 uploads/day. If two
  laptops run at once, de-duplicate before uploading: check the playlist with `playlistItems.list` (1 unit) or keep
  one uploader.
- **Errors.** `quotaExceeded` (403) means wait for midnight PT. `uploadLimitExceeded` (400) is "The channel's daily
  video upload limit has been reached. This is a YouTube platform restriction and is entirely separate from your
  Google Cloud project's API quota." Treat both as long waits, not 15-second retries.
- **Channel daily upload limit.** The number is not published (**unconfirmed**; third-party sources say it varies
  by channel). YouTube Help 9891124 lists "higher daily upload limits" among *advanced* features: phone verification
  plus channel history or ID/video verification. The same page says repeatedly uploading content you don't own can
  delay or restrict feature access.
- **Requesting more quota:** use the same Audit and Quota Extension Form (A.2). Since 2026-06-01 extra quota is
  requested per method bucket ("This update simplifies the path to quota increases..."). Turnaround is not published.
- **Inactive projects.** Developer Policies say YouTube may cut quota or credentials after 90 consecutive inactive
  days. Re-check before next season.

### A.4 OAuth for an installed desktop app

**Client type and redirect** ("OAuth 2.0 for Desktop Apps", developers.google.com):

- Create a client of type **Desktop app**. "Desktop apps are public clients and cannot keep secrets confidential.
  Always use PKCE (Proof Key for Code Exchange) when requesting authorization codes."
- Redirect to a **loopback** address, `http://127.0.0.1:port` or `http://[::1]:port`, on "a random available port".
  The out-of-band (OOB) copy/paste flow "has been deprecated and is no longer supported".
- "Note that refresh tokens are always returned for installed applications."
- "Incremental authorization is not supported for installed apps or devices": request every scope in the first
  login.
- In Python, `google-auth-oauthlib`'s `InstalledAppFlow.run_local_server()` implements this loopback flow. That is a
  library detail and was not re-verified here.

**Scopes needed.** From each method's Authorization section:

| Method | Accepted scopes |
|---|---|
| videos.insert | youtube.upload, youtube, youtubepartner, youtube.force-ssl |
| playlists.insert, playlistItems.insert, videos.update | youtube, youtube.force-ssl, youtubepartner |

`youtube.upload` alone cannot create playlists or add to them. The smallest single scope that covers upload,
playlist, update and delete is **`https://www.googleapis.com/auth/youtube`** ("Manage your YouTube account").
`youtube.force-ssl` ("See, edit, and permanently delete your YouTube videos, ratings, comments, and captions") also
works. The Developer Policies require the narrowest scopes for features the client actually has.

**Sensitive vs restricted:**

- Google's "Restricted Scopes" list (Cloud Help 13464325) includes **no** YouTube Data API scope. Its only YouTube
  entries are the Data Portability scopes (`dataportability.youtube.*`, `dataportability.myactivity.youtube`).
- So `youtube`, `youtube.upload`, `youtube.force-ssl` and `youtube.readonly` are not restricted. That they are
  classified *sensitive* comes from secondary sources and the fact that they trigger the unverified-app screen. I
  found no public Google page that lists them, so this is **unconfirmed** from a primary list. The Cloud Console
  shows each scope's category when it is added (Cloud Help 13463073).

**Testing vs In production (External user type).** From Cloud Help "Manage App Audience" (15549945), paraphrased with
short quotes:

- **Testing.** Only listed test users can authorise, "up to 100 test users". They see a warning that the app is
  unverified. "Authorizations by a test user will expire seven days from the time of consent", and so do the refresh
  tokens. A Brand Account can authorise if a listed test user manages it.
- **In production.** Any Google account can authorise. Unverified sensitive or restricted scopes show the "unverified
  app" warning, with a user cap of "100 new users in total". The cap "applies over the entire lifetime of the
  project, and it cannot be reset".
- **The OAuth 2.0 overview (developers.google.com), verbatim:** "A Google Cloud Platform project with an OAuth
  consent screen configured for an external user type and a publishing status of "Testing" is issued a refresh token
  expiring in 7 days, unless the only OAuth scopes requested are a subset of name, email address, and user profile".
  It also says: "There is currently a limit of 100 refresh tokens per Google Account per OAuth 2.0 client ID. If the
  limit is reached, creating a new refresh token automatically invalidates the oldest refresh token without
  warning." A refresh token also dies after six months unused, on revocation, and in a few other cases.
- **Is it allowed to stay unverified?** Yes, under Cloud Help "When is verification not needed" (13464323). Personal
  use apps with fewer than 100 users can be used without verification, and users may click through the
  unverified-app warnings. Verification itself needs a homepage and privacy policy on a verified domain and a
  sensitive-scope review, and can take a long time (the Cloud Help "Unverified apps" page says "several months" for
  sensitive data). It is not needed for one team.
- **Accounts that may be blocked:** Workspace (school) accounts whose admin restricts third-party apps, and accounts
  in Advanced Protection (Cloud Help 15549945). If the team channel sits under a school Workspace account, check this
  first.

**One project for team 5026 or one per team?**

- **Team 5026's members: one project is fine.** The project is the API client's registration, and each member's
  login is a *user* of that client. In Testing every member's Google account must be listed; In production anyone can
  log in, up to the 100-user lifetime cap. What matters is which account authorises (A.5), not which laptop.
- **Other teams: each should create its own project and paste in its own client JSON.** This is my reading of
  Developer Policies III.D.1.c, an interpretation and **unconfirmed** by YouTube. The policy says: "You may share
  your API Credentials with agents operating solely on your behalf and under a written duty of confidentiality.
  However, you must not share or disclose your API Credentials to any other third party, allow access to or use of
  your API Credentials by any other third party, or embed your API Credentials in open source projects." It also
  says "you must create exactly one (1) API Project for that API Client".
- **Practical rule:** never commit the OAuth client JSON. The design already keeps `youtube_client` as a secret.
  Hand it to team 5026 members privately, and make the wizard accept a client JSON so other teams can bring their
  own.

**Policy items the wizard must satisfy** (Developer Policies and Required Minimum Functionality):

- Display a link to the YouTube Terms of Service (https://www.youtube.com/t/terms) and state that users agree to be
  bound by them.
- Have users agree to a privacy policy that:
  - says the tool uses YouTube API Services
  - links the Google Privacy Policy (http://www.google.com/policies/privacy)
  - says what data is stored: the token, kept locally
  - says access can be revoked at https://security.google.com/settings/security/permissions
- Get express consent before automated uploads: "you must not automate or trigger views, uploads, comments, likes,
  dislikes, or other actions without the user's prior specific and express consent". The wizard should state "every
  non-5026 match will be uploaded unlisted to <channel> into <playlist>" and get a yes.
- RMF for uploads: users must be able to set `snippet.title` (≤100 chars), `snippet.description` (≤5000 bytes) and
  `status.privacyStatus` (public/private/unlisted). The editable formats and `youtube.privacy` cover this.
- Never collect or store the user's YouTube/Google password; the browser OAuth flow handles login.

### A.5 Choosing the channel when an account has several channels or Brand Accounts

- **The token is bound to a channel chosen during consent.** This is documented only indirectly. After choosing the
  Google account, a user who manages Brand Accounts gets a second screen to pick the personal channel or a Brand
  Account, and the resulting token acts as that channel. The screen's existence and wording come from community
  reports (Stack Overflow, gists), so the exact UI is **unconfirmed**. I found no developers.google.com page that
  describes it.
- **Google's primary statements:**
  - Cloud Help 15549945: "A Brand Account may authorize scopes requested by your project's OAuth clients if a
    specified test user manages the Brand Account." In Testing, list the *person's* Google account, not the Brand
    Account.
  - YouTube Help 9481328, channel permissions: invited users cannot access "YouTube APIs". Someone who only has
    Studio channel permissions (Manager, Editor, ...) cannot authorise the API for that channel. They must be the
    channel owner, or Owner/Manager of the Brand Account at the Google-account level.
- **What the wizard should do:**
  - After login, call `channels.list(part=snippet, mine=true)` (1 unit). The `mine` parameter "instruct[s] the API to
    only return channels owned by the authenticated user".
  - Show the channel title and URL and ask "Upload to this channel?".
  - If wrong, revoke the token and log in again with `prompt=select_account`. That parameter value is documented in
    Google's OAuth guides ("Prompt the user to select an account"); I did not separately confirm that it forces the
    Brand Account picker (**unconfirmed**).
- `onBehalfOfContentOwner` / `onBehalfOfContentOwnerChannel` are only for YouTube CMS content partners. They do not
  apply here.

### A.6 Resumable uploads

From "Resumable Uploads" (developers.google.com/youtube/v3/guides/using_resumable_upload_protocol, 2026-11-03).

**1. Start the session.**

- `POST https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status`
- Headers: `Authorization`, `Content-Type: application/json; charset=UTF-8`, `X-Upload-Content-Length: <file
  bytes>`, `X-Upload-Content-Type: video/*` (or `application/octet-stream`).
- Body: the video resource (title, description, categoryId, `privacyStatus: "unlisted"`).

**2. Save the session URI.** A `200 OK` returns a `Location` header holding the session URI. **Persist it at once,
with the file path and size**, so it survives a process restart. The design's `state["upload_uri"]` does this.

**3. Upload.** `PUT <session URI>` with the bytes.

**4. Finish.**

- `201 Created` means done, and the body is the video resource: record `id`.
- Retryable: lost connection, or **500, 502, 503, 504** ("Your code should use an exponential backoff strategy when
  resuming uploads after receiving any of these response codes").
- Permanent: any other 4xx, or any other 5xx.

**Resuming, including after a restart:**

- Send an empty `PUT <session URI>` with `Content-Range: bytes */<total>`.
- `308 Resume Incomplete` with `Range: bytes=0-N` means continue from byte N+1. No `Range` header means nothing has
  arrived yet. "If the API response also includes the Retry-After header, use that header's value".
- If the upload had already finished, the API "will return the same response that it sent when the upload originally
  completed". This is how a restart avoids a duplicate video: query the status first, never re-POST blindly.
- `404` means the session expired. Start a new session from byte 0.
- The next PUT must begin exactly at the next byte; overlapping or skipped bytes are rejected.

**Session URI lifetime.** The YouTube guide only says each session URI "has a finite lifetime and eventually
expires" and advises resuming "shortly after the interruption". It gives no number, so the YouTube figure is
**unconfirmed**. Analogues on other Google upload APIs: Drive ("A resumable session URI expires after one week") and
Cloud Storage ("A session URI expires after one week"). Do not rely on more than about a day for YouTube; handle 404
by restarting.

**Chunk size.**

- Chunking is "rarely necessary and is actually discouraged" but "might be useful if you are trying to display a
  progress indicator on a very unstable network".
- If you chunk, each chunk must be a multiple of 256 KB, all the same size except the last; "larger chunks are more
  efficient".
- The official Python sample uses `MediaFileUpload(file, chunksize=-1, resumable=True)`, one request for the whole
  file, which "will still be retried where it left off".
- For ~90 MB over venue Wi-Fi, whole-file is fine. 8 MiB chunks (32 × 256 KB) give progress reporting at small cost;
  that is my suggestion, not Google's.

**Backoff guidance (official sample):**

- `RETRIABLE_STATUS_CODES = [500, 502, 503, 504]`, plus transport errors (`HttpLib2Error`, `IOError`, ...).
- `MAX_RETRIES = 10`; `sleep_seconds = random.random() * 2 ** retry`.
- The Outbox's "retry forever" wrapper can sit on top of that.
- Special cases: 401 means refresh the token; `quotaExceeded` means wait for midnight PT; `uploadLimitExceeded`
  means wait hours.
- `videos.update` gotcha: a `snippet` update must include both `snippet.title` and `snippet.categoryId`, or it fails
  with `invalidVideoMetadata` (errors reference). The videos.update reference says any field left out of an updated
  part is reset. For example, sending `status` without `privacyStatus` means the video "will revert to the default
  privacy setting", now public. So when filling in the score later, update only `snippet`, and send title, description and categoryId
  together.

Other upload parameters worth setting:

- `notifySubscribers=false`. The default is true; the reference suggests false for channels uploading many videos.
- `status.selfDeclaredMadeForKids=false`, set explicitly. It is an audience designation; recommended, not
  researched further.
- `snippet.categoryId`: 28 "Science & Technology" or 17 "Sports". The ID values are from my knowledge; check them
  with `videoCategories.list`.

### A.7 Fallbacks if uploads still come back locked, and the YouTube Terms on automating Studio

- **Upload private, then flip in Studio?** This only works for ordinary private videos. Studio bulk edit (Help
  6249768: Content, tick videos, Edit, Visibility) changes visibility for many videos at once. A video *locked*
  private for unverified-API upload cannot be appealed or changed and must be re-uploaded (Help 7300965). So no, not
  for locked videos.
- **Manual upload in YouTube Studio (best fallback).**
  - The web uploader takes "up to 15 videos at a time" (Help 57407), and the upload dialog sets Visibility (Unlisted)
    and Playlist.
  - StreamSplitter keeps cutting the clips and writes the rendered title and description into a sidecar `.txt` per
    clip, or one `uploads.csv`, so a person can paste them.
  - This costs no API quota and is not affected by any API lock.
- **Audit by Friday:** not realistic (A.2).
- **Ordinary private upload, then `videos.update` to unlisted:** only possible if the video is not locked, which is
  the normal case now.
- **YouTube Terms of Service on automation** (https://www.youtube.com/t/terms, dated 2023-12-15).
  - Under "Permissions and Restrictions", you may not "access the Service using any automated means (such as robots,
    botnets or scrapers)". The only exceptions are public search engines following YouTube's robots.txt, or
    YouTube's prior written permission.
  - Scripting Studio through a browser (Selenium, Playwright, etc.) is therefore not permitted without YouTube's
    written permission. Not recommended; reported only.
- **Related terms, for the footage question.** The ToS "Uploading Content" section says submitted content must not
  include third-party intellectual property without permission. Help 9891124 lists "repeatedly uploading content you
  don't own" among actions that restrict feature access.

---

## B. FIRST Robotics Competition YouTube conventions

Source: the official channel **FIRSTRoboticsCompetition** (`@FIRSTRoboticsCompetition`, channel id
`UCHA7zyPRS3VPDvl4ehss-zw`, ~74K videos), read from its playlist and video pages on 2026-11-04. Every match video
I opened carries the credit line "Uploaded by MatchLIVE from JK Productions": FIRST's match clips are produced by
the MatchLIVE service. All videos are 189-215 s long.

### B.1 Playlist names (verbatim)

Format: `<year> <event name as listed by FIRST>`, sponsor suffix included when the event has one:

- `2025 Silicon Valley Regional` (PLZT9pIgNOV6be_lj4SeSFut3juyhyLyCi)
- `2024 Silicon Valley Regional` (PLZT9pIgNOV6ZyAD70UYuQ_rjWWV58MjIe)
- `2026 CA District Silicon Valley Event presented by Apple` (PLZT9pIgNOV6bRyxgbzpKYOgC23QfKAkJP)
- `2026 CA District San Gabriel Valley Event`, `2026 New England FIRST District Championship`
- `2026 FIRST Championship - FIRST Robotics Competition - Johnson Division presented by Gene Haas Foundation`. This is
  the match playlist; a separate `... - Johnson Division` playlist holds the full-day streams, titled `... - Day 1`
  to `Day 4`.
- Others from 2025: `2025 Utah Regional`, `2025 Hawaii Regional`, `2025 Regional Monterrey presented by PrepaTec`.

### B.2 Video titles (verbatim)

Format: `<match label> - <event>`. In 2024-2025 the event part includes the year (`2025 Silicon Valley Regional`).
In 2026 the year is dropped from titles but kept in playlist names (`CA District Silicon Valley Event presented by
Apple`).

| Level | Label | Examples |
|---|---|---|
| Qualification | `Qualification N` | `Qualification 1 - 2025 Silicon Valley Regional`; `Qualification 19 - 2024 Silicon Valley Regional`; `Qualification 1 - CA District Silicon Valley Event presented by Apple` |
| Double-elimination playoff (matches 1-13) | `Match N (Rk)` | `Match 1 (R1) - 2025 Silicon Valley Regional`; `Match 10 (R3) - 2024 Silicon Valley Regional`; `Match 13 (R5) - CA District San Gabriel Valley Event` |
| Finals | `Final 1`, `Final 2`, `Final Tiebreaker` | `Final 1 - 2025 Silicon Valley Regional`; `Final Tiebreaker - 2024 Silicon Valley Regional`; `Final 2 - CA District Silicon Valley Event presented by Apple` |
| Championship (for reference) | `Einstein N`, `Einstein Final 1` | `Einstein Final Tiebreaker - FIRST Championship - FIRST Robotics Competition`; `Qualification 1 - FIRST Championship - FIRST Robotics Competition - Johnson Division` |

The round numbers seen in titles: matches 1-4 = R1, 5-8 = R2, 9-10 = R3, 11-12 = R4, 13 = R5.

Finals in the FRC Events links are playoff matches 14, 15 and 16. Examples:
`Final 1 - 2025 Silicon Valley Regional` links `/2025/CASJ/playoffs/14`; `Final Tiebreaker - 2024 Silicon Valley
Regional` links `/2024/CASJ/playoffs/16`.

Mapping to TBA match keys, checked on TBA pages for 2025casj:

| TBA key | FIRST title |
|---|---|
| `2025casj_sf1m1` (TBA "Playoff Match 1") | `Match 1 (R1)`, same video Cnp5dYYCkoU |
| `2025casj_sf13m1` | `Match 13 (R5)`, video 2WGAGla1_ZY |
| `2025casj_f1m1` (TBA "Finals 1") | `Final 1`, video KUyodlPE3k4 |

So `qmN` becomes `Qualification N`, `sfNm1` becomes `Match N (R<round of N>)`, and `f1m1`, `f1m2`, `f1m3` become
`Final 1`, `Final 2`, `Final Tiebreaker`.

### B.3 Descriptions

The structure is identical across 2024, 2025 and 2026:

- the title repeated
- Red alliance team numbers and score
- Blue alliance team numbers and score
- the FRC Events match URL
- a blank line, the MatchLIVE credit, and a copyright line

Verbatim example (video EaRs_WdhNpM, published 2025-03-15):

```
Qualification 1 - 2025 Silicon Valley Regional
Red (Teams 6059, 10059, 668) - 23
Blue (Teams 649, 1351, 6418) - 78
https://frc-events.firstinspires.org/2025/CASJ/Qualification/1

Uploaded by MatchLIVE from JK Productions
(c) 2025 FIRST Robotics Competition
```

A playoff example has the same shape. Video E2xgukqCSyc, 2024, with team 5026 on Blue: first line
`Match 10 (R3) - 2024 Silicon Valley Regional`, then `Red (Teams 1967, 1868, 1351) - 50`,
`Blue (Teams 2813, 5026, 668) - 68` and `https://frc-events.firstinspires.org/2024/CASJ/playoffs/10`. The URL path
segment changed from `playoffs` (2024-2025) to `Playoff` (2026), for example
`https://frc-events.firstinspires.org/2026/CASNV/Playoff/15`.

### B.4 Suggested defaults for 2026mttd

These follow the convention above. The event name comes from TBA at run time: TBA's page title for 2026mttd reads
"MadTown Throwdown (2026)", and for 2025mttd "Madtown Throwdown (2025)".

- **Playlist:** `2026 MadTown Throwdown`, privacy **unlisted**.
- **Titles** (2024-2025 style, with the year, which is clearer on a team channel that holds several seasons):
  - `Qualification 12 - 2026 MadTown Throwdown`
  - `Match 5 (R2) - 2026 MadTown Throwdown`
  - `Final 1 - 2026 MadTown Throwdown`
  - `Final Tiebreaker - 2026 MadTown Throwdown`
- **Description:** the first four lines in FIRST's shape, linking TBA instead of FRC Events, since FRC Events does
  not carry offseason match data (**unconfirmed** for 2026mttd):

  ```
  Qualification 12 - 2026 MadTown Throwdown
  Red (Teams 1323, 254, 649) - 123
  Blue (Teams 1678, 581, 6418) - 98
  https://www.thebluealliance.com/match/2026mttd_qm12
  ```

  - Leave out the score part (` - 123`) until TBA posts it, then fill it with one `videos.update` (50 units).
  - **Do not copy** the "Uploaded by MatchLIVE from JK Productions" and "(c) 2026 FIRST Robotics Competition"
    lines. They would misattribute the upload and imply it is FIRST's. Use an honest credit instead, for example
    `Recorded from the event webcast by FRC Team 5026`, plus the stream source if the organisers ask for it.
  - The team numbers above are placeholders.

---

## Sources

YouTube Data API and policies (developers.google.com; fetched 2026-11-04):
- Revision history (2026-11-02 lock reversal; 2026-06-01 quota buckets; 2025-12-04 cost change; 2020-07-28 lock): https://developers.google.com/youtube/v3/revision_history
- Videos: insert: https://developers.google.com/youtube/v3/docs/videos/insert
- Videos: update: https://developers.google.com/youtube/v3/docs/videos/update
- Playlists: insert: https://developers.google.com/youtube/v3/docs/playlists/insert
- PlaylistItems: insert: https://developers.google.com/youtube/v3/docs/playlistItems/insert
- Channels: list: https://developers.google.com/youtube/v3/docs/channels/list
- Quota Calculator: https://developers.google.com/youtube/v3/determine_quota_cost
- Getting started (quota usage): https://developers.google.com/youtube/v3/getting-started
- Quota and Compliance Audits: https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits
- Audit and Quota Extension Form: https://support.google.com/youtube/contact/yt_api_form
- Errors (uploadLimitExceeded, quotaExceeded, invalidVideoMetadata): https://developers.google.com/youtube/v3/docs/errors
- Resumable uploads: https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol
- Uploading a video (Python sample, retry/backoff, chunksize): https://developers.google.com/youtube/v3/guides/uploading_a_video
- Authentication / scopes: https://developers.google.com/youtube/v3/guides/authentication
- Developer Policies: https://developers.google.com/youtube/terms/developer-policies
- Required Minimum Functionality: https://developers.google.com/youtube/terms/required-minimum-functionality
- API Services terms revision history: https://developers.google.com/youtube/terms/revision-history

Google OAuth and Cloud:
- OAuth 2.0 for Desktop Apps: https://developers.google.com/identity/protocols/oauth2/native-app (served at https://developers.google.com/youtube/v3/guides/auth/installed-apps)
- OAuth 2.0 overview (refresh-token expiration, 7-day Testing rule, 100 tokens/client): https://developers.google.com/identity/protocols/oauth2
- Manage App Audience (Testing/In production, test users, user cap, Brand Accounts): https://support.google.com/cloud/answer/15549945
- Unverified apps: https://support.google.com/cloud/answer/7454865
- When is verification not needed: https://support.google.com/cloud/answer/13464323
- Restricted scopes list: https://support.google.com/cloud/answer/13464325
- OAuth App Verification Help Center: https://support.google.com/cloud/answer/13463073
- Verification requirements: https://support.google.com/cloud/answer/13464321
- Drive resumable uploads (one-week session analogue): https://developers.google.com/workspace/drive/api/guides/manage-uploads
- Cloud Storage resumable uploads (one-week session analogue): https://cloud.google.com/storage/docs/resumable-uploads

YouTube Help and Terms:
- YouTube Terms of Service: https://www.youtube.com/t/terms
- Videos locked as private: https://support.google.com/youtube/answer/7300965
- Video privacy settings: https://support.google.com/youtube/answer/157177
- Intermediate and advanced features (higher daily upload limits): https://support.google.com/youtube/answer/9891124
- Upload videos longer than 15 minutes: https://support.google.com/youtube/answer/71673
- Upload YouTube videos (15 at a time): https://support.google.com/youtube/answer/57407
- Make bulk changes to videos: https://support.google.com/youtube/answer/6249768
- Channel permissions (no YouTube APIs for invitees): https://support.google.com/youtube/answer/9481328

FRC examples (YouTube, fetched 2026-11-04):
- Channel playlists: https://www.youtube.com/@FIRSTRoboticsCompetition/playlists
- 2025 Silicon Valley Regional playlist: https://www.youtube.com/playlist?list=PLZT9pIgNOV6be_lj4SeSFut3juyhyLyCi
- 2026 CA District Silicon Valley Event presented by Apple playlist: https://www.youtube.com/playlist?list=PLZT9pIgNOV6bRyxgbzpKYOgC23QfKAkJP
- Qualification 1 - 2025 Silicon Valley Regional: https://www.youtube.com/watch?v=EaRs_WdhNpM
- Match 1 (R1) - 2025 Silicon Valley Regional: https://www.youtube.com/watch?v=Cnp5dYYCkoU
- Final 1 - 2025 Silicon Valley Regional: https://www.youtube.com/watch?v=KUyodlPE3k4
- Match 10 (R3) - 2024 Silicon Valley Regional: https://www.youtube.com/watch?v=E2xgukqCSyc
- Final Tiebreaker - 2024 Silicon Valley Regional: https://www.youtube.com/watch?v=tZX56PSGlRk
- Match 13 (R5) - CA District San Gabriel Valley Event: https://www.youtube.com/watch?v=grUW_T6hqGQ
- Final 2 - CA District Silicon Valley Event presented by Apple: https://www.youtube.com/watch?v=TiEjN8zf2wU
- Qualification 1 - FIRST Championship - FIRST Robotics Competition - Johnson Division: https://www.youtube.com/watch?v=r_3El4O21kI
- TBA cross-checks: https://www.thebluealliance.com/match/2025casj_sf1m1, https://www.thebluealliance.com/match/2025casj_f1m1, https://www.thebluealliance.com/event/2026mttd
