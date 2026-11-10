"""The live hook: the follower hands every clip to the Publisher, which decides where it goes and queues it.

    on_clip(row)   the match clip was just cut: the team's own match goes to Slack at once (a new message).
    on_score(row)  the pipeline's search for the results screen settled (a few minutes later; the row may have been
                   renamed by the results screen's title): the score version goes to Slack as a reply in the clip's
                   thread, and YouTube gets the score version, or the match clip when no results screen was found
                   (youtube.version = "match": always the match clip). YouTube waits until now so the title uses the
                   final name.

Both only add rows to the outbox (no network; the outbox hard-links the file into its own folder, or copies it where
the disk has no hard links), so the follower never waits on the internet. Job ids are
"<name>@<stream start, 0.1 s>:<kind>:<destination>", stable across restarts, so queueing the same clip twice does
nothing. The start is in the id because names are not unique over a run: when the results screen renames a clip
(posted as qm13, really qm12), the name qm13 is free again and the real qm13 gets it; with the name alone its post
would count as already sent. The id of a row's Slack clip job is kept in the row (row["jobs"]; live.follow saves it
with the score searches still open), so the score reply threads under that clip and no other, also after a restart.
When the results screen renames a clip, its Slack post still waiting is given the corrected match, and the reply and
the YouTube upload use the new name.

Partial clips (a match the overlay lost early: possibly an aborted attempt that is replayed, or the stream dropped near
its end) have no results-screen search: they go to Slack when routed there and to YouTube from on_clip, titled
"... (Partial)" by the title templates (lead, 2026-11-04: a partial clip is better than none on YouTube). With the
pipeline's results-screen search turned off (score_search=False), YouTube gets every match clip from on_clip.

Before each YouTube attempt (in the outbox worker, not the follower) a hook fills in TBA's final score (from the
schedule refreshed every few minutes, else /match/<key>), so the description carries it once posted, and, for a playoff
match, the bracket size from TBA's event (4 or 8 alliances; [event] playoff_alliances overrides) for its title. A
video that went up before TBA posted the score gets a background "describe" job (same state, kind "describe"):
YouTubeDestination sees the video is uploaded and only updates the description once the hook has the score; it
retries every DESCRIBE_EVERY_S for DESCRIBE_FOR_S, never holding up uploads.

The team wiki ([wiki] enabled) gets every match like YouTube does, independently of it: the score version (or the
match clip; wiki.version) from on_score, partial clips and score_search=False from on_clip. Its hook fills in the score
and the bracket size the same way, best effort: a section added before TBA posted the score is not edited later
(editing a section could undo what a person changed in it).

Google Drive ([drive] enabled) is queued the same way (drive.version), with the match clip's cut time in the job's
state ("played": the video's modifiedTime, so Drive sorts by play order). Its hook is YouTube's (TBA's score, the
bracket size) plus the event's last qualification number from the schedule (the zero-padding of "Q07"); a video that
went up before the score gets a background "describe" job, as for YouTube.

With [youtube] tba_links on (off by default; for event organizers with a TBA write key, publish/tbalink.py), each
YouTube upload of a match TBA has a page for also queues a background "link" job ("<name>@<start>:link:tba") that adds
the video to that page. Partial clips are never linked.
"""

from __future__ import annotations

import os
import re
import time

from .base import Job, TransientError
from .router import alliance_teams, match_from_row, route, row_teams, schedule_entry

_TBA_KEY = re.compile(r"_(qm\d+|(ef|qf|sf|f)\d+m\d+)$")     # keys TBA has a /match page for (not practice)
SCORE_RECHECK_S = 300.0     # TBA is asked for one job's score at most this often
DESCRIBE_EVERY_S = 600.0    # a describe job asks again this often ...
DESCRIBE_FOR_S = 6 * 3600.0  # ... for this long after its video went up
EVENT_RECHECK_S = 1800.0    # a failed read of TBA's event (the bracket size) is retried after this
_FOUR_ALLIANCES = {2, 11}   # TBA playoff_type: BRACKET_4_TEAM, DOUBLE_ELIM_4_TEAM


def clip_tag(row: dict) -> str:
    """"@<stream start>" (0.1 s) for a row's job ids: one per clip however names move (module docstring)."""
    try:
        return f"@{float(row['start']):.1f}"
    except (KeyError, TypeError, ValueError):
        return ""


def job_id(row: dict, match, kind: str, destination: str) -> str:
    return f"{match.name}{clip_tag(row)}:{kind}:{destination}"


class Publisher:
    def __init__(self, outbox, *, cfg_event: dict | None = None, cfg_slack: dict | None = None,
                 cfg_youtube: dict | None = None, cfg_wiki: dict | None = None, schedule=None,
                 score_search: bool = True, tba_get=None, log=None, clock=time.time, cfg_drive: dict | None = None):
        """`schedule`: a callable returning the current TBA schedule (live: the pipeline's, refreshed every few
        minutes). `tba_get(path)`: TBA reads for the score hook (default tba.get)."""
        self.outbox = outbox
        self.cfg_event = dict(cfg_event or {})
        self.cfg_slack = dict(cfg_slack or {})
        self.cfg_youtube = dict(cfg_youtube or {})
        self.cfg_wiki = dict(cfg_wiki or {})
        self.cfg_drive = dict(cfg_drive or {})
        self.schedule = schedule or (lambda: [])
        self.score_search = score_search
        self._tba_get = tba_get
        self.log = log or getattr(outbox, "log", print)
        self.clock = clock
        self._score_checked: dict[str, float] = {}
        self._alliances: dict[str, tuple[int | None, float]] = {}   # event key -> (bracket size, checked at)
        outbox.hooks["youtube"] = self._before_youtube
        outbox.after_send["youtube"] = self._after_youtube
        outbox.hooks["wiki"] = self._before_wiki
        outbox.hooks["drive"] = self._before_drive
        outbox.after_send["drive"] = self._after_drive

    # follower side ------------------------------------------------------------------------------------------------
    def _route(self, row: dict):
        sched = self.schedule() or []
        entry = schedule_entry((row.get("naming") or {}).get("key"), sched)
        match = match_from_row(row, sched, self.cfg_event)
        return match, route(row_teams(row, entry), self.cfg_slack, self.cfg_youtube, self.cfg_wiki, self.cfg_drive)

    def on_clip(self, row: dict) -> None:
        match, dests = self._route(row)
        if "slack" in dests:
            self._slack_clip(row, match)
        for dest in ("youtube", "drive", "wiki"):
            if dest in dests and (row.get("partial") or not self.score_search):
                # no results-screen search for these: a partial clip is never searched (pipeline._finish)
                self._youtube(row, match, use_score=False, dest=dest)

    def on_score(self, row: dict) -> None:
        match, dests = self._route(row)
        rec = (row.get("jobs") or {})
        clip_job, clip_name = rec.get("slack_clip"), rec.get("slack_clip_name")
        if clip_job and clip_name != match.name:
            # renamed by the results screen: a clip post still waiting gets the right name (its file is the
            # outbox's own copy, so the rename did not move it); the reply and YouTube use the new name anyway
            self.outbox.retarget(clip_job, match=match)
        if "slack" in dests:
            if not clip_job:                      # its name changed to one of the team's matches: post it now
                clip_job = self._slack_clip(row, match)
            score = row.get("score_clip")
            if score and score.get("path") and self.cfg_slack.get("score_in_thread", True):
                self.outbox.add(Job(id=job_id(row, match, "score", "slack"), destination="slack", kind="score",
                                    path=str(score["path"]), match=match, state={"after": clip_job}))
        for dest, cfg in (("youtube", self.cfg_youtube), ("drive", self.cfg_drive), ("wiki", self.cfg_wiki)):
            if dest in dests:
                use_score = (cfg.get("version", "score") == "score" and not row.get("partial")
                             and bool((row.get("score_clip") or {}).get("path")))
                self._youtube(row, match, use_score=use_score, dest=dest)

    def _slack_clip(self, row: dict, match) -> str:
        """Queue the clip's Slack post; its job id (and the name then) go into the row for the score reply."""
        job = Job(id=job_id(row, match, "clip", "slack"), destination="slack", kind="clip",
                  path=str(row["clip"]["path"]), match=match)
        self.outbox.add(job)
        row.setdefault("jobs", {}).update(slack_clip=job.id, slack_clip_name=match.name)
        return job.id

    def _youtube(self, row: dict, match, use_score: bool, dest: str = "youtube") -> None:
        """Queue the upload of every match: to YouTube, or with `dest` "wiki" / "drive" to the team wiki or Drive."""
        kind = "score" if use_score else "clip"
        path = (row["score_clip"] if use_score else row["clip"])["path"]
        state = {}
        if dest == "drive":
            try:                                   # the match clip is cut right after the match: when it was played
                state["played"] = os.path.getmtime((row.get("clip") or {}).get("path") or path)
            except (OSError, TypeError, ValueError):
                pass
        self.outbox.add(Job(id=job_id(row, match, kind, dest), destination=dest, kind=kind, path=str(path),
                            match=match, state=state))

    # worker side (an outbox hook, run just before each attempt) ----------------------------------------------------
    def _get(self, path: str):
        get = self._tba_get
        if get is None:
            from .. import tba
            get = tba.get
        return get(path)

    def _before_youtube(self, job: Job) -> None:
        """TBA's final score (and the teams, for a clip named without a schedule) into the match before an upload, and
        the playoff bracket size into the destination. Best effort for an upload (offline or not posted yet, the
        video goes up without the score); a describe job waits (TransientError) until the score is there."""
        m = job.match
        self._bracket(m)
        if not m.key or not _TBA_KEY.search(m.key):
            return
        self._fill_score(job)
        if job.kind == "describe" and not self._has_score(job):
            if self.clock() < float(job.state.get("describe_until") or 0):
                raise TransientError("TBA has not posted the score yet", retry_after=DESCRIBE_EVERY_S)
            # given up: sending it changes nothing and closes the job

    def _before_wiki(self, job: Job) -> None:
        """The wiki's section line and file page carry TBA's score when it is posted by the time of the attempt,
        and a playoff section title its round (the bracket size); never waits for either."""
        m = job.match
        self._bracket(m, "wiki")
        if m.key and _TBA_KEY.search(m.key):
            self._fill_score(job)

    def _before_drive(self, job: Job) -> None:
        """As for YouTube (TBA's score and teams, the bracket size; a describe job waits for the score), plus the
        event's last qualification number for the destination's "Q07" padding."""
        m = job.match
        self._bracket(m, "drive")
        dest = getattr(self.outbox, "destinations", {}).get("drive")
        if dest is not None and hasattr(dest, "quals"):
            quals = [e.get("match_number") for e in self.schedule() or []
                     if e.get("comp_level") == "qm" and isinstance(e.get("match_number"), int)]
            if quals:
                dest.quals = max(quals)
        if not m.key or not _TBA_KEY.search(m.key):
            return
        self._fill_score(job)
        if job.kind == "describe" and not self._has_score(job):
            if self.clock() < float(job.state.get("describe_until") or 0):
                raise TransientError("TBA has not posted the score yet", retry_after=DESCRIBE_EVERY_S)

    def _after_drive(self, job: Job, result) -> None:
        """A video that went up without TBA's score: queue its description update for when the score is posted."""
        m = job.match
        if job.kind not in ("clip", "score") or not job.state.get("file_id") or self._has_score(job):
            return
        if not m.key or not _TBA_KEY.search(m.key):
            return
        now = self.clock()
        prefix = job.id.rsplit(":", 2)[0]
        self.outbox.add(Job(id=f"{prefix}:describe:drive", destination="drive", kind="describe", path=job.path,
                            match=m, state={**job.state, "describe_until": now + DESCRIBE_FOR_S}),
                        background=True, not_before=now + DESCRIBE_EVERY_S)

    def _fill_score(self, job: Job) -> None:
        """TBA's final score (and the teams, for a clip named without a schedule) into job.match: from the schedule
        refreshed since the clip was cut, else TBA's /match/<key> (asked at most every SCORE_RECHECK_S per job)."""
        m = job.match
        if self._has_score(job):
            return
        now = self.clock()
        entry = schedule_entry(m.key, self.schedule() or [])
        if entry is not None and _fill(m, entry):
            return
        last = self._score_checked.get(job.id)
        if last is None or now - last >= SCORE_RECHECK_S:
            self._score_checked[job.id] = now
            try:
                data = self._get(f"/match/{m.key}")
            except Exception as e:  # noqa: BLE001 - no key, offline, not posted: go on without it
                self.log(f"{m.name}: TBA score not available ({type(e).__name__})")
                data = None
            if isinstance(data, dict):
                _fill(m, data)

    @staticmethod
    def _has_score(job: Job) -> bool:
        m = job.match
        return m.red_score is not None and m.blue_score is not None and bool(m.red or m.blue)

    def _after_youtube(self, job: Job, result) -> None:
        """A video that went up without TBA's score: queue its description update for when the score is posted. With
        [youtube] tba_links on, also queue the link to it on its TBA match page."""
        m = job.match
        if job.kind in ("clip", "score") and job.state.get("video_id"):
            self._tba_link(job)
        if job.kind not in ("clip", "score") or not job.state.get("video_id") or self._has_score(job):
            return
        if not m.key or not _TBA_KEY.search(m.key):
            return
        now = self.clock()
        prefix = job.id.rsplit(":", 2)[0]         # "<name>@<start>": the describe job belongs to this upload
        self.outbox.add(Job(id=f"{prefix}:describe:youtube", destination="youtube", kind="describe", path=job.path,
                            match=m, state={**job.state, "describe_until": now + DESCRIBE_FOR_S}),
                        background=True, not_before=now + DESCRIBE_EVERY_S)

    def _tba_link(self, job: Job) -> None:
        """Queue the "link" job that puts this YouTube video on its match's TBA page (tbalink.py): only with [youtube]
        tba_links on, for a match TBA has a page for, never for a partial clip (perhaps an aborted attempt)."""
        m = job.match
        if not self.cfg_youtube.get("tba_links") or not m.key or not _TBA_KEY.search(m.key):
            return
        if "_partial" in (m.name or ""):
            return
        from .tbalink import LINK_FOR_S
        prefix = job.id.rsplit(":", 2)[0]
        self.outbox.add(Job(id=f"{prefix}:link:tba", destination="tba", kind="link", path=job.path, match=m,
                            state={"video_id": job.state["video_id"], "link_until": self.clock() + LINK_FOR_S}),
                        background=True)

    def _bracket(self, m, dest_name: str = "youtube") -> None:
        """Set a destination's playoff_alliances (the "(R2)" in playoff titles) from TBA's event."""
        if m.comp_level in (None, "qm", "p") and "playoff" not in (m.key or ""):
            return
        dest = getattr(self.outbox, "destinations", {}).get(dest_name)
        if dest is None or not hasattr(dest, "playoff_alliances"):
            return
        n = self.cfg_event.get("playoff_alliances")
        if not n and m.event_key:
            n, checked = self._alliances.get(m.event_key, (None, -1e18))
            if n is None and self.clock() - checked >= EVENT_RECHECK_S:
                try:
                    n = playoff_alliances(self._get(f"/event/{m.event_key}"))
                except Exception as e:  # noqa: BLE001 - keep the default (8) until TBA answers
                    self.log(f"TBA event {m.event_key}: bracket size not available ({type(e).__name__})")
                self._alliances[m.event_key] = (n, self.clock())
        if n:
            dest.playoff_alliances = int(n)


def playoff_alliances(event: dict) -> int | None:
    """4 or 8 from a TBA event record (playoff_type_string, else playoff_type); None when it says neither."""
    if not isinstance(event, dict):
        return None
    text = str(event.get("playoff_type_string") or "")
    if re.search(r"\b4\s*(alliance|team)", text, re.I):
        return 4
    if re.search(r"\b8\s*(alliance|team)", text, re.I):
        return 8
    code = event.get("playoff_type")
    if isinstance(code, int):
        return 4 if code in _FOUR_ALLIANCES else 8
    return None


def _fill(m, entry: dict) -> bool:
    """Teams (when the match has none) and the final score (alliances.*.score >= 0; TBA posts -1 until then) from a
    TBA match record into `m`. True when the score was there."""
    if not m.red and not m.blue:
        m.red, m.blue = alliance_teams(entry, "red"), alliance_teams(entry, "blue")
    al = entry.get("alliances") or {}
    red, blue = (al.get("red") or {}).get("score"), (al.get("blue") or {}).get("score")
    if isinstance(red, int) and isinstance(blue, int) and red >= 0 and blue >= 0:
        m.red_score, m.blue_score = red, blue
        return True
    return False
