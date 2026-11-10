# Videos onto The Blue Alliance

The footage belongs to the event. Ask its organizers before you link videos of their webcast on The Blue Alliance
(TBA); if they upload their own, leave TBA to them.

```
run.bat tba                    # report: what TBA has, what this computer uploaded, how to add the rest
run.bat tba --event 2026mttd   # another event than the one set up
run.bat tba --all              # also the matches TBA already has a video for
run.bat tba --retitle          # put each match's TBA id in its YouTube title: "... (qm12)"
run.bat tba --yes              # do not ask before adding links or renaming
```

The report changes nothing. It reads this computer's finished YouTube uploads, picks the best one per match (the
latest play, then the newest upload; partial clips and private videos are left out), and checks on YouTube that
each still exists when the computer is logged in.

## Without a write key (most teams)

1. `run.bat tba --retitle` adds the match id to each title that lacks it. This costs 50 YouTube quota units per
   video, about 200 videos a day.
2. `run.bat tba` prints two links: TBA's playlist page for the event
   (`https://www.thebluealliance.com/suggest/event/video?event_key=<event>`) and your playlist.
3. On TBA's page, signed in to TBA: paste the playlist link, click *Submit Playlist*, check the matches it filled
   in, click *Add Suggestions*. TBA reads each video's match from the end of its title, which is why step 1 comes
   first. A TBA moderator reviews the suggestions; the videos appear after that.

To have new uploads carry the id from the start: `run.bat --section tbalinks`, answer no to the write key and
yes to the title question. (It adds `[ ({tba_id})]` to the end of the title format.)

## With a write key (event organizers)

TBA gives the organizers of an event a write key for it: ask at `https://www.thebluealliance.com/request/apiwrite`.
Save it with `run.bat --section tbalinks`. It is kept in the password store like every other key.

- `run.bat tba` then adds the links itself, after asking.
- During an event, `tba_links` under `[youtube]` (the same setup step turns it on) adds each video as soon as it
  is uploaded.
- Links go one match per request: TBA drops a whole request that names a match it does not have.

## At MadTown 2026

TBA listed 87 matches. 48 had a video when the event ended, all of them this computer's uploads. `run.bat tba`
found 35 more that could be added; 4 matches had no clip ([results.md](results.md)).
