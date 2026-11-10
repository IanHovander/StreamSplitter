# Lessons: YouTube, Google, Slack, The Blue Alliance

Each line: what happened, then what StreamSplitter does about it (or what you should do). Code:
`streamsplitter/publish/`. Limits in numbers: [requirements-and-limits.md](../requirements-and-limits.md).

## YouTube and Google

- **100 uploads a day, per Google Cloud project, for every computer using it together.** It resets at midnight US
  Pacific. MadTown ran out at 20:44 on Saturday. Uploads wait in a queue on disk and go out after midnight; the
  alerts contact is told once. Do not spend uploads on a stream you will replace.
- **Everything else shares 10,000 units a day.** Adding to a playlist costs 50, changing a title or description 50.
  That is about 99 matches a day, or 200 renames.
- **Set the Google sign-in app to "In production".** In "Testing", teammates get "Error 403: access_denied" and the
  login dies after 7 days, which is always mid-season. Unverified is fine: click *Advanced*, then *Go to
  StreamSplitter*.
- **Check that uploads are not locked private.** Google used to lock uploads from new projects; it stopped on
  2026-11-02. The setup's test upload checks anyway.
- **One Google project per team.** Google's rules forbid sharing API credentials with other groups, and the daily
  limit would be shared too.
- **Google Drive only lets the app into folders the app made.** The setup makes the folder; you share it. Pointing
  it at an existing folder fails.
- **Drive is the fast one.** Clips reached Drive in about 20 s; YouTube needs the results screen first. The drive
  team should watch Slack or Drive.
- **Nothing is sent twice.** Every clip is a job in a small database on disk with a stable name. Uploads resume
  after a drop or a restart, and a finished upload whose answer was lost is found again, not repeated.

## Slack

- **Never message someone who did not ask.** An early setup sent a how-to note to 12 teammates at once, and the
  app was removed from the workspace. Now only the person who typed a command and the alerts contact get messages.
  Adding someone sends them nothing unless the admin says yes.
- **A cancelled scheduled message can still be posted.** The stopped-running alarm is a scheduled message that is
  pushed back while the program runs. Slack still posted ones cancelled less than about 5 minutes before they were
  due. Each is now cancelled at least 5.5 minutes ahead, so the alarm arrives 6-7 minutes after a stop.
- **A question nobody answered must say so.** `split` used to wait for `yes` and expire in silence. It now starts
  at once for an event on today or tomorrow, reminds after 5 minutes otherwise, and says when it has given up.
- **One program reads a chat.** Two programs answering the same direct message both replied. Only one reads now.
- **One alert per problem.** A retry loop once sent the same warning again and again.
- **Tokens stay on the computer.** Settings can be changed from Slack; keys and logins cannot.

## The Blue Alliance

- **The footage is the event's.** Ask the organizers before linking videos of their webcast. Links are off unless
  turned on.
- **TBA drops a whole request that names one match it does not have.** Links are sent one match per request.
- **The write-key request page** is `thebluealliance.com/request/apiwrite`.
- **TBA's playlist page reads the match from the title.** Titles that end in "(qm12)" are matched by themselves;
  `run.bat tba --retitle` adds that to videos already uploaded ([tba.md](../tba.md)).
