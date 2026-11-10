"""Telling a person what is going on: info, warnings and errors, and an alarm when StreamSplitter stops running.

Every message goes to the console / log. With a Slack contact ([notify] slack_user) it also goes to them by DM. DMs
wait in memory and are retried with backoff (5 s doubling to 5 min), so a short outage delays them instead of losing
them. A message given a `key` goes by DM at most once per 30 min (the log still gets every one), so a problem that
repeats every poll does not flood anyone's phone.

The stopped-running alarm
-------------------------
A program cannot report its own death (a power cut, Windows crashing, a laptop lid closed, the process killed), so the
alarm lives on Slack's side: a scheduled DM, "StreamSplitter on <host> has not checked in since <time>; it may have
stopped", due alarm_after_s (7 min) ahead. heartbeat(), called every heartbeat_s (1 min; live.Heartbeat), pushes it
back by scheduling the next one and then deleting the previous one. If the PC dies, the last alarm scheduled goes off
6-7 min later; nothing on the PC has to work for that. (The heartbeat also stops while the follower is stuck for 5
min, live.FOLLOWER_HUNG_S, so a hang raises the alarm too.)

The timing (user, 2026-11-04: "every minute or so"; 2026-11-05: 7 min, as short as Slack allows). The beat at time T
schedules the alarm due at T + 420 and deletes the one the beat before scheduled, now due at about T + 360. Why so far
ahead: Slack answers ok to a delete made less than about 5 min before due, drops the message from
chat.scheduledMessages.list, and posts it anyway. 2026-11-05 03:13-03:18 and again at 04:08-04:12, on a new app, every
alarm deleted ~120 s ahead was posted, one a minute: "stopped at 04:08", "04:09", ... while the program ran. Measured
the same morning (test DMs, deleted gap s before due): posted at gap 240 and 270; held at 300, 330 (3 of 3), 360 and
~480 (13 of 13 on 2026-11-04). So deletes are made at least SAFE_DELETE_S (330 s) ahead. Slack also refuses outright
to delete a message due within 60 s, so nothing is tried within DELETE_MARGIN_S (90 s). As a guard if the limit moves,
a cancelled alarm is looked for in the DM once due (_check_cancelled): one Slack posted anyway is edited into a
false-alarm note, and one that had been deleted 330 s or more ahead turns the alarm off for the run with one DM. A
beat takes two calls of well under a second and the next starts 60 s after its start (live.Heartbeat.next_wait: a slow
call does not delay the beat after it), so in normal running none goes off.
alarm_timing() keeps configured values on the safe side: alarm_after_s >= heartbeat_s + 330 + 30.
Rate limits (Slack's docs, 2026-11-04): chat.scheduleMessage and chat.deleteScheduledMessage are Tier 3 (50+ calls a
minute); this makes one of each a minute, plus one conversations.history read of the DM at most every 2 min (the
check). At most 30 messages may be scheduled to post within any 5 minutes in one channel; one or two alarms are pending
at a time (three after a missed delete), all minutes apart.

How a false alarm is avoided:
- schedule first, delete second: there is always at least one alarm pending, never a moment with none;
- Slack refuses to delete a scheduled message due within 60 s, so an alarm is deleted only while it is due more than
  DELETE_MARGIN_S (90 s) away (measured after the new alarm is scheduled, so a slow call is counted). One that got
  closer (Slack was unreachable for more than about 30 s, or the PC was too busy to beat) is kept, since deleting it
  would fail, and the next is scheduled anyway; if it goes off while this process is alive, an all-clear DM ("False
  alarm: ... is still running") follows at the next beat that reaches Slack;
- a restarted StreamSplitter deletes the previous run's alarm instead of letting it go off: the pending alarm ids are
  saved (`state_path`), and the first heartbeat also lists the app's scheduled messages (SlackClient.list_scheduled)
  and deletes any earlier alarm of this host and tag (the text names both, so a second recording on the same PC
  keeps its own alarm); it says "running again" if the previous run's alarm already went off;
- stopped() (a clean stop, Ctrl+C, a crash caught on the way out) deletes the alarm and DMs the reason.
If Slack is unreachable, heartbeat() returns False and is called again sooner; the alarm already scheduled still
fires if the outage outlasts it, which is right: nobody can reach the PC either. The all-clear follows on reconnect.
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
from collections import deque
from pathlib import Path

from .base import PermanentError, TransientError

DM_KEY_S = 1800.0          # one DM per key per 30 min
DM_BACKOFF = (5.0, 300.0)  # first retry, longest wait between retries
DM_QUEUE_MAX = 200         # DMs kept while Slack is unreachable (oldest dropped first, and counted)
DELETE_MARGIN_S = 90.0     # Slack: "cannot delete a message scheduled within 60 s"; 30 s more for skew and latency
SAFE_DELETE_S = 330.0      # a delete this far ahead of due is known to take (below ~300: Slack says ok, then posts it)
FIRED_GRACE_S = 5.0        # an alarm this far past its due time has been posted
CHECK_S = 120.0            # cancelled alarms are looked for in the DM at most this often (one history read)
CHECK_GRACE_S = 30.0       # ... once they are this far past due (Slack posted them about 1 s late on 2026-11-05)
HEARTBEAT_S = 60.0        # default: the alarm is pushed back every minute ...
ALARM_AFTER_S = 420.0      # ... to 7 min ahead: it goes off 6-7 min after the last sign of life
HEARTBEAT_RANGE = (30.0, 300.0)   # [notify] heartbeat_s is kept in here
LATE_S = 30.0              # how late a beat may run before the previous alarm is too close to delete
ALARM_MAX_S = 3600.0       # [notify] alarm_after_s at most this
# what the setup wrote (not a person's choice): before 2026-11-04 evening, until 2026-11-05 04:30, then until 14:10
LEGACY_DEFAULTS = {(120.0, 600.0), (60.0, 180.0), (60.0, 600.0)}


def alarm_timing(heartbeat_s=None, alarm_after_s=None) -> tuple[float, float, str | None]:
    """(heartbeat_s, alarm_after_s, note) from [notify]'s values (None or 0: the default), kept safe: the heartbeat
    between 30 s and 5 min, the alarm at least heartbeat + SAFE_DELETE_S + LATE_S ahead (else the previous alarm is
    deleted too close to due, and Slack posts it anyway) and at most an hour. `note` says what was changed, or None."""
    try:
        hb = float(heartbeat_s or 0) or HEARTBEAT_S
        al = float(alarm_after_s or 0) or ALARM_AFTER_S
    except (TypeError, ValueError):
        hb, al = HEARTBEAT_S, ALARM_AFTER_S
    if (hb, al) in LEGACY_DEFAULTS:
        return HEARTBEAT_S, ALARM_AFTER_S, None
    want = (hb, al)
    hb = min(max(hb, HEARTBEAT_RANGE[0]), HEARTBEAT_RANGE[1])
    al = min(max(al, hb + SAFE_DELETE_S + LATE_S), ALARM_MAX_S)
    note = None if (hb, al) == want else (f"[notify] heartbeat_s / alarm_after_s {want[0]:g} / {want[1]:g} s changed "
                                          f"to {hb:g} / {al:g} s (alarm at least heartbeat + "
                                          f"{SAFE_DELETE_S + LATE_S:g} s ahead)")
    return hb, al, note

_SECRET = re.compile(r"(xox[a-z]-[A-Za-z0-9-]+|xapp-[A-Za-z0-9-]+|ya29\.[A-Za-z0-9._-]+|1//[A-Za-z0-9._-]{20,}"
                     r"|GOCSPX-[A-Za-z0-9_-]+|(?i:bearer)\s+[A-Za-z0-9._~+/=-]+)")


def redact(text) -> str:
    """`text` with anything shaped like a Slack or Google token replaced: error messages are logged and DMed, and
    a library could one day put a token in one."""
    return _SECRET.sub("[redacted]", str(text))


def _esc(text: str) -> str:
    """Plain text for Slack's mrkdwn: &, < and > would otherwise start an entity or a link."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _print(line: str) -> None:
    print(line, flush=True)


def _clock_text(t: float) -> str:
    return time.strftime("%a %H:%M", time.localtime(t))


class Notifier:
    def __init__(self, client=None, user_id: str | None = None, *, host: str | None = None, tag: str | None = None,
                 alarm_after_s: float = ALARM_AFTER_S, state_path: Path | None = None, log=None, clock=time.time,
                 sleep=time.sleep, threaded: bool = True):
        """`client`: a publish.slack.SlackClient (or None: console only). `user_id`: the Slack user who gets the DMs
        and the alarm. `tag`: the recording's name, in the alarm's text. `state_path`: where pending alarm ids are
        kept across restarts. threaded=False (tests): DMs are sent only when pump() is called."""
        self.client = client if (client is not None and user_id) else None
        self.user = user_id
        self.host = host or socket.gethostname()
        self.tag = tag
        self._swept = False                         # the previous runs' alarms looked for (first heartbeat)
        self.alarm_after_s = float(alarm_after_s)
        self.state_path = Path(state_path) if state_path else None
        self.log = log or _print
        self.clock, self.sleep = clock, sleep
        self.last_warning: dict | None = None      # {"time", "level", "text"}, for status.json
        self._t_started = clock()
        self._q_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._queue: deque[dict] = deque()          # {"text", "attempts", "next_try"}
        self._dropped = 0
        self._keys: dict[str, float] = {}           # key -> time of its last DM
        self._dm_failing = False
        self._alarm_lock = threading.Lock()
        self._alarms: list[dict] = self._load()     # {"id", "post_at", "channel", "checked_in", "text"?, "previous"?}
        self._cancelled: list[dict] = []            # alarms deleted this run, looked for once due (_check_cancelled)
        self._next_check = 0.0
        self._can_check = True
        self._check_failing = False
        self._posted_anyway = 0
        self.alarm_off = False                      # Slack kept posting cancelled alarms: none scheduled this run
        self._hb_failing = False
        self._dm_channel: str | None = None
        self._wake = threading.Event()
        self._closed = threading.Event()
        self._thread = None
        if self.client is not None and threaded:
            self._thread = threading.Thread(target=self._run, name="notify-dm", daemon=True)
            self._thread.start()

    @property
    def dm_enabled(self) -> bool:
        return self.client is not None

    # messages -----------------------------------------------------------------------------------------------------
    def info(self, text: str, key: str | None = None, dm: bool = True) -> bool:
        return self._emit("info", text, key, dm)

    def warn(self, text: str, key: str | None = None, dm: bool = True) -> bool:
        return self._emit("warn", text, key, dm)

    def error(self, text: str, key: str | None = None, dm: bool = True) -> bool:
        return self._emit("error", text, key, dm)

    def _emit(self, level: str, text: str, key: str | None, dm: bool) -> bool:
        """Log `text`; queue it as a DM unless DMs are off or its key had one in the last 30 min. True when queued."""
        text = redact(text)
        now = self.clock()
        word = {"info": "", "warn": "WARNING ", "error": "ERROR "}[level]
        self.log(f"{time.strftime('%H:%M:%S', time.localtime(now))} {word}{text}")
        if level != "info":
            self.last_warning = {"time": now, "level": level, "text": text}
        if self.client is None or not dm:
            return False
        with self._q_lock:
            if key is not None:
                last = self._keys.get(key)
                if last is not None and now - last < DM_KEY_S:
                    return False
                self._keys[key] = now
        prefix = {"info": "", "warn": "Warning: ", "error": "Error: "}[level]
        self._enqueue(f"{prefix}{text} [{self.signature()}]")
        return True

    def _enqueue(self, text: str) -> None:
        with self._q_lock:
            self._queue.append({"text": text, "attempts": 0, "next_try": 0.0})
            while len(self._queue) > DM_QUEUE_MAX:
                self._queue.popleft()
                self._dropped += 1
        self._wake.set()

    def pending(self) -> int:
        with self._q_lock:
            return len(self._queue)

    def pump(self, force: bool = False) -> float:
        """Send the queued DMs in order until one fails. Returns the seconds until the next try (60 when idle).
        `force`: ignore the backoff (the last tries on the way out)."""
        with self._send_lock:                   # the DM thread and stopped() must not send the same DM twice
            return self._pump(force)

    def _pump(self, force: bool) -> float:
        while True:
            now = self.clock()
            with self._q_lock:
                if not self._queue:
                    return 60.0
                item = self._queue[0]
                if not force and item["next_try"] > now:
                    return item["next_try"] - now
                text = item["text"]
                if self._dropped:
                    text = f"({self._dropped} earlier message(s) were dropped while Slack was unreachable)\n{text}"
            try:
                self.client.dm(self.user, _esc(text))
            except PermanentError as e:
                self.log(f"could not DM the contact: {redact(e)} (message dropped; check [notify] slack_user)")
                self._pop(item)
                continue
            except Exception as e:  # noqa: BLE001 - transient, or an unmapped error: keep it and try again later
                item["attempts"] += 1
                ra = getattr(e, "retry_after", None) if isinstance(e, TransientError) else None
                delay = ra if ra is not None else min(DM_BACKOFF[1], DM_BACKOFF[0] * 2 ** (item["attempts"] - 1))
                item["next_try"] = now + delay
                if not self._dm_failing:
                    self.log(f"Slack DM not sent yet ({redact(e)}); retrying with backoff")
                self._dm_failing = True
                return delay
            if self._dm_failing:
                self.log("Slack DMs are going through again")
            self._dm_failing = False
            with self._q_lock:
                self._dropped = 0
            self._pop(item)

    def _pop(self, item: dict) -> None:
        with self._q_lock:
            if self._queue and self._queue[0] is item:
                self._queue.popleft()

    def _run(self) -> None:
        while not self._closed.is_set():
            self._wake.clear()
            try:
                wait = self.pump()
            except Exception as e:  # noqa: BLE001 - the DM thread must never die
                self.log(f"notifier: {type(e).__name__}: {redact(e)}")
                wait = 30.0
            self._wake.wait(min(max(wait, 0.5), 60.0))

    # the stopped-running alarm ------------------------------------------------------------------------------------
    def _channel(self) -> str:
        """The DM conversation with the contact (scheduled messages and their deletion need the same channel)."""
        if self._dm_channel is None:
            opener = getattr(self.client, "open_dm", None)
            self._dm_channel = opener(self.user) if callable(opener) else self.user
        return self._dm_channel

    # the words (a subclass speaks for another program: listener.ListenerNotifier) ----------------------------------
    def signature(self) -> str:
        """Who sent a DM, at its end."""
        return f"StreamSplitter on {self.host}"

    def _alarm_marker(self) -> str:
        """The start of the alarm's text, by which a restarted run finds its predecessor's alarm (_sweep)."""
        return f"StreamSplitter on {self.host}" + (f" ({self.tag})" if self.tag else "") + " has not checked in"

    def alarm_text(self, checked_in: float) -> str:
        return (f"{self._alarm_marker()} since {_clock_text(checked_in)}; it may have stopped. Check that the PC is "
                f"on, awake and online, and start it again if needed.")

    def false_alarm_text(self, last: float) -> str:
        return (f"False alarm: StreamSplitter on {self.host} is still running. It could not push its alarm back after "
                f"{_clock_text(last)} (no internet, or Slack unreachable).")

    def running_again_text(self) -> str:
        return f"StreamSplitter on {self.host} is running again (since {_clock_text(self._t_started)})."

    def stopped_text(self, reason: str) -> str:
        return f"StreamSplitter on {self.host} stopped: {reason}."

    def posted_anyway_text(self, checked_in: float) -> str:
        """What an alarm Slack posted although it had been cancelled is edited into (an edit sends no notification)."""
        return (f"False alarm: {self.signature()} was still running after {_clock_text(checked_in)}. Slack sent this "
                f"alarm although it had been cancelled. Nothing to do.")

    def alarm_off_text(self) -> str:
        return (f"{self.signature()} turned its stopped-running alarm off: Slack sent {self._posted_anyway} alarms that "
                f"had been cancelled (they now say False alarm). It keeps running, but it cannot warn you now if this "
                f"computer stops. Restarting it tries the alarm again.")

    def heartbeat(self) -> bool:
        """Push the stopped-running alarm alarm_after_s ahead: schedule the new alarm, then delete the earlier ones
        that are still far enough from due, then look for cancelled ones Slack posted anyway. False when Slack could
        not be reached (call again soon)."""
        if self.client is None:
            return True
        with self._alarm_lock:
            now = self.clock()
            if self.alarm_off:                   # only the cancelled alarms still to look for
                try:
                    self._check_cancelled(self._channel(), now)
                except Exception:  # noqa: BLE001 - no DM channel yet (offline): nothing to look in
                    pass
                return True
            self._check_fired(now)
            post_at = int(now + self.alarm_after_s)
            text = _esc(self.alarm_text(now))
            try:
                channel = self._channel()
                sid = self.client.schedule_message(channel, text, post_at)
            except Exception as e:  # noqa: BLE001 - offline: the alarm already scheduled stays in place
                if not self._hb_failing:
                    self.log(f"could not push the stopped-running alarm back ({redact(e)}); will keep trying")
                self._hb_failing = True
                return False
            if self._hb_failing:
                self.log("stopped-running alarm pushed back again")
            self._hb_failing = False
            old, self._alarms = self._alarms, [{"id": sid, "post_at": post_at, "channel": channel,
                                                "checked_in": now, "text": text}]
            after = self.clock()                 # the schedule call may have been slow: judge the deletes from now
            for a in old:
                if not self._cancel(a, after):
                    self._alarms.append(a)
            if not self._swept:
                self._sweep(channel, after)
            self._save()
            self._check_cancelled(channel, self.clock())
            return True

    def _sweep(self, channel: str, now: float) -> None:
        """Delete alarms an earlier run of this host and tag left scheduled (its state file lost, or it crashed
        between scheduling and saving). Runs once, after this run's own alarm is in place."""
        lister = getattr(self.client, "list_scheduled", None)
        if not callable(lister):
            self._swept = True
            return
        try:
            listed = lister(channel)
        except Exception as e:  # noqa: BLE001 - try again at the next heartbeat
            self.log(f"could not list scheduled messages ({redact(e)}); will try again")
            return
        self._swept = True
        mine = {a["id"] for a in self._alarms}
        marker = _esc(self._alarm_marker())
        for m in listed or []:
            if m.get("id") in mine or not str(m.get("text") or "").startswith(marker):
                continue
            a = {"id": m["id"], "post_at": float(m.get("post_at") or 0), "channel": m.get("channel_id") or channel,
                 "checked_in": now, "previous": True}
            if not self._cancel(a, now):
                self._alarms.append(a)
            else:
                self.log(f"deleted an earlier run's stopped-running alarm ({a['id']})")

    def _cancel(self, a: dict, now: float) -> bool:
        """Delete one scheduled alarm. True when it is gone; False to keep it (too close to due, or no answer)."""
        if a["post_at"] - now <= DELETE_MARGIN_S:
            # Slack would refuse; it goes off and the all-clear follows. Said once per alarm (the log is how a
            # repeated alarm is explained: 2026-11-05, three listener alarms in a row and no line saying why)
            if not a.get("kept"):
                a["kept"] = True
                self.log(f"stopped-running alarm {a['id']} is due in {a['post_at'] - now:.0f} s, too close to "
                         f"cancel: it will go off")
            return False
        try:
            if self.client.delete_scheduled(a["channel"], a["id"]) is False:
                # already posted or deleted (it is due over DELETE_MARGIN_S away, so not Slack's 60 s rule, unless
                # this computer's clock is off by more than 30 s, which SlackClient corrects from Slack's replies)
                self.log(f"stopped-running alarm {a['id']} was already gone")
            else:                                # Slack said ok, which is not proof: looked for once it is due
                self._cancelled.append({**a, "cancelled_at": now})
            return True
        except PermanentError as e:              # e.g. the channel is gone: nothing left to delete
            self.log(f"stopped-running alarm {a['id']}: {redact(e)}")
            return True
        except Exception as e:  # noqa: BLE001 - try again at the next heartbeat
            self.log(f"could not cancel stopped-running alarm {a['id']} ({redact(e)}); will try again")
            return False

    def _check_fired(self, now: float) -> None:
        """Alarms whose due time has passed while this process is alive went off falsely: say so."""
        fired = [a for a in self._alarms if a["post_at"] + FIRED_GRACE_S <= now]
        if not fired:
            return
        self._alarms = [a for a in self._alarms if a not in fired]
        self._save()
        if any(not a.get("previous") for a in fired):
            last = max(a["checked_in"] for a in fired if not a.get("previous"))
            self._enqueue(self.false_alarm_text(last))
        if any(a.get("previous") for a in fired):
            self._enqueue(self.running_again_text())

    def _check_cancelled(self, channel: str, now: float) -> None:
        """Slack can answer ok to a delete and post the alarm anyway (2026-11-05: every alarm deleted about 2 min before
        due was posted). Once cancelled alarms are due, read the DM (at most every CHECK_S) for them. One found is
        edited into a false-alarm note (an edit makes no new notification). One that had been deleted SAFE_DELETE_S or
        more ahead turns the alarm off for this run, with one DM saying so: the alarms already in flight (about
        alarm_after_s / heartbeat_s of them) may still post, each edited the same way, and then nothing more."""
        due = [a for a in self._cancelled if a["post_at"] + CHECK_GRACE_S <= now]
        if not due or now < self._next_check:
            return
        reader = getattr(self.client, "history", None)
        if not callable(reader) or not self._can_check:   # a client that cannot read the DM: nothing to look in
            self._cancelled = []
            return
        self._next_check = now + CHECK_S
        try:
            msgs = reader(channel, limit=50, oldest=f"{min(a['post_at'] for a in due) - CHECK_GRACE_S:.6f}")
        except PermanentError as e:              # e.g. no im:history (an older install): stop looking
            self.log(f"cannot look for cancelled alarms Slack posted anyway ({redact(e)})")
            self._cancelled = []
            self._can_check = False
            return
        except Exception as e:  # noqa: BLE001 - try again at a later beat
            if not self._check_failing:
                self.log(f"could not look for cancelled alarms Slack posted anyway ({redact(e)}); will try again")
            self._check_failing = True
            return
        self._check_failing = False
        self._cancelled = [a for a in self._cancelled if a not in due]
        marker = _esc(self._alarm_marker())
        found = []
        for a in due:
            for m in msgs or []:
                text = str(m.get("text") or "")
                try:
                    ts = float(m.get("ts") or 0)
                except (TypeError, ValueError):
                    continue
                if (m.get("bot_id") or m.get("app_id")) and (
                        text == a.get("text") or (text.startswith(marker) and abs(ts - a["post_at"]) <= 30)):
                    found.append((a, m))
                    break
        if not found:
            return
        editor = getattr(self.client, "update_message", None)
        for a, m in found:
            self._posted_anyway += 1
            self.log(f"Slack posted stopped-running alarm {a['id']} although it was cancelled "
                     f"{a['post_at'] - a['cancelled_at']:.0f} s before due; it now says False alarm")
            if callable(editor):
                try:
                    editor(channel, m["ts"], _esc(self.posted_anyway_text(a["checked_in"])))
                except Exception as e:  # noqa: BLE001 - it stays as posted
                    self.log(f"could not edit that alarm into a false-alarm note ({redact(e)})")
        # one deleted closer than SAFE_DELETE_S (a late beat, a restart's sweep) is expected; one deleted further ahead
        # means Slack's limit moved, and every alarm in flight may follow: schedule no more
        if not self.alarm_off and any(a["post_at"] - a["cancelled_at"] >= SAFE_DELETE_S for a, _ in found):
            self._turn_off(now)

    def _turn_off(self, now: float) -> None:
        """No more alarms this run: cancel the pending ones (still looked for once due) and say so once."""
        self.alarm_off = True
        self._alarms = [a for a in self._alarms if not self._cancel(a, now)]
        self._save()
        self.log("stopped-running alarm off for this run: Slack keeps posting alarms that were cancelled")
        self._enqueue(self.alarm_off_text())

    def stopped(self, reason: str, timeout: float = 15.0) -> None:
        """A clean stop: cancel the alarm and DM why. Bounded by `timeout`, since it runs on the way out."""
        reason = redact(reason)
        self.log(f"{time.strftime('%H:%M:%S', time.localtime(self.clock()))} stopping: {reason}")
        if self.client is None:
            return
        t = threading.Thread(target=self._final, args=(reason,), name="notify-final", daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            self.log("could not reach Slack while stopping; the stopped-running alarm will tell the contact")

    def _final(self, reason: str) -> None:
        self._closed.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(5.0)
        with self._alarm_lock:
            now = self.clock()
            kept = [a for a in self._alarms if not self._cancel(a, now)]
            self._alarms = kept
            self._save()
        text = self.stopped_text(reason)
        if kept:
            due = max(a["post_at"] for a in kept)
            text += (f" (Its stopped-running alarm could not be cancelled and may still arrive at "
                     f"{_clock_text(due)}; it is about this stop.)")
        self._enqueue(text)
        for i in range(3):
            if not self.pending():
                break
            wait = self.pump(force=True)
            if self.pending() and i < 2:
                self.sleep(min(wait, 2.0))

    def close(self) -> None:
        """Stop the DM thread without cancelling the alarm (tests; a process that wants the alarm to stand)."""
        self._closed.set()
        self._wake.set()

    # pending alarms across restarts -------------------------------------------------------------------------------
    def _load(self) -> list[dict]:
        if self.state_path is None:
            return []
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return [{**a, "previous": True} for a in data.get("alarms", [])]
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            return []

    def _save(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"alarms": self._alarms}, indent=1), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as e:
            self.log(f"could not save the alarm state ({e})")
