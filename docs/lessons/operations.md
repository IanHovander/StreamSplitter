# Lessons: the computer and the people

Each line: what happened, then what to do.

- **Nobody needs a laptop at the venue.** It records the webcast, so a computer at home with good internet does
  the work, and teammates start and stop it from Slack on their phones.
- **One computer per event.** Two that publish the same event post every match twice and split YouTube's 100
  uploads a day. A backup computer can record with publishing off.
- **A running program keeps its old code.** After an update, the recording and the Slack listener ran the old
  version until they were restarted, and fixes seemed not to work. Restart both after every update.
- **Keep the folder out of cloud-synced places, on a short path.** A synced folder uploads 700 MB of packages. A
  path longer than about 150 characters stopped the text reader from loading on Windows.
- **Recording needs a disk that keeps up.** A slow hard disk (the shingled kind) fell behind while chunks were
  written and read at once. Put `work_root` on an SSD; clips can stay on the big disk (`data_root`).
- **Sleep, lids and updates stop it.** StreamSplitter keeps the computer awake when idle. It cannot stop a closed
  lid or a Windows Update restart: plug it in, set the lid to do nothing, pause updates for the weekend. After a
  restart nothing runs until someone logs in.
- **Have the alarm come from outside.** If the computer dies it cannot say so. Slack holds a scheduled "it
  stopped" message that the program keeps pushing back; it arrives 6-7 minutes after the program stops.
- **Give more than one person the keys to fix things.** The person who set it up handed the alerts to a teammate
  and lost the right to change settings mid-event. The old owner now stays an admin, and admins can add admins.
- **Set a second computer up from the team settings file.** It takes minutes, not half an hour. Whoever can open
  the file can post to your Slack and upload to your channel, so send it like a password.
- **Do the test upload before the event,** on the computer that will record, the week of the event. Logins expire
  and limits change.
- **Start early.** Starting the evening before costs nothing: it waits for the stream.
- **Watch the first match.** Open `run.bat status` (or `status` in Slack) after the first match of each day. A
  wrong stream or a missing overlay shows there first.
- **Running the tests:** give them a time limit. A hung test run once grew to 31 GB of memory
  ([code-map.md](../code-map.md)).
