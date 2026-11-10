# Phase 0 plan: offline detection proof

Written 2026-11-03, before any detector was run on these days. Changes after this point are dated and justified
below the line at the end.

## Question

Fed a full saved event-day stream as if it were live, can StreamSplitter cut every played match with the right name
and boundaries, fast enough, without false clips?

## Days (fixed now)

All are saved 2026 YouTube day streams at 1080p H.264 with audio (probed 2026-11-03, `<data>/phase0/probe_*.json`).
None of these events is used by the research project, whose held-out matches must stay unopened (`RESEARCH_EVENTS`
in `find_streams.py`).

| Role | Event | Day | Video | Hours | TBA matches that day | Why |
|---|---|---|---|---|---|---|
| Dev D1 | 2026cancmp (CA Northern State Championship, 5026 attended) | 2026-04-11 | VPIY_1F3Mvg | 9.58 | 58 | 5026's own production style, a full qualification day |
| Dev D2 | 2026cagle (CA District Glendale, 5026 attended) | 2026-03-29 | 3-wNH_65zBM | 9.44 | 38 | Last day: quals and playoffs |
| Dev D3 | 2026hop (Champs, Hopper Division) | 2026-05-01 | VqeEcZXbR5A | 11.30 | 62 | Champs production, long day |
| Dev reserve D4 | 2026micmp1 (Michigan State Championship, DTE Division) | 2026-04-17 | _DTdFpvdGt0 | 10.84 | 58 | Used only if D1-D3 leave a production style untested |
| **Sealed S1** | 2026pncmp (PNW District Championship) | 2026-04-11 | s3X4yqNg-88 | 8.50 | 28 | A region and production no dev day uses; includes playoffs |
| **Sealed S2** | 2026new (Champs, Newton Division) | 2026-05-01 | K4m0GSSTM18 | 10.70 | 62 | Second sealed day, so the sealed result is not one day |

The sealed days are not downloaded, opened or looked at until the detectors are frozen. Their TBA records were
fetched only to count matches; no per-match data was stored.

## Truth

- **Which matches were played, and their names:** TBA match records (`actual_time` for every match, teams, scores).
  TBA has no practice matches; practice clips are counted and reported but are outside the pass bar in phase 0.
- **Where each match is in the video:** `actual_time` plus that day's stream offset. The offset is fitted per day as
  the median of (detected start − `actual_time`) over confident matches. It locates matches to about ±2 s, which is
  enough for presence and naming, not for boundaries.
- **Boundaries, two independent checks:**
  1. Every match: the field-sound start and end against the scoreboard-clock start and end. These are different
     signals (audio, pixels). A disagreement over 1 s is listed and looked at.
  2. A random sample of 20 matches per day (seeded, drawn after the run, not from the failures): Claude views
     full-resolution crops of the clock at the cut points, ±1 s around the detected start and end, and records what
     the clock shows. The detectors do not see these labels.

## Metrics (per day and pooled, each with a 95 % Wilson interval)

- **Recall:** played matches with a match clip that contains the whole match, start padding 3-8 s, end padding 3-8 s.
- **Naming:** clips whose match key equals the truth key.
- **Boundary:** |start error| and |end error| ≤ 1 s (check 1 on all matches; check 2 on the sample).
- **False clips:** clips matching no played match. Replays of a match (both kept by design) and saved-unmatched items
  are listed separately.
- **Score clips:** played matches with a score clip whose appended piece shows that match's results screen (checked
  on the same visual sample).
- **Latency:** in the fake-live replay, seconds from the end buzzer arriving to the match clip being written.

## Pass bar (dev days pooled, then sealed days once)

| Metric | Bar |
|---|---|
| Recall | ≥ 99 % |
| Naming | ≥ 99 % |
| Boundary | ≥ 99 % within ±1 s on check 1; no sampled clip that cuts into its match on check 2 |
| False clips | ≤ 1 unexplained per day |
| Score clips | ≥ 95 % where a results screen aired (a broadcast that never shows it is not a miss) |
| Latency | ≤ 120 s for the match clip |

Pooled dev is about 158 matches. Zero misses in 158 still leaves a 95 % upper bound of about 2.4 %, so the report
gives intervals, not just the point. The sealed days run once, after the freeze, with no tuning afterwards. A
sealed miss is reported as found, not fixed and re-scored.

## Procedure

1. Download D1-D3 (`phase0/get_day.py`, detached), into `<data>/phase0/vod/`.
2. Fake-live harness: cut a day into 1-minute MPEG-TS chunks, stream copy, and release them at real speed or faster
   with the wall-clock stamps a live recorder would have written.
3. Detectors running on chunks as they arrive: field sounds (`detect/audio.py`), scoreboard clock and team numbers
   (`detect/clock.py`), results screen (`detect/results.py`).
4. Matcher (state machine plus naming) and cutter (match clip, score clip).
5. Scorecard (`phase0/score.py`) per day; failure pictures in a folder labelled as failures, plus plain random
   samples.
6. Freeze, then sealed S1 and S2 once.

## Hygiene

- Directory scans in phase 0 code read only the day files named in this plan.
- No research-event video is opened. The per-match research benchmark clips (`Matches/_benchmark`) are used only as
  unit-test inputs for porting the clock and audio code, and only the dev files the research already treats as open
  (2026casnv Q1, 2026necmp1 Q10, 2026mawor Q10).

---
Changes after the plan was written (all 2026-11-03, before any day-level score was seen):

1. **Audio features.** The v0 templates from casnv qm1 found only 1 of 5 cues on D1 qm40: the cancmp broadcast puts
   the commentators far louder than the field sounds. A 0.31 s moving average along time was added to the
   spectrogram (steady FMS tones survive, gliding speech smears), and the threshold dropped from 0.45 to 0.40. On
   qm40 the true-cue NCC rose to 0.62-0.69 while the best non-cue value in that 210 s window was 0.36. The start
   sound is still not recognised on D1; the matcher starts the match from the other cues' implied start.
2. **Templates and references are built from dev data:** audio v0 from casnv qm1, and results-screen references from
   two D1 screens (qm39, qm40). Any refinement built from D1 (e.g. averaged v1 templates) makes D1 a training day.
   Its numbers are then reported but not used for the pass bar; D2 and D3 carry the dev measurement.
3. **Keyframes.** The saved day videos have a keyframe every 5.0 s, so stream-copy padding before the start is 3 to
   8 s on these days, not the 3 to 5 s expected from live streams (2 s keyframes, to be measured in phase 1).
   Recall's padding window in the metric is widened from 3-8 s to 3-9 s before the start to allow for it; the end
   padding is exactly 3 s.
4. **Cutter bug found and fixed before any run:** cutting at the keyframe's display time dropped the keyframe (its
   decode time is earlier with B-frames), and the picture started 5 s after the sound. The cut now starts 0.1 s
   earlier.

Changes on 2026-11-04. D1 run1 (audio only, v0 templates) had been run and scored before these; none of them was tuned
to its numbers except where said:

5. **Truth join allows stream drops** (`phase0/truth.py`). Prompted by D1 run1: the saved D1 video loses about 15
   minutes after qm80, so one day-wide offset mis-joined everything after it. The join is now monotone with a
   piecewise-constant offset that may only step down (video loses time, never gains it). TBA matches that fall
   between two offset segments need a by-eye verdict in `phase0/truth_notes.json` before they count: absent, partial
   or present. Recall counts only matches whose whole window is in the video. D1: qm81 absent (stream drop) and qm84
   partial (the video resumes at 1:02 left), both checked on frame sheets in `<data>/phase0/truth_pictures/`.
6. **Naming under test is the live namer** (`streamsplitter/namer.py`). It uses team numbers read off the
   scoreboard, then TBA times already posted when the clip is cut, then schedule order. Run1's names came from the
   after-the-fact time join, so they are not a naming measurement.
7. **The scoreboard clock is fused into the matcher** as planned in step 3 of the procedure. An episode that passes
   the clock checks counts as two roles, and audio still sets the boundaries when it heard enough. The first fused
   run is D1 run2.
8. **Frame-grabber seek fix (phase 0 pictures only).** Seeking before `-i` on an MPEG-TS chunk can land on the next
   keyframe, up to 5 s late. Picture tools and the results-screen refinement now use an exact two-step seek
   (`media.exact_seek`). The detectors were not affected: the refinement always seeks exactly to a keyframe, which
   was verified to land right. Only the coarse frame sheets made before the fix could be up to 5 s off, and no
   conclusion drawn from them depends on that.
9. **The score clip is decided live.** It is cut once its piece is settled: the screen has been up for 60 s, the next
   match has started, or 420 s have passed.
10. **Pins at stream drops.** A drop that loses about one match cycle can't be placed by time. On D1 the real qm83
    sits 3.5 s from qm82's old-offset time, and the time-only join mis-joined it on one run. Every offset jump is now
    checked by eye: the scoreboard title of the first matches after the jump is read and recorded as a pin in
    `truth_notes.json`, and the join must go through the pins. D1: the matches at 26121 and 26591 are qm82 and qm83.
11. **v1 audio templates built from D1** (`refine_templates.py`, 48 matches pinned by 3 or more cues). D1 is now a
    training day: its v1 numbers are reported but not used for the pass bar. Audio alone on D1 went from 55/56
    (v0, start sound heard in 1 match) to 56/56 (v1, start sound in 50). D2 and D3 run with v1 and carry the dev
    measurement.
12. **User redirect, 2026-11-04: trial one match first.** The full-day runs were stopped: D1 run2 was partial and
    D3 chunking was not finished. The trial was D2 (cagle) qm63, a 5026 match, with v1 templates and the clock, over
    stream seconds 6650-7500:
    - All five field sounds were heard (start 6901.96, end 7067.28).
    - Match clip: 4.3 s padding before and 3.0 s after, written 21 s after the end buzzer.
    - Score clip: the results piece was appended (reveal plus 30 s of screen).
    - Both MP4s are clean. Checked by eye on `<data>/phase0/visual/D2_trial_qm63/`.

    Found: the clock reader cannot cold-start just before a match. The clock is frozen at 0:20 until the start, so it
    locked onto changing text in the arena and tracked it for 400 s, and so read no team numbers. The namer gained a
    cold-start fallback for this: when a match has no name at cut time, it retries at score-clip time using the
    posted TBA time with offset 0 and a ±90 s window, then renames the clips. qm63 was named that way, 135 s after
    the end.
13. **Clock reader cold start fixed** (details at the end of `phase0/notes_clock.md`):
    - a challenger search with a once-a-second rhythm test while the tracked place is unproven;
    - only proven places are resumed;
    - the RMS check is widened to 0.25 s;
    - the namer accepts one side's whole alliance.

    qm63 trial, run again cold: the clock replaces the clutter one chunk after the start, the episode is ok, and the
    clip is named qm63 by team numbers at cut time (21.8 s after the end). Benchmark tests 18/18; D1 0-6000 s
    unchanged (9/9).
14. **Champs single-match trial** (D3 2026hop qm87, stream seconds 13840-14560, cold start):
    - All five field sounds were heard.
    - The clock reader located clutter first; the challenger switched to the real clock at 14220.75 (rhythm 60
      against 1), and the re-read recovered AUTO.
    - Episode ok (rms 0.038 s); all 6 team numbers read right; named qm87 by teams.
    - Clip: 4.1 s before, written 30.6 s after the end. The score clip shows the qm87 results screen; the Champs title
      bar did not stop the D1-built references.
    - Checked by eye.

    Two false matches appeared and are now rejected by a new matcher rule: a match needs evidence from its end (the
    endgame whistle, the end buzzer, or an ok clock episode).
    - An instant replay of qm87's AUTO played with its field sounds, 66 s after qm87 ended.
    - A stray pair at the window start: qm86's end buzzer plus a false bell.

    On D1 this rule would cost 1 of 56 matches in audio-only mode (qm71) and none with the clock. Noted: the detected
    end buzzer was 1.4 s after start + 165.05 s, so the end padding here is about 4.4 s.
15. **Playoff trial: the D2 Final Tiebreaker** (stream seconds 32420-33150, cold start).
    - **First result.** The clip was cut cleanly, but named f1m2 from team numbers plus TBA time. That was wrong: the
      scoreboard title said "Final Tiebreaker", and the score matched f1m3.
    - **Cause.** All three finals share the same six teams, and the saved video had lost about 27 minutes to stream
      drops, so stream time plus the video's start time was 27 minutes off the wall clock. f1m2's TBA time happened to
      land 59 s from where f1m3 appears in the video. A live recorder stamps true wall time.
    - **Fixes.**
      - Ties skip matches TBA has already posted as played at another time.
      - The challenger search uses only the last 75 s of the search window, so the frozen 0:20 frames don't outvote
        the countdown. All three trials now switch in the first chunk after the start.
      - The cold-start time window is widened to 150 s.
      - **Final names come from the results screen's QR code**, a link to the official match page
        (frc.events/2026/CAGLE/qualification/63, .../playoffs/16, .../practice/N). It is read when the score clip is
        cut and overrides earlier names. Playoff N is TBA's N-th playoff match; practice becomes `<event>_p<N>`.
    - **Result.** All three trials now end QR-named: f1m3 corrected from f1m2 184 s after the end, with both clips
      renamed; qm63 and qm87 confirmed. Tests 30/30.
    - **Truth consequence for full-day scoring.** D2 lost about 27 minutes before the finals, so D2's evening needs
      pins (change 10) before any day-level score.
16. **Event-agnostic path (user, 2026-11-04).** The user wants StreamSplitter to work whatever screens and sounds an
    event uses. It is developed on Chezy Champs 2026 (C1, Saturday: Cheesy Arena, not FMS), checked on another game
    (W1, Kettering 2025 Week 0; the official Merrimack Week 0 has no saved full stream), and finally tested on MadTown
    2025 day 2 (M2, FINAL: no dev use; it times the first match, minute by minute).

    The aim is that drive team gets each clip within 4 minutes of the match end at MadTown 2026 (2026-11-06 to 11-08).

    - **Overlay reader** (`streamsplitter/detect/overlay.py`). OCR (RapidOCR, ONNX, CPU) reads a whole frame every
      4 s. Every text that reads as a time becomes a candidate timer place, re-read at 4 fps (6 ms each).
      - A place that counts down once a second gives countdown episodes. C is the time the display reads 0, the
        median of (change time + new value).
      - No layout, font, colour or sound is assumed.
      - The match title and the team numbers are kept from the whole-frame reads.
    - **Timeline** (`streamsplitter/timeline.py`). A match is a short countdown (auto) followed within 12 s by a long
      one on the same place (teleop). The end is teleop's zero; the start is auto's zero minus the auto length.
      - Pieces split by misreads are merged.
      - A teleop that stops above 0 is an abort, not cut.
      - Without an auto, a teleop counts only once the event's own values are learned.
      - The auto length is proven when the display sat at it before the match (FMS shows 0:20). Otherwise it is
        taken as the next multiple of 5 s: Chezy's broadcast fades the overlay in ~1.5 s into the match, first
        showing 0:19, which made the first run's starts 1 s late.
    - **Naming.** The overlay title ("Qualification 1", "Final Tiebreaker", FMS "Match 10 (R3)") is checked against
      the team numbers read; if they disagree, the teams win. Without a title, the old order applies: teams, then
      posted TBA time, then schedule order.
    - **Score version.** The results screen is found without knowing its look. It is a whole-frame read with >= 4 of
      the match's teams, no running timer and no other match's title. Its scene change places the appearance to
      0.25 s. Its title confirms or corrects the name.
    - **Cutter.** These streams put keyframes 5-7 s apart (Chezy median 5.7 s), so a keyframe cut gave 3-10 s of
      lead. Clips are now copied from the keyframe, then remuxed with an MP4 edit list that starts playback exactly.
      The padding is 3.5 s nominal at each end (3-5 s in effect given the ~0.5 s display offsets).
    - **First results.**
      - On the D2 Final Tiebreaker (FMS), named 2026cagle_f1m3 from the title (48 reads) and all six teams. Start
        and end were within 0.6 s of the audio cues. The score piece appeared 65 s after the end.
      - On C1 qm1 (Cheesy Arena), named 2026cc_qm1 from the title and teams. The end is exactly at the display's
        0:00.
      - Timer (processing start of the start minute -> clip on disk): 49-61 s. Projected live latency after the
        match end: 60-81 s, plus YouTube's delay.
17. **Chezy Champs 2026 Saturday, whole day (C1, 616 chunks; runs dev3 + dev3b, split by an accidental stop).**
    - **Matches.** 56 of 56 qualification matches were found and named right: 55 from the title and teams, and one
      by the teams overruling a stale title. There were no misses and no extras.
    - **Boundaries.** Every start came from the auto countdown and 55 of 56 ends from the display's 0:00. The
      clip-start minus TBA actual_time offset was 6.5-8.0 s, flat all day; lengths were 161.6-163.6 s. Three random
      clips were checked by eye: 0:19 shows one second after the start and 0:00 lands at the end.
    - **Timing.** The timer (processing start of the start minute to clip on disk) was 34.6-68.3 s, median 46.5 s.
      The projected live time after the match end was 17.7-82.9 s, plus YouTube's delay, plus the recorder listing
      the chunk 13-16 s after the platform got it.
    - **Score.** 55 of 56 score clips found their results screen without a template, 46-270 s after the end.
      Three of them were the broadcast's lower-third score bar over the commentators, which also passes the test.
    - **qm54 fixed.** The first attempt lost its overlay at 1:10 left and was replayed 5 minutes later, so it had
      taken the name and the real match became `_r2`. A teleop last seen more than 20 s before its zero is now
      `_partial` and does not use up the name. Pieces of one countdown merge across hides up to 150 s.
    - **False score piece fixed.** qm54's score piece was read off the end-of-match overlay, where one whole-frame
      read missed the "0:00". A read no longer counts as a results screen while the match's timer place still reads
      a time. Rerun of 34500-35800: `2026cc_qm54_partial`, then `2026cc_qm54`, with its results 75 s after the end.
18. **2025 Week 0, other game.**
    - **Kettering (W1).** The broadcast has no overlay (a fixed overhead camera with logos), and the FMS sounds
      barely register (a few false cues in 30 minutes). OCR finds nothing: 0 clips in 3.5 h, stopped. A stream with
      neither an overlay nor field sound needs another cue (robot motion); not attempted.
    - **Eagan Week Zero (W2).** It has its own home-made overlay (a "0:15" box and team panels), and is the check
      for another game. TBA has no matches for it, so names come only from the overlay.
    - **Eagan Week Zero (W2), 8700-16000 s (the first 2 h of play; the first 2.4 h were pre-event).** Its overlay is a
      reused 2024 CRESCENDO overlay: scores stay 0, the timer comes on part-way into auto, and it vanished at 0:40
      left in the first match.
      - Two fixes came from it:
        - **Backfill.** A new timer place is read on the last 4 s of frames, so the first value seen is 0:14, not
          0:10, and the auto is 15 s, not 10.
        - **Habit, not abort.** An overlay that vanishes at the same point as in an earlier match is the
          broadcast's habit, not a partial match.
      - **Truth from the field's FMS sounds (phase0/audio_check.py).** 11 matches (start sound plus end buzzer 153 s
        later). The overlay gave 9; the other 2 had no overlay at all.
      - **FMS sound fallback added to the pipeline.** A start sound followed one match length later by the end
        buzzer, with no overlay match there, is cut from the sounds. Eagan is now 11 of 11 (9 overlay, 2 audio).
        On C1 the fallback adds nothing false: all 28 start+buzzer pairs of the day fall inside overlay matches.
        Cheesy Arena plays FMS-like sounds too.
      - **Display offset.** Eagan's own timer runs ~1 s ahead of the field (start sound +0.5-1.0 s, end buzzer
        +0.9-2.9 s after the timer's), so the end margin is now 1.0 s: 3.5-4.5 s of padding in effect on every
        display seen.
19. **Final test: MadTown 2025 day 2 (M2), first match, cold, minute by minute (code sha256 ba6653998a25c59d).**
    - **Match.** 2025mttd_qm55, the first match on TBA's day-2 schedule, named from the overlay title (44 reads) and
      5 of its 6 teams; the B team shows as 9999 on the overlay. 418.01-570.94 s (152.9 s, right for 2025). The start
      came from the auto countdown and the end from the display's 0:00.
    - **Checked by eye.** 0:14 shows one second after the start and 0:00 lands at the end. Padding is 3.5 s before
      and 4.0 s after.
    - **Timer (the user's definition: processing start of the minute holding the start -> clip on disk).** 45.1 s.
      At live pace (`--pace 1`, minutes released when they would exist live): clip on disk 45.3 s after the match
      end, timer 193.8 s (the match itself takes 153 s).
    - **Bug found by the test.** The same match was cut twice (`qm55_r2`): a second timer was readable in the
      overlay's lower camera view. Fixed after the test: one match per zero, whatever place counted it. A rerun
      (post hoc) gives one clip, timer 46.4 s.
20. **MadTown 2025 Friday practice (M0, 0-3000 s).** The four practice matches in the stream were cut and named from
    the overlay title alone, since TBA has no practice matches: 2025mttd_p2, p3, p4, p5. The stream opens on
    "Practice 2" before its match; p1 was not streamed. All starts came from the auto countdown and all ends from
    0:00. Timer 34.8-42.7 s.
21. **MadTown 2024 at live speed, cold, on an event never seen (user, 11-04).** `phase0/vod_live.py` plays the saved
    stream from YouTube at real-time speed (`ffmpeg -re`) into chunks, listed 15 s after they close (the recorder's
    lag), and `streamsplitter live --no-record` follows them. B teams show under stand-in numbers (9993 = 254B).
    - **60 s chunks, from 1:23:12.** qm6 written 31 s after its end, qm7 88 s: qm7's end plus padding ran 3 s past a
      chunk boundary, so it waited for a whole chunk. Both named right (title + the teams on the schedule).
    - **Recorder chunks now 20 s** (`record.py` CHUNK_S; the follower re-reads 300 s, not 5 chunks, after a restart;
      a timer found near a chunk's start is backfilled from the previous chunk). Rerun from 1:32:40: qm7 27 s,
      qm8 33 s after the end; chunks read in 3-7 s. The two runs' qm7 clips are frame-identical at the end.
    - **Score clips missed with stand-in numbers.** qm7's results screen showed only 3 schedule teams (< 4). The
      numbers the overlay showed through the match now count too (`numbers_shown`); the 20 s chunks rerun offline
      gives qm7_score and qm8_score.
