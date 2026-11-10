# Results

## MadTown Throwdown 2026 (2026mttd, November 6-8), the first full event

One home PC recorded the webcast all weekend, started and stopped from Slack. Nobody ran it at the venue.

| What | Number |
|---|---|
| Matches The Blue Alliance lists | 87 (72 qualification, 13 semifinal, 2 final) |
| Matches with a clip on YouTube | 83 of 87 |
| Matches with a clip on Google Drive | 83 of 87 |
| Missed | 4: qm55, qm56, qm57 and semifinal 11. No clip was cut; the cause is not found yet |
| Our own matches posted to the team's Slack channel | 9 |
| Clip on Google Drive | 1 to 1.5 minutes after the match ended |
| Score version on Google Drive | 2 to 3 minutes after the match ended |
| Wait from "clip ready" to "sent" | median 35 s YouTube (longest 99 s), 20 s Google Drive, 19 s Slack |
| Videos on The Blue Alliance when the event ended | 48 of 87, all ours; 35 more can still be added |

What went wrong, and what changed because of it:

- **The stream.** The Blue Alliance listed YouTube day videos that never went live; the event streamed on Twitch,
  and that stream glitched all Saturday. A team's own YouTube stream was clean but on no list. StreamSplitter now
  searches for such a stream every 5 minutes and moves to it ([event-day.md](event-day.md)).
- **YouTube's daily limit.** Saturday's 54 uploads from the glitchy stream were wasted: the same matches were cut
  again from the better stream, and the 100 uploads a day ran out at 20:44. The rest waited until midnight Pacific.
  Over the weekend, 83 matches took 161 uploads.
- **A wrong match.** A "Field Break" countdown sat where the match timer sits, and its end was taken for the
  finals. Fixed ([lessons/detection.md](lessons/detection.md)).
- **Slack.** Commands were hard to guess and slow to answer. They were rewritten after the event
  ([start-from-slack.md](start-from-slack.md)).

## Earlier tests

All on saved webcasts ([phase0/PLAN.md](../phase0/PLAN.md) has the full record).

| Webcast | Result |
|---|---|
| Chezy Champs 2026, Saturday | 56 of 56 qualification matches found and named |
| Eagan Week Zero 2025 (home-made overlay) | 11 of 11, 2 of them with no overlay, cut from the field sounds |
| MadTown 2025 | right on the first match of day 2 and on 4 practice matches |
| MadTown 2024 at live speed | clips 27-33 s after each match ended, named right |
| Kettering Week 0 2025 | failed: no overlay, field sounds inaudible |

More numbers, and what a team needs: [requirements-and-limits.md](requirements-and-limits.md).
