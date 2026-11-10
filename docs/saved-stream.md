# A saved stream

For after the event: a Twitch VOD (`twitch.tv/videos/<id>`) or the YouTube video of a past stream, split like a
live one.

```
run.bat vod https://www.twitch.tv/videos/VIDEO_ID --event 2026mttd --to drive
```

- It copies the video at download speed (a 1080p60 hour in under 3 minutes on the test PC), cuts every match as
  `live` would (an hour in about 12 minutes), and publishes the clips.
- `--to drive` sends them only to Google Drive. With no `--to` they go everywhere `config.toml` turns on.
  `--no-publish` only cuts.
- It stops by itself once the video is read and the uploads are done. Run again with the same tag, it cuts and
  sends no match twice.
- Each clip's file time, which Drive shows, is when its match was played: the video's start plus the time into the
  video.
- A saved stream leaves out whatever the live stream dropped, so its time runs short of the clock (MadTown 2026's
  practice day kept 61 of 94 minutes). The overlay timer jumping ahead is read as lost video, not as a new
  countdown; each clip holds what the stream kept, and matches after a drop show a file time that is too early.
- Every YouTube upload counts against the 100 a day ([lessons/publishing.md](lessons/publishing.md)), so re-running
  a whole event to YouTube takes planning. `--to drive` does not.
