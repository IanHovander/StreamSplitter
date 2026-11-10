# Lessons: finding and recording the stream

Each line: what happened, then what StreamSplitter does about it (or what you should do). Code:
`streamsplitter/record.py`, `streams.py`, `scout.py`.

- **The Blue Alliance's webcast list can be wrong.** For MadTown 2026 it listed YouTube day videos that never went
  live; the event streamed on Twitch. A team's own YouTube stream, far cleaner, was on no list until Sunday.
  StreamSplitter keeps a list of links, YouTube before Twitch, and searches YouTube's live videos every 5 minutes
  for a stream of the event that nobody listed. It reads the event's team numbers off the picture before trusting
  one.
- **"Live" does not mean video arrives.** On Sunday the official YouTube video said live and sent nothing, 18 times.
  A link counts as better only once it delivers a new piece of video within 12 seconds. A link that fails waits
  5 minutes, then 10, up to an hour.
- **A glitchy stream wastes uploads.** Saturday's Twitch stream dropped and stuttered all day. Every clip from it
  was uploaded, then cut again from the better stream: 161 YouTube uploads for 83 matches, and the daily limit
  ran out. Switching happens between matches now, and early.
- **Twitch affiliate and partner channels open every connection with about 30 s of ad.** It is recorded like the
  stream. Each reconnect costs that much of the event, so prefer YouTube when both carry it.
- **Short drops can be refilled.** YouTube lets a player rewind a live stream; the recorder fills short gaps from
  there after a drop, a stall, an expired link or a new video.
- **Record in short pieces.** A clip can be cut only once the piece holding its end is complete, so the piece
  length is the worst-case delay. It went from 60 s to 20 s.
- **A saved stream is not the live stream.** Twitch VODs and YouTube archives leave out what the stream dropped,
  so their time runs short of the clock: MadTown's practice-day VOD kept 61 of 94 minutes. The match timer jumping
  ahead is read as lost video, not as a new match ([saved-stream.md](../saved-stream.md)).
- **Some streams carry sound a clip file cannot hold.** One stream reported a sample rate of 0 for five hours. The
  clip is then written with the picture alone, which beats no clip.
- **YouTube refuses many cloud servers.** Downloads from datacenter addresses are often blocked, so a rented
  server is a poor recorder. A home PC with the Slack listener does the job; the long version is in
  [research/cloud-server.md](../research/cloud-server.md).
- **yt-dlp goes stale.** YouTube changes break old versions. If a stream that plays in a browser will not record,
  update it first: `.tools\uv\uv.exe pip install --python .venv\Scripts\python.exe -U yt-dlp`.
