"""Pipeline: one recorded chunk at a time, the way a live recording arrives: overlay reader -> timeline -> namer -> cutter.

It is event-agnostic: the timer, title and team numbers are read by OCR (detect/overlay.py), and matches come from
the countdown structure (timeline.py). The field's own FMS sounds (the same files every season) are a fallback for a
match the overlay did not show (Eagan 2025: 2 of 11 matches had no overlay at all): a start sound followed one match
length later by the end buzzer, with no overlay match there, is cut from the sounds alone (`start_src` "audio"). The schedule (TBA) is optional; it only turns the team numbers read into
names. A match clip is written as soon as the chunk holding its end + padding has been read. A copy goes to
`deliver_dir` (e.g. a cloud-synced folder drive team opens): it is written under a temporary name and renamed when
complete, so a sync client never uploads half a file.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from collections import Counter
from pathlib import Path

from . import cutter, ffmpeg
from .detect import audio as fms_audio
from .detect.overlay import OverlayReader
from .detect.overlay import parse_time
from .namer import Namer, order_key, parse_title, teams_of
from .timeline import Timeline

log = logging.getLogger(__name__)

_TEAM = re.compile(r"^\s*(\d{1,5}[A-Z]?)\s*$")     # offseason B teams: TBA keys like frc1323B


class Pipeline:
    def __init__(self, out_dir: Path, *, event: str | None = None, schedule: list[dict] | None = None,
                 release_ts: float = 0.0, pad: float = 3.0, start_extra: float = 0.5, end_extra: float = 1.0, deliver_dir: Path | None = None,
                 deliver_height: int | None = None,
                 reader=None, score: bool = True, fms_sounds: bool = True):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.reader = reader or OverlayReader()
        self.timeline = Timeline()
        self.namer = Namer(schedule or [], release_ts, event=event)
        self.team_set = {t for e in (schedule or []) for t in teams_of(e)}
        self.pad = pad
        # Overlay timers run up to ~1 s ahead of the field (FMS D2 f1m3: start 0.56 s and end 0.40 s before the
        # audio cues; Eagan 2025's own overlay: 1.0 s and 0.9 s; Cheesy Arena: on time), so the end padding gets this
        # much more to stay >= `pad`: 3.5-4.5 s in effect at both ends.
        self.end_extra = end_extra
        self.start_extra = start_extra       # the same margin before the start: 3.5 s nominal, 3-5 s in effect
        self.deliver_dir = Path(deliver_dir) if deliver_dir else None
        self.deliver_height = deliver_height   # e.g. 720: a smaller copy for phones (3x smaller, ~7 s to make)
        self.chunks: list[dict] = []
        self.pending: list[dict] = []
        self.rows: list[dict] = []
        self.score = score
        self.fms = None
        if fms_sounds:
            tpl = Path(__file__).resolve().parent / "seasons" / "2026" / "cues_v1.npz"
            self.fms = fms_audio.AudioDetector(fms_audio.load_templates(tpl), thr=0.40)
        self.cues: list[dict] = []
        self._audio_used: list[float] = []
        self.skip_until: float | None = None   # matches ending by then were cut before a restart
        self.score_pending: list[dict] = []
        self.score_done: list[dict] = []      # rows whose score clip was settled by the last feed()
        self._audio_failed = False

    def set_schedule(self, schedule: list[dict]) -> None:
        """A newer schedule (live: TBA publishes it during the event). Names already given stay."""
        self.namer.schedule = sorted(schedule, key=order_key)
        self.namer.event = self.namer.event or next((e["event_key"] for e in schedule if e.get("event_key")), None)
        self.team_set = {t for e in schedule for t in teams_of(e)}

    def feed(self, path, c: dict, now_wall: float | None = None) -> list[dict]:
        """Read one chunk ({"t0", "t1"} stream seconds); returns the rows of the clips written because of it."""
        self.chunks.append({"path": str(path), "t0": c["t0"], "t1": c["t1"], "gap_before_s": c.get("gap_before_s")})
        self.reader.feed(path, c["t0"])
        if self.fms is not None:
            try:
                self.cues += [e for e in self.fms.feed(path, c["t0"]) if e["score"] >= 0.5]
            except (subprocess.SubprocessError, OSError) as e:
                # sound that cannot be decoded (1671's MadTown 2026 stream: sample rate 0 for 5 hours) loses only the
                # sound fallback; the overlay still finds the matches (it used to lose the whole chunk, every playoff)
                if not self._audio_failed:
                    log.warning("the stream's sound cannot be decoded (%s); matches are found from the overlay alone "
                                "until it can", type(e).__name__)
                self._audio_failed = True
            else:
                self._audio_failed = False
            self.cues = [e for e in self.cues if c["t1"] - e["t"] < 1200.0]
        new = self.timeline.update(self.reader.episodes(), c["t1"])
        for m in [m for m in new if self._under_break_banner(m)]:
            new.remove(m)
            self.timeline.matches.remove(m)
            self.timeline.rejected.append({**m, "why": "break banner"})
            log.info("countdown to %.1f is under a break banner, not a match", m["end"])
        self.pending += new
        self.pending += self._audio_matches(c["t1"])
        done = []
        for m in list(self.pending):
            if m["end"] + self.end_extra + self.pad + 0.2 > c["t1"]:
                continue                       # its end padding is in the next chunk
            self.pending.remove(m)
            if self.skip_until is not None and m["end"] <= self.skip_until + 1.0:
                # cut before a restart (live.follow); a score search restored with it gets its timer place back
                for r in self.score_pending:
                    if r.get("restored") and r.get("place") is None and abs(r["end"] - m["end"]) <= 2.0:
                        r["place"] = m.get("place")
                continue
            done.append(self._finish(m, now_wall if now_wall is not None else c["t1"]))
        self.score_done = self._scores(c["t1"]) if self.score else []
        return done

    def restore_pending(self, rows: list[dict]) -> None:
        """Score searches a run that stopped without settling them left open (live.follow keeps them in
        follower.json); the follower re-reads their chunks from the match start. A timer place id belongs to the
        reader that read it, so a restored row's place is unknown (no timer check) until the re-read finds its match
        again (feed, where that match is skipped as already cut)."""
        for r in rows:
            r["place"], r["restored"] = None, True
            self.score_pending.append(r)

    # Break and timeout screens count down in the match timer's place (MadTown 2026 finals: "Field Break ... Next Up:
    # Final 1" over the timer); a countdown that shows such words on most whole-frame reads of its last minute is one
    BREAK_WORDS = re.compile(r"\b(break|time\s?out|next\s?up)\b", re.I)

    def _under_break_banner(self, m: dict) -> bool:
        reads = self.reader.full_reads(m["end"] - 60.0, m["end"])
        hits = sum(1 for _, tx in reads if any(self.BREAK_WORDS.search(x["text"]) for x in tx))
        return hits >= 2 and hits >= len(reads) / 2

    def overlay_names(self, m: dict) -> tuple[set[str], dict | None, set[str]]:
        """Team numbers read at least twice during the match (only numbers on the schedule, when there is one), the
        most read match title, and every number shown on at least half of the match's whole-frame reads (the
        overlay's team panel as shown: offseason B teams appear under stand-in numbers, 9993 for 254B at MadTown)."""
        texts = self.reader.texts_between(m["start"] - 20.0, m["end"] + 5.0)
        title_ok = {id(x) for _, tx in self.reader.full_reads(m["start"] - 20.0, m["end"] + 5.0)
                    for x in self._title_texts(tx)}
        nums = Counter()
        shown = Counter()
        titles = Counter()
        for x in texts:
            mt = _TEAM.match(x["text"])
            if mt and m["start"] <= x["t"] <= m["end"]:
                shown[mt.group(1)] += 1
                if not self.team_set or mt.group(1) in self.team_set:
                    nums[mt.group(1)] += 1
            p = parse_title(x["text"]) if id(x) in title_ok else None
            if p:
                titles[(p["level"], p["n"])] += 1
        teams = {t for t, n in nums.most_common(6) if n >= 2} if self.team_set else set()
        reads = len({x["t"] for x in texts if m["start"] <= x["t"] <= m["end"]})
        shown_set = {t for t, n in shown.items() if n >= max(2, reads / 2)}
        title = None
        if titles:
            (lv, n), k = titles.most_common(1)[0]
            if k >= 2:
                title = {"level": lv, "n": n, "reads": k}
        return teams, title, shown_set

    def _finish(self, m: dict, now_wall: float) -> dict:
        teams, title, shown = self.overlay_names(m)
        naming = self.namer.name_overlay(m["start"], teams, title, now_wall)
        if m.get("partial") and naming.get("key"):
            # possibly an aborted attempt: keep it, but the replay gets the match name
            k = naming["key"]
            self.namer.used[k] = self.namer.used.get(k, 1) - 1
            if self.namer.used[k] <= 0:
                del self.namer.used[k]
            n = sum(1 for r in self.rows if (r["name"] or "").startswith(f"{k}_partial"))
            naming = {**naming, "name": f"{k}_partial" + (f"{n + 1}" if n else "")}
        stem = naming["name"] or f"t{m['start']:08.1f}"
        t_cut = time.perf_counter()
        clip = cutter.cut(self.chunks, m["start"] - self.start_extra, m["end"] + self.end_extra, self.out_dir / f"{stem}.mp4", pad=self.pad)
        clip["cut_s"] = round(time.perf_counter() - t_cut, 2)
        if self.deliver_dir:
            delivered = self._deliver_safe(Path(clip["path"]))
            if delivered:
                clip["delivered"] = delivered
        row = {"name": naming["name"], "naming": naming, "start": m["start"], "end": m["end"],
               "start_src": m["start_src"], "end_src": m["end_src"], "L_auto": m.get("L_auto"),
               "L_tele": m.get("L_tele"), "pause": m.get("pause"), "place": m["place"],
               "auto": m.get("auto"), "teleop": m["teleop"], "clip": clip, "written_perf": time.perf_counter(),
               "gap_inside": any((x.get("gap_before_s") or 0) > 0 and m["start"] < x["t0"] < m["end"]
                                 for x in self.chunks)}
        row["partial"] = bool(m.get("partial"))
        row["numbers_shown"] = sorted(shown)
        self.rows.append(row)
        if self.score and not row["partial"]:
            self.score_pending.append(row)
        return row

    # score version ------------------------------------------------------------------------------------------------
    # The results screen is found without knowing what it looks like: a whole-frame read showing at least
    # RESULTS_TEAMS of the match's teams, no running timer (no "m:ss" text), and no other match's title. The piece is
    # the user's rule (2026-11-03): from 3 s before the screen appears until it is removed (and stays away past the
    # cap), at most 30 s after it appeared. Its title, when it shows one, confirms or corrects the match name.
    RESULTS_TEAMS = 4
    RESULTS_SEARCH_S = 420.0
    RESULTS_LEAD_S = 3.0
    RESULTS_CAP_S = 30.0

    def _is_results(self, texts: list[dict], row: dict) -> bool:
        # the end-of-match overlay shows the teams too; a read can miss its "0:00" but the timer place does not
        if texts and row["place"] is not None and self.reader.timer_read_near(row["place"], texts[0]["t"]):
            return False
        teams = set(row["naming"].get("teams_read") or [])
        key = row["naming"].get("key")
        e = next((x for x in self.namer.schedule if x["key"] == key), None)
        if e is not None:
            teams |= teams_of(e)
        shown = set(row.get("numbers_shown") or [])
        if len(shown) >= self.RESULTS_TEAMS:      # the panel as shown, stand-in numbers included
            teams |= shown
        if any((parse_time(x["text"]) or (0, ""))[1] == "mss" for x in texts):
            return False
        want = row["naming"].get("title")
        for x in texts:
            p = parse_title(x["text"])
            if p and want and (p["level"], p["n"]) != (want["level"], want["n"]):
                return False
        nums = {mt.group(1) for x in texts if (mt := _TEAM.match(x["text"]))}
        if teams:
            return len(nums & teams) >= min(self.RESULTS_TEAMS, len(teams))
        return bool(want) and sum(1 for n in nums if len(n) >= 3) >= self.RESULTS_TEAMS

    def _scores(self, now: float) -> list[dict]:
        """The rows whose results-screen search settled by stream time `now`. A row that raises (a cut that keeps
        failing) is settled without a score version (row["score_error"] says why; its match clip goes to YouTube
        instead), so it never holds up the rows after it."""
        settled = []
        for row in list(self.score_pending):
            try:
                done = self._score_row(row, now)
            except Exception as e:  # noqa: BLE001 - one bad cut must not stop every later upload
                log.warning("score version of %s failed: %s: %s", row.get("name"), type(e).__name__, e,
                            exc_info=True)
                row["score_piece"] = None
                row.pop("score_clip", None)
                row["score_error"] = f"{type(e).__name__}: {e}"
                done = True
            if done:
                self.score_pending.remove(row)
                settled.append(row)
        return settled

    def _score_row(self, row: dict, now: float) -> bool:
        """One row's search at `now`: True when it settled (score clip cut, or no results screen in time)."""
        nxt = min((m["start"] for m in self.timeline.matches if m["start"] > row["end"]), default=None)
        limit = min(row["end"] + self.RESULTS_SEARCH_S, nxt if nxt is not None else float("inf"))
        reads = self.reader.full_reads(row["end"] + 2.0, min(now, limit))
        flags = [(t, self._is_results(tx, row), tx) for t, tx in reads]
        first = next((i for i, f in enumerate(flags) if f[1]), None)
        if first is None:
            if now >= limit:
                row["score_piece"] = None
                return True
            return False
        t_a = flags[first][0]
        t_prev = flags[first - 1][0] if first > 0 else t_a - self.reader.full_every_s
        appear = self.reader.arrived(t_a, t_prev) or t_a
        cap = appear + self.RESULTS_CAP_S
        if now < cap + 0.5:
            return False
        # the end: the start of the last absence that runs through the cap, else the cap
        end, last_on = cap, t_a
        for t, f, _ in flags[first + 1:]:
            if t > cap:
                break
            if f:
                last_on, end = t, cap
            elif end == cap:
                end = self.reader.left(last_on, t) or t
        piece = {"appear": round(appear, 3), "start": round(appear - self.RESULTS_LEAD_S, 3),
                 "end": round(end, 3), "first_read": t_a}
        row["score_piece"] = piece
        on_reads = [tx for t, f, tx in flags if f and appear <= t <= cap]
        self._confirm_name(row, on_reads)
        stem = row["name"] or f"t{row['start']:08.1f}"
        row["score_clip"] = cutter.cut_score(self.chunks, row["start"] - self.start_extra, row["end"] + self.end_extra,
                                             piece["start"], piece["end"], self.out_dir / f"{stem}_score.mp4",
                                             pad=self.pad)
        if self.deliver_dir:
            delivered = self._deliver_safe(Path(row["score_clip"]["path"]))
            if delivered:
                row["score_clip"]["delivered"] = delivered
        row["score_written_t"] = now
        return True

    _ADVANCES = re.compile(r"\badvances?\b|\bnext\s?up\b", re.I)

    @classmethod
    def _title_texts(cls, tx: list[dict]) -> list[dict]:
        """A read's texts that may be this match's title: not a label naming another match ("Advances to Final 1" under
        the winner on MadTown 2026's results screen renamed sf11m1 to f1m1), nor the line just below such a label."""
        labels = [x["box"] for x in tx if cls._ADVANCES.search(x["text"])]

        def under(b, lb):
            h = lb[3] - lb[1]
            return lb[1] < b[1] <= lb[3] + 1.5 * h and min(b[2], lb[2]) - max(b[0], lb[0]) > 0
        return [x for x in tx if not cls._ADVANCES.search(x["text"]) and not any(under(x["box"], lb) for lb in labels)]

    def _confirm_name(self, row: dict, on_reads: list[list[dict]]) -> None:
        titles = Counter((p["level"], p["n"]) for tx in on_reads for x in self._title_texts(tx)
                         if (p := parse_title(x["text"])))
        if not titles:
            return
        (lv, n), k = titles.most_common(1)[0]
        final = self.namer.confirm_parsed(row["naming"], {"level": lv, "n": n}, f"results title x{k}",
                                          source="results_title")
        if final is None:
            return
        if final["name"] == row["name"]:
            row["naming"] = final
            return
        old = Path(row["clip"]["path"])
        new = old.with_name(f"{final['name']}.mp4")
        try:
            old.replace(new)
        except OSError:            # Windows: the clip is open (a player, an upload); copy it and leave the old one
            shutil.copyfile(old, new)
            try:
                old.unlink()
            except OSError:
                log.warning("renamed %s to %s by copying; the old file is still open", old.name, new.name)
        row["clip"]["path"] = str(new)
        if row["clip"].get("delivered"):
            try:
                Path(row["clip"]["delivered"]).unlink(missing_ok=True)
            except OSError:
                pass
            row["clip"]["delivered"] = self._deliver_safe(new)
        row["name"], row["naming"] = final["name"], final

    def _deliver_safe(self, path: Path) -> str | None:
        """_deliver, or None when the copy failed (logged): the delivery folder (a cloud-synced one may be locked or
        full) is a convenience, and a failed copy must not lose the clip for Slack and YouTube."""
        try:
            return str(self._deliver(path))
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("could not copy %s to %s: %s: %s", path.name, self.deliver_dir, type(e).__name__, e)
            return None

    def _deliver(self, path: Path) -> Path:
        self.deliver_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.deliver_dir / f".{path.name}.part"
        if self.deliver_height:
            # re-encoded small copy (libx264 veryfast, everywhere; 6.7 s for a 170 s 1080p60 clip on the home PC,
            # 126 MB -> 40 MB). The edit list of the clip is honoured, so the copy starts exactly too.
            subprocess.run([ffmpeg.find(), "-v", "error", "-y", "-i", str(path), "-vf",
                            f"scale=-2:{int(self.deliver_height)},fps=30", "-c:v", "libx264", "-preset", "veryfast",
                            "-crf", "26", "-c:a", "copy", "-movflags", "+faststart", "-f", "mp4", str(tmp)], check=True)
        else:
            shutil.copyfile(path, tmp)
        dst = self.deliver_dir / path.name
        tmp.replace(dst)
        return dst

    # FMS sound fallback ------------------------------------------------------------------------------------------
    AUDIO_WAIT_S = 30.0       # decide an audio match only once the overlay timeline has had time to claim it
    AUDIO_LEN = (148.0, 170.0)   # start sound to end buzzer before the event's own length is learned (2025: 153 s)

    def _audio_matches(self, now: float) -> list[dict]:
        out = []
        vals = self.timeline.values()
        length = vals["L_auto"] + vals["pause"] + vals["L_tele"] if vals else None
        starts = [e["t"] for e in self.cues if e["cue"] == "start"]
        buzz = [e["t"] for e in self.cues if e["cue"] == "buzzer"]
        known = self.timeline.matches + self.pending
        for s0 in starts:
            if any(abs(s0 - u) < 5.0 for u in self._audio_used):
                continue
            if length is not None:
                ends = [b for b in buzz if abs(b - s0 - length) <= 3.0]
            else:
                ends = [b for b in buzz if self.AUDIO_LEN[0] <= b - s0 <= self.AUDIO_LEN[1]]
            if not ends or now < ends[0] + self.AUDIO_WAIT_S:
                continue
            e = ends[0]
            if any(m["start"] - 20.0 < s0 < m["end"] for m in known):
                self._audio_used.append(s0)          # the overlay has it (or the start cue is inside a match)
                continue
            self._audio_used.append(s0)
            out.append({"start": round(s0, 3), "end": round(e, 3), "start_src": "audio", "end_src": "audio",
                        "place": None, "teleop": None, "auto": None, "partial": False})
        return out
