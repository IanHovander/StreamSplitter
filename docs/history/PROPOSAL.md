# StreamSplitter: proposal (history)

**History: the plan written on 2026-11-03, before any code.** Much of it changed while building (detection reads
the overlay by OCR, not field sounds; clips go to Slack, YouTube and Google Drive). For what exists now, start at
[AGENTS.md](../../AGENTS.md). Kept for the reasoning behind the first choices.

Draft for review, 2026-11-03, revised the same day with the user's answers to the open questions (section 2).
Nothing is built yet; this folder holds only this document.

## 1. Goal

Record an FRC event's live stream (YouTube first, Twitch later) and save each match as its own video file
within a few minutes of the match ending. This is for events that publish only the stream and never split it into
matches afterwards. The clips stay local, and later team5026.com serves them to logged-in team members.

The tool must run on **any team member's computer**, so the team never depends on one machine 100+ miles away
staying on. Several members can run it at the same time; each copy works on its own.

## 2. Decisions so far

| Question | Answer |
|---|---|
| Where the code lives | a `StreamSplitter` folder inside the research project for now, self-contained (it imports nothing from the research code). It moves to its own team GitHub repo once it works. |
| Which computers run it | Any team member's computer (Windows, macOS, Linux), several at once. The author's PC is just one of them. |
| How fast clips appear | Within a few minutes of the match ending, so live recording with detection as it happens. |
| Which streams | 5026's own events plus a watch list (a few streams at once). |
| How it starts | One command: `run.bat` / `run.sh` installs what it needs and opens a local status page. |
| Where clips go in v1 | They stay on the computer that made them. Sharing between computers and the website come later. |
| What a clip contains | Two versions. **Match clip**: 3 s before the match starts to 3 s after it ends. **Score clip**: the same match clip with the score-reveal announcement appended at the end; the dead time between the match and the reveal is skipped. |
| Padding accuracy | Cutting without re-encoding gives 3 s up to about 5 s of padding; that is fine. Re-encoding stays an optional switch, off by default. |
| Score-reveal piece | Starts 3 s before the results screen appears. It ends at whichever comes first: just before the screen is removed and stays removed for more than 30 s, or 30 s after the screen appeared. Shorter removals (a camera cut and back) do not end it. |
| Practice matches | Clipped too, labelled as practice. |
| Quality | Keep the stream's 1080p as-is; 720p is a setting, off by default. |
| Raw recording | Kept 28 hours, then deleted, but never before every clip it covers (including score clips) is finished. |

## 3. What a match looks like on the stream (2026 numbers, from `seasons/season_2026.json`)

- Field sounds (manual 5.12): Cavalry Charge at the start of the match, buzzer at the end of AUTO, 3 Bells at the
  start of TELEOP, steam whistle at END GAME, buzzer at the end of the match.
- Clock: AUTO 0:20 to 0:00, a hold of about 4 s, then TELEOP 2:20 to 0:00. About 164 s from the start sound to the
  end buzzer. Scoring counts for 3 s after 0:00, so the 3 s end padding covers it exactly.
- FIRST's scoreboard overlay shows the clock, both scores and the six team numbers. We measured two layouts in 2026
  (compact at casnv/mawor, wide at necmp1). On some streams the overlay clock runs slow: 1.17 % on casnv.
- The results screen (final scores, the match, the teams) comes up a few minutes after the match, once the referees
  commit the score.
- So a match clip is about 2:50, roughly 130 MB at 1080p60 (6 Mbps). The score clip adds about 20 s.

## 4. Options considered

### 4.1 Capturing the stream

| Option | Good | Bad | Verdict |
|---|---|---|---|
| **yt-dlp → ffmpeg, saving 1-minute chunks** | Handles YouTube and Twitch; we already use it; refreshes stream URLs that expire; can rewind with `--live-from-start` to recover after a crash | YouTube changes break it every few weeks, so it needs frequent updates | **Use for YouTube** |
| streamlink → ffmpeg chunks | Best Twitch support (ad handling, login tokens); YouTube too | One more tool to install | **Add for Twitch** |
| ytarchive | Very robust from-the-start YouTube archiving | YouTube only; a Go binary to ship | Fallback if yt-dlp's live mode breaks |
| ffmpeg pointed straight at the HLS URL | Fewest parts | The URL expires after hours; no re-resolution | No |
| Wait for YouTube's saved video after the stream, then split it | Simplest; no live machinery | Not "within minutes"; long streams may never be saved; the video can be taken down | Later, as a **repair path** only |

Chunks are MPEG-TS copied straight from the stream, with no re-encoding: nearly zero CPU, and a crash loses at
most the chunk being written. A small SQLite index records each chunk's stream time and the wall-clock time it
arrived.

### 4.2 Finding the match start and end

| Approach | How | Names the match? | Weak when |
|---|---|---|---|
| **A. Schedule API** (TBA, FRC Events; Nexus at Nexus events) | The API gives each match's FMS start time; add the stream delay we measure | Yes | Results post late or never (offseasons); the stream delay changes after a restart; the API alone can't tell if the stream was showing the field |
| **B. Field sounds** | Match the five FMS sounds against reference clips | No | Field audio is quiet, muted or talked over; sound tests between matches |
| **C. Scoreboard clock + team numbers** | Read the m:ss clock and the six team numbers on the overlay | Yes (teams + schedule) | Custom overlays (Champs, offseasons); overlay hidden; instant replays shown with the overlay |
| D. Trained frame classifier | A CNN for "match in progress" | No | New production styles; needs training data. Not needed |
| E. Saved-video splitting | A–C run after the stream ends | n/a | Not live (see 4.1) |

**Recommendation: combine A, B and C.** Each is cheap, and each fails on different streams:

- **Confident clip:** two signals agree within about 1.5 s.
- **One signal only:** still cut, with wider padding, and flagged "check me".
- **API only:** place the match with the day's measured stream delay, pad by about 15 s, and flag it.

The research code already reads the clock with digit shapes it learns from the video itself (no OCR engine). It
also detects all nine 2026 field-sound events reliably, with timing to within ±0.5 s. Both were proven on casnv,
necmp1 and mawor. They need porting from whole-file analysis to a version that runs on chunks as they arrive.

### 4.3 Naming the match (which match is this?)

1. The six team numbers read from the scoreboard pick out a unique scheduled match. This is immediate and works
   for a team's first match too.
2. Otherwise, the nearest API start time after correcting for the stream delay.
3. Otherwise, the next unplayed match in the schedule.

The score clip confirms the name: the final scores on the results screen must equal the API's scores.

### 4.4 Cutting

| Option | Padding accuracy | Cost | Verdict |
|---|---|---|---|
| **Stream copy** (no re-encode) | Can only cut at keyframes, so padding is 3 s up to about 5 s (YouTube keyframes are usually 2 s apart) | Instant, any laptop | **Default** |
| Re-encode | Exactly 3 s | Fast on computers with NVIDIA, Intel or Apple hardware encoders; minutes per clip on CPU-only laptops | Optional switch, off by default (3-5 s accepted 2026-11-03) |

The score clip is two pieces, the match and the reveal, joined into one MP4 (faststart, so browsers can play it
directly from the website later).

### 4.5 Running on many computers

| Option | Verdict |
|---|---|
| **Independent recorders**: every volunteer records the same stream and cuts their own clips | **v1.** No coordination needed; bandwidth is only ~6 Mbps per stream. Each clip's metadata (match key, confidence, recorder id) is designed so a later upload step can keep the best copy per match. |
| Leader + standby: one records, others take over when its heartbeat stops | Needs a shared server; takeover gaps; later at most. |
| One central server | This is the single point of failure the team wants to avoid. |

If every recorder was down for a while, the next one to start fills the gap from YouTube's rewind window, when that
window still covers it. After the stream ends, a repair step can fetch any missed minutes from the saved video.

## 5. Proposed design

```
 TBA (schedule, webcast links, start times, scores)
        |
 [schedule] ---------------------------------------------+
        |                                                |
 [source] yt-dlp / streamlink --> [recorder] ffmpeg --> 1-min .ts chunks + chunk index (SQLite)
                                                         |
                       +---------------------------------+------------------+
                       |                                 |                  |
                [audio detector]                 [scoreboard reader]  [results-screen detector]
                 field sounds                     clock, teams         final scores
                       +----------------+----------------+------------------+
                                        |
                                   [matcher]  state machine: idle > AUTO > hold > TELEOP > end (+3 s)
                                        |      fuses the signals, names the match, handles replays and aborts
                                   [cutter]   match clip now; score clip once the reveal has aired
                                        |
                       clips/<event>/<match>.mp4 + <match>_score.mp4 + <match>.json
                                        |
                              [status page] http://localhost:<port>
                     streams, recording health, which signals work, clips, warnings
```

Code layout (planned):

```
StreamSplitter/
  run.bat, run.sh          install uv if missing, then `uv run streamsplitter`
  pyproject.toml           yt-dlp, numpy, opencv-python-headless, av, a static ffmpeg package
  streamsplitter/
    sources/               youtube.py (yt-dlp), twitch.py (streamlink, phase 3)
    recorder.py chunks.py  chunking, supervisor (restart, re-resolve, catch-up), chunk index
    detect/                audio.py, clock.py, results.py   (ported from overlay_reader.py)
    matcher.py cutter.py schedule.py keepawake.py
    seasons/2026.toml      timings and field-sound templates (one file per season)
    status/                local web page
  tests/
```

### Output

- `clips/<event>/<event>_qm12.mp4` (match clip) and `<event>_qm12_score.mp4` (score clip).
- Practice matches are `<event>_p7.mp4`. TBA does not list practice matches, so practice names come from the FRC
  Events API's practice schedule, which needs a free FRC Events key. Without one, practice clips are named by time
  and the team numbers read off the scoreboard (`<event>_practice_<time>_<teams>.mp4`). Practice schedules often
  change at the event, so the team numbers decide the name, not the slot.
- A replayed match keeps both attempts: `_r2` marks the later one.
- Anything not matched to the schedule is kept as `unmatched_<time>.mp4`, never deleted.
- `<event>_qm12.json` records the teams, the cut times in stream time and wall-clock time, which signals agreed,
  the confidence, the recorder id, the stream URL and the software version.
- The data folder is chosen at first run (on the author's PC: a 2 TB data disk).

## 6. Portability

- **Install:** `run.bat` / `run.sh` fetch [uv](https://docs.astral.sh/uv/), which installs the right Python and the
  dependencies, including a static ffmpeg. Recent yt-dlp versions also want a JavaScript runtime (Deno or Node) for
  YouTube; the installer checks for one and fetches it if missing (confirm in phase 1).
- **yt-dlp updates on every start**, because YouTube breaks old versions. streamlink is the second route.
- **Keep-awake:** the computer is kept from sleeping while recording (Windows, macOS and Linux each have a call for
  this). The status page warns about lid-close and Windows Update restarts.
- **Disk:**
  - Raw chunks are kept 28 hours (rolling buffer), then deleted, never before their clips are finished.
  - At 1080p that buffer holds about one full event day per stream: 25-35 GB. Clips add about 15-20 GB per event
    for each version.
  - The tool checks free space at start and refuses to record a stream without about 40 GB free for it (each
    stream on the watch list needs its own share).
  - If space runs low while recording, it deletes the oldest raw chunks whose clips are finished, then warns.
    Clips are never deleted.
  - Optional 720p recording halves disk and bandwidth.
- **Light on resources:** recording is a stream copy. The audio detector is tiny. The scoreboard reader decodes a
  small crop a few times a second, using hardware decoding where available. Target: a 4-core laptop handles two or
  three streams (measured in phase 1).
- **Keys:** a TBA read key goes in a local config file that is never committed; the team can share one key. Without
  a key the tool still works from a pasted stream URL, using sounds and the scoreboard, and names clips by teams and
  time.

## 7. Edge cases

| Case | Handling |
|---|---|
| Match replayed after a field fault | Both attempts saved; the later one is marked `_r2` |
| Match aborted partway | Clip runs until the clock stops + 3 s, flagged "aborted" |
| Instant replay shown with the scoreboard | The clock runs without a start sound and the match is already clipped, so it is saved as unmatched |
| Sound tests or timeouts between matches | No clock run follows, so nothing is cut |
| Stream drops; a new video each day | The supervisor re-resolves the stream from TBA's webcast list or the channel's /live page; gaps are logged; catch-up from the rewind window |
| Broadcast cuts to another field mid-match (we saw this at necmp1 qm4) | The clock jumps or the team numbers change, so the clip is flagged |
| No results screen aired | No score clip; noted in the JSON |
| Event running late or schedule changed | Naming uses the teams read and the actual start times, not predicted times |

## 8. How we will know it works

**Phase 0 test bed:** the 2026 season's full-day stream videos are still on YouTube. A "fake live" harness feeds a
day through the same pipeline at real speed or faster, so chunks arrive exactly as they would live.

**Truth:**
- TBA's actual start times and scores for every match.
- On a sample of matches, exact boundaries from the research repo's offline overlay reader (already validated).
- TBA's official per-match videos, where they exist, for a visual check.

**Scorecard per day:**
- recall: played matches with a clip
- naming accuracy
- start and end error (target within ±1 s, so 3 s padding never cuts into the match)
- false clips
- score clips found
- delay from the end buzzer reaching us to the clip being ready (target ≤ 2 min for the match clip)

**Days:** development days from different production styles (casnv: compact overlay, 60 fps, field audio; necmp1:
wide overlay, 30 fps, division cuts; one Champs division day). One more event-day stays sealed until the end and is
scored once.

**Standard:** 99 % of matches captured and named correctly. One day is about 100 matches, which cannot prove 99 % on
its own (0 misses in 100 still allows up to about 3.7 % at 95 % confidence), so days are pooled and the confidence
interval is reported with every number.

**Live soak:** at least 24 h on any YouTube live stream, with forced kills, network drops and stream restarts, on
Windows plus at least one Mac or Linux machine. Then a real FRC stream: an offseason event if one is live this
winter, otherwise week 0 of 2027.

## 9. Phases

| Phase | Builds | Done when |
|---|---|---|
| **0. Detection proof (offline)** | Audio, clock and results detectors ported to work on chunks; matcher; cutter; fake-live harness; scorecard | The dev days meet the scorecard targets; then the sealed day is scored once |
| **1. Portable recorder** | Package, `run.bat`/`run.sh`, recorder + supervisor + chunk index, keep-awake, status page, TBA schedule and webcast lookup | 24 h soak with no unexplained gaps on Windows plus one Mac or Linux machine; fresh install on a team member's computer from one command |
| **2. Live end to end** | Detection running on live chunks; both clip versions; stream-delay calibration; repair from the saved video | Fake-live day replay matches phase 0 numbers; a real live stream produces named clips within ~2 min |
| **3. Watch list + Twitch** | Several streams per computer, Twitch via streamlink (Twitch ads can replace live footage for non-subscribers; fix with a login token or repair from the Twitch VOD), move to the team GitHub repo, one-page volunteer guide | Two volunteers record the same event independently and both produce complete clip sets |
| 4. Later | Upload to shared storage with best-copy-per-match de-duplication; team5026.com login and player | Separate plan |

## 10. Risks

- **YouTube breaks yt-dlp** (it happens every few weeks). Mitigations: update on start, streamlink as a second
  route, ytarchive as a third.
- **Streams without usable field audio, or with custom overlays.** Each stream's status shows which signals work;
  two of three is enough.
- **Volunteer computers sleep, close or restart.** Mitigations: keep-awake, warnings, several volunteers, catch-up
  from the rewind window.
- **Terms.** YouTube's terms restrict downloading, and FIRST owns the webcast. Team-internal use behind a login is
  the plan; check with FIRST before anything public.

## 11. Settled 2026-11-03

The five open questions are answered and recorded in section 2: practice matches clipped, 1080p, 3-5 s padding
by stream copy, the score-reveal rule, and 28 hours of raw recording.

Still to arrange (not blocking phase 0): a free FRC Events API key, for practice-match names (section 5).

## 12. What we reuse from the research repo (ported, not imported)

- `overlay_reader.py`:
  - clock location, self-learned digit templates, countdown labelling, discontinuity checks (sections 3-5)
  - results-card reader (section 9)
  - field-sound onsets and self-template matching (section 8)
- `seasons/season_2026.json`: period timings and the field-sound table.
- `download_match.py`: yt-dlp format choice (H.264 in MP4, up to 1080p) and ffmpeg discovery.
- `tba_index.py`, `tba_matches.py`: TBA fetching (`actual_time`, `post_result_time`, scores, videos).
- `docs/phase0-broadcast-layer.md`: measured clock and audio behaviour per stream.
- The necmp1 division-cut finding (`tracklock/staged_crop/cuts.py`): screen for cuts to another field.
