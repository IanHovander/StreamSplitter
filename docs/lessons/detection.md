# Lessons: finding, cutting and naming matches

Each line: what happened, then what StreamSplitter does about it. Code: `streamsplitter/detect/overlay.py`,
`timeline.py`, `namer.py`, `cutter.py`, `pipeline.py`.

- **Read the overlay, not the game.** The first version looked for the 2026 field sounds and FIRST's 2026
  scoreboard, so it could not work at an event run on other software or in another season. Now the match timer,
  title and team numbers are read as text (OCR), which works for FMS, Cheesy Arena and home-made overlays. A
  webcast with no timer on screen cannot be split.
- **The display runs ahead of the field.** Overlay timers were 0 to 1 s early against the field sounds. Clips take
  0.5 s more at the start and 1.0 s more at the end, plus 3 s of padding: about 3.5 s either side.
- **A clip must start on a keyframe.** Clips are copied, not re-encoded, so they are made in seconds at the
  stream's quality. Streams put keyframes 3-7 s apart, so the copy starts at the keyframe before the wanted start
  and the file tells players to skip the extra.
- **Other countdowns sit where the match timer sits.** MadTown showed a "Field Break" timer there, and its last
  2:20 looked like a match that had lost its autonomous period: it was posted as the finals. A countdown first
  seen in teleop counts as a match only if it starts within 5 s of teleop's full length.
- **Broadcasts hide the overlay at odd moments.** One fades it in 1.5 s into the match; another hides it as teleop
  starts. The match's start is worked out from the timer's value, not from when the timer first appeared.
- **Titles are not schedule keys.** "Qualification 12" is easy. Double-elimination playoffs read "Match 11 - Round
  4 Upper", which is semifinal 11 on The Blue Alliance. The name is checked against the team numbers on screen
  and TBA's schedule (fetched every 5 minutes).
- **The results screen has the last word.** Its QR code links to the match on frc.events. When it disagrees with
  the name given at the cut, the clip is renamed before YouTube gets it, and a Slack post still waiting is
  corrected.
- **Practice matches are in no schedule.** They are named `<event>_p<N>`, from the results screen's code alone.
- **Offseason B teams show under stand-in numbers** (9993 for 254B). The name check accepts a match when the
  other teams agree with the schedule.
- **A replayed match** gets `_r2`; the later play is the one linked on TBA.
- **A match the overlay lost early** (an aborted start, a stream drop near the end) is still posted, titled
  "(Partial)". A partial clip is better than none.
- **Not everything is found.** At MadTown 2026, 4 of 87 matches got no clip (qm55-57 and semifinal 11), and the
  cause is not found yet. Check `run.bat status` during the day, and fill holes afterwards from the saved stream
  ([saved-stream.md](../saved-stream.md)).
- **Field sounds remain as a fallback** (the field's start sound and end buzzer; `streamsplitter/seasons/2026/`):
  2 of Eagan's 11 test matches had no overlay and were cut from the sounds. Kettering's sounds were inaudible, so
  nothing could be cut there.
