# Event day

## Before

- The setup is done and its test upload passed ([setup.md](setup.md)).
- The event's organizers said yes, if the webcast is not yours.
- One computer per event. Two would post every match twice.
- The computer stays on, plugged in and online all day. It need not be at the venue. StreamSplitter keeps it from
  sleeping when idle; it cannot stop a closed lid or a Windows Update restart.
- After the code was updated, restart what is running (`run.bat stop`, `run.bat listen stop`, then start again). A
  running program keeps its old code.

## Start, look, stop

```
run.bat start      # record the set-up event, in the background
run.bat status     # the stream, the clips made, the uploads waiting
run.bat stop
```

Start any time; the evening before is fine. While the stream is not live it looks again every 45 s. Started again
for the same event, it continues where it was and cuts no match twice.

With [Start from Slack](start-from-slack.md) on, the same works from a phone, as messages to the StreamSplitter app:

| Type | What happens |
|---|---|
| `split 2026mttd` | Starts that event at once if it is on today or tomorrow; otherwise it asks, and you reply `yes` or `no`. |
| `status` | What is recording, the clips, the uploads, and how quickly the listener reads your chat. |
| `stop` | Asks, then stops. `stop yes` stops at once. |
| `help` | The commands that matter right now. `help all` lists every one. |
| `set stream <link>`, `set stream auto` | Admins: record this stream; go back to choosing by itself. |
| `pause youtube`, `resume youtube` | Admins: hold the uploads and release them (`drive` and `wiki` too). |
| `add @Sam` | Admins: let a teammate use these commands. Nothing is sent to them unless you say so. |

The computer reads the chat every 20 seconds, and every 3 seconds for 2 minutes after someone writes.

## What comes out

| Where | What | When |
|---|---|---|
| `<data_root>/clips/<tag>/<event>_qm12.mp4` | The match with about 3.5 s either side | about 30 s after the match ends, a minute at worst |
| `..._qm12_score.mp4` | The same clip with the results screen appended | 1-5 minutes after the match ends |
| Slack channel | Your teams' matches: the clip, then the score version as a reply in its thread | about 20 s after each file exists |
| Google Drive | Every match, in the event's folder: the clip, then the score version | 1-1.5 minutes after the match ends; 2-3 minutes |
| YouTube playlist | Every match, unlisted: the score version, or the plain clip when no results screen was shown | a few minutes after the match ends |

Names come from the overlay's title ("Qualification 12"), checked against the team numbers and TBA's schedule
(fetched every 5 minutes). A practice match is `<event>_p<N>`; a replay gets `_r2`. A stream with no overlay timer
gives no clips ([requirements-and-limits.md](requirements-and-limits.md), "What the webcast must have").

## Which stream it records

It keeps a list of the event's streams, best first: the link chosen for this run, then YouTube links, then Twitch;
the ones The Blue Alliance lists before the ones it found itself.

- Every 5 minutes it checks whether a better link than the one being recorded is live **and sending video**, and
  moves to it between matches. A link that fails is tried again after 5 minutes, then 10, up to an hour.
- Every 5 minutes on event days it also searches YouTube's live videos for a stream of the event that is on no
  list (a team's own stream, say). A hit must carry the event's name in its title and show three of the event's
  team numbers in its picture before it joins the list. A hit that only matches by title is suggested to the alerts
  contact once, and not recorded.
- `set stream <link>` from Slack, or `run.bat --section stream`, picks a stream by hand.
- To turn the search off: `find_streams = false` under `[event]` in `config.toml`.

Why: at MadTown 2026 the listed Twitch stream glitched all Saturday while a team's YouTube stream, on no list, was
clean ([lessons/streams.md](lessons/streams.md)).

## `live` in a window

`run.bat start` runs this in the background with the saved settings. By hand:

```
run.bat live https://www.youtube.com/@EventChannel/live --event 2026mttd --deliver "C:\Users\me\OneDrive\Clips"
```

- `--deliver FOLDER` (or `deliver_dir` in `config.toml`) also copies every clip into `FOLDER/<tag>/`, complete
  files only: a cloud-synced folder shared with the drive team works with no accounts set up.
- `--deliver-height 720` delivers a smaller copy (a 170 s 1080p60 clip: 126 MB becomes 40 MB in about 7 s).
- `--no-publish` only cuts: nothing goes to Slack, YouTube or Drive. Stop with Ctrl+C.

## After the event

- Videos onto The Blue Alliance: [tba.md](tba.md).
- Matches the live run missed, or a cleaner copy from the saved stream: [saved-stream.md](saved-stream.md).
- With the background Slack listener, the recording stops by itself at 06:00 the morning after the event's last
  day; otherwise `run.bat stop`. Clips are never deleted; recorded chunks are, after 28 hours.
