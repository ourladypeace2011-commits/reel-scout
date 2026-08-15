from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

import pytest

from reel_scout import batch as batch_mod
from reel_scout import db
from reel_scout.mcp import tools
from reel_scout.mcp.batch_worker import BATCH_SOURCE


def _parse(result: Any) -> Any:
    return json.loads(result["content"][0]["text"])


@pytest.fixture
def no_spawn(monkeypatch):
    """Record spawns instead of making them. Nothing here should ever really
    start a worker: these tests are about the decisions, not the downloads."""
    calls = []
    monkeypatch.setattr(tools, "_spawn_worker",
                        lambda batch_id, log: calls.append(batch_id) or 4242)
    return calls


def _conn(temp_db):
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    return conn


# --- the mode question is a question, not a decision --------------------------

def test_no_vlm_asks_the_user_instead_of_picking(temp_db, no_spawn, monkeypatch):
    monkeypatch.setattr(batch_mod, "probe", lambda: {"vlm": False, "whisper": True})

    result = tools.call_tool("batch_start", {"urls": ["https://www.youtube.com/shorts/aaaaaaaaaaa"]})

    assert "isError" not in result, "a question must not look like a failure"
    payload = _parse(result)
    assert payload["status"] == "needs_mode_choice"
    assert {c["mode"] for c in payload["choices"]} == {"agent", "transcript", "full"}
    assert [c["mode"] for c in payload["choices"] if c.get("recommended")] == ["agent"]
    assert "do not choose for them" in payload["action_required"].lower()


def test_asking_the_user_starts_nothing(temp_db, no_spawn, monkeypatch):
    """A question with side effects is not a question."""
    monkeypatch.setattr(batch_mod, "probe", lambda: {"vlm": False, "whisper": True})

    tools.call_tool("batch_start", {"urls": ["https://www.youtube.com/shorts/aaaaaaaaaaa"]})

    conn = _conn(temp_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
    finally:
        conn.close()
    assert no_spawn == []


def test_requesting_full_without_a_vlm_also_asks(temp_db, no_spawn, monkeypatch):
    monkeypatch.setattr(batch_mod, "probe", lambda: {"vlm": False, "whisper": True})
    result = tools.call_tool("batch_start", {
        "urls": ["https://www.youtube.com/shorts/aaaaaaaaaaa"], "mode": "full"})
    assert _parse(result)["status"] == "needs_mode_choice"


def test_a_reachable_vlm_starts_without_asking(temp_db, no_spawn, monkeypatch):
    monkeypatch.setattr(batch_mod, "probe", lambda: {"vlm": True, "whisper": True})
    payload = _parse(tools.call_tool("batch_start", {
        "urls": ["https://www.youtube.com/shorts/aaaaaaaaaaa"]}))
    assert payload["status"] == "started"
    assert payload["mode"] == "full"


def test_mode_agent_does_not_pay_for_a_probe(temp_db, no_spawn, monkeypatch):
    """probe() shells out to ffmpeg and yt-dlp and makes two HTTP calls — up to
    ~16s — and resolve_mode ignores its answer on this path anyway."""
    def explode():
        raise AssertionError("probe() must not be called for mode=agent")

    monkeypatch.setattr(batch_mod, "probe", explode)
    payload = _parse(tools.call_tool("batch_start", {
        "urls": ["https://www.youtube.com/shorts/aaaaaaaaaaa"], "mode": "agent"}))
    assert payload["status"] == "started"


# --- input handling -----------------------------------------------------------

def test_the_same_link_pasted_twice_runs_once(temp_db, no_spawn):
    """batch_items is keyed (batch_id, url) and create_batch INSERTs each one, so
    an un-deduped list is an IntegrityError rather than a duplicate."""
    url = "https://www.youtube.com/shorts/aaaaaaaaaaa"
    payload = _parse(tools.call_tool("batch_start", {"urls": [url, url], "mode": "agent"}))
    assert payload["count"] == 1

    conn = _conn(temp_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM batch_items").fetchone()[0] == 1
    finally:
        conn.close()


def test_a_pasted_blob_keeps_only_ingestable_links(temp_db, no_spawn):
    text = (
        "look at these\n"
        "Amy Wu https://www.instagram.com/reel/AAAAAAAAAAA/\n"
        "https://drive.google.com/file/d/xyz/view\n"
        "https://www.youtube.com/watch?v=longformvideo\n"
    )
    payload = _parse(tools.call_tool("batch_start", {"text": text, "mode": "agent"}))
    assert payload["count"] == 1


def test_labels_from_pasted_text_survive_into_the_rows(temp_db, no_spawn):
    text = "Amy Wu https://www.instagram.com/reel/AAAAAAAAAAA/"
    tools.call_tool("batch_start", {"text": text, "mode": "agent"})
    conn = _conn(temp_db)
    try:
        assert conn.execute("SELECT label FROM batch_items").fetchone()[0] == "Amy Wu"
    finally:
        conn.close()


def test_no_source_is_an_error(temp_db, no_spawn):
    assert tools.call_tool("batch_start", {"mode": "agent"}).get("isError")


def test_two_sources_is_an_error(temp_db, no_spawn):
    assert tools.call_tool("batch_start", {
        "urls": ["https://www.youtube.com/shorts/aaaaaaaaaaa"],
        "text": "x", "mode": "agent"}).get("isError")


def test_input_with_no_links_says_so(temp_db, no_spawn):
    result = tools.call_tool("batch_start", {"text": "no links here", "mode": "agent"})
    assert result["isError"] is True
    assert "no Instagram" in result["content"][0]["text"]


def test_a_google_doc_sharing_error_reaches_the_agent_verbatim(temp_db, no_spawn, monkeypatch):
    """The hint names the fix; swallowing it leaves the student with a bare 401."""
    def refuse(_url):
        raise RuntimeError("that Doc came back as a sign-in page — set sharing to "
                           "'Anyone with the link'")

    monkeypatch.setattr(batch_mod, "fetch", refuse)
    result = tools.call_tool("batch_start", {"doc": "https://docs.google.com/d/x/edit",
                                             "mode": "agent"})
    assert result["isError"] is True
    assert "Anyone with the link" in result["content"][0]["text"]


# --- starting is not doing ----------------------------------------------------

def test_start_returns_before_any_video_is_processed(temp_db, no_spawn, monkeypatch):
    def explode(*a, **k):
        raise AssertionError("run_batch must not run in the server process")

    monkeypatch.setattr(batch_mod, "run_batch", explode)
    payload = _parse(tools.call_tool("batch_start", {
        "urls": ["https://www.youtube.com/shorts/aaaaaaaaaaa"], "mode": "agent"}))
    assert payload["status"] == "started"
    assert no_spawn == [payload["batch_id"]]


def test_a_second_start_while_one_is_live_is_refused(temp_db, no_spawn):
    args = {"urls": ["https://www.youtube.com/shorts/aaaaaaaaaaa"], "mode": "agent"}
    first = _parse(tools.call_tool("batch_start", args))
    conn = _conn(temp_db)
    try:
        db.touch_batch_heartbeat(conn, first["batch_id"])
    finally:
        conn.close()

    second = tools.call_tool("batch_start", {
        "urls": ["https://www.youtube.com/shorts/bbbbbbbbbbb"], "mode": "agent"})
    assert second["isError"] is True
    assert "force" in second["content"][0]["text"]


# --- status tells the truth, including when the truth is "it died" ------------

def test_status_reports_completed_work_after_the_worker_vanishes(temp_db, no_spawn):
    """The requirement this whole design exists for: a timeout or a reboot must
    not cost the work that finished."""
    urls = ["https://www.youtube.com/shorts/aaaaaaaaaaa",
            "https://www.youtube.com/shorts/bbbbbbbbbbb"]
    started = _parse(tools.call_tool("batch_start", {"urls": urls, "mode": "agent"}))
    conn = _conn(temp_db)
    try:
        db.update_batch_item(conn, started["batch_id"], urls[0], "done", video_id="v1")
        conn.execute("UPDATE batches SET heartbeat_at = datetime('now', '-30 minutes') "
                     "WHERE id = ?", (started["batch_id"],))
        conn.commit()
    finally:
        conn.close()

    payload = _parse(tools.call_tool("batch_status", {}))
    assert payload["state"] == "stalled"
    assert payload["counts"]["done"] == 1
    assert payload["done"][0]["video_id"] == "v1"
    assert "will not run" in payload["note"]


def test_a_fresh_heartbeat_is_not_called_stalled(temp_db, no_spawn):
    started = _parse(tools.call_tool("batch_start", {
        "urls": ["https://www.youtube.com/shorts/aaaaaaaaaaa"], "mode": "agent"}))
    conn = _conn(temp_db)
    try:
        db.touch_batch_heartbeat(conn, started["batch_id"])
    finally:
        conn.close()
    assert _parse(tools.call_tool("batch_status", {}))["state"] == "running"


def test_status_defaults_to_the_most_recent_batch(temp_db, no_spawn):
    tools.call_tool("batch_start", {
        "urls": ["https://www.youtube.com/shorts/aaaaaaaaaaa"], "mode": "agent"})
    assert _parse(tools.call_tool("batch_status", {}))["counts"]["total"] == 1


def test_status_before_anything_started_is_a_clean_error(temp_db):
    result = tools.call_tool("batch_status", {})
    assert result["isError"] is True
    assert "no batch" in result["content"][0]["text"]


def test_status_never_writes_the_schema(temp_db, no_spawn, monkeypatch):
    """init_db runs executescript over the whole schema, which takes the schema
    lock — against a batch's analyze child that is the likeliest way to see
    'database is locked'. Invisible mitigation, trivially regressed."""
    tools.call_tool("batch_start", {
        "urls": ["https://www.youtube.com/shorts/aaaaaaaaaaa"], "mode": "agent"})

    def explode(*a, **k):
        raise AssertionError("batch_status must not call init_db")

    monkeypatch.setattr(db, "init_db", explode)
    assert "isError" not in tools.call_tool("batch_status", {})


def test_status_hands_back_the_videos_still_needing_a_visual_layer(temp_db, no_spawn, monkeypatch):
    started = _parse(tools.call_tool("batch_start", {
        "urls": ["https://www.youtube.com/shorts/aaaaaaaaaaa"], "mode": "agent"}))
    conn = _conn(temp_db)
    try:
        db.set_batch_item_progress(conn, started["batch_id"],
                                   "https://www.youtube.com/shorts/aaaaaaaaaaa",
                                   video_id="v1", status="needs_vision")
    finally:
        conn.close()

    monkeypatch.setattr(batch_mod, "needs_completion", lambda conn, vid: True)
    payload = _parse(tools.call_tool("batch_status", {}))
    assert [e["video_id"] for e in payload["needs_visual_layer"]] == ["v1"]
    # The App has no shell, so the handoff must name tools, not CLI commands.
    assert "keyframes" in payload["next_steps"]
    assert "reel-scout ingest" not in payload["next_steps"]

    # And it must shrink as the agent works through it.
    monkeypatch.setattr(batch_mod, "needs_completion", lambda conn, vid: False)
    assert _parse(tools.call_tool("batch_status", {}))["needs_visual_layer"] == []


# --- cancel is honest about what it did ---------------------------------------

def test_cancel_does_not_claim_the_current_video_stopped(temp_db, no_spawn):
    tools.call_tool("batch_start", {
        "urls": ["https://www.youtube.com/shorts/aaaaaaaaaaa"], "mode": "agent"})
    payload = _parse(tools.call_tool("batch_cancel", {}))
    assert payload["status"] == "cancel_requested"
    assert "will finish first" in payload["note"]


def test_cancel_sets_the_flag_the_worker_polls(temp_db, no_spawn):
    started = _parse(tools.call_tool("batch_start", {
        "urls": ["https://www.youtube.com/shorts/aaaaaaaaaaa"], "mode": "agent"}))
    tools.call_tool("batch_cancel", {})
    conn = _conn(temp_db)
    try:
        assert db.batch_cancel_requested(conn, started["batch_id"]) is True
    finally:
        conn.close()


# --- stdout is the NDJSON channel ---------------------------------------------

@pytest.mark.parametrize("tool,args", [
    ("batch_start", {"urls": ["https://www.youtube.com/shorts/aaaaaaaaaaa"], "mode": "agent"}),
    ("batch_status", {}),
    ("batch_cancel", {}),
])
def test_batch_tools_never_write_to_stdout(temp_db, no_spawn, capsys, tool, args):
    tools.call_tool("batch_start", {
        "urls": ["https://www.youtube.com/shorts/zzzzzzzzzzz"], "mode": "agent"})
    capsys.readouterr()
    tools.call_tool(tool, dict(args, force=True) if tool == "batch_start" else args)
    assert capsys.readouterr().out == ""


# --- "completed" must not mean "completed with a hole in it" ------------------
#
# `mark_batch_completed` never consulted the failure counter, so a batch whose
# every item failed reported `state: "completed"` with `counts.failed: N` sitting
# right beside it, ignored. Students reach this package through the MCP server
# (Claude Desktop, no terminal), so this -- not the CLI exit code -- is the path
# that told them a batch was fine when it was not.


def _finished(temp_db, urls, done=(), failed=()):
    """A batch the worker ran to the end, with the given per-item outcomes."""
    started = _parse(tools.call_tool("batch_start", {"urls": list(urls), "mode": "agent"}))
    conn = _conn(temp_db)
    try:
        for u in done:
            db.update_batch_item(conn, started["batch_id"], u, "done", video_id="v")
        for u in failed:
            db.update_batch_item(conn, started["batch_id"], u, "error",
                                 error="analyze exited non-zero")
        db.mark_batch_completed(conn, started["batch_id"])
    finally:
        conn.close()
    return started["batch_id"]


def test_a_batch_that_finished_with_failures_is_not_called_completed(temp_db, no_spawn):
    urls = ["https://www.youtube.com/shorts/aaaaaaaaaaa",
            "https://www.youtube.com/shorts/bbbbbbbbbbb",
            "https://www.youtube.com/shorts/ccccccccccc"]
    _finished(temp_db, urls, done=urls[:1], failed=urls[1:])

    payload = _parse(tools.call_tool("batch_status", {}))
    assert payload["state"] == "completed_with_failures"
    assert payload["counts"]["failed"] == 2
    assert "not a crash" in payload["note"], (
        "the note has to separate this from a dead worker, or the reader learns "
        "nothing the counts did not already say")


def test_a_batch_where_everything_landed_is_still_plain_completed(temp_db, no_spawn):
    # The derivation must not fire on a clean run, or the new state means nothing.
    urls = ["https://www.youtube.com/shorts/aaaaaaaaaaa"]
    _finished(temp_db, urls, done=urls)
    assert _parse(tools.call_tool("batch_status", {}))["state"] == "completed"


def test_a_dead_worker_is_still_called_failed(temp_db, no_spawn):
    """The non-collision requirement, executable.

    `failed` means and only means "the worker process died". A batch that ran to
    the end with failed items is a different fact and must never borrow the word,
    because one is a hole in the results and the other is a hole in the run.
    """
    urls = ["https://www.youtube.com/shorts/aaaaaaaaaaa"]
    started = _parse(tools.call_tool("batch_start", {"urls": urls, "mode": "agent"}))
    conn = _conn(temp_db)
    try:
        db.set_batch_meta(conn, started["batch_id"], status="failed")
    finally:
        conn.close()

    payload = _parse(tools.call_tool("batch_status", {}))
    assert payload["state"] == "failed"
    assert payload["counts"]["failed"] == 0, "no item failed -- the worker did"
    assert "batch.log" in payload["note"], (
        "a bare `failed` with no pointer to the traceback is the least "
        "explanatory state carrying the most alarming word")
