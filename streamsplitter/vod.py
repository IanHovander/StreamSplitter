"""A saved stream split like a live one: a Twitch VOD (twitch.tv/videos/<id>) or the YouTube video of a past stream.

    streamsplitter vod URL --event 2026mttd [--tag TAG] [--to drive] [--from-s S] [--until-s S] [--no-publish]

The video is copied from the platform (no re-encode) at download speed into the recorder's ~20 s MPEG-TS chunks
and index.json in <work_root>/live/<tag>/ (record.py's format, with `vod: true`), and the live follower
(live.follow) cuts every match in it and hands each clip to the publisher, exactly as it would have live. The run
ends by itself once the whole video is read (the index's `finished`) and every upload is done or has failed; one
still failing stays queued in the outbox and goes at the next run with the same tag.

Stream time t is seconds into the video, and t = 0 is the video's start (Twitch's created_at, YouTube's
release_timestamp), so wall0 is when the platform showed it: the namer's posted-time matching works as live, and each
clip file's time is set to when its match ended (Google Drive shows that, not the night it was split).

--to slack,youtube,drive,wiki: only these destinations, of those config.toml turns on (default: all of them). A rerun
with the same tag copies the video again (unless the last copy finished) but the follower carries on from
follower.json, so no match is cut or sent twice. The chunks are kept until the next run with the tag, like the
recorder's (28 h).

User, 2026-11-06: MadTown Throwdown 2026 streamed on Twitch only (its YouTube day videos never went live); test
splitting the practice day's Twitch VOD into the team's Google Drive.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import threading
import time
from pathlib import Path

from . import config, ffmpeg
from .record import js_runtimes, write_json_atomic

CHUNK_S = 20.0
DESTINATIONS = ("slack", "youtube", "drive", "wiki")
DRAIN_FAIL_S = 600.0      # stop waiting for a destination that has failed this long (its clips stay queued)
DRAIN_MAX_S = 4 * 3600.0
COPY_TRIES = 4


def format_selector(max_height: int) -> str:
    """Video-only H.264 + m4a audio (YouTube), else one muxed H.264 rendition (Twitch: "1080p60"), else anything."""
    h = int(max_height)
    return "/".join([f"bv[height<={h}][vcodec^=avc1]+ba[ext=m4a]", f"b[height<={h}][vcodec^=avc1]",
                     f"bv[height<={h}]+ba", f"b[height<={h}]", "b"])


def resolve(url: str, max_height: int = 1080) -> dict:
    """{"id", "title", "start_ts", "duration", "inputs": [{"url", "headers"}], "fmt", "webpage_url"} for a saved
    video. ValueError when it is live (use `live`), upcoming, or has no video."""
    import yt_dlp

    if "://" not in url:
        url = "https://" + url
    params = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True,
              "format": format_selector(max_height), "socket_timeout": 20}
    rt = js_runtimes()
    if rt:
        params["js_runtimes"] = rt
    try:
        with yt_dlp.YoutubeDL(params) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise ValueError(str(e).replace("ERROR: ", "").strip().splitlines()[0][:300]) from None
    if info.get("_type") == "playlist" or not info.get("formats"):
        raise ValueError("no video at this link (a channel page? give the link of one saved video)")
    status = info.get("live_status")
    if status == "is_live":
        raise ValueError("this video is still live: use `streamsplitter live` for it")
    if status in ("is_upcoming", "post_live"):
        raise ValueError(f"this video is {status.replace('_', ' ')}: it cannot be read yet")
    req = info.get("requested_formats") or [info]
    inputs = [{"url": f["url"], "headers": dict(f.get("http_headers") or info.get("http_headers") or {})}
              for f in req]
    start = info.get("release_timestamp") or info.get("timestamp")
    return {"id": str(info.get("id")), "title": str(info.get("title") or ""),
            "start_ts": float(start) if start else None, "duration": info.get("duration"),
            "webpage_url": str(info.get("webpage_url") or url), "inputs": inputs,
            "fmt": " + ".join(str(f.get("format_id")) for f in req),
            "height": max([f.get("height") or 0 for f in req] or [0])}


def default_tag(event: str | None, video_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", f"{event + '_' if event else 'vod_'}{video_id}")


def copy_cmd(ff: str, src: dict, out: Path, prefix: str, from_s: float, until_s: float | None,
             chunk_s: float = CHUNK_S) -> list[str]:
    """ffmpeg: the video's inputs from `from_s` (to `until_s`) -> <prefix>_NNNNN.ts chunks + <prefix>.csv."""
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-y"]
    for i in src["inputs"]:
        ua = (i.get("headers") or {}).get("User-Agent")
        if ua:
            cmd += ["-user_agent", ua]
        if from_s > 0:
            cmd += ["-ss", f"{from_s:.3f}"]
        if until_s is not None:
            cmd += ["-t", f"{max(0.0, until_s - from_s):.3f}"]
        cmd += ["-i", i["url"]]
    amap = ["-map", "1:a:0"] if len(src["inputs"]) > 1 else ["-map", "0:a:0?"]
    return cmd + ["-map", "0:v:0", *amap, "-c", "copy", "-f", "segment", "-segment_time", f"{chunk_s:g}",
                  "-segment_format", "mpegts", "-segment_list", str(out / f"{prefix}.csv"),
                  "-segment_list_type", "csv", str(out / f"{prefix}_%05d.ts")]


class Copier:
    """Copies the video into chunks on its own thread and keeps index.json up to date (a chunk is listed once ffmpeg
    has finished it). A copy that fails is started again from the end of what is listed, up to COPY_TRIES times;
    `finished` is set in the index only when the whole range was copied, `failed` when it gave up."""

    def __init__(self, src: dict, folder: Path, tag: str, *, event: str | None, source_url: str, from_s: float = 0.0,
                 until_s: float | None = None, chunk_s: float = CHUNK_S, log=print, popen=subprocess.Popen,
                 poll_s: float = 1.0, retry_s: float = 5.0):
        self.src, self.folder, self.tag, self.event = src, Path(folder), tag, event
        self.from_s, self.until_s, self.chunk_s = float(from_s), until_s, chunk_s
        self.log, self.popen, self.poll_s, self.retry_s = log, popen, poll_s, retry_s
        self.ff = ffmpeg.find()
        self.start_ts = src.get("start_ts") or time.time()
        self.index = {"tag": tag, "event": event, "date": time.strftime("%Y-%m-%d"), "video": src["id"],
                      "title": src.get("title"), "release_timestamp": self.start_ts, "live": False, "vod": True,
                      "source_url": source_url, "chunk_s": chunk_s, "status": "recording", "finished": False,
                      "updated": time.time(), "next_seq": 0, "chunks": [], "runs": []}
        self.error: str | None = None
        self._stop = threading.Event()
        self._proc = None
        self._thread = threading.Thread(target=self._run, name="vod-copy", daemon=True)

    @property
    def index_path(self) -> Path:
        return self.folder / "index.json"

    def done_before(self) -> bool:
        """The last run with this tag copied this same video and range completely: reuse its chunks."""
        try:
            old = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        same = (old.get("video") == self.src["id"] and old.get("finished") and old.get("from_s", 0.0) == self.from_s
                and old.get("until_s") == self.until_s)
        if same and all((self.folder / c["path"]).is_file() for c in old.get("chunks") or []):
            self.index = old
            return True
        return False

    def clear(self) -> None:
        """A fresh copy: the old chunks and index go, the follower's own files (follower.json, its lock) stay."""
        for p in [*self.folder.glob("v[0-9][0-9]_*.ts"), *self.folder.glob("v[0-9][0-9].csv"),
                  *self.folder.glob("v[0-9][0-9].log"), self.index_path]:
            p.unlink(missing_ok=True)

    def start(self) -> None:
        self.index.update(from_s=self.from_s, until_s=self.until_s)
        self._save()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        p = self._proc
        if p is not None and p.poll() is None:
            p.kill()
        if self._thread.is_alive():
            self._thread.join(10.0)

    def _save(self) -> None:
        self.index["updated"] = time.time()
        write_json_atomic(self.index_path, self.index)

    def _list(self, prefix: str, start: float, seen: int, final: bool) -> int:
        """List the chunks <prefix>.csv names that are not listed yet; returns how many rows were read."""
        path = self.folder / f"{prefix}.csv"
        try:
            with path.open(newline="") as f:
                rows = [r for r in csv.reader(f) if len(r) == 3]
        except OSError:
            return seen
        for name, a, b in rows[seen:]:
            chunks = self.index["chunks"]
            t0, t1 = start + float(a), start + float(b)
            if chunks and t0 < chunks[-1]["t1"]:          # a restarted copy overlaps the last listed chunk: no gap
                t0 = chunks[-1]["t1"]
            gap = None if not chunks else round(max(0.0, t0 - chunks[-1]["t1"]), 3)
            chunks.append({"seq": self.index["next_seq"], "path": name, "t0": t0, "t1": t1,
                           "wall0": self.start_ts + t0, "wall_closed": time.time(), "run": len(self.index["runs"]) - 1,
                           "source_video": self.src["id"], "gap_before_s": gap})
            self.index["next_seq"] += 1
            self.index["t_end"] = t1
        if len(rows) > seen or final:
            self._save()
        return len(rows)

    def _run(self) -> None:
        start = self.from_s
        for attempt in range(COPY_TRIES):
            if self._stop.is_set():
                return
            prefix = f"v{attempt:02d}"
            cmd = copy_cmd(self.ff, self.src, self.folder, prefix, start, self.until_s, self.chunk_s)
            self.index["runs"].append({"run": attempt, "video": self.src["id"], "started": time.time(),
                                       "t_base": start, "csv": f"{prefix}.csv", "format": self.src.get("fmt")})
            self._save()
            err_path = self.folder / f"{prefix}.log"
            with err_path.open("wb") as err:
                self._proc = p = self.popen(cmd, stdout=subprocess.DEVNULL, stderr=err)
                seen = 0
                while p.poll() is None and not self._stop.is_set():
                    seen = self._list(prefix, start, seen, final=False)
                    self._stop.wait(self.poll_s)
            code = p.wait()
            self._list(prefix, start, seen, final=True)
            if self._stop.is_set():
                return
            run = self.index["runs"][-1]
            run.update(ended=time.time(), exit=code)
            chunks = self.index["chunks"]
            end = chunks[-1]["t1"] if chunks else start
            total = self.until_s if self.until_s is not None else (self.src.get("duration") or None)
            short = total is not None and end < float(total) - 2 * self.chunk_s
            if code == 0 and not short:
                self.index.update(status="stopped", finished=True)
                self._save()
                self.log(f"copied the whole video: {len(chunks)} chunks, {end - self.from_s:.0f} s", flush=True)
                return
            why = err_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1:] or ["no message"]
            self.log(f"the copy stopped at {end:.0f} s (ffmpeg exit {code}: {why[0][:200]}); "
                     f"{'starting it again from there' if attempt + 1 < COPY_TRIES else 'giving up'}", flush=True)
            start = end
            self._stop.wait(self.retry_s * (attempt + 1))
        self.error = f"the copy of the video failed {COPY_TRIES} times"
        self.index.update(status="stopped", finished=True, failed=self.error)
        self._save()


def _restrict(only: set[str] | None):
    """build_publishing's view of config.toml with the destinations outside `only` turned off."""
    if only is None:
        return None
    unknown = only - set(DESTINATIONS)
    if unknown:
        raise ValueError(f"--to: unknown destination(s) {', '.join(sorted(unknown))} (use {', '.join(DESTINATIONS)})")
    return only


def drain(outbox, log=print, poll_s: float = 5.0, fail_s: float = DRAIN_FAIL_S, max_s: float = DRAIN_MAX_S,
          clock=time.time, sleep=time.sleep) -> dict:
    """Wait until the outbox has nothing queued or sending for its destinations; a destination failing for `fail_s`
    is not waited for (its clips stay queued for the next run). Returns the outbox status at the end."""
    t0 = last_said = clock()
    while True:
        st = outbox.status()
        waiting = {d: s["queued"] + s["running"] for d, s in st.items() if s.get("active")
                   and not (s.get("paused") or (s.get("failing_since") is not None
                                                and clock() - s["failing_since"] >= fail_s))
                   and s["queued"] + s["running"] > 0}
        if not waiting or clock() - t0 >= max_s:
            return st
        if clock() - last_said >= 60:
            last_said = clock()
            log("waiting for uploads: " + ", ".join(f"{d} {n}" for d, n in waiting.items()), flush=True)
        sleep(poll_s)


def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("url", help="a saved video: twitch.tv/videos/<id>, or a YouTube video of a past stream")
    ap.add_argument("--event", required=True, help="TBA event key (e.g. 2026mttd), for match names from the schedule")
    ap.add_argument("--tag", help="name of this run's folders (default: <event>_<video id>)")
    ap.add_argument("--to", help="only these destinations, comma-separated: slack, youtube, drive, wiki "
                    "(default: every one config.toml turns on)")
    ap.add_argument("--from-s", type=float, default=0.0, help="start this many seconds into the video")
    ap.add_argument("--until-s", type=float, help="stop this many seconds into the video")
    ap.add_argument("--max-height", type=int, default=1080, help="highest video height to copy (default 1080)")
    ap.add_argument("--deliver", type=Path, help="also copy every clip here (default: deliver_dir in config.toml)")
    ap.add_argument("--deliver-height", type=int, help="make the delivered copies this tall")
    ap.add_argument("--no-publish", action="store_true", help="cut the clips only: nothing to Slack, YouTube, "
                    "Google Drive or the wiki, and no Slack DMs")


def run_from_args(a: argparse.Namespace) -> int:
    from . import live

    try:
        only = _restrict({d.strip().lower() for d in a.to.split(",") if d.strip()} if a.to else None)
        src = resolve(a.url, a.max_height)
    except ValueError as e:
        print(f"streamsplitter vod: {e}", flush=True)
        return 2
    tag = a.tag or default_tag(a.event, src["id"])
    folder = live.live_folder(tag)
    folder.mkdir(parents=True, exist_ok=True)
    lock = live.RunLock(folder / live.LOCK_FILE)
    if not lock.acquire(wait_s=live.LOCK_WAIT_S):
        print(f"Another run of {tag} is still going (it holds {lock.path}); this one does not start.", flush=True)
        return 1
    held = {"outbox": None}
    try:
        return _split(a, src, tag, folder, only, held)
    finally:
        box = held["outbox"]
        if box is None or not box.busy():
            lock.release()


def _split(a, src: dict, tag: str, folder: Path, only, held: dict) -> int:
    from . import live
    from .publish.notify import Notifier

    out = config.data_root() / "clips" / tag
    deliver = a.deliver or config.deliver_dir()
    if deliver is not None:
        deliver = Path(deliver) / tag
    (folder / live.STOP_FILE).unlink(missing_ok=True)
    start = time.strftime("%Y-%m-%d %H:%M", time.localtime(src["start_ts"])) if src.get("start_ts") else "unknown"
    print(f"{src['webpage_url']}: {src['title']} ({src['fmt']}, {src['height']}p, "
          f"{(src.get('duration') or 0) / 60:.0f} min, started {start})\nchunks -> {folder}\nclips -> {out}", flush=True)
    if not src.get("start_ts"):
        print("the platform does not say when the video started: wall times are this run's, so names come from "
              "the overlay only", flush=True)
    copier = Copier(src, folder, tag, event=a.event, source_url=a.url, from_s=a.from_s, until_s=a.until_s)
    if copier.done_before():
        print(f"reusing the {len(copier.index['chunks'])} chunks of the last complete copy", flush=True)
    else:
        copier.clear()
        copier.start()
    if a.no_publish:
        notifier, outbox, publisher, summary = Notifier(), None, None, "publishing is off"
    else:
        notifier, outbox, publisher, _, summary = live.build_publishing(tag, only=only, event=a.event)
    held["outbox"] = outbox
    print(f"publishing: {summary}", flush=True)
    monitor = live.Monitor(folder, notifier, publisher, tag=tag,
                           disk_paths=[("clips (data_root)", config.data_root()),
                                       ("recorded chunks (work_root)", config.work_root())])
    reason = "stopped"
    try:
        pipe = live.Pipeline(out, event=a.event, deliver_dir=deliver, deliver_height=a.deliver_height)
        if publisher is not None:
            publisher.schedule = lambda: pipe.namer.schedule
            publisher.score_search = pipe.score
        reason = live.follow(folder / "index.json", pipe, event=a.event, publisher=publisher, notifier=notifier,
                             monitor=monitor)
        if outbox is not None:
            print("every chunk is read; waiting for the uploads", flush=True)
            st = drain(outbox)
            for d, s in st.items():
                if s.get("active"):
                    print(f"{d}: {s['done']} sent, {s['queued'] + s['running']} still queued, {s['failed']} failed"
                          + (f" (last error: {s['last_error']})" if s.get("last_error") else ""), flush=True)
    except BaseException as e:
        reason = "stopped (Ctrl+C)" if isinstance(e, KeyboardInterrupt) else f"crashed: {type(e).__name__}: {e}"
        raise
    finally:
        copier.stop()
        if outbox is not None:
            outbox.stop(timeout=5.0)
        monitor.write_status(final=True, reason=reason)
        if notifier.pending():
            notifier.pump(force=True)
        notifier.close()
    clips = sorted(p.name for p in out.glob("*.mp4")) if out.is_dir() else []
    print(f"done ({reason}): {len(clips)} clip file(s) in {out}" + (f"; {copier.error}" if copier.error else ""),
          flush=True)
    return 1 if copier.error else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="streamsplitter vod", description="Split a saved stream into match clips.")
    add_arguments(ap)
    return run_from_args(ap.parse_args(argv))
