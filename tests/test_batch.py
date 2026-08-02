"""`reel-scout batch` — a doc full of links, one bundle each.

Two properties carry the weight here. First, a machine without a VLM must **stop
and ask** rather than quietly produce transcript-only bundles: silent degradation
is how the craft score goes missing without anyone noticing. Second, an entry is
only ever paired with a video it definitely produced — handing one person's
analysis to another is the worst outcome this command has.
"""
from __future__ import annotations

import pytest

from reel_scout import batch


# --- capability -> mode ------------------------------------------------------

def test_a_reachable_vlm_makes_full_unambiguous():
    """Nothing to choose when the machine can do everything."""
    assert batch.resolve_mode(None, {"vlm": True, "whisper": True}) == ("full", "")


def test_no_vlm_refuses_to_pick_for_the_user():
    mode, msg = batch.resolve_mode(None, {"vlm": False, "whisper": True})
    assert mode is None
    for expected in ("--mode agent", "--mode transcript", "--mode full"):
        assert expected in msg
    assert "not an error" in msg


def test_no_vlm_does_not_silently_fall_back_to_transcript():
    """The regression that matters: quietly dropping the visual layer + score."""
    mode, _ = batch.resolve_mode(None, {"vlm": False, "whisper": True})
    assert mode != "transcript"


def test_asking_for_full_without_a_vlm_is_refused_with_the_fix():
    mode, msg = batch.resolve_mode("full", {"vlm": False, "whisper": True})
    assert mode is None
    assert "ollama serve" in msg or "--mode agent" in msg


@pytest.mark.parametrize("mode", ["agent", "transcript"])
def test_explicit_modes_are_honoured_without_a_vlm(mode):
    assert batch.resolve_mode(mode, {"vlm": False, "whisper": False}) == (mode, "")


def test_unknown_mode_lists_the_valid_ones():
    mode, msg = batch.resolve_mode("turbo", {"vlm": True, "whisper": True})
    assert mode is None
    assert "agent" in msg and "transcript" in msg


# --- sub-invocation ----------------------------------------------------------

def test_sub_commands_target_this_interpreter_not_a_path_lookup():
    """Found by actually running it: `./env/bin/reel-scout batch ...` without an
    activated venv died on FileNotFoundError('reel-scout') partway through, because
    the bare name only resolves when the venv's bin is on PATH."""
    import sys

    cmd = batch.self_cmd("analyze", "https://example.com/x")
    assert cmd[0] == sys.executable
    assert cmd[1:3] == ["-m", "reel_scout.cli"]
    assert "reel-scout" not in cmd


# --- pairing safety ----------------------------------------------------------

class _Cur:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, ids, urlmap=None):
        self.ids, self.urlmap = ids, urlmap or {}

    def execute(self, sql, params=()):
        if "WHERE url" in sql:
            vid = self.urlmap.get(params[0])
            return _Cur({"id": vid} if vid else None)
        return [(i,) for i in self.ids]


def test_exactly_one_new_row_is_the_one_we_just_made():
    assert batch.resolve_video_id(_Conn({"a", "b"}), {"a"}, "u") == "b"


def test_two_new_rows_refuses_to_guess():
    assert batch.resolve_video_id(_Conn({"a", "b", "c"}), {"a"}, "u") is None


def test_already_analyzed_falls_back_to_an_exact_url_match():
    assert batch.resolve_video_id(_Conn({"a"}, {"u": "z"}), {"a"}, "u") == "z"


def test_tracking_parameters_that_break_url_equality_refuse_to_guess():
    assert batch.resolve_video_id(_Conn({"a"}), {"a"}, "u") is None


# --- parsing -----------------------------------------------------------------

FORM_CSV = (
    "時間戳記,姓名,連結,問題\n"
    "2026/08/01 9:15,陳小明,https://www.instagram.com/reel/AAA111/?igsh=track,開頭\n"
    "2026/08/01 9:22,Amy Wu,https://www.tiktok.com/@x/video/7401234567890123456,節奏\n"
    "2026/08/01 9:40,陳小明,https://www.instagram.com/reel/AAA111/?igsh=track,重複\n"
)

FREE_TEXT = """我想拆的片
王美麗 https://www.instagram.com/reel/BBB222/
張阿強 — https://vt.tiktok.com/ZSJqwerty/
(還沒填)
Kevin Chen：https://youtu.be/ccc333
"""


def test_csv_reads_the_label_column_not_the_form_timestamp():
    assert [n for n, _ in batch.parse_rows(FORM_CSV)] == ["陳小明", "Amy Wu"]


def test_the_same_link_twice_runs_once():
    assert len(batch.parse_rows(FORM_CSV)) == 2


def test_free_text_takes_whatever_precedes_the_link():
    assert [n for n, _ in batch.parse_rows(FREE_TEXT)] == ["王美麗", "張阿強", "Kevin Chen"]


def test_links_the_pipeline_cannot_ingest_are_left_alone():
    text = ("a https://drive.google.com/file/d/abc/view\n"
            "b https://www.youtube.com/watch?v=longform\n"
            "c https://www.instagram.com/reel/CCC444/\n")
    assert [u for _, u in batch.parse_rows(text)] == [
        "https://www.instagram.com/reel/CCC444/"]


def test_an_unlabelled_link_still_counts():
    assert batch.parse_rows("https://www.instagram.com/reel/DDD555/\n") == [
        ("", "https://www.instagram.com/reel/DDD555/")]


# --- google export endpoints -------------------------------------------------

@pytest.mark.parametrize("src,want", [
    ("https://docs.google.com/document/d/1AbC-dEf/edit?usp=sharing",
     "https://docs.google.com/document/d/1AbC-dEf/export?format=txt"),
    ("https://docs.google.com/spreadsheets/d/9XyZ_123/edit#gid=0",
     "https://docs.google.com/spreadsheets/d/9XyZ_123/export?format=csv"),
    ("https://example.com/list.txt", "https://example.com/list.txt"),
])
def test_an_ordinary_edit_link_becomes_a_no_auth_export_link(src, want):
    assert batch.export_url(src) == want


# --- output naming -----------------------------------------------------------

@pytest.mark.parametrize("label,idx,want", [
    ("陳小明", 1, "陳小明"),
    ("Amy Wu", 2, "Amy-Wu"),
    ("", 3, "clip-03"),
    ("林大華 (學員)", 4, "林大華-學員"),
    ("王/美/麗", 5, "王美麗"),
])
def test_slugify_keeps_cjk_and_drops_path_separators(label, idx, want):
    assert batch.slugify(label, idx) == want


# --- on_progress: so an interrupted run leaves a record, not a lie ------------

def test_run_batch_without_a_callback_behaves_exactly_as_before(temp_db, tmp_path, monkeypatch):
    """The kwarg defaults to None and every existing caller passes nothing."""
    monkeypatch.setattr(batch, "_run", lambda cmd, verbose: 0)
    monkeypatch.setattr(batch, "_video_ids", lambda conn: set())
    monkeypatch.setattr(batch, "resolve_video_id", lambda conn, before, url: "vid-1")
    monkeypatch.setattr(batch, "needs_completion", lambda conn, vid: False)

    result = batch.run_batch([("A", "https://x/1")], str(tmp_path / "out"), "agent")

    assert set(result) == {"mode", "done", "failed", "pending_completion"}
    assert result["done"][0]["video_id"] == "vid-1"


def test_every_item_transition_is_reported_before_the_batch_returns(
        temp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(batch, "_run", lambda cmd, verbose: 0)
    monkeypatch.setattr(batch, "_video_ids", lambda conn: set())
    monkeypatch.setattr(batch, "resolve_video_id", lambda conn, before, url: "vid-" + url[-1])
    monkeypatch.setattr(batch, "needs_completion", lambda conn, vid: True)

    seen = []
    batch.run_batch([("A", "https://x/1"), ("B", "https://x/2")],
                    str(tmp_path / "out"), "agent", on_progress=seen.append)

    kinds = [e["event"] for e in seen]
    assert kinds.count("item_start") == 2
    assert kinds.count("item_done") == 2
    assert kinds[-1] == "batch_done"
    assert kinds.index("item_start") < kinds.index("item_done")
    # The locators a status view needs, present at the time they are known.
    starts = [e for e in seen if e["event"] == "item_start"]
    assert starts[0]["label"] == "A" and starts[0]["slug"]


def test_a_failing_item_still_reports_and_the_run_continues(temp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(batch, "_video_ids", lambda conn: set())
    monkeypatch.setattr(batch, "needs_completion", lambda conn, vid: False)
    monkeypatch.setattr(batch, "resolve_video_id", lambda conn, before, url: "vid-" + url[-1])
    # Only the first URL's analyze fails.
    monkeypatch.setattr(batch, "_run",
                        lambda cmd, verbose: 1 if ("https://x/1" in cmd and "analyze" in cmd) else 0)

    seen = []
    result = batch.run_batch([("A", "https://x/1"), ("B", "https://x/2")],
                             str(tmp_path / "out"), "agent", on_progress=seen.append)

    assert [e["reason"] for e in seen if e["event"] == "item_failed"] == [
        "analyze exited non-zero"]
    assert len(result["done"]) == 1
    assert result["done"][0]["label"] == "B"


def test_an_unresolved_video_is_reported_failed_not_done(temp_db, tmp_path, monkeypatch):
    """The mispairing invariant: when it cannot tell which video an analyze
    produced it must skip, never guess. Now also over the callback."""
    monkeypatch.setattr(batch, "_run", lambda cmd, verbose: 0)
    monkeypatch.setattr(batch, "_video_ids", lambda conn: set())
    monkeypatch.setattr(batch, "resolve_video_id", lambda conn, before, url: None)
    monkeypatch.setattr(batch, "needs_completion", lambda conn, vid: False)

    seen = []
    result = batch.run_batch([("A", "https://x/1")], str(tmp_path / "out"), "agent",
                             on_progress=seen.append)

    assert result["done"] == []
    assert result["failed"][0]["reason"] == "video id unresolved"
    assert [e["event"] for e in seen if e["event"] == "item_done"] == []


def test_cancel_stops_at_the_next_entry_not_mid_video(temp_db, tmp_path, monkeypatch):
    """Half an analysis is worse than one more finished video, so a cancel is
    honoured between entries and never interrupts one in flight."""
    monkeypatch.setattr(batch, "_run", lambda cmd, verbose: 0)
    monkeypatch.setattr(batch, "_video_ids", lambda conn: set())
    monkeypatch.setattr(batch, "resolve_video_id", lambda conn, before, url: "vid-" + url[-1])
    monkeypatch.setattr(batch, "needs_completion", lambda conn, vid: False)

    seen = []

    def sink(event):
        seen.append(event)
        # Ask to stop only once the first item is already underway.
        if event["event"] == "item_start" and event["index"] == 2:
            return "cancel"
        return None

    result = batch.run_batch(
        [("A", "https://x/1"), ("B", "https://x/2"), ("C", "https://x/3")],
        str(tmp_path / "out"), "agent", on_progress=sink)

    assert len(result["done"]) == 1          # the first finished
    assert result["done"][0]["label"] == "A"
    assert result.get("cancelled") is True
    assert [e["event"] for e in seen].count("item_start") == 2  # B started, then stopped


def test_a_broken_progress_sink_does_not_kill_the_job(temp_db, tmp_path, monkeypatch):
    """Progress reporting is bookkeeping; the job is the point."""
    monkeypatch.setattr(batch, "_run", lambda cmd, verbose: 0)
    monkeypatch.setattr(batch, "_video_ids", lambda conn: set())
    monkeypatch.setattr(batch, "resolve_video_id", lambda conn, before, url: "vid-1")
    monkeypatch.setattr(batch, "needs_completion", lambda conn, vid: False)

    def explode(_event):
        raise RuntimeError("database is locked")

    result = batch.run_batch([("A", "https://x/1")], str(tmp_path / "out"), "agent",
                             on_progress=explode)

    assert len(result["done"]) == 1


# --- URL harvesting --------------------------------------------------------
#
# The batch list regex had the same account-scoped blind spot as the crawler,
# but failed worse: the crawler at least raises, whereas an unmatched line here
# is silently dropped from the batch — the run reports success on fewer clips
# than the sheet contained.

def test_an_account_scoped_reel_link_is_not_silently_dropped():
    rows = batch.parse_rows("Zachary\thttps://www.instagram.com/zacharywinterton/reel/Da3UcDsudRN/")
    assert [u for _, u in rows] == [
        "https://www.instagram.com/zacharywinterton/reel/Da3UcDsudRN/"
    ]


def test_canonical_and_account_scoped_links_both_survive_a_mixed_list():
    text = "\n".join([
        "https://www.instagram.com/reel/AAAAAAAAAAA/",
        "https://www.instagram.com/some.user_1/reel/BBBBBBBBBBB/",
        "https://www.instagram.com/someuser/p/CCCCCCCCCCC/",
    ])
    assert len(batch.parse_rows(text)) == 3


def test_a_bare_profile_link_is_still_left_alone():
    """Profiles are not ingestible clips; harvesting one would send the batch
    off to download a page."""
    assert batch.parse_rows("https://www.instagram.com/someuser/") == []


# --- scoring: deferred to one pass after the loop ------------------------------
#
# `analyze` does not score; the documented per-URL flow is `analyze --score`.
# The batch loop called plain `analyze` + `export`, so every reel that went
# through `reel-scout batch` landed with no verdict at all. Scoring now runs,
# but *after* the loop: doing it per item forces the visual model and the
# scorer to swap in and out on every reel, measured at ~2.2x the per-item
# cycle. These pin down both the presence and the placement.

def _sub_of(cmd):
    for token in cmd:
        if token in ("analyze", "score", "export"):
            return token
    return "?"


def _subcommands(seen):
    return [_sub_of(cmd) for cmd in seen]


def _record_runs(monkeypatch, rc_for=None, rc_seq=None):
    """Capture every sub-command run_batch shells out to."""
    seen = []
    calls = {}

    def fake_run(cmd, verbose):
        seen.append(cmd)
        sub = _sub_of(cmd)
        calls[sub] = calls.get(sub, 0) + 1
        if rc_seq and sub in rc_seq:
            seq = rc_seq[sub]
            return seq[min(calls[sub] - 1, len(seq) - 1)]
        if rc_for:
            for needle, rc in rc_for.items():
                if needle in cmd:
                    return rc
        return 0

    monkeypatch.setattr(batch, "_run", fake_run)
    monkeypatch.setattr(batch, "_video_ids", lambda conn: set())
    monkeypatch.setattr(batch, "resolve_video_id", lambda conn, before, url: "vid-" + url[-1])
    monkeypatch.setattr(batch, "needs_completion", lambda conn, vid: False)
    return seen


def test_scoring_runs_once_after_the_loop_not_per_item(temp_db, tmp_path, monkeypatch):
    """Two reels must not interleave score calls between their analyses."""
    monkeypatch.setattr(batch, "has_score", lambda conn, vid: False)
    seen = _record_runs(monkeypatch)

    batch.run_batch([("A", "https://x/1"), ("B", "https://x/2")],
                    str(tmp_path / "out"), "full")

    subs = _subcommands(seen)
    last_analyze = max(i for i, s in enumerate(subs) if s == "analyze")
    assert subs.index("score") > last_analyze, (
        "a score call ran before the last analyze -- that is the per-item "
        "placement that forces a model swap on every reel")
    assert subs.count("score") == 2
    assert subs[-1] == "export", "bundles that gained a verdict are refreshed after"


def test_an_already_scored_video_is_not_scored_again(temp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(batch, "has_score", lambda conn, vid: True)
    seen = _record_runs(monkeypatch)

    batch.run_batch([("A", "https://x/1")], str(tmp_path / "out"), "full")

    assert "score" not in _subcommands(seen)


def test_agent_mode_does_not_score_over_an_unfinished_visual_layer(
        temp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(batch, "has_score", lambda conn, vid: False)
    seen = _record_runs(monkeypatch)

    batch.run_batch([("A", "https://x/1")], str(tmp_path / "out"), "agent")

    assert "score" not in _subcommands(seen)


def test_no_score_opts_out(temp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(batch, "has_score", lambda conn, vid: False)
    seen = _record_runs(monkeypatch)

    batch.run_batch([("A", "https://x/1")], str(tmp_path / "out"), "full", score=False)

    assert _subcommands(seen) == ["analyze", "export"]


def test_a_timed_out_score_is_retried_once(temp_db, tmp_path, monkeypatch):
    """The first score after a batch of VLM work pays the model load; retry it."""
    monkeypatch.setattr(batch, "has_score", lambda conn, vid: False)
    seen = _record_runs(monkeypatch, rc_seq={"score": [1, 0]})

    events = []
    result = batch.run_batch([("A", "https://x/1")], str(tmp_path / "out"), "full",
                             on_progress=events.append)

    assert _subcommands(seen).count("score") == 2, "a failed score must be retried once"
    kinds = [e["event"] for e in events]
    assert "item_scored" in kinds and "item_score_failed" not in kinds
    assert result["done"] and not result["failed"]


def test_a_failing_scorer_never_costs_the_analysis(temp_db, tmp_path, monkeypatch):
    """The expensive work is already in the database; a scorer error is not fatal."""
    monkeypatch.setattr(batch, "has_score", lambda conn, vid: False)
    seen = _record_runs(monkeypatch, rc_for={"score": 1})

    events = []
    result = batch.run_batch([("A", "https://x/1")], str(tmp_path / "out"), "full",
                             on_progress=events.append)

    assert _subcommands(seen).count("score") == 2, "failed twice = tried and retried"
    assert result["done"] and result["done"][0]["video_id"] == "vid-1"
    assert not result["failed"]
    assert "item_score_failed" in [e["event"] for e in events]


def test_has_score_reads_the_scores_table(temp_db):
    import sqlite3
    from reel_scout import db as rsdb

    conn = sqlite3.connect(temp_db)
    rsdb.init_db(conn)
    assert batch.has_score(conn, "vid-1") is False
    conn.execute("INSERT INTO scores (video_id, overall) VALUES (?,?)", ("vid-1", 7.0))
    conn.commit()
    assert batch.has_score(conn, "vid-1") is True
    conn.close()
