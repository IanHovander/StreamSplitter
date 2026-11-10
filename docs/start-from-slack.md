# Start from Slack

With Start from Slack on, you can start and stop a recording from your phone. Send a message to the StreamSplitter
app in Slack: `split 2026mttd` starts recording that event on the computer, `stop` stops it. Admins can also change
every setting and let other people in, all from Slack. There is no server and no open port: the computer asks Slack
for new messages (every 20 seconds, and every 3 seconds for 2 minutes after you write).

Turn it on in the setup: `run.bat` (Mac/Linux: `sh run.sh`), *Change a setting*, *Start from Slack*; or
`run.bat --section listen`. It needs Slack set up with an alerts contact, and the Slack app's `im:history`
permission (the setup walks you through adding it if the app lacks it). It is set per computer: the team settings
file does not carry it.

## Two ways to listen

**In the background** (recommended for a computer left on at home). It starts by itself, hidden, each time you log
in to the computer, and keeps listening until you turn it off. While it runs:

- the computer does not go to sleep (the screen may still turn off);
- an event started from Slack is kept going: if its recording stops by itself (a crash, a reboot), it is started
  again and you get a DM; at most 3 times an hour;
- the morning after the event's last day (06:00, the computer's time) the recording stops and you get one line:
  clips made, uploads still waiting;
- if the listener itself stops, you get a Slack DM 6-7 minutes later: "The StreamSplitter listener on *computer*
  stopped at *time* ...". (Not sooner: Slack still posts an alarm that was cancelled less than about 5 minutes before
  it was due, so each one is cancelled 6 minutes ahead; publish/notify.py.)

**Only while open in a window** (for a laptop someone sits at). Nothing starts at log-in and nothing runs in the
background. Open it from the menu (`run.bat`, *Listen for Slack commands in this window*) or type
`streamsplitter listen`. The window says where it listens, then one line per message:

```
21:04 Listening for `split <event>` in Ian H's DM. Close this window to stop listening (a recording keeps going).
21:06 Ian H: split -> Starting MadTown Throwdown (2026mttd) on HOMEPC now; its "StreamSplitter started" message ...
```

Closing the window, or Ctrl+C, stops listening only: a recording started from Slack runs on its own and keeps
going. You get a DM when it starts listening and "No longer listening for Slack commands on *computer*" when it
stops. There is no stopped-running alarm in this mode, since closing the window is on purpose.

## Who may do what

- **Admins** change settings, add and remove people, and change a running recording (`set stream`, `set teams`,
  `pause`, `resume`). They can do everything people can do too.
- **The owner** is the admin who gets the alerts (the person the setup picked for alerts). There is always one.
- **People** may use `split`, `stop`, `status`, `yes`, `no`, `people` and `help`.

`set owner @name` sends the alerts to someone else (it asks first). **The old owner stays an admin**, so the person
who set StreamSplitter up keeps the right to fix settings during the event (before 2026-11-10 they lost it). An admin
makes someone else an admin with `add admin @name`, and takes it back with `remove admin @name`. The owner cannot be
removed, only replaced, so there is always an admin. Anyone allowed can reply `people` to see who the admins are.
Every "no" says who can do it and what to ask them for, for example:

```
Only admins can change settings and people: Jamie Lee and Ian H. Ask one of them to do it, or to make you an
admin (they reply add admin @Sam K). Reply help for what you can do.
```

Nobody is set up in advance: the setup asks only for the alerts contact. Admins add people later from Slack.

**Nobody gets a message they did not ask for.** Adding someone sends them nothing. The app then asks "Send them a
short how-to note? Reply yes or no", and sends it only on `yes`. People who were added open the StreamSplitter app in
Slack (under Apps), type a command there, and the answer comes back in that chat. Alerts go only to the owner. The
one exception: when the owner hands over (`set owner @name`), the new owner is told, since the alerts go to them from
then on.

**Updating from an older version where the alerts were already handed over?** The old owner is then an ordinary
person. The owner fixes that with `add admin @<old owner>`, or the old owner makes themselves the owner again on the
computer: `run.bat --section notify`.

## The commands

Send them to the StreamSplitter app in Slack (the same chat where its alerts arrive), one per message. Capitals, a
full stop at the end and Slack formatting (`code`, *bold*, a pasted link) do not matter. `help` shows what is going
on and the few commands that matter right now; `help all` lists every command; `help <command>` explains one
(`help split`, `help set`, `help add`).

**Recording** (everyone allowed):

| Type | What happens |
|---|---|
| `split 2026mttd` | Starts recording that event at once, when it is on today or tomorrow, and answers with what it started: the stream, where the clips go. Reply `stop` if something is wrong. (`start` and `record` work too.) |
| `split madtown` | Searches this year's events by name, short name or city. Several hits: they are listed with their codes. One hit: the same summary, then **"Reply yes to start recording, or no."** |
| `split 2026casj` | An event that is not on today or tomorrow: it asks first, and says why. |
| `split 2026casj yes` | Starts without asking. |
| `split` alone | Asks whether to start the event set up on the computer. |
| `yes` / `no` | Answers the question asked last, only from the person who was asked (`ok`, `sure` and `cancel` work too). A question waits 15 minutes; a split question reminds you once after 5 minutes and says when it has expired. A new question replaces the old one. |
| `stop` | "Stop recording MadTown Throwdown (2026mttd)? Reply yes". After `yes` it stops the recording and says when it has. `stop yes`, or `stop 2026mttd`, stops at once. |
| `status` | Whether a recording runs and how (the stream, the clips, the uploads), and whether the Slack listener runs and how quickly it reads your chat. `list` works too. |
| `people` | Who may use the app, and who the admins are. |
| `help`, `help all`, `help <command>` | What you can do now; every command; one explained. |

**The running recording** (admins). While nothing records, these are saved for the next start.

| Type | What happens |
|---|---|
| `set stream <link>` / `set stream auto` | Record this YouTube or Twitch link instead / go back to the link The Blue Alliance lists. (`stream <link>` works too.) |
| `set teams 5026, 254` | Whose matches go to the team's Slack channel (`set teams none`: nobody's). (`teams 5026` works too.) |
| `pause youtube` / `resume youtube` | Hold the uploads, or send them. Also `drive` and `wiki`. |

**Settings** (admins):

| Type | What happens |
|---|---|
| `settings` | Every setting with its value and the exact command that changes it, grouped by area. `settings youtube` shows one area. |
| `set <area> <name> <value>` | Changes one, checked as the setup checks it. The reply says when it applies. `help set` lists them all. |
| `set event 2026mttd` | The same summary as `split`; `yes` saves the event without starting it. |
| `set listener off` (or `stop listener`) | Stops listening now and no longer starts at log-in. It asks first. Turning it on again is done on the computer. |

The `set` commands, by area: `set slack on|off`, `set slack channel #name`, `set slack size 720|full`;
`set youtube on|off`, `set youtube privacy unlisted|public|private`, `set youtube matches all|others`,
`set youtube playlist <name>`, `set youtube title <format>`, `set youtube description <format>` (several lines are
fine), `set youtube tbalinks on|off`, `set youtube version score|clip`; `set drive on|off`,
`set drive size 720|540|full`, `set drive names <format>`, `set drive version score|clip`; `set wiki on|off`,
`set wiki private on|off`, `set wiki page <title>`, `set wiki embed <markup>`, `set wiki version score|clip`;
`set listener alarm on|off`, `set listener awake on|off`.

**When a change applies.** A recording reads its settings when it starts. So most changes apply when the next
recording starts; the reply says so, and says when one is running with the old setting. `set stream`, `set teams`,
`pause` and `resume` change a running recording at once. People, admin and owner changes apply at once.

**Changes that ask first** (reply `yes` within 15 minutes, or `no`): YouTube public; The Blue Alliance video links
(only with the write key already saved; the warning is shown); YouTube at an official FIRST event; a new owner;
turning the listener off.

**People** (admins):

| Type | What happens |
|---|---|
| `add @Sam` | Type `@` and pick them, or part of their name, or their email. Several matches: they are listed by number; reply with the number. Then "Send them a short how-to note? Reply yes or no". |
| `add admin @Sam` | The same, as an admin (or makes someone already added an admin). |
| `remove @Sam` | Takes it away. They get no message. `remove admin @Sam` keeps them as a person. |
| `set owner @Sam` | The alerts go to Sam (it asks first). You stay an admin. |

**A message it does not understand** gets what it probably meant ("Did you mean `pause youtube`?"; `TBA` gets "Did
you mean `set stream auto`?"), or else the three or four commands that matter right now. It never does a guessed
command by itself.

**Only on the computer.** Slack keeps every message, so passwords and keys are never typed into Slack. These are
set on the computer with `run.bat --section <step>` (Mac/Linux: `sh run.sh --section <step>`); asking for them in
Slack answers with the exact command (`help computer` lists them):

| What | Step | Why |
|---|---|---|
| the Slack app's token | `slack` | a password |
| the YouTube login, and YouTube's Terms of Service | `youtube` | a Google sign-in in the browser there, and a person's own OK to the terms |
| the Google Drive login and its folder | `drive` | a Google sign-in; a new folder must also be shared with the team in Drive |
| The Blue Alliance read key | `tba` | a password |
| The Blue Alliance write key (video links) | `tbalinks` | a password |
| the wiki's address, bot name and password | `wiki` | a password, checked against the wiki as it is typed |
| the team settings file | `team` | it carries every password |
| how the listener runs (background or window), and turning it on | `listen` | it adds or removes the log-in start and starts a program there |
| the test upload | `test` | a person checks the sample video on each site |
| the folders (`data_root`, `work_root`, `deliver_dir`) | config.toml | folders on that computer |

If a message looks like a token or key, the app does not use it or repeat it. It says to revoke it and make a new
one (it is in Slack's history now), then enter the new one on the computer.

## Who answers, and how quickly

**One answer per message.** While the listener runs, it reads every allowed person's chat and answers every
message. A command for the running recording (`status`, `set stream`, `set teams`, `pause`, `resume`) is passed to
the recording on the same computer (through files in its folder), and the recording's own answer comes back in your
chat, usually within a second or two. The recording itself reads no Slack then. Without the listener (Start from Slack
off, or not running), the recording reads the owner's chat itself and answers everything there, settings and people
included; `split` and `stop` then say that Start from Slack is not running. (Before 2026-11-10 both programs read the
owner's chat and could both answer one message, sometimes contradicting each other.)

A recording started before an update keeps working as before until it restarts: it answers in its owner's chat, and
the listener answers only what the older listener used to. Restart both after an update (`stop`, then `split` again).

**How quickly.** The owner's and the admins' chats are read every 20 seconds. Someone who wrote in the last 10
minutes, or has a question waiting, is read every 20 seconds too (up to 5 people at once with the admins). Everyone
else is read in turn, about every 3 seconds times the number of people, and at least every 20 seconds: 36 seconds
for 12 people, a minute for 20. **After you write, your chat is read every 3 seconds for 2 minutes** (up to 3 chats
at once), so a conversation goes quickly. All of this stays under Slack's limit of about 50 reads a minute: never more
than 45 go out in any minute, however many people are added. `help` and `status` say how quickly your chat is read.

**:eyes: on your message** means the app has read it and is working on it. That needs the app's optional
`reactions:write` permission (apps made from the manifest since 2026-11-10 have it). Without it, nothing changes:
the app just answers. To add it to an older app: <https://api.slack.com/apps>, **StreamSplitter**, **OAuth &
Permissions**, **Add an OAuth Scope**, `reactions:write`, then **Reinstall to Workspace** (the token stays the same).

**After a restart.** The listener keeps the event it keeps going, the questions waiting and how far it has read, so
`status` still names the event and a `yes` still answers its question. A message sent in the 15 minutes before it
started again is answered; older ones get one note ("StreamSplitter was not reading Slack when you sent 2 messages
..., so they were not done. Send again what you still want."), and nothing in them is done.

What a start looks like:

```
you:  split 2026mttd
app:  Starting MadTown Throwdown (2026mttd) on HOMEPC now; its "StreamSplitter started" message comes here in
      about 30 s. YouTube playlist: https://...
      Stream: https://www.youtube.com/@Madtown_Throwdown/live (from The Blue Alliance).
      Slack: your team's matches (5026) go to #errata.
      YouTube: every match, unlisted, to the playlist "2026 MadTown Throwdown".
      Google Drive: every match, to the folder "FRC match videos/2026 MadTown Throwdown".
      Wrong event or setting? Reply stop to stop it.
app:  StreamSplitter started on HOMEPC for MadTown Throwdown (2026mttd): ... Reply help for what you can do;
      replies come within about 20 s.
```

When it starts, the event is saved as the setup's event step saves it: the old event's playlist and Drive event folder
are forgotten, and this event's YouTube playlist is made. At an official FIRST event (a regional, district or
championship event) YouTube is off, as in the setup: the summary says `YouTube: off (official FIRST event)`. If the
playlist cannot be made, the event is recorded without YouTube and the reply says how to add it.

One recording per computer: `split` while another event records says to `stop` it first.

## What the computer needs

- On and logged in all event weekend (locked is fine) for the background mode, or the window left open for the
  window mode. Plugged in; closing the lid set to do nothing.
- **Windows Update:** set *Active hours* to cover the event days (Settings > Windows Update > Advanced options), or
  *Pause updates* for the weekend. After an update restart, nothing listens until someone logs in.
- Home internet: about 6 Mbit/s download all event day for the stream, plus upload for the clips
  ([requirements-and-limits.md](requirements-and-limits.md)).
- The rest is as for any recording: about 100 GB free disk, StreamSplitter set up with `run.bat`.

## Troubleshooting

| Problem | What to do |
|---|---|
| No answer at all | Wait as long as `help` said (20 s for admins; longer with many people). Is the listener running? `run.bat`, *Show the setup summary*, or `streamsplitter listen status`. Background mode: log in, or `run.bat` and *Start listening for Slack commands*. Window mode: open its window. Without a listener, only the owner's chat is read, and only while a recording runs. |
| "Your Slack app needs one more permission" | Add `im:history` to the app and reinstall it ([setup-slack.md](setup-slack.md)), then `run.bat --section listen`. |
| "There is no Blue Alliance key on ..." | `run.bat --section tba` on that computer. |
| "It was restarted 3 times in the last hour" | The recording keeps failing: read its log, `<data folder>/logs/live_<event>.log`, then `split <event>` again. |
| "The recording on ... did not take that within 20 s" | The recording is busy or stuck: reply `status`; if it does not answer, stop it on the computer and `split` again. Nothing of that command was done. |
| "The recording running now started before an update" | Restart it (`stop`, then `split <event>`): from then on every admin can change it. |
| The alarm says the listener stopped | The computer restarted, slept, or lost power or internet. Log in; it starts by itself. |
| It should stop starting at log-in | `set listener off` in Slack, or `run.bat`, *Change a setting*, *Start from Slack*, *Off* (or *Only while it is open in a window*). |
| Someone added gets no answer | Their first message can take a minute with many people (`people` says how long); after that they are read every few seconds. A message sent before they were added is never answered: send it again. Is the listener running? |
| "Only admins can ..." | Ask one of the admins it names to do it, or to make you an admin (`add admin @you`). An admin can always fix settings on the computer with `run.bat`. |
| A password or key was sent in Slack | Revoke it and make a new one, as the reply says; delete the message; enter the new one on the computer. |

The listener's log is `<data folder>/logs/listener.log` (background mode); its files are in `<data folder>/listen/`.
How it works inside: the docstrings of `streamsplitter/listener.py`, `streamsplitter/publish/commands.py` and
`streamsplitter/slackadmin.py`.
