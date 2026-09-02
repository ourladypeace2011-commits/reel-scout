"""Shot-size labelling (roadmap §6B, the producer half)."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from unittest.mock import patch

from reel_scout import db, label_shots
from reel_scout.shot_size import UNKNOWN
from reel_scout.shots import Shot


def _temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn, path


def _seed(conn, frames=((11, 2.0), (22, 8.0))):
    vid = db.upsert_video(conn, "youtube", "p1", "https://y/p1", title="Clip")
    db.save_shots(conn, vid, [Shot(0, 0.0, 5.0, 5.0), Shot(1, 5.0, 12.0, 7.0)])
    run = None
    for fid, t in frames:
        conn.execute(
            "INSERT INTO keyframes (id, video_id, frame_index, timestamp_sec, "
            "file_path, strategy) VALUES (?,?,?,?,?,?)",
            (fid, vid, fid, t, "/tmp/frame_%d.jpg" % fid, "scene"))
    conn.commit()
    return vid


def test_representative_frames_one_per_shot_that_has_one():
    conn, path = _temp_db()
    try:
        vid = _seed(conn)
        got = label_shots.representative_frames(conn, vid)
        assert [s.index for s, _ in got] == [0, 1]
        assert [f["id"] for _, f in got] == [11, 22]
    finally:
        conn.close(); os.unlink(path)


def test_supplied_labels_skip_the_model_entirely():
    conn, path = _temp_db()
    try:
        vid = _seed(conn)
        with patch.object(label_shots, "classify_with_ollama") as ask:
            tally = label_shots.label_video(
                conn, vid, "m", supplied={"11": "CU", "22": "LS"})
        ask.assert_not_called()
        assert tally["labelled"] == 2
        rows = db.get_shot_labels(conn, vid, kind="shot_size")
        assert {r["value"] for r in rows} == {"CU", "LS"}
        # Supplied labels must not be stamped with a model they did not come from.
        assert all(r["source"] == "supplied" and r["model"] is None for r in rows)
    finally:
        conn.close(); os.unlink(path)


def test_a_supplied_value_that_is_not_a_code_is_skipped_not_stored():
    conn, path = _temp_db()
    try:
        vid = _seed(conn)
        tally = label_shots.label_video(
            conn, vid, "m", supplied={"11": "probably a medium shot"})
        assert tally["skipped"] == 2 and tally["labelled"] == 0
        assert db.get_shot_labels(conn, vid, kind="shot_size") == []
    finally:
        conn.close(); os.unlink(path)


def test_unreadable_frames_are_missing_not_refused():
    # The first run of this command reported "39 refused" when every frame file
    # was simply unreadable. Blaming the model for a path problem is the exact
    # conflation this library keeps paying for.
    conn, path = _temp_db()
    try:
        vid = _seed(conn)
        with patch.object(label_shots, "classify_with_ollama") as ask:
            tally = label_shots.label_video(conn, vid, "qwen")
        ask.assert_not_called()
        assert tally["missing"] == 2 and tally["refused"] == 0
    finally:
        conn.close(); os.unlink(path)


def test_a_model_that_will_not_answer_a_code_is_refused_and_stores_nothing():
    conn, path = _temp_db()
    try:
        vid = _seed(conn)
        with patch("os.path.exists", return_value=True), \
             patch.object(label_shots, "classify_with_ollama", return_value=None):
            tally = label_shots.label_video(conn, vid, "qwen")
        assert tally["refused"] == 2 and tally["labelled"] == 0
        assert db.get_shot_labels(conn, vid, kind="shot_size") == []
    finally:
        conn.close(); os.unlink(path)


def test_unknown_is_stored_rather_than_dropped():
    # "asked, no answer" and "never asked" are different states, and only one of
    # them is worth re-running.
    conn, path = _temp_db()
    try:
        vid = _seed(conn)
        with patch("os.path.exists", return_value=True), \
             patch.object(label_shots, "classify_with_ollama", return_value=UNKNOWN):
            tally = label_shots.label_video(conn, vid, "qwen")
        assert tally["unknown"] == 2 and tally["labelled"] == 0
        rows = db.get_shot_labels(conn, vid, kind="shot_size")
        assert [r["value"] for r in rows] == [UNKNOWN, UNKNOWN]
    finally:
        conn.close(); os.unlink(path)


def test_every_stored_row_carries_the_model_that_produced_it():
    conn, path = _temp_db()
    try:
        vid = _seed(conn)
        with patch("os.path.exists", return_value=True), \
             patch.object(label_shots, "classify_with_ollama", return_value="MS"):
            label_shots.label_video(conn, vid, "qwen2.5vl:7b")
        rows = db.get_shot_labels(conn, vid, kind="shot_size")
        assert all(r["model"] == "qwen2.5vl:7b" and r["source"] == "vlm" for r in rows)
    finally:
        conn.close(); os.unlink(path)


def test_two_models_coexist_instead_of_overwriting():
    # A craft score taught this repo that a model-produced value without its
    # origin is unusable the moment a second model touches the library.
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        db.save_shot_label(conn, vid, 2.0, "shot_size", "CU", "vlm", "model-a")
        db.save_shot_label(conn, vid, 2.0, "shot_size", "MS", "supplied", None)
        rows = db.get_shot_labels(conn, vid, kind="shot_size")
        assert len(rows) == 2
        assert {r["value"] for r in rows} == {"CU", "MS"}
    finally:
        conn.close(); os.unlink(path)


def test_re_running_the_same_source_updates_rather_than_duplicates():
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        db.save_shot_label(conn, vid, 2.0, "shot_size", "CU", "vlm", "m")
        db.save_shot_label(conn, vid, 2.0, "shot_size", "ECU", "vlm", "m")
        rows = db.get_shot_labels(conn, vid, kind="shot_size")
        assert len(rows) == 1 and rows[0]["value"] == "ECU"
    finally:
        conn.close(); os.unlink(path)


def test_labels_survive_the_shot_table_being_replaced():
    # The whole reason they hang off a timestamp instead of a shots.id.
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        db.save_shot_label(conn, vid, 2.0, "shot_size", "CU", "vlm", "m")
        db.save_shots(conn, vid, [Shot(0, 0.0, 12.0, 12.0)])   # re-analyze
        rows = db.get_shot_labels(conn, vid, kind="shot_size")
        assert len(rows) == 1 and rows[0]["value"] == "CU"
    finally:
        conn.close(); os.unlink(path)


def test_migration_creates_shot_labels_on_a_v14_database():
    # Built from a bare file rather than by renaming the table aside: in SQLite
    # a rename carries the table's indexes with it, so `CREATE UNIQUE INDEX IF
    # NOT EXISTS` would then skip on a name that still exists elsewhere and the
    # upsert would fail at runtime. Testing the migration directly avoids
    # asserting against an artefact of the test's own trick.
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY);"
            "INSERT INTO schema_version (version) VALUES (14);"
            "CREATE TABLE videos (id TEXT PRIMARY KEY);"
        )
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='shot_labels'"
        ).fetchone()[0] == 0
        db._migrate_v14_to_v15(conn)
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 15
        conn.execute("INSERT INTO videos (id) VALUES ('v')")
        db.save_shot_label(conn, "v", 2.0, "shot_size", "CU", "vlm", "m")
        db.save_shot_label(conn, "v", 2.0, "shot_size", "ECU", "vlm", "m")
        rows = db.get_shot_labels(conn, "v")
        assert len(rows) == 1 and rows[0]["value"] == "ECU", "upsert needs the unique index"
    finally:
        conn.close()
        os.unlink(path)


def test_migration_is_idempotent():
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        db.save_shot_label(conn, vid, 2.0, "shot_size", "CU", "vlm", "m")
        db._migrate_v14_to_v15(conn)
        assert len(db.get_shot_labels(conn, vid)) == 1
    finally:
        conn.close(); os.unlink(path)


# --- interviews are skipped by default -------------------------------------

def _analysis(conn, vid, style_format):
    db.save_analysis(conn, vid, summary="", topics_json="[]",
                     hooks_json="{}", style_json=json.dumps({"format": style_format}),
                     engagement_signals_json="{}",
                     full_json=json.dumps({"style": {"format": style_format}}))


def test_an_interview_is_skipped_and_says_so():
    # Every shot is the same locked-off framing; at ~3.7s a frame that is half
    # an hour spent confirming one answer.
    conn, path = _temp_db()
    try:
        vid = _seed(conn)
        _analysis(conn, vid, "talking_head")
        with patch.object(label_shots, "classify_with_ollama") as ask:
            tally = label_shots.label_video(conn, vid, "m")
        ask.assert_not_called()
        assert tally["skipped_format"] == "talking_head"
        assert tally["labelled"] == 0
        assert db.get_shot_labels(conn, vid, kind="shot_size") == []
    finally:
        conn.close(); os.unlink(path)


def test_force_labels_an_interview_anyway():
    # "mostly uniform" is not "uniform" — the exceptions have to stay reachable.
    conn, path = _temp_db()
    try:
        vid = _seed(conn)
        _analysis(conn, vid, "talking_head")
        with patch("os.path.exists", return_value=True), \
             patch.object(label_shots, "classify_with_ollama", return_value="MCU"):
            tally = label_shots.label_video(conn, vid, "m", force=True)
        assert tally["skipped_format"] is None and tally["labelled"] == 2
    finally:
        conn.close(); os.unlink(path)


def test_other_formats_are_not_skipped():
    conn, path = _temp_db()
    try:
        vid = _seed(conn)
        _analysis(conn, vid, "montage")
        assert label_shots.skip_reason(conn, vid) is None
    finally:
        conn.close(); os.unlink(path)


def test_a_clip_with_no_analysis_is_not_skipped():
    # "we never classified it" is not evidence that it is an interview, and
    # refusing on missing data would quietly shrink the run.
    conn, path = _temp_db()
    try:
        vid = _seed(conn)
        assert label_shots.skip_reason(conn, vid) is None
    finally:
        conn.close(); os.unlink(path)
