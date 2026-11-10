"""Live recorder bookkeeping: atomic index writes, listing only finished chunks, gaps across runs, salvage, pruning,
HLS playlist parsing and start-segment choice. No internet; the last test runs the real fetch -> relay -> segment
path on a local HLS playlist served from 127.0.0.1 (skipped without ffmpeg).

    .venv\\Scripts\\python.exe -m pytest -p no:cacheprovider tests\\test_record.py
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from streamsplitter import ffmpeg, record  # noqa: E402
from streamsplitter.record import LiveIndex  # noqa: E402


def touch(path: Path, age_s: float = 0.0, data: bytes = b"x") -> Path:
    path.write_bytes(data)
    if age_s:
        t = time.time() - age_s
        os.utime(path, (t, t))
    return path


def write_list(path: Path, rows) -> None:
    path.write_text("".join(f"{n},{a:.6f},{b:.6f}\n" for n, a, b in rows), encoding="utf-8")


# ---------------------------------------------------------------------------------------------------------------
# atomic writes and the segment list
# ---------------------------------------------------------------------------------------------------------------

def test_atomic_write_replaces_whole_file_and_leaves_no_temp(tmp_path):
    p = tmp_path / "index.json"
    record.write_json_atomic(p, {"a": 1})
    record.write_json_atomic(p, {"a": 2, "chunks": list(range(1000))})
    assert json.loads(p.read_text(encoding="utf-8"))["a"] == 2
    assert [x.name for x in tmp_path.iterdir()] == ["index.json"]


def test_atomic_write_retries_while_a_reader_holds_the_file(tmp_path, monkeypatch):
    p = tmp_path / "index.json"
    real, calls = os.replace, []

    def flaky(a, b):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("held by a reader")
        real(a, b)

    monkeypatch.setattr(record.os, "replace", flaky)
    record.write_json_atomic(p, {"ok": True})
    assert len(calls) == 3 and json.loads(p.read_text(encoding="utf-8")) == {"ok": True}


def test_segment_list_ignores_a_half_written_line(tmp_path):
    p = tmp_path / "r000.csv"
    p.write_text("r000_00000.ts,0.000000,60.000000\nr000_00001.ts,60.000000,12", encoding="utf-8")
    assert record.read_segment_list(p) == [("r000_00000.ts", 0.0, 60.0)]
    assert record.read_segment_list(tmp_path / "missing.csv") == []


def test_a_chunk_is_listed_only_once_the_next_file_exists_or_ffmpeg_exited(tmp_path):
    entries = [("r002_00000.ts", 0.0, 60.0), ("r002_00001.ts", 60.0, 120.0)]
    touch(tmp_path / "r002_00000.ts")
    touch(tmp_path / "r002_00001.ts")
    assert record.ready_entries(entries, 0, tmp_path, 2, finished=False) == entries[:1]  # 00002 not opened yet
    touch(tmp_path / "r002_00002.ts")
    assert record.ready_entries(entries, 0, tmp_path, 2, finished=False) == entries
    assert record.ready_entries(entries, 1, tmp_path, 2, finished=False) == entries[1:]
    assert record.ready_entries(entries[:1], 0, tmp_path, 2, finished=True) == entries[:1]


# ---------------------------------------------------------------------------------------------------------------
# runs, gaps and wall times
# ---------------------------------------------------------------------------------------------------------------

def new_index(tmp_path, **kw) -> LiveIndex:
    return LiveIndex.open(tmp_path, "test", source_url="https://example.invalid/live", **kw)


def test_first_run_defines_the_origin_and_chunks_are_contiguous(tmp_path):
    idx = new_index(tmp_path)
    r = idx.begin_run(anchor_wall=1000.0, wall_source="pdt", video="vidA", now=1010.0)
    assert idx.data["release_timestamp"] == 1000.0 and r["t_base"] == 0.0 and r["gap_before_s"] is None
    a = idx.add_chunk(r, "r000_00000.ts", 0.0, 60.0, 1075.0)
    b = idx.add_chunk(r, "r000_00001.ts", 60.0, 121.5, 1137.0)
    assert (a["seq"], a["t0"], a["t1"], a["wall0"], a["gap_before_s"]) == (0, 0.0, 60.0, 1000.0, None)
    assert (b["seq"], b["t0"], b["gap_before_s"], b["wall0"]) == (1, 60.0, 0.0, 1060.0)
    assert a["wall_closed"] == 1075.0 and a["source_video"] == "vidA" and a["run"] == 0
    assert idx.data["t_end"] == 121.5 and idx.data["wall_end"] == 1121.5 and idx.resume_wall() == 1121.5
    assert idx.data["video"] == "vidA" and idx.data["next_seq"] == 2


def test_a_restart_records_the_real_gap_and_keeps_wall0_consistent(tmp_path):
    idx = new_index(tmp_path)
    r0 = idx.begin_run(anchor_wall=1000.0, wall_source="pdt", video="vidA")
    idx.add_chunk(r0, "r000_00000.ts", 0.0, 60.0, 1070.0)
    idx.end_run(r0, "input ended")
    r1 = idx.begin_run(anchor_wall=1100.0, wall_source="pdt", video="vidB")    # 40 s of stream missed
    assert r1["run"] == 1 and r1["t_base"] == 100.0 and r1["gap_before_s"] == 40.0
    c = idx.add_chunk(r1, "r001_00000.ts", 0.0, 60.0, 1170.0)
    assert (c["t0"], c["gap_before_s"], c["source_video"], c["run"]) == (100.0, 40.0, "vidB", 1)
    assert c["wall0"] == idx.data["release_timestamp"] + c["t0"]                 # release_ts + t stays the wall time
    assert idx.data["video"] == "vidA"                                         # the index's first video


def test_a_seamless_resume_has_zero_gap(tmp_path):
    idx = new_index(tmp_path)
    r0 = idx.begin_run(anchor_wall=1000.0, wall_source="pdt", video="v")
    idx.add_chunk(r0, "r000_00000.ts", 0.0, 60.0, 1070.0)
    r1 = idx.begin_run(anchor_wall=1060.0, wall_source="pdt", video="v")
    c = idx.add_chunk(r1, "r001_00000.ts", 0.0, 60.0, 1130.0)
    assert r1["gap_before_s"] == 0.0 and c["gap_before_s"] == 0.0 and c["t0"] == 60.0


def test_an_overlapping_restart_never_moves_time_backwards(tmp_path):
    idx = new_index(tmp_path)
    r0 = idx.begin_run(anchor_wall=1000.0, wall_source="pdt", video="v")
    idx.add_chunk(r0, "r000_00000.ts", 0.0, 60.0, 1070.0)
    r1 = idx.begin_run(anchor_wall=1055.0, wall_source="arrival", video="v")   # 5 s before the recorded end
    assert r1["t_base"] == 60.0 and r1["gap_before_s"] == 0.0 and r1["overlap_s"] == 5.0
    assert r1["wall_offset"] == 995.0


def test_a_hole_inside_a_run_shows_as_a_gap(tmp_path):
    idx = new_index(tmp_path)
    r = idx.begin_run(anchor_wall=1000.0, wall_source="pdt", video="v")
    idx.add_chunk(r, "r000_00000.ts", 0.0, 60.318333, 1070.0)
    c = idx.add_chunk(r, "r000_00001.ts", 60.318667, 120.0, 1130.0)   # ffmpeg's list: 0.3 ms apart = continuous
    assert c["gap_before_s"] == 0.0 and r["holes"] == 0
    c = idx.add_chunk(r, "r000_00002.ts", 135.0, 195.0, 1205.0)       # platform failed to serve 15 s
    assert c["gap_before_s"] == 15.0 and r["holes"] == 1


def test_reopening_the_index_continues_seq_and_runs(tmp_path):
    idx = new_index(tmp_path)
    r = idx.begin_run(anchor_wall=1000.0, wall_source="pdt", video="v")
    idx.add_chunk(r, "r000_00000.ts", 0.0, 60.0, 1070.0)
    idx.end_run(r, "stopped")
    idx.save()
    again = LiveIndex.open(tmp_path, "test")
    r1 = again.begin_run(anchor_wall=2000.0, wall_source="pdt", video="v")
    c = again.add_chunk(r1, "r001_00000.ts", 0.0, 60.0, 2070.0)
    assert r1["run"] == 1 and c["seq"] == 1 and c["t0"] == 1000.0 and c["gap_before_s"] == 940.0


def test_salvage_lists_what_a_killed_recorder_left_and_closes_the_run(tmp_path):
    idx = new_index(tmp_path)
    r = idx.begin_run(anchor_wall=1000.0, wall_source="pdt", video="v")
    for k in range(3):
        touch(tmp_path / record.chunk_name(0, k))
    write_list(tmp_path / "r000.csv", [(record.chunk_name(0, k), 60.0 * k, 60.0 * k + 60) for k in range(3)])
    r["csv"] = "r000.csv"
    idx.add_chunk(r, record.chunk_name(0, 0), 0.0, 60.0, 1070.0)        # listed before the kill
    idx.save()
    again = LiveIndex.open(tmp_path, "test")
    (run,) = again.open_runs()
    got = again.salvage(run)
    assert [c["seq"] for c in got] == [1, 2] and all(c["salvaged"] for c in got)
    assert [c["t0"] for c in again.chunks] == [0.0, 60.0, 120.0]
    mtime = (tmp_path / record.chunk_name(0, 2)).stat().st_mtime
    assert abs(got[-1]["wall_closed"] - mtime) < 0.01
    assert again.open_runs() == [] and "salvaged" in run["end_reason"]


# ---------------------------------------------------------------------------------------------------------------
# pruning
# ---------------------------------------------------------------------------------------------------------------

def test_prune_deletes_only_chunks_older_than_keep_hours(tmp_path):
    now = time.time()
    idx = new_index(tmp_path, keep_hours=28.0)
    r = idx.begin_run(anchor_wall=now - 40 * 3600, wall_source="pdt", video="v")
    ages_h = [30.0, 28.5, 27.9, 1.0]                                    # wall_closed this many hours ago
    for k, age in enumerate(ages_h):
        touch(tmp_path / record.chunk_name(0, k), age * 3600)
        idx.add_chunk(r, record.chunk_name(0, k), 60.0 * k, 60.0 * k + 60, now - age * 3600)
    touch(tmp_path / "r009_00000.ts", 29 * 3600)                        # stray from a crashed run: old
    touch(tmp_path / "r009_00001.ts", 10)                               # stray, young (maybe being written)
    removed = idx.prune(now)
    assert sorted(removed) == sorted([record.chunk_name(0, 0), record.chunk_name(0, 1), "r009_00000.ts"])
    assert [c["seq"] for c in idx.chunks] == [2, 3] and idx.data["pruned_through_seq"] == 1
    assert (tmp_path / record.chunk_name(0, 2)).exists() and (tmp_path / "r009_00001.ts").exists()
    assert not (tmp_path / record.chunk_name(0, 0)).exists()
    idx.save()
    assert [c["seq"] for c in json.loads(idx.path.read_text(encoding="utf-8"))["chunks"]] == [2, 3]


def test_prune_keeps_everything_younger_than_keep_hours(tmp_path):
    now = time.time()
    idx = new_index(tmp_path, keep_hours=28.0)
    r = idx.begin_run(anchor_wall=now - 3600, wall_source="pdt", video="v")
    for k in range(5):
        touch(tmp_path / record.chunk_name(0, k), 27.99 * 3600)
        idx.add_chunk(r, record.chunk_name(0, k), 60.0 * k, 60.0 * k + 60, now - 27.99 * 3600)
    assert idx.prune(now) == [] and len(idx.chunks) == 5


# ---------------------------------------------------------------------------------------------------------------
# HLS playlists
# ---------------------------------------------------------------------------------------------------------------

YT = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:5
#EXT-X-MEDIA-SEQUENCE:100
#EXT-X-PROGRAM-DATE-TIME:2026-11-04T12:00:00.000+00:00
#EXTINF:5.0,
https://rr1.example/videoplayback/itag/312/sq/100/file/seg.ts
#EXTINF:5.0,
https://rr1.example/videoplayback/itag/312/sq/101/file/seg.ts
#EXTINF:5.0,
https://rr1.example/videoplayback/itag/312/sq/102/file/seg.ts
#EXTINF:5.0,
https://rr1.example/videoplayback/itag/312/sq/103/file/seg.ts
"""

TWITCH = """#EXTM3U
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:7
#EXT-X-PROGRAM-DATE-TIME:2026-11-04T12:00:00.000Z
#EXTINF:2.000,live
seg7.ts
#EXT-X-PROGRAM-DATE-TIME:2026-11-04T12:00:02.000Z
#EXTINF:2.000,live
seg8.ts
"""

T0 = 1793793600.0   # 2026-11-04T12:00:00Z


def test_parse_playlist_carries_one_program_date_time_forward():
    pl = record.parse_playlist(YT, "https://manifest.example/index.m3u8")
    assert pl["media_seq"] == 100 and pl["target"] == 5.0 and not pl["ended"]
    assert [s["seq"] for s in pl["segments"]] == [100, 101, 102, 103]
    assert [s["pdt"] - T0 for s in pl["segments"]] == [0.0, 5.0, 10.0, 15.0]
    assert record.has_pdt(pl)


def test_parse_playlist_per_segment_dates_and_relative_uris():
    pl = record.parse_playlist(TWITCH, "https://video.example/v1/playlist/abc.m3u8")
    assert [s["uri"] for s in pl["segments"]] == ["https://video.example/v1/playlist/seg7.ts",
                                                  "https://video.example/v1/playlist/seg8.ts"]
    assert [s["pdt"] - T0 for s in pl["segments"]] == [0.0, 2.0]


def test_pick_start_resumes_at_the_first_segment_after_the_recording():
    pl = record.parse_playlist(YT, "")
    assert record.pick_start(pl, None) == -3                     # fresh: ffmpeg's default near the live edge
    assert record.pick_start(pl, T0 + 5.0) == 1                  # recording ended exactly at segment 101
    assert record.pick_start(pl, T0 + 6.0) == 2                  # ended inside 101: skip the rest of it
    assert record.pick_start(pl, T0 - 300.0) == 0                # outage longer than the window: oldest kept
    assert record.pick_start(pl, T0 + 20.0) is None              # nothing newer yet: wait
    assert record.pick_start(pl, None, rewind_s=10.0) == 2
    assert record.pick_start(pl, T0 - 300.0, max_resume_s=12.0) == 2   # no more than 12 s back from the edge


def test_match_segment_by_url_or_youtube_sequence_number():
    pl = record.parse_playlist(YT, "")
    assert record.match_segment("https://rr1.example/videoplayback/itag/312/sq/102/file/seg.ts", pl)["seq"] == 102
    other_host = "https://rr5.example/videoplayback/itag/312/sq/101/x/y"
    assert record.match_segment(other_host, pl)["seq"] == 101
    assert record.match_segment("https://rr5.example/videoplayback/itag/234/sq/101/x", pl) is None   # audio
    assert record.match_segment("https://rr1.example/other.ts", pl) is None


# ---------------------------------------------------------------------------------------------------------------
# command lines and names
# ---------------------------------------------------------------------------------------------------------------

def src(audio: bool) -> record.Source:
    return record.Source(video="v", title="t", webpage_url="w", extractor="x", video_release_ts=None,
                         video_url="https://h/v.m3u8", audio_url="https://h/a.m3u8" if audio else None,
                         headers={"User-Agent": "UA", "Accept": "*/*"})


def test_fetch_cmd_maps_separate_or_muxed_audio_and_copies():
    two = record.fetch_cmd("ffmpeg", src(True), 4, 4)
    assert two.count("-i") == 2 and two.count("-live_start_index") == 2
    assert two[two.index("-map") + 1] == "0:v:0" and "1:a:0" in two and "-copyts" in two
    one = record.fetch_cmd("ffmpeg", src(False), None, None, seg_retry=False)
    assert one.count("-i") == 1 and "0:a:0?" in one and "-live_start_index" not in one
    assert "-seg_max_retry" not in one and one[one.index("-user_agent") + 1] == "UA"
    assert one[-3:] == ["-f", "mpegts", "pipe:1"] and one[one.index("-c") + 1] == "copy"


def test_segment_cmd_cuts_by_copy_and_offsets_file_timestamps(tmp_path):
    cmd = record.segment_cmd("ffmpeg", tmp_path, 3, 1234.5, 60)
    assert cmd[cmd.index("-initial_offset") + 1] == "1234.500000"
    assert cmd[cmd.index("-segment_time") + 1] == "60" and cmd[cmd.index("-segment_format") + 1] == "mpegts"
    assert cmd[cmd.index("-segment_list") + 1].endswith("r003.csv") and cmd[-1].endswith("r003_%05d.ts")
    assert "-initial_offset" not in record.segment_cmd("ffmpeg", tmp_path, 3, None, 60)


def test_default_tag():
    assert record.default_tag("https://www.youtube.com/@SomeChannel/live") == "SomeChannel"
    assert record.default_tag("https://www.youtube.com/channel/UC123/live") == "UC123"
    assert record.default_tag("https://www.youtube.com/watch?v=abc_DEF-12") == "abc_DEF-12"
    assert record.default_tag("twitch.tv/firstinspires") == "twitch_firstinspires"


# ---------------------------------------------------------------------------------------------------------------
# several links: the best one that works (MadTown 2026: ten hours of a glitchy Twitch stream while a good YouTube
# stream ran; an official YouTube video that said it was live and sent nothing, 18 one-minute runs)
# ---------------------------------------------------------------------------------------------------------------

YT_LINK, TW_LINK = "https://www.youtube.com/watch?v=aaaaaaaaaaa", "https://www.twitch.tv/madtownthrowdown"


def fake_src(name, video=None):
    return types.SimpleNamespace(name=name, video=video or name, expire=None, video_url=f"https://h/{name}.m3u8",
                                 headers={})


def links_recorder(tmp_path, live, flow=lambda src: True, **kw):
    """A recorder of YT_LINK with TW_LINK as its other link; `live` is the set of links that resolve."""
    tried = []

    def resolver(url, _h):
        tried.append(url)
        if url not in live:
            raise record.NotLive(f"{url}: not live")
        return fake_src(url)
    rec = record.Recorder(YT_LINK, tmp_path, "t", resolver=resolver, also=[TW_LINK], flow=flow, **kw)
    rec.tried = tried
    return rec


def look_now(rec):
    rec._next_look = 0.0
    rec._look_due()
    if rec._looker is not None:
        rec._looker.join(timeout=10)


def test_links_json_is_used_only_when_written_for_this_link(tmp_path):
    rec = links_recorder(tmp_path, {TW_LINK})
    assert rec._links() == [YT_LINK, TW_LINK]
    found = "https://www.youtube.com/watch?v=CI6Dm6TSHzs"
    record.write_json_atomic(tmp_path / record.LINKS_NAME, {"for": "another link", "links": [found]})
    assert rec._links() == [YT_LINK, TW_LINK]
    record.write_json_atomic(tmp_path / record.LINKS_NAME, {"for": YT_LINK, "links": [found, YT_LINK, TW_LINK, found]})
    assert rec._links() == [found, YT_LINK, TW_LINK]
    record.write_json_atomic(tmp_path / record.LINKS_NAME, {"for": YT_LINK, "links": [found]})
    assert rec._links() == [found, YT_LINK, TW_LINK]              # the recorder's own links are never dropped
    (tmp_path / record.LINKS_NAME).write_text("{ damaged", encoding="utf-8")
    assert rec._links() == [YT_LINK, TW_LINK]


def test_a_look_takes_the_better_link_back_once_it_delivers(tmp_path):
    live = {TW_LINK}
    delivering = []
    rec = links_recorder(tmp_path, live, flow=lambda src: src.name in delivering)
    src, why = rec._resolve_live()
    assert src.name == TW_LINK and rec.cur_url == TW_LINK and why is None
    look_now(rec)
    assert rec._found is None                                     # the YouTube link is not live
    live.add(YT_LINK)
    look_now(rec)
    assert rec._found is None and rec._cooling(YT_LINK)           # live, but it sends nothing: left alone for a while
    del rec.tried[:]
    rec._looker = None
    look_now(rec)
    assert rec.tried == [] and rec._looker is None                # no look at a link that just failed
    rec._cool.clear()
    delivering.append(YT_LINK)
    look_now(rec)
    assert rec._found[0] == YT_LINK
    del rec.tried[:]
    src, why = rec._resolve_live()
    assert src.name == YT_LINK and rec.cur_url == YT_LINK and rec._chosen == YT_LINK and rec.tried == []
    rec._looker = None
    look_now(rec)
    assert rec._found is None and rec._looker is None             # the best link is recorded: nothing to look for


def test_a_better_link_to_the_video_being_recorded_changes_nothing(tmp_path):
    """A channel's live page and the video it shows may both be listed: one stream, no change."""
    page = "https://www.youtube.com/@FRC1671/live"
    rec = record.Recorder(page, tmp_path, "t", resolver=lambda url, _h: fake_src(url, video="CI6Dm6TSHzs"),
                          also=[YT_LINK], flow=lambda src: True)
    rec.cur_url, rec.cur_video = YT_LINK, "CI6Dm6TSHzs"
    look_now(rec)
    assert rec._found is None
    rec.cur_video = "another"
    look_now(rec)
    assert rec._found[0] == page


def test_no_look_while_a_match_is_being_played_or_when_switched_off(tmp_path):
    rec = links_recorder(tmp_path, {TW_LINK, YT_LINK})
    rec.cur_url = TW_LINK
    record.write_json_atomic(tmp_path / record.MATCH_ON_NAME, {"counting": True, "wall": time.time() - 30})
    rec._next_look = time.monotonic()
    rec._look_due()
    assert rec._looker is None                                    # a match timer is counting: wait for its end
    rec._next_look = time.monotonic() - record.HOLD_MAX_S - 1     # ... but never longer than HOLD_MAX_S
    rec._look_due()
    rec._looker.join(timeout=10)
    assert rec._found[0] == YT_LINK
    rec._found = rec._looker = None
    record.write_json_atomic(tmp_path / record.MATCH_ON_NAME, {"counting": True, "wall": time.time() - 600})
    look_now(rec)                                                 # a stale file (the follower stopped) holds nothing
    assert rec._found[0] == YT_LINK
    off = links_recorder(tmp_path, {TW_LINK, YT_LINK}, recheck_s=0)
    off.cur_url = TW_LINK
    look_now(off)
    assert off._looker is None and off._found is None


def test_a_link_that_failed_is_tried_after_the_others_for_a_doubling_time(tmp_path):
    rec = links_recorder(tmp_path, {TW_LINK, YT_LINK})
    assert rec._resolve_live()[0].name == YT_LINK
    rec._after_run(YT_LINK, 61.0, "no data for 60 s")             # stalled after a minute (MadTown, Sunday morning)
    assert rec._cooling(YT_LINK) and rec._cool[YT_LINK][1] == record.COOL_S
    del rec.tried[:]
    assert rec._resolve_live()[0].name == TW_LINK and rec.tried == [TW_LINK]
    rec._after_run(YT_LINK, 61.0, None)                           # again: twice as long
    assert rec._cool[YT_LINK][1] == 2 * record.COOL_S
    for _ in range(10):
        rec._bad(YT_LINK, "failed")
    assert rec._cool[YT_LINK][1] == record.COOL_MAX_S
    rec._after_run(TW_LINK, 61.0, "stopped")                      # ended on purpose: not the link's fault
    rec._after_run(TW_LINK, 61.0, "a better stream is live: x")
    rec._after_run(TW_LINK, 61.0, "stream URL about to expire; refreshing")
    assert not rec._cooling(TW_LINK)
    rec._after_run(YT_LINK, record.HEALTHY_S + 1, None)           # a long run clears it
    assert not rec._cooling(YT_LINK) and YT_LINK not in rec._cool
    rec._bad(TW_LINK, "failed")
    rec._bad(YT_LINK, "failed")
    del rec.tried[:]
    assert rec._resolve_live()[0].name == YT_LINK and rec.tried == [YT_LINK]   # all failed: the list's own order

    def off(url, _h):
        raise record.NotLive(f"{url}: off")
    rec.resolver = off
    assert rec._resolve_live() == (None, f"{YT_LINK}: off; {TW_LINK}: off")


def test_flowing_needs_a_new_segment(monkeypatch):
    def pl(last):
        return {"target": 0.05, "segments": [{"seq": last - 1}, {"seq": last}]}
    src = fake_src("x")
    served = iter([pl(10), pl(10), pl(11)])
    monkeypatch.setattr(record, "fetch_playlist", lambda url, headers=None: next(served))
    assert record.flowing(src, wait_s=5.0, sleep=lambda s: None)
    monkeypatch.setattr(record, "fetch_playlist", lambda url, headers=None: pl(10))
    assert not record.flowing(src, wait_s=0.2, sleep=lambda s: time.sleep(0.02))    # the same segment for 0.2 s
    monkeypatch.setattr(record, "fetch_playlist", lambda url, headers=None: None)
    assert not record.flowing(src, wait_s=0.2, sleep=lambda s: None)
    monkeypatch.setattr(record, "fetch_playlist", lambda url, headers=None: {"target": 2.0, "segments": []})
    assert not record.flowing(src, wait_s=0.2, sleep=lambda s: None)


def test_the_recorder_changes_to_the_better_link_while_recording(tmp_path, monkeypatch):
    """The whole loop with a stand-in for the ffmpeg run: Twitch is recorded while the YouTube link is not live;
    once it is, the Twitch run ends for it and the next run is the YouTube one, on a short stall time."""
    live = {TW_LINK}
    runs = []
    real = record.LiveRun

    class Run:
        def __init__(self, rec, src, attempt=0):
            self.rec, self.src, self.probation = rec, src, False
            self.started_mono, self.stop_reason, self.done, self.polls = time.monotonic(), None, False, 0
            self.p1 = self.p2 = types.SimpleNamespace(poll=lambda: None, returncode=0)
            self.log_path = tmp_path / "r.log"
            runs.append(self)

        def start(self):
            return "ok"

        def poll(self):
            self.polls += 1
            if self.src.name == TW_LINK and self.polls == 3:
                live.add(YT_LINK)                                 # the YouTube stream starts
                self.rec._next_look = 0.0                         # ... and a look is due
            if self.src.name == YT_LINK and self.polls == 3:
                self.rec.request_stop()

        def finished(self):
            return self.done

        def stalled(self):
            return False

        def stall_s(self):
            return real.stall_s(self)

        def stop(self, reason):
            self.stop_reason, self.done = self.stop_reason or reason, True

        def close(self):
            pass

    monkeypatch.setattr(record, "LiveRun", Run)
    rec = links_recorder(tmp_path, live, recheck_s=3600.0)
    rec._sleep = lambda s: time.sleep(0.01)
    assert rec.record() == 0
    assert [(r.src.name, r.stop_reason, r.probation) for r in runs] == [
        (TW_LINK, f"a better stream is live: {YT_LINK}", False), (YT_LINK, "stopped", True)]
    assert runs[1].stall_s() == record.PROBATION_STALL_S and runs[0].stall_s() == rec.stall_s
    assert not rec._cool                                          # neither link failed


# ---------------------------------------------------------------------------------------------------------------
# the real fetch -> relay -> segment path, on a local HLS playlist (127.0.0.1 only)
# ---------------------------------------------------------------------------------------------------------------

def _have_ffmpeg() -> bool:
    try:
        ffmpeg.find()
        return True
    except FileNotFoundError:
        return False


@pytest.mark.skipif(not _have_ffmpeg(), reason="needs ffmpeg")
def test_recorder_end_to_end_on_a_local_hls_playlist(tmp_path):
    import subprocess
    hls = tmp_path / "hls"
    hls.mkdir()
    subprocess.run([ffmpeg.find(), "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x90:r=30", "-f", "lavfi",
                    "-i", "sine=f=440:r=48000", "-t", "50", "-c:v", "libx264", "-preset", "ultrafast", "-g", "30",
                    "-c:a", "aac", "-f", "hls", "-hls_time", "2", "-hls_list_size", "0",
                    "-hls_flags", "program_date_time", "-hls_segment_filename", str(hls / "s%03d.ts"),
                    str(hls / "v.m3u8")], check=True)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(hls))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/v.m3u8"
    calls = []

    def resolver(_url, _h):
        calls.append(1)
        if len(calls) > 1:
            rec.request_stop()
            raise record.NotLive("ended")
        return record.Source(video="local", title="local test", webpage_url=url, extractor="test",
                             video_release_ts=None, video_url=url, audio_url=None, fmt={"video": "v", "audio": "a"})

    out = tmp_path / "live"
    rec = record.Recorder("local", out, "local", chunk_s=20, resolver=resolver)
    try:
        assert rec.record() == 0
    finally:
        server.shutdown()
    idx = json.loads((out / "index.json").read_text(encoding="utf-8"))
    ch = idx["chunks"]
    assert len(ch) == 3 and idx["status"] == "stopped" and idx["runs"][0]["wall_source"] == "pdt"
    assert idx["runs"][0]["end_reason"].startswith("input ended")
    assert all(abs(b["t0"] - a["t1"]) < 1e-6 for a, b in zip(ch, ch[1:]))
    assert abs(ch[-1]["t1"] - 50.0) < 0.1 and all((out / c["path"]).is_file() for c in ch)
    assert all(abs(c["wall0"] - (idx["release_timestamp"] + c["t0"])) < 0.01 for c in ch)
    starts = [record.read_segment_list(out / "r000.csv")[k][1] for k in range(3)]
    assert starts == [c["t0"] for c in ch]
