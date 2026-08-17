"""Re-extracting keyframes without destroying what the last run saw.

`analyze` used to skip extraction entirely whenever a clip already had
keyframes, which made `--keyframe-strategy` unreachable for anything in the
library and left the sampling fix with no way to reach the 101 clips it was
written for.

Overwriting was not the answer either. The scene extractor writes
`<video_id>_scene_%03d.jpg` with `-y` into the same directory every time, so a
second pass replaces the first run's images while every stored row still points
at those filenames. No file is deleted and every earlier description ends up
describing a picture that is no longer there -- the letter of "never auto-delete"
kept, the point of it lost.

So an extraction is a run: new frames go in `r<id>/`, the previous run is marked
superseded, and nothing is removed.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from reel_scout import config, db, ingest
from reel_scout.analyze import pipeline


def _conn(temp_db):
    c = sqlite3.connect(temp_db)
    c.row_factory = sqlite3.Row
    return c


def _video(conn, vid="v1"):
    return db.upsert_video(conn, platform="yt", platform_id=vid, url="u/" + vid,
                           title="t")


def _frames(n, base=0.0, path_prefix="/tmp/a"):
    return [{"frame_index": i, "timestamp_sec": base + i,
             "file_path": "%s_%d.jpg" % (path_prefix, i), "strategy": "scene"}
            for i in range(n)]


# --- the run itself ------------------------------------------------------------

def test_a_new_run_hides_the_old_frames_without_removing_them(temp_db):
    conn = _conn(temp_db)
    try:
        vid = _video(conn)
        r1 = db.begin_keyframe_run(conn, vid, "scene", 4)
        db.save_keyframes(conn, vid, _frames(4, path_prefix="/tmp/old"), run_id=r1)
        db.commit_keyframe_run(conn, r1, vid)

        r2 = db.begin_keyframe_run(conn, vid, "scene", 4)
        db.save_keyframes(conn, vid, _frames(4, base=10.0, path_prefix="/tmp/new"),
                          run_id=r2)
        db.commit_keyframe_run(conn, r2, vid)

        current = db.get_keyframes(conn, vid)
        assert len(current) == 4
        assert all("new" in r["file_path"] for r in current)

        everything = db.get_keyframes(conn, vid, include_superseded=True)
        assert len(everything) == 8, "the old run has to still be there to check"
    finally:
        conn.close()


def test_a_run_that_produced_nothing_does_not_retire_the_one_that_did(temp_db):
    # Superseding on `begin` would mean an extraction that dies halfway leaves
    # the video with no current run at all: old evidence retired before new
    # evidence exists, which is worse than either state on its own.
    conn = _conn(temp_db)
    try:
        vid = _video(conn)
        r1 = db.begin_keyframe_run(conn, vid, "scene", 4)
        db.save_keyframes(conn, vid, _frames(4, path_prefix="/tmp/old"), run_id=r1)
        db.commit_keyframe_run(conn, r1, vid)

        db.begin_keyframe_run(conn, vid, "interval", 8)  # started, never committed

        current = db.get_keyframes(conn, vid)
        assert len(current) == 4
        assert all("old" in r["file_path"] for r in current)
    finally:
        conn.close()


def test_descriptions_follow_the_current_run(temp_db):
    conn = _conn(temp_db)
    try:
        vid = _video(conn)
        r1 = db.begin_keyframe_run(conn, vid, "scene", 2)
        old_ids = db.save_keyframes(conn, vid, _frames(2, path_prefix="/tmp/old"),
                                    run_id=r1)
        db.commit_keyframe_run(conn, r1, vid)
        for kid in old_ids:
            db.save_vision_description(conn, kid, "old picture", "[]", "", "ollama", "m")

        r2 = db.begin_keyframe_run(conn, vid, "scene", 2)
        db.save_keyframes(conn, vid, _frames(2, base=9.0, path_prefix="/tmp/new"),
                          run_id=r2)
        db.commit_keyframe_run(conn, r2, vid)

        # The backfill gate must see zero described frames, or the new run's
        # frames are never sent to the VLM and the clip keeps the old readings.
        assert db.get_described_keyframe_ids(conn, vid) == set()
        rows = db.get_keyframes_with_descriptions(conn, vid)
        assert len(rows) == 2
        assert all(r["description"] is None for r in rows)
    finally:
        conn.close()


def test_a_frame_index_locator_never_resolves_to_a_superseded_frame(temp_db):
    """Why the frames could not simply be added alongside the old ones.

    `ingest.resolve_keyframe` matches a `frame_index` by returning the *first*
    row that carries it. With both runs visible, an agent that views the new
    frames through the `keyframes` tool and writes its findings back by index
    attaches them to the old frame -- and every later read shows the old picture
    with the new caption. `--mode agent` is the path SKILL.md recommends, so
    that is not a corner.
    """
    conn = _conn(temp_db)
    try:
        vid = _video(conn)
        r1 = db.begin_keyframe_run(conn, vid, "scene", 3)
        db.save_keyframes(conn, vid, _frames(3, path_prefix="/tmp/old"), run_id=r1)
        db.commit_keyframe_run(conn, r1, vid)
        r2 = db.begin_keyframe_run(conn, vid, "scene", 3)
        new_ids = db.save_keyframes(conn, vid, _frames(3, base=20.0,
                                                      path_prefix="/tmp/new"),
                                    run_id=r2)
        db.commit_keyframe_run(conn, r2, vid)

        rows = db.get_keyframes(conn, vid)
        assert ingest.resolve_keyframe(rows, {"frame_index": 1}) == new_ids[1]
    finally:
        conn.close()


# --- the migration -------------------------------------------------------------

def test_existing_frames_are_backfilled_into_one_run_per_video(temp_db):
    conn = _conn(temp_db)
    try:
        v1, v2 = _video(conn, "a"), _video(conn, "b")
        # Rows as they existed before runs: no run_id at all.
        for vid in (v1, v2):
            for i in range(3):
                conn.execute(
                    "INSERT INTO keyframes (video_id, frame_index, timestamp_sec, "
                    "file_path, strategy) VALUES (?,?,?,?,?)",
                    (vid, i, float(i), "/tmp/%s_%d.jpg" % (vid, i), "scene"))
        conn.execute("DELETE FROM keyframe_runs")
        conn.execute("UPDATE keyframes SET run_id = NULL")
        conn.execute("UPDATE schema_version SET version = 11")
        conn.commit()

        db.init_db(conn)

        assert conn.execute("SELECT COUNT(*) FROM keyframe_runs").fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM keyframes WHERE run_id IS NULL").fetchone()[0] == 0
        # selector_version 0 is how a re-run finds what still needs doing.
        assert conn.execute(
            "SELECT COUNT(*) FROM keyframe_runs WHERE selector_version = 0 "
            "AND superseded_at IS NULL").fetchone()[0] == 2
        assert len(db.get_keyframes(conn, v1)) == 3
    finally:
        conn.close()


def test_rows_written_without_a_run_are_still_readable(temp_db):
    # Any writer that predates runs, or does not know about them, must not have
    # its frames quietly filtered out of existence.
    conn = _conn(temp_db)
    try:
        vid = _video(conn)
        db.save_keyframes(conn, vid, _frames(2))  # no run_id
        assert len(db.get_keyframes(conn, vid)) == 2
    finally:
        conn.close()


# --- when analyze decides to re-extract ----------------------------------------

def _opts(**kw):
    return pipeline.PipelineOptions(**kw)


def test_analyze_re_extracts_when_the_request_actually_differs(temp_db):
    conn = _conn(temp_db)
    try:
        vid = _video(conn)
        r1 = db.begin_keyframe_run(conn, vid, "scene", 8)
        db.save_keyframes(conn, vid, _frames(3), run_id=r1)
        db.commit_keyframe_run(conn, r1, vid)

        assert pipeline._wants_new_sampling(conn, vid, _opts(keyframe_strategy="interval"))
        assert pipeline._wants_new_sampling(conn, vid, _opts(keyframe_max=40))
        assert pipeline._wants_new_sampling(conn, vid, _opts(force_keyframes=True))
    finally:
        conn.close()


def test_analyze_does_not_re_extract_when_nothing_was_asked_for(temp_db, monkeypatch):
    """The scoping that keeps 1300 unwanted VLM calls from firing by accident.

    `keyframe_strategy` defaults to None and resolves to `config.KEYFRAME_STRATEGY`,
    an environment variable. If the check compared resolved values, anyone who
    changed that variable -- or upgraded into a release with a different default
    -- would silently re-extract the entire library.
    """
    conn = _conn(temp_db)
    try:
        vid = _video(conn)
        r1 = db.begin_keyframe_run(conn, vid, "scene", 8)
        db.save_keyframes(conn, vid, _frames(3), run_id=r1)
        db.commit_keyframe_run(conn, r1, vid)

        monkeypatch.setattr(config, "KEYFRAME_STRATEGY", "interval")
        assert not pipeline._wants_new_sampling(conn, vid, _opts()), (
            "changing the environment default must not re-extract anything")

        # And asking for exactly what is already recorded is not a change either.
        assert not pipeline._wants_new_sampling(
            conn, vid, _opts(keyframe_strategy="scene", keyframe_max=8))
    finally:
        conn.close()


def test_a_video_with_no_run_yet_is_not_a_re_extraction(temp_db):
    conn = _conn(temp_db)
    try:
        vid = _video(conn)
        assert not pipeline._wants_new_sampling(conn, vid, _opts(keyframe_strategy="scene"))
    finally:
        conn.close()


# --- the reason runs need their own directory ----------------------------------

def _stub_ffmpeg(monkeypatch, payload):
    """An ffmpeg that writes `payload` to whatever -y target it is handed.

    Real bytes, real files: the whole point here is what happens on disk, and no
    amount of asserting on database rows can see an image being replaced under a
    row that still points at it.
    """
    from reel_scout.vision import keyframe as kf_mod

    class _Res:
        stderr = "\n".join("n:%d pts_time:%.1f" % (i, i * 2.0) for i in range(6))
        stdout = ""
        returncode = 0

    def _run(cmd, **kw):
        if "-y" in cmd:
            target = cmd[cmd.index("-y") + 1]
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as fh:
                fh.write(payload)
        return _Res()

    monkeypatch.setattr(kf_mod.subprocess, "run", _run)
    monkeypatch.setattr(kf_mod, "_get_duration", lambda p: 12.0)


def test_a_second_extraction_into_the_same_directory_destroys_the_first(
    tmp_path, monkeypatch
):
    """The failure that makes run-scoped paths mandatory, demonstrated.

    `_extract_scene` writes `<video_id>_scene_%03d.jpg` and passes `-y`. Point a
    second extraction at the same directory and run 1's images are gone --
    while its rows, and every description written against them, still name those
    exact files. Nothing was deleted; the evidence was replaced in place, which
    is worse, because nothing about the database says so.
    """
    from reel_scout.vision.keyframe import extract_keyframes

    shared = str(tmp_path / "v1")

    _stub_ffmpeg(monkeypatch, b"RUN-ONE")
    first = extract_keyframes("/tmp/v.mp4", shared, "v1", strategy="scene", max_frames=3)
    assert first, "the stub must have produced frames for this to mean anything"
    kept = first[0].file_path

    _stub_ffmpeg(monkeypatch, b"RUN-TWO")
    extract_keyframes("/tmp/v.mp4", shared, "v1", strategy="scene", max_frames=3)

    assert open(kept, "rb").read() == b"RUN-TWO", (
        "if this ever fails, the extractor stopped reusing filenames and the "
        "run-scoped directory below is no longer load-bearing")


def test_giving_each_run_its_own_directory_keeps_both(tmp_path, monkeypatch):
    from reel_scout.vision.keyframe import extract_keyframes

    base = str(tmp_path / "v1")

    _stub_ffmpeg(monkeypatch, b"RUN-ONE")
    first = extract_keyframes("/tmp/v.mp4", base, "v1", strategy="scene", max_frames=3)
    kept = first[0].file_path

    _stub_ffmpeg(monkeypatch, b"RUN-TWO")
    second = extract_keyframes("/tmp/v.mp4", os.path.join(base, "r2"), "v1",
                               strategy="scene", max_frames=3)

    assert open(kept, "rb").read() == b"RUN-ONE", "run 1's images must survive"
    assert open(second[0].file_path, "rb").read() == b"RUN-TWO"
    assert os.path.dirname(second[0].file_path) != os.path.dirname(kept)


# --- what analyze actually does with a run -------------------------------------
#
# The tests above hold the database layer and the extractor to their contracts.
# These two hold the wiring between them, which is where both guards live and
# where neither was covered: a mutation could remove the run-scoped directory,
# or supersede a good run on a failed extraction, and everything still passed.


class _Frame:
    def __init__(self, i, ts, path):
        self.frame_index, self.timestamp_sec = i, ts
        self.file_path, self.strategy = path, "scene"


@pytest.fixture
def local_clip(temp_db, tmp_path, monkeypatch):
    """A registered local video, with everything after keyframes switched off."""
    monkeypatch.setattr(config, "KEYFRAMES_DIR", str(tmp_path / "kf"))
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"not really media")
    monkeypatch.setattr(pipeline, "_probe_duration", lambda p: 12.0)
    # Everything downstream of keyframes wants a model or an audio track. This
    # test is about which directory the frames land in and which run survives,
    # so the rest is stubbed rather than skipped -- skipping would change the
    # code path being exercised.
    monkeypatch.setattr(pipeline, "merge_analysis", lambda conn, vid: None)
    monkeypatch.setattr(pipeline, "measure_shots", lambda *a, **k: None, raising=False)
    return str(clip)


def _run_once(conn, clip, monkeypatch, frames, seen_dirs):
    def _extract(video_path, output_dir, video_id, **kw):
        seen_dirs.append(output_dir)
        return [_Frame(i, float(i), os.path.join(output_dir, "f%d.jpg" % i))
                for i in range(frames)]

    monkeypatch.setattr(pipeline, "extract_keyframes", _extract)
    pipeline._process_single(
        conn, clip,
        pipeline.PipelineOptions(skip_vision=True, skip_transcribe=True,
                                 force_keyframes=True))


def test_a_re_extraction_writes_into_its_own_directory(
    temp_db, tmp_path, monkeypatch, local_clip
):
    conn = _conn(temp_db)
    seen = []
    try:
        _run_once(conn, local_clip, monkeypatch, 3, seen)
        _run_once(conn, local_clip, monkeypatch, 3, seen)
    finally:
        conn.close()

    assert len(seen) == 2, "the second analyze must actually re-extract"
    assert seen[0] != seen[1], (
        "sharing a directory means run 2 overwrites run 1's images while run 1's "
        "rows still point at them -- nothing deleted, all the evidence replaced")
    assert os.path.basename(seen[1]).startswith("r")


def test_an_extraction_that_found_nothing_keeps_the_previous_run(
    temp_db, tmp_path, monkeypatch, local_clip
):
    conn = _conn(temp_db)
    seen = []
    try:
        _run_once(conn, local_clip, monkeypatch, 3, seen)
        before = db.get_keyframes(conn, db.get_video_by_url(conn, local_clip)["id"])
        assert len(before) == 3

        _run_once(conn, local_clip, monkeypatch, 0, seen)  # extraction yields nothing

        vid = db.get_video_by_url(conn, local_clip)["id"]
        after = db.get_keyframes(conn, vid)
        assert [r["id"] for r in after] == [r["id"] for r in before], (
            "a run that produced no frames must not retire the run that did -- "
            "that leaves the clip with no current frames at all")
    finally:
        conn.close()
