# Scoreboard-clock reader: notes

Written 2026-11-04 by the clock agent.  Code: `streamsplitter/detect/clock.py`.  Tests and tools: `tests/test_clock.py`,
`tests/clock_harness.py`, `tests/run_clock_day.py`, `tests/analyze_clock_day.py`.  Scratch output:
`F:\StreamSplitterData\phase0\scratch_clock\`.

## API

```python
from streamsplitter.detect.clock import ClockReader

reader = ClockReader()                 # options: sample_fps=10, threads=1, rescan_after_s=30, relocate_after_s=300, ...
for path, t0 in chunks:                # complete chunks, in stream order; t0 = stream seconds of the chunk's first video frame
    readings = reader.feed(path, t0)   # [{"t": 1477.6, "text": "0:20", "value_s": 20, "conf": 0.93}, ...] sorted by t
reader.matches()                       # one dict per countdown episode; the last may be provisional (final=False)
reader.teams(t)                        # {"red": [...], "blue": [...], "complete": bool, "agreement": ..} or None
reader.status()                        # mode, clock place, templates, last-call timing
```

- `t` = t0 + (frame pts − the chunk's first video pts), so stream time, whatever the container's own timestamps are.
- Readings usually belong to the chunk just fed.  When the clock is found (or found again) the call also reads the
  one or two previous chunks it still has paths for; when the digit templates are first learned it returns every
  buffered sample (up to 7 min back).  Each sample is reported once.  Consumers must accept readings older than the
  current chunk.
- `matches()` entries: `auto_start`, `auto_end`, `teleop_start`, `match_end` (stream s), `rate` (video s per
  displayed s; 1.012 = clock 1.2 % slow), `pause_s`, `rms_s`, `n_auto`, `n_teleop`, `auto_inferred`, `ok` and
  `failures` (the research checks), `discontinuities`, `first_t`/`last_t`, `close_reason`, `teams`, `final`.
  Non-ok episodes are real overlay countdowns that are not a whole match: instant replays (1x and 2x), aborted
  attempts, a match cut by the end of the input.  `close_reason` / `failures` say which check failed.
- The reader is picklable (the day runner pickles it between pieces).  Not thread-safe; one reader per stream.

## How it works (one paragraph each)

- **Search** (no clock, or lost): keyframes only (`-skip_frame nokey`), colour, full frame.  White boxes in the top or
  bottom 35 % are kept for 150 s; a box whose dark ink is an m:ss line (3-4 tall glyphs + colon) in most of >= 5
  frames, with >= 4 different shapes, changing, and digits 2.2-10 % of the picture height, is the clock.  Once digit
  templates exist, a new candidate must also read as a countdown (value drops by the elapsed time ±2 s).  A place
  where a clock was found before is resumed as soon as 3 frames of the last minute read as m:ss there.  Trimmed-box
  and enlarged-overlay (zoom) glyph rules from the research are kept as retries.
- **Track**: reference frames only (`-skip_frame noref`), cropped to the clock, grey, at most `sample_fps`.  Glyph
  shapes are stored per sample; runs of an unchanged ones digit are labelled by the research countdown rules (chain
  ending in the 0:00 or 2:20 hold counted back from 0:01; chain after the 2:20 or 0:20 hold counted forward; tens and
  minutes changes must fall where the labels say; only chains of >= 10 runs train) and train 0-9 templates.  Then
  each sample is read by NCC.  Each clock place has its own templates; a new place borrows the last place's until it
  has its own (normalised glyphs are size-free, so a zoomed overlay reads immediately).
- **Episodes and fit**: a transition is a new value held >= 0.4 s; its time is the midpoint between samples.
  Transitions are grouped into episodes, closed by 0:00, a reset (clock goes up, or jumps to 0:20 / 2:20 other than
  by counting), an inconsistent countdown, or 45 s past the predicted end.  The research model is fitted:
  `t = a_phase(+piece) + rate × elapsed`, research checks (rate 0.97-1.06, pause 3.0-5.5 s, rms <= 0.1 s, >= 5 AUTO
  and >= 30 TELEOP transitions, no jump > 2 s), TELEOP-only fallback with AUTO inferred 20 s + 4 s earlier.
- **Relocation**: if the clock is missing 30 s (10 s inside an episode), each chunk's keyframes are also searched;
  after 300 s missing the reader returns to the cheap search mode.
- **Team numbers** (second priority): once per episode while TELEOP shows, keyframes around the clock box are decoded
  in colour; white digit groups on the dark red / blue panels are read with the clock templates (2x upsampled, ink
  thresholds 130/150/170) and voted across frames, preferring a longer read that contains the most frequent one.

## Results

All runs feed 60 s MPEG-TS chunks cut by stream copy (`ffmpeg -c copy -f segment`), in order, with t0 from each
chunk's first video pts.  CPU = ffmpeg user+sys (`-benchmark`) + Python process time, ffmpeg `-threads 1`,
OpenCV 1 thread, so "x real time per core" is honest single-core cost.  Wall time was often 2-4x the CPU time
because F: was shared with the downloads and the audio agent; that is disk wait, not compute.

### Benchmark clips (`tests/test_clock.py`, 18 tests, all pass)

| | casnv qm1 (60 fps, compact) | necmp1 qm10 (29.97 fps, wide) | mawor qm10 (58 fps, compact) |
|---|---|---|---|
| clock located | first chunk | first chunk | first chunk |
| AUTO start / TELEOP start / match end (s) | 5.577 / 29.856 / 171.519 | 5.905 / 29.898 / 169.941 | 5.843 / 29.898 / 170.214 |
| research product | 5.584 / 29.877 / 171.515 (docs/phase0-broadcast-layer.md 2.4; no casnv timing.json found) | 5.925 / 29.914 / 169.956 (timing.json) | 5.857 / 29.899 / 170.235 (timing.json) |
| difference | −7 / −21 / +4 ms | −20 / −16 / −15 ms | −14 / −1 / −21 ms |
| clock rate (research) | 1.012 (1.0117) | 1.000 (1.0003) | 1.002 (1.0024) |
| readings vs research per-frame reads | no per-frame product | 1242/1242 | 975/975 |
| readings vs own fitted model (±0.15 s of a tick skipped) | 879/880 (*) | 859/859 | 695/695 |
| team numbers | 6/6 right | 6/6 right | none complete (research: none either) |
| CPU, x real time per core | 18x | 29x | 18x |

(*) the one "disagreement" is a real one-frame "0:00" the overlay shows between AUTO 0:01 and the 2:20 hold
(checked frame by frame).  19 seeded random casnv readings were checked by eye on full-resolution crops: 19/19 right.
The research timing is 10-20 ms later because it stamps a change at the first frame showing it; this reader uses the
midpoint between samples.

Synthetic relocation test (`test_relocates_after_zoom_mid_match`): necmp1 qm10 re-encoded with the picture zoomed
1.33x from 100 s.  The clock (now 86 px digits, 50 px lower) is found again in the same chunk, borrows the old
templates, learns its own, no reading gap > 1 s, fit unchanged (5.903 / 29.890 / 169.932), 885/885 readings agree with
the model.

### D1: 2026cancmp 2026-04-11, video 0-18000 s (5.0 h of 9.6 h; `tests/run_clock_day.py`, `tests/analyze_clock_day.py`)

Covers the 24-min pre-show, qm39-qm65 (27 TBA matches), the 51-min lunch, two overlay layouts (compact 32 px and wide
65 px digits; the broadcast switched between them 6 times), two instant replays shown with the overlay, and one
aborted-and-restarted match (qm62).

- **Clock located**: at the end of the chunk after qm39's AUTO started (AUTO 1477.6 s; located with the chunk ending
  1561 s, ~84 s of stream later).  The call back-filled the previous chunks, so qm39's AUTO is measured.  Layout
  switches: new wide place found at 4321 s; afterwards every switch resumed a known place (no match lost).  Lunch:
  dropped after 344 s unseen (14041 s), cheap search, resumed the known place at 16381 s, 2 min before qm63.
- **Matches**: 27/27 TBA matches have an episode with `ok` (all research checks passed).  AUTO start minus
  (TBA actual_time + fitted offset 7.8 s): median 0.00 s, range −0.92..+2.94 s.  TBA times are whole seconds and the
  first three matches sit at +2-3 s, so this checks presence and naming, not sub-second accuracy.  Rates 1.000-1.006.
- **Episodes that are not matches (4, all `ok=False` with a reason)**: an instant replay of qm44's AUTO at 1x
  (4271 s), a 2x replay of qm52's TELEOP (8527 s, its "2.0x" badge is on screen), qm62's aborted first attempt
  (started 13295 s, stopped at 2:07, 0:20 for 95 s, restarted 13439 s; closed as "clock reset to 0:20"), and qm66's
  start cut by the end of the range.
- **Readings outside matches**: 0 readings in the pre-show before 1386 s; 33556 readings outside the 27 matches, of
  which 32899 are the static 0:20 / 0:00 / 2:20 displays and the other 657 all fall in the four episodes above (real
  overlay content, not misreads).  Seeded by-eye checks on full-resolution crops: 24/24 random readings right,
  18/18 random outside-match readings right.  Reading confidence: 1st percentile 0.857; 4 of 69870 readings < 0.6.
- **Team numbers** (overlay with team avatars): in the reader, complete on 19/27 matches, of which 16 right.  Per
  number (offline re-read with the same function, `scratch_clock/eval_teams_day.py`): 151/162 right, 3 wrong, every
  match has >= 5 right and <= 1 wrong.  Naming each match by the schedule (the day's match sharing the most read
  numbers, >= 4 and unique) names 27/27 right, 0 wrong.  Wrong reads are truncations ("84" for 840) and one
  spurious "4909"; misses are 649 (4x), 846, 5419 and single-digit team 8 (by design: 2-5 digit groups only).
- **Speed**: 300 chunks; CPU per 60 s chunk median 2.6 s, 90th percentile 4.0 s, max 9.1 s (4 chunks > 6 s: the calls
  that also back-filled 1-2 earlier chunks after a location or layout switch).  Track mode 20x, search mode 43x real
  time per core; 23x overall (787 s CPU for 18002 s).

## Known failure modes

- **First location of the day needs a running countdown** (>= 5 keyframes with >= 4 shapes): the first match is
  located one chunk late and recovered by back-fill, which needs the previous chunk files on disk (the reader keeps
  the last 3 paths).  A static clock cannot be located the first time; once a place is known it is resumed on any
  readable m:ss.
- **Templates need a labelled chain**: a countdown chain of >= 10 one-second runs next to a 0:00, 2:20 or 0:20 hold.
  Until then no readings; when learned, the buffered samples are read late (up to 7 min back).
- **Overlay time is not FMS time**: on casnv the clock ticks 1.2 % slow, so `match_end` trails the real end by up to
  ~1.4 s (research finding).  The fitted `rate` exposes it; the audio detector is the real-time reference.
- **Instant replays and aborted attempts** produce correct readings and non-ok episodes; the matcher must use `ok`
  and the close reason, not the mere presence of a countdown.  An aborted match restarted without a 0:20 / 2:20 /
  0:00 display in between would not be split (not seen).
- **Team numbers**: not readable on mawor (soft 13-14 px digits, as in the research); "complete" does not mean right
  (3/19 complete reads on D1 had one wrong number); single-digit teams are never read.  Match them to the schedule
  (>= 4 of 6) rather than trusting them.
- **False clocks**: before this was fixed, D1 locked onto a 138 px "clock" in the lower camera view and a 16 px line
  on an "Up next" card (each cost minutes of clock).  The digit-height band (2.2-10 % of picture height) and the
  countdown check now reject both; a timer elsewhere in the picture with a plausible size that counts down at 1 s/s
  (a break timer) could still be taken, and is dropped only if it reads > 2:40 for a whole chunk.
- **A new overlay font at a new place** needs 10 frames and 6 shapes before it is accepted (untested on real data).
  The enlarged-overlay glyph rule (zoomed colon dots) is ported but was not needed by any test.
- **Back-fill calls are CPU spikes** (up to ~9 s for one call).  Under disk contention each chunk's wall time grows,
  because it is read by 1-3 ffmpeg processes (track decode; keyframe search during rescans; team read once a match).

## Not done / not verified

- D1 after 18000 s (qm66-qm96) and the other dev days (D2, D3) were not run; no 720p, Twitch or Champs stream tried.
- Sub-second accuracy is verified only on the three benchmark clips; on D1 only against TBA (whole seconds).
- Speed was measured on this desktop under contention, not on a 4-core laptop; hardware decoding is not used.
- Picture-size change (forces relocation), missing chunks and overlapping chunks are handled in code but untested;
  `feed` assumes the chunk file is complete.
- State persistence is by pickling the reader (`run_clock_day.py` does it); there is no save/load API.
- The season timing (AUTO 20 s, pause 4 s, TELEOP 140 s) comes from `SEASON_2026`; other seasons need a `season=`
  dict.  Results screen (final scores) is not part of this module.

## Files

- `streamsplitter/detect/clock.py`: the reader.
- `tests/test_clock.py`: benchmark clips + synthetic zoom (pytest; ~1-2 min).
- `tests/clock_harness.py`: split a clip into chunks, feed, compare with the model / research per-frame reads.
- `tests/run_clock_day.py`: fake-live day runner (segment muxer cutting while the reader reads; pickled state;
  pieces under a wall budget).  `tests/analyze_clock_day.py`: score a day run against TBA.
- Scratch (`F:\StreamSplitterData\phase0\scratch_clock\`): `d1/piece_*.json` (readings, matches, events, per-chunk
  stats of the final D1 run), `d1.pkl` (reader state at 18000 s), `dbg/*.png` (by-eye contact sheets),
  `eval_teams_day.py`, `visual_sample_day.py`.

## Change 2026-11-04 (lead): cold start next to a frozen clock

Found on a single-match trial (D2 2026cagle, stream seconds 6650-7500, qm63 at 6902):

- **Problem.** Before a match the scoreboard clock sits frozen at 0:20, so the "changing m:ss line" location rule
  cannot find it. The reader located three things that do change instead: rows of white folding chairs with people
  passing them, and the letters of a "NITROS" banner (picture: `<data>/phase0/truth_pictures/`; checked by eye).
  Tracking a place showed no rescan trigger, so it held the last one for 400 s and missed the whole match.
- **Fix 1: challenger search.** While the tracked place has no digit templates of its own, every chunk's keyframes
  are searched again, excluding that place, excluded places and unproven known places. A newcomer wins in either
  case:
  - it is a proven known place showing m:ss, or
  - its seconds digit changes once a second (`once_a_second` >= 5 back-to-back ~1 s runs this chunk) and the
    tracked place's does not as well.

  The loser is excluded for 30 min and the recent chunks are read again at the winner. On qm63 the switch came at
  6962.6 s (rhythm 35 against 0), and the back-fill recovered AUTO from 6901.8 s.
- **Fix 2: resume only proven places.** Only places that learned their own templates are resumed. Unproven known
  places were the way back to clutter.
- **Fix 3: wider RMS limit.** `CHECK_RMS_S` went from 0.10 to 0.25 s. qm63, a full match, ticks with rms 0.14 s and
  max 0.35 s at 7.3 samples/s. D1's replays and aborted attempts fail on structure (AUTO only, rate, reset), not RMS.
- **Regression.**
  - `tests/test_clock.py` 18/18.
  - D1 0-6000 s: same location history as before (1561 located, 4321 wide layout, 4441 resumed); 9/9 ok episodes,
    named right by team numbers; the 1x replay is still not ok.
- **Still open.** On qm63 only red's numbers were read (5026, 8060, 696), and the blue side gave nothing. The namer
  now accepts a whole alliance that plays together in exactly one scheduled match.
