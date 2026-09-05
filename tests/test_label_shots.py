"""Shot-size labelling (roadmap §6B, the producer half)."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from unittest.mock import mock_open, patch

import pytest

from reel_scout import config, db, label_shots
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
            tally = label_shots.label_video(conn, vid, "qwen", gate=False)
        ask.assert_not_called()
        assert tally["missing"] == 2 and tally["refused"] == 0
    finally:
        conn.close(); os.unlink(path)


def test_a_model_that_will_not_answer_a_code_is_refused_and_stores_nothing():
    conn, path = _temp_db()
    try:
        vid = _seed(conn)
        with patch("os.path.exists", return_value=True), \
             patch.object(label_shots, "classify_with_ollama",
                          return_value=(None, label_shots.REFUSAL_UNPARSEABLE)):
            tally = label_shots.label_video(conn, vid, "qwen", gate=False)
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
             patch.object(label_shots, "classify_with_ollama", return_value=(UNKNOWN, None)):
            tally = label_shots.label_video(conn, vid, "qwen", gate=False)
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
             patch.object(label_shots, "classify_with_ollama", return_value=("MS", None)):
            label_shots.label_video(conn, vid, "qwen2.5vl:7b", gate=False)
        rows = db.get_shot_labels(conn, vid, kind="shot_size")
        assert all(r["model"] == "qwen2.5vl:7b" and r["source"] == "vlm" for r in rows)
    finally:
        conn.close(); os.unlink(path)


def test_a_model_reading_and_a_persons_correction_coexist():
    # Renamed 2026-09-05. It was `test_two_models_coexist_instead_of_overwriting`
    # and it never tested two models: both rows below are different *sources*.
    # `model` is not in the uniqueness key, so a second model under the same
    # source replaces the first -- which is correct (one asker changing its
    # mind is not two claims) but is the opposite of what the name promised.
    # See `test_a_second_model_replaces_the_first_it_does_not_sit_beside_it`.
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


def test_a_second_model_replaces_the_first_it_does_not_sit_beside_it():
    # The behaviour the renamed test above claimed to cover, now actually
    # covered -- and asserted as replacement, because that is what happens and
    # two docstrings said otherwise until 2026-09-05.
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        db.save_shot_label(conn, vid, 2.0, "shot_size", "CU", "vlm", "qwen2.5vl:7b")
        db.save_shot_label(conn, vid, 2.0, "shot_size", "MS", "vlm", "qwen3-vl:8b")
        rows = db.get_shot_labels(conn, vid, kind="shot_size")
        assert [(r["value"], r["model"]) for r in rows] == [("MS", "qwen3-vl:8b")]
    finally:
        conn.close(); os.unlink(path)


def test_a_label_whose_keyframe_is_gone_is_kept_but_not_counted():
    # R6, and the fix that was *not* made: deleting these contradicts the
    # decision that labels hang off timestamps so re-analysis cannot destroy
    # them (`test_a_refusal_leaves_labels_at_other_timestamps_alone` guards
    # it). Unreachable is not wrong -- so the row stays, and the readers stop
    # treating it as a live measurement.
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        db.save_shot_label(conn, vid, 2.0, "shot_size", "CU", "vlm", "m",
                           label_shots.prompt_fingerprint())
        # The orphan: a keyframe that used to be at 2.5 and is not any more.
        db.save_shot_label(conn, vid, 2.5, "shot_size", "ECU", "vlm", "m", "OLD")
        assert label_shots.orphaned_labels(conn, vid) == 1
        # Kept.
        assert len(db.get_shot_labels(conn, vid, kind="shot_size")) == 2
        # Not counted, and not prescribed a re-run that cannot reach it.
        assert label_shots.analysable(conn, vid) == (1, 1)
        assert label_shots.stale_videos(conn) == []
    finally:
        conn.close(); os.unlink(path)


def test_an_orphan_does_not_win_a_span_from_a_live_reading():
    # Sizes match a shot by span, not equality, so the earliest timestamp in
    # the span wins -- and after `--force-keyframes` that is often the orphan.
    from reel_scout import inspector
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        db.save_shot_label(conn, vid, 0.5, "shot_size", "ECU", "vlm", "m", "OLD")
        db.save_shot_label(conn, vid, 2.0, "shot_size", "CU", "vlm", "m",
                           label_shots.prompt_fingerprint())
        rows = inspector._shot_grammar(conn, vid)["rows"]
        assert rows[0]["size"] == "CU"
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
            tally = label_shots.label_video(conn, vid, "m", gate=False)
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
             patch.object(label_shots, "classify_with_ollama", return_value=("MCU", None)):
            tally = label_shots.label_video(conn, vid, "m", gate=False, force=True)
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


def test_a_relative_keyframe_path_is_resolved_not_read_raw(monkeypatch, tmp_path):
    # The first real run of `shot-size` reported "39 refused" — every frame file
    # was simply unreadable from that cwd, and the raw check could not tell.
    conn, path = _temp_db()
    root = str(tmp_path)
    try:
        kf_dir = os.path.join(root, "data", "keyframes")
        os.makedirs(kf_dir)
        open(os.path.join(kf_dir, "f11.jpg"), "wb").write(b"0")
        vid = db.upsert_video(conn, "youtube", "p9", "https://y/p9", title="C")
        db.save_shots(conn, vid, [Shot(0, 0.0, 5.0, 5.0)])
        conn.execute(
            "INSERT INTO keyframes (id, video_id, frame_index, timestamp_sec, "
            "file_path, strategy) VALUES (?,?,?,?,?,?)",
            (11, vid, 0, 2.0, "./data/keyframes/f11.jpg", "scene"))
        conn.commit()
        monkeypatch.setattr(config, "DATA_DIR", os.path.join(root, "data"))
        elsewhere = tmp_path / "elsewhere"; elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        assert not os.path.exists("./data/keyframes/f11.jpg")
        with patch.object(label_shots, "classify_with_ollama", return_value=("CU", None)) as ask:
            tally = label_shots.label_video(conn, vid, "m", gate=False)
        assert tally["missing"] == 0 and tally["labelled"] == 1
        # And the model must be handed something openable, not the stored string.
        assert os.path.isabs(ask.call_args[0][0])
    finally:
        conn.close(); os.unlink(path)

# --- the prompt is part of the provenance (schema v17) ---------------------

def test_the_prompt_defines_each_code_not_just_names_it():
    # "MLS = medium long shot" is the same words in a longer form, not a
    # definition. Measured on 24 frames: codes+names returned ECU ten times
    # where codes+definitions returned it once.
    from reel_scout.shot_size import classification_prompt, SHOT_SIZES
    p = classification_prompt()
    for code in SHOT_SIZES:
        assert code in p
    for phrase in ("head to chest", "waist up", "knees up",
                   "SUBJECT-TO-FRAME RATIO"):
        assert phrase in p, phrase


def test_the_fingerprint_follows_the_prompt_text():
    from reel_scout import shot_size
    before = shot_size.prompt_fingerprint()
    original = shot_size._DEFINITIONS["MS"]
    try:
        shot_size._DEFINITIONS["MS"] = "something else entirely"
        assert shot_size.prompt_fingerprint() != before, (
            "a changed prompt must change the fingerprint, or the column "
            "records nothing")
    finally:
        shot_size._DEFINITIONS["MS"] = original
    assert shot_size.prompt_fingerprint() == before


def test_a_vlm_label_records_the_prompt_that_produced_it():
    from reel_scout.shot_size import prompt_fingerprint
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        with patch("os.path.exists", return_value=True), \
             patch.object(label_shots, "classify_with_ollama", return_value=("MCU", None)):
            label_shots.label_video(conn, vid, "m", gate=False)
        row = db.get_shot_labels(conn, vid, kind="shot_size")[0]
        # `gate=False` above, so the ungated fingerprint -- stamping the gated
        # one would claim the gate ran on a run that never asked it.
        assert row["prompt_hash"] == prompt_fingerprint(gate=False)
        assert prompt_fingerprint(True) != prompt_fingerprint(False)
    finally:
        conn.close(); os.unlink(path)


def test_a_supplied_label_carries_no_prompt_fingerprint():
    # It did not come from our prompt; stamping one would claim a provenance it
    # does not have.
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        label_shots.label_video(conn, vid, "m", supplied={"11": "CU"})
        row = db.get_shot_labels(conn, vid, kind="shot_size")[0]
        assert row["prompt_hash"] is None
    finally:
        conn.close(); os.unlink(path)


def test_a_label_with_no_fingerprint_counts_as_stale():
    # Rows written before v17 have none, and "unknown provenance" is not the
    # same claim as "current".
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        db.save_shot_label(conn, vid, 2.0, "shot_size", "CU", "vlm", "m")
        assert label_shots.stale_videos(conn) == [vid]
    finally:
        conn.close(); os.unlink(path)


def test_a_label_from_the_current_prompt_leaves_the_clip_off_the_list():
    from reel_scout.shot_size import prompt_fingerprint
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        db.save_shot_label(conn, vid, 2.0, "shot_size", "CU", "vlm", "m",
                           prompt_fingerprint())
        assert label_shots.stale_videos(conn) == []
    finally:
        conn.close(); os.unlink(path)


def test_a_supplied_label_never_puts_a_clip_on_the_re_run_list():
    # It carries no fingerprint by design, so a naive "NULL is stale" query
    # would queue every hand-supplied clip for a re-run that would overwrite
    # nothing and cost a GPU pass each time.
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        db.save_shot_label(conn, vid, 2.0, "shot_size", "CU", "supplied", None)
        assert label_shots.stale_videos(conn) == []
    finally:
        conn.close(); os.unlink(path)


def test_the_re_run_list_names_each_clip_once():
    # Several stale labels on one clip is still one `shot-size` invocation.
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0), (12, 5.0)))
        db.save_shot_label(conn, vid, 2.0, "shot_size", "CU", "vlm", "m")
        db.save_shot_label(conn, vid, 5.0, "shot_size", "LS", "vlm", "m")
        assert label_shots.stale_videos(conn) == [vid]
    finally:
        conn.close(); os.unlink(path)


# --- an outage is not a verdict (2026-09-03) --------------------------------

def _outcome(payload=None, raise_=None):
    """A fake urlopen returning `payload`, or raising `raise_`."""
    import contextlib, io as _io, json as _json

    @contextlib.contextmanager
    def fake(req, timeout=None):
        if raise_ is not None:
            raise raise_
        yield _io.BytesIO(_json.dumps(payload).encode("utf-8"))
    return fake


def test_an_answer_outside_the_vocabulary_is_unparseable():
    # The real one: `EUC`, a transposition of `ECU`, at temperature 0 -- so it
    # comes back identical every run and no amount of retrying helps.
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=b"x")), \
         patch("urllib.request.urlopen",
               _outcome({"response": "EUC"})):
        code, why = label_shots.classify_with_ollama("f.jpg", "m")
    assert code is None and why == label_shots.REFUSAL_UNPARSEABLE


def test_ollama_being_down_is_unreachable_not_a_refusal():
    import urllib.error
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=b"x")), \
         patch("urllib.request.urlopen",
               _outcome(raise_=urllib.error.URLError("down"))):
        code, why = label_shots.classify_with_ollama("f.jpg", "m")
    assert code is None and why == label_shots.REFUSAL_UNREACHABLE


# --- a 200 is not an answer (2026-09-05) ------------------------------------
#
# Every case below arrived as HTTP 200 and was read as text, so the caller
# parsed a non-answer and reported REFUSAL_UNPARSEABLE -- "the model answered
# and broke the vocabulary". That verdict is the one thing allowed to retire a
# stored label. A model that said nothing was therefore emptying the table.
# Measured 2026-09-05: qwen3-vl:8b, which wrote 2593 of this library's vision
# descriptions, returns an empty string for BOTH questions.

@pytest.mark.parametrize("payload, label", [
    ({"error": "model requires more system memory"}, "error envelope"),
    ({"response": ""}, "empty string (the qwen3-vl:8b case)"),
    ({"response": "   \n"}, "whitespace only"),
    ({"done_reason": "stop"}, "no response field at all"),
    ({"response": "<think>\nThe user asks", "done_reason": "length"},
     "budget spent on reasoning, no verdict"),
])
def test_a_200_without_an_answer_is_inconclusive_not_a_refusal(payload, label):
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=b"x")), \
         patch("urllib.request.urlopen", _outcome(payload)):
        code, why = label_shots.classify_with_ollama("f.jpg", "m")
        answer, gate_why = label_shots.ask_subject_gate("f.jpg", "m")
    assert (code, why) == (None, label_shots.REFUSAL_INCONCLUSIVE), label
    assert (answer, gate_why) == (None, label_shots.REFUSAL_INCONCLUSIVE), label


def test_an_answer_that_fits_inside_a_spent_budget_is_still_an_answer():
    # The other side of the same line: `done_reason == "length"` is not itself
    # a failure. A four-token budget holds `MCU`, and rejecting every truncated
    # reply would throw away good answers to avoid the bad ones.
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=b"x")), \
         patch("urllib.request.urlopen",
               _outcome({"response": "MCU", "done_reason": "length"})):
        code, why = label_shots.classify_with_ollama("f.jpg", "m")
    assert (code, why) == ("MCU", None)


def test_an_unparseable_answer_stays_a_refusal_when_nothing_was_truncated():
    # The guard above must not swallow the real refusal it sits next to:
    # `EUC` at temperature 0, done_reason "stop" -- the model answered, and
    # will answer identically forever.
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=b"x")), \
         patch("urllib.request.urlopen",
               _outcome({"response": "EUC", "done_reason": "stop"})):
        code, why = label_shots.classify_with_ollama("f.jpg", "m")
    assert (code, why) == (None, label_shots.REFUSAL_UNPARSEABLE)


def test_a_model_that_answers_nothing_never_removes_a_label():
    # The failure this whole section exists for. Same shape as
    # `test_an_outage_never_removes_a_label`, different cause -- and this one
    # was live: the outage path was guarded, the silent-200 path was not.
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        db.save_shot_label(conn, vid, 2.0, "shot_size", "MCU", "vlm", "m", "OLD")
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=b"x")), \
             patch("urllib.request.urlopen", _outcome({"response": ""})):
            tally = label_shots.label_video(conn, vid, "m", gate=False)
        assert tally["inconclusive"] == 1
        assert tally["refused"] == 0 and tally["dropped"] == 0
        rows = db.get_shot_labels(conn, vid, kind="shot_size")
        assert [(r["value"], r["prompt_hash"]) for r in rows] == [("MCU", "OLD")]
    finally:
        conn.close(); os.unlink(path)


# --- one frame, one model verdict (2026-09-05) -------------------------------
#
# The gate and the classifier are two questions in one asking, but they stored
# under different `source` values -- and the unique key includes `source`, so
# neither replaced the other. A frame answered by the gate on one run and the
# classifier on the next ended up holding both. Consequences, all measured:
# the clip never leaves `stale_videos` (one row always carries the retired
# prompt), `analysable` counts one frame as two, and the inspector shows
# whichever source sorts first -- 'gate' does.

def _labelled(conn, vid):
    return [(r["t_sec"], r["source"], r["value"], r["prompt_hash"])
            for r in db.get_shot_labels(conn, vid, kind="shot_size")]


def test_a_gated_frame_does_not_keep_the_classifier_row_beside_it():
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        db.save_shot_label(conn, vid, 2.0, "shot_size", "MCU", "vlm", "m", "OLD")
        with patch("os.path.exists", return_value=True), \
             patch.object(label_shots, "ask_subject_gate",
                          return_value=(False, None)):
            tally = label_shots.label_video(conn, vid, "m")
        assert tally["gated"] == 1 and tally["collapsed"] == 1
        assert _labelled(conn, vid) == [
            (2.0, "gate", UNKNOWN, label_shots.prompt_fingerprint())]
        assert label_shots.stale_videos(conn) == []
    finally:
        conn.close(); os.unlink(path)


def test_a_classified_frame_does_not_keep_the_gate_row_beside_it():
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        db.save_shot_label(conn, vid, 2.0, "shot_size", UNKNOWN, "gate", "m", "OLD")
        with patch("os.path.exists", return_value=True), \
             patch.object(label_shots, "ask_subject_gate",
                          return_value=(True, None)), \
             patch.object(label_shots, "classify_with_ollama",
                          return_value=("CU", None)):
            tally = label_shots.label_video(conn, vid, "m")
        assert tally["labelled"] == 1 and tally["collapsed"] == 1
        assert _labelled(conn, vid) == [
            (2.0, "vlm", "CU", label_shots.prompt_fingerprint())]
        assert label_shots.stale_videos(conn) == []
    finally:
        conn.close(); os.unlink(path)


def test_analysable_counts_frames_not_rows():
    # The unit the caller prints is "%d/%d frame(s)". A frame holding a row
    # from each source was two frames to this query, so a library with a gate
    # pass reported a denominator twice its own size.
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0), (12, 5.0)))
        db.save_shot_label(conn, vid, 2.0, "shot_size", UNKNOWN, "gate", "m", "H")
        db.save_shot_label(conn, vid, 2.0, "shot_size", UNKNOWN, "vlm", "m", "H")
        db.save_shot_label(conn, vid, 5.0, "shot_size", "CU", "vlm", "m", "H")
        assert label_shots.analysable(conn, vid) == (1, 2)
    finally:
        conn.close(); os.unlink(path)


def test_a_persons_correction_still_sits_beside_the_models_answer():
    # The other half of the rule, and the reason it is not "one row per frame":
    # a human call and a model guess are different kinds of claim. Only the two
    # halves of one asking collapse into each other.
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        db.save_shot_label(conn, vid, 2.0, "shot_size", "CU", "supplied", None)
        with patch("os.path.exists", return_value=True), \
             patch.object(label_shots, "ask_subject_gate",
                          return_value=(True, None)), \
             patch.object(label_shots, "classify_with_ollama",
                          return_value=("MS", None)):
            tally = label_shots.label_video(conn, vid, "m")
        assert tally["collapsed"] == 0
        assert sorted(r[1] for r in _labelled(conn, vid)) == ["supplied", "vlm"]
    finally:
        conn.close(); os.unlink(path)


def test_a_re_run_after_a_prompt_change_converges():
    # What PR #105 claimed and did not deliver across sources: run twice, and
    # the second run must find nothing left to do.
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0), (12, 5.0)))
        for t in (2.0, 5.0):
            db.save_shot_label(conn, vid, t, "shot_size", "MCU", "vlm", "m", "OLD")
        assert label_shots.stale_videos(conn) == [vid]
        with patch("os.path.exists", return_value=True), \
             patch.object(label_shots, "ask_subject_gate",
                          side_effect=[(False, None), (True, None)]), \
             patch.object(label_shots, "classify_with_ollama",
                          return_value=("LS", None)):
            label_shots.label_video(conn, vid, "m")
        assert label_shots.stale_videos(conn) == []
        assert label_shots.analysable(conn, vid) == (1, 2)
    finally:
        conn.close(); os.unlink(path)


def test_a_frame_whose_file_vanishes_mid_run_keeps_its_label():
    # `missing` is checked before the model is asked, so reaching the reader is
    # a race -- and the race used to land in `refused`, which drops. Third face
    # of the same asymmetry: only a verdict may retire a row.
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        db.save_shot_label(conn, vid, 2.0, "shot_size", "MCU", "vlm", "m", "OLD")
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", side_effect=OSError("gone")):
            tally = label_shots.label_video(conn, vid, "m", gate=False)
        assert tally["missing"] == 1
        assert tally["refused"] == 0 and tally["dropped"] == 0
        assert len(db.get_shot_labels(conn, vid, kind="shot_size")) == 1
    finally:
        conn.close(); os.unlink(path)


def test_an_outage_never_removes_a_label():
    # The reason the two causes had to be split at all. Folded together, a
    # five-minute ollama restart would have deleted every superseded label in
    # the library and called it convergence.
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        db.save_shot_label(conn, vid, 2.0, "shot_size", "ECU", "vlm", "m", "OLD")
        with patch("os.path.exists", return_value=True), \
             patch.object(label_shots, "classify_with_ollama",
                          return_value=(None, label_shots.REFUSAL_UNREACHABLE)):
            tally = label_shots.label_video(conn, vid, "m", gate=False)
        assert tally["unreachable"] == 1 and tally["dropped"] == 0
        assert len(db.get_shot_labels(conn, vid, kind="shot_size")) == 1
    finally:
        conn.close(); os.unlink(path)


def test_a_retired_label_the_prompt_cannot_reproduce_is_dropped():
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        db.save_shot_label(conn, vid, 2.0, "shot_size", "ECU", "vlm", "m", "OLD")
        with patch("os.path.exists", return_value=True), \
             patch.object(label_shots, "classify_with_ollama",
                          return_value=(None, label_shots.REFUSAL_UNPARSEABLE)):
            tally = label_shots.label_video(conn, vid, "m", gate=False)
        assert tally["refused"] == 1 and tally["dropped"] == 1
        assert db.get_shot_labels(conn, vid, kind="shot_size") == []
        # And so the clip converges: this is the whole reason for the drop.
        assert label_shots.stale_videos(conn) == []
    finally:
        conn.close(); os.unlink(path)


def test_a_current_label_is_never_dropped_on_a_refusal():
    # Only labels from a retired prompt are at stake. A refusal on a frame
    # whose label is already current must leave it alone.
    from reel_scout.shot_size import prompt_fingerprint
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        db.save_shot_label(conn, vid, 2.0, "shot_size", "CU", "vlm", "m",
                           prompt_fingerprint())
        with patch("os.path.exists", return_value=True), \
             patch.object(label_shots, "classify_with_ollama",
                          return_value=(None, label_shots.REFUSAL_UNPARSEABLE)):
            tally = label_shots.label_video(conn, vid, "m", gate=False)
        assert tally["dropped"] == 0
        assert len(db.get_shot_labels(conn, vid, kind="shot_size")) == 1
    finally:
        conn.close(); os.unlink(path)


def test_a_refusal_drops_only_the_frame_it_refused():
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0), (12, 5.0)))
        db.save_shot_label(conn, vid, 2.0, "shot_size", "ECU", "vlm", "m", "OLD")
        db.save_shot_label(conn, vid, 5.0, "shot_size", "LS", "vlm", "m", "OLD")
        answers = [("MCU", None), (None, label_shots.REFUSAL_UNPARSEABLE)]
        with patch("os.path.exists", return_value=True), \
             patch.object(label_shots, "classify_with_ollama",
                          side_effect=answers):
            tally = label_shots.label_video(conn, vid, "m", gate=False)
        assert tally["labelled"] == 1 and tally["dropped"] == 1
        rows = db.get_shot_labels(conn, vid, kind="shot_size")
        assert [(r["t_sec"], r["value"]) for r in rows] == [(2.0, "MCU")]
    finally:
        conn.close(); os.unlink(path)


def test_a_supplied_label_survives_a_refusal_from_the_model():
    # It carries no fingerprint by design. A drop keyed on "no fingerprint"
    # instead of "not the current one, and ours" would delete hand-supplied
    # work the model was never asked to reproduce.
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        db.save_shot_label(conn, vid, 2.0, "shot_size", "CU", "supplied", None)
        with patch("os.path.exists", return_value=True), \
             patch.object(label_shots, "classify_with_ollama",
                          return_value=(None, label_shots.REFUSAL_UNPARSEABLE)):
            tally = label_shots.label_video(conn, vid, "m", gate=False)
        assert tally["dropped"] == 0
        assert len(db.get_shot_labels(conn, vid, kind="shot_size")) == 1
    finally:
        conn.close(); os.unlink(path)


def test_a_valid_answer_is_returned_with_no_refusal():
    # The success path had no test at all, so "always unparseable" passed the
    # suite -- and every label in the library would have been dropped.
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=b"x")), \
         patch("urllib.request.urlopen", _outcome({"response": "MCU"})):
        code, why = label_shots.classify_with_ollama("f.jpg", "m")
    assert (code, why) == ("MCU", None)


def test_a_refusal_leaves_labels_at_other_timestamps_alone():
    # `shot_labels` hangs off timestamps rather than shot ids precisely so that
    # re-analysis cannot delete it, which means a clip can carry labels at
    # timestamps no longer represented by any shot. A drop scoped to the clip
    # instead of to the frame would sweep those away as collateral -- and the
    # earlier test could not see it, because in that one the surviving label
    # had already been refreshed to the current prompt by the same run.
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        db.save_shot_label(conn, vid, 2.0, "shot_size", "ECU", "vlm", "m", "OLD")
        db.save_shot_label(conn, vid, 99.0, "shot_size", "LS", "vlm", "m", "OLD")
        with patch("os.path.exists", return_value=True), \
             patch.object(label_shots, "classify_with_ollama",
                          return_value=(None, label_shots.REFUSAL_UNPARSEABLE)):
            tally = label_shots.label_video(conn, vid, "m", gate=False)
        assert tally["dropped"] == 1
        rows = db.get_shot_labels(conn, vid, kind="shot_size")
        assert [(r["t_sec"], r["value"]) for r in rows] == [(99.0, "LS")]
    finally:
        conn.close(); os.unlink(path)


# --- the gate: ask whether the question applies before asking it -----------

def test_the_gate_records_unknown_without_calling_the_classifier():
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        with patch("os.path.exists", return_value=True), \
             patch.object(label_shots, "ask_subject_gate", return_value=(False, None)), \
             patch.object(label_shots, "classify_with_ollama") as ask:
            tally = label_shots.label_video(conn, vid, "m")
        ask.assert_not_called()
        assert tally["gated"] == 1 and tally["unknown"] == 1
        row = db.get_shot_labels(conn, vid, kind="shot_size")[0]
        assert row["value"] == UNKNOWN
        # Stamped current, or the frame is re-asked on every single run.
        from reel_scout.shot_size import prompt_fingerprint
        assert row["prompt_hash"] == prompt_fingerprint()
        assert label_shots.stale_videos(conn) == []
    finally:
        conn.close(); os.unlink(path)


def test_a_frame_the_gate_accepts_still_goes_to_the_classifier():
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        with patch("os.path.exists", return_value=True), \
             patch.object(label_shots, "ask_subject_gate", return_value=(True, None)), \
             patch.object(label_shots, "classify_with_ollama",
                          return_value=("MCU", None)) as ask:
            tally = label_shots.label_video(conn, vid, "m")
        ask.assert_called_once()
        assert tally["gated"] == 0 and tally["labelled"] == 1
        assert db.get_shot_labels(conn, vid, kind="shot_size")[0]["value"] == "MCU"
    finally:
        conn.close(); os.unlink(path)


def test_a_gate_that_cannot_answer_falls_through_to_the_classifier():
    # Failing closed would let one unparseable reply mark the frame unusable --
    # a bigger claim than a gate that answered nothing has earned.
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        with patch("os.path.exists", return_value=True), \
             patch.object(label_shots, "ask_subject_gate",
                          return_value=(None, label_shots.REFUSAL_UNPARSEABLE)), \
             patch.object(label_shots, "classify_with_ollama",
                          return_value=("LS", None)) as ask:
            tally = label_shots.label_video(conn, vid, "m")
        ask.assert_called_once()
        assert tally["gated"] == 0 and tally["labelled"] == 1
    finally:
        conn.close(); os.unlink(path)


def test_an_outage_at_the_gate_does_not_reach_the_classifier():
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        with patch("os.path.exists", return_value=True), \
             patch.object(label_shots, "ask_subject_gate",
                          return_value=(None, label_shots.REFUSAL_UNREACHABLE)), \
             patch.object(label_shots, "classify_with_ollama") as ask:
            tally = label_shots.label_video(conn, vid, "m")
        ask.assert_not_called()
        assert tally["unreachable"] == 1 and tally["gated"] == 0
        assert db.get_shot_labels(conn, vid, kind="shot_size") == []
    finally:
        conn.close(); os.unlink(path)


def test_no_gate_asks_every_frame_for_a_code():
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        with patch("os.path.exists", return_value=True), \
             patch.object(label_shots, "ask_subject_gate") as gate, \
             patch.object(label_shots, "classify_with_ollama",
                          return_value=("CU", None)):
            tally = label_shots.label_video(conn, vid, "m", gate=False)
        gate.assert_not_called()
        assert tally["labelled"] == 1
    finally:
        conn.close(); os.unlink(path)


def test_the_fingerprint_covers_the_gate_prompt_too():
    # A gate change that left the fingerprint alone would put two different
    # measurements back in one column -- the thing the column exists to stop.
    from reel_scout import shot_size
    before = shot_size.prompt_fingerprint()
    original = shot_size.subject_gate_prompt
    try:
        shot_size.subject_gate_prompt = lambda: "something else"
        assert shot_size.prompt_fingerprint() != before
    finally:
        shot_size.subject_gate_prompt = original
    assert shot_size.prompt_fingerprint() == before


def test_the_gate_answer_is_prefix_matched_not_equality_matched():
    from reel_scout.shot_size import parse_gate_answer
    assert parse_gate_answer("YES, a person") is True
    assert parse_gate_answer("no.") is False
    assert parse_gate_answer("Maybe") is None
    assert parse_gate_answer("") is None
    assert parse_gate_answer(None) is None


# --- the analysable ratio: reported, never enforced (2026-09-03) -----------

def test_analysable_counts_a_gated_frame_as_not_analysable():
    from reel_scout.shot_size import SOURCE_GATE
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0), (12, 5.0)))
        db.save_shot_label(conn, vid, 2.0, "shot_size", "MCU", "vlm", "m")
        db.save_shot_label(conn, vid, 5.0, "shot_size", UNKNOWN, SOURCE_GATE, "m")
        assert label_shots.analysable(conn, vid) == (1, 2)
    finally:
        conn.close(); os.unlink(path)


def test_analysable_keys_on_the_value_not_the_source():
    # Rows written before SOURCE_GATE existed carry `vlm`. Keying on source
    # would report those clips as 100% analysable -- a wrong number that reads
    # like a healthy one.
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0), (12, 5.0)))
        db.save_shot_label(conn, vid, 2.0, "shot_size", UNKNOWN, "vlm", "m")
        db.save_shot_label(conn, vid, 5.0, "shot_size", UNKNOWN, "vlm", "m")
        assert label_shots.analysable(conn, vid) == (0, 2)
    finally:
        conn.close(); os.unlink(path)


def test_analysable_ignores_supplied_labels():
    # Hand-supplied codes did not come from the pass this ratio describes.
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0), (12, 5.0)))
        db.save_shot_label(conn, vid, 2.0, "shot_size", "CU", "supplied", None)
        db.save_shot_label(conn, vid, 5.0, "shot_size", UNKNOWN, "vlm", "m")
        assert label_shots.analysable(conn, vid) == (0, 1)
    finally:
        conn.close(); os.unlink(path)


def test_analysable_is_zero_over_zero_when_nothing_was_labelled():
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        assert label_shots.analysable(conn, vid) == (0, 0)
    finally:
        conn.close(); os.unlink(path)


def test_a_gated_row_is_written_with_the_gate_as_its_source():
    from reel_scout.shot_size import SOURCE_GATE, prompt_fingerprint
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        with patch("os.path.exists", return_value=True), \
             patch.object(label_shots, "ask_subject_gate", return_value=(False, None)):
            label_shots.label_video(conn, vid, "m")
        row = db.get_shot_labels(conn, vid, kind="shot_size")[0]
        assert row["source"] == SOURCE_GATE
        # Still ours, so a prompt change must still bring it back for a re-run.
        assert row["prompt_hash"] == prompt_fingerprint()
        assert label_shots.stale_videos(conn) == []
    finally:
        conn.close(); os.unlink(path)


def test_a_gated_row_from_an_old_prompt_is_queued_for_a_re_run():
    from reel_scout.shot_size import SOURCE_GATE
    conn, path = _temp_db()
    try:
        vid = _seed(conn, frames=((11, 2.0),))
        db.save_shot_label(conn, vid, 2.0, "shot_size", UNKNOWN, SOURCE_GATE,
                           "m", "OLD")
        assert label_shots.stale_videos(conn) == [vid]
    finally:
        conn.close(); os.unlink(path)
