from __future__ import annotations

import json
import os
import sqlite3
import shutil
from io import StringIO
from typing import Any

import pytest

from reel_scout import config, db
from reel_scout.mcp import server, tools


TMP_ROOT = os.path.join(os.path.dirname(__file__), "_tmp")


def _parse_result(result: Any) -> Any:
    return json.loads(result["content"][0]["text"])


@pytest.fixture
def temp_db(monkeypatch):
    os.makedirs(TMP_ROOT, exist_ok=True)
    data_dir = os.path.join(TMP_ROOT, "mcp_db")
    shutil.rmtree(data_dir, ignore_errors=True)
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "reel_scout.db")
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "VIDEOS_DIR", os.path.join(data_dir, "videos"))
    monkeypatch.setattr(config, "KEYFRAMES_DIR", os.path.join(data_dir, "keyframes"))
    monkeypatch.setattr(config, "ANALYSIS_DIR", os.path.join(data_dir, "analysis"))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    conn.close()
    try:
        yield db_path
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


def test_list_tools_count():
    tool_defs = tools.list_tools()
    assert len(tool_defs) == 5


def test_list_tools_schema():
    tool_defs = tools.list_tools()
    for tool_def in tool_defs:
        assert "name" in tool_def
        assert "description" in tool_def
        assert "inputSchema" in tool_def


def test_call_list_videos_empty(temp_db):
    result = tools.call_tool("list_videos", {})
    assert "isError" not in result
    assert _parse_result(result) == []


def test_call_show_video_not_found(temp_db):
    result = tools.call_tool("show_video", {"video_id": "missing"})
    assert result["isError"] is True
    assert "Video not found" in result["content"][0]["text"]


def test_call_unknown_tool():
    result = tools.call_tool("unknown", {})
    assert result["isError"] is True
    assert "Unknown tool" in result["content"][0]["text"]


def test_handle_initialize():
    response = server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert response["result"]["protocolVersion"] == "2024-11-05"
    assert response["result"]["serverInfo"]["name"] == "reel-scout"


def test_handle_tools_list():
    response = server.handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    )
    assert len(response["result"]["tools"]) == 5


def test_message_framing_round_trip():
    payload = {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}}
    output = StringIO()
    server.write_message(payload, output)
    serialized = output.getvalue()
    assert serialized.startswith("Content-Length: ")

    input_stream = StringIO(serialized)
    parsed = server.read_message(input_stream)
    assert parsed == payload


def test_call_export_json(temp_db):
    conn = db.get_connection()
    try:
        video_id = db.upsert_video(
            conn,
            platform="youtube",
            platform_id="abc123",
            url="https://youtube.com/shorts/abc123",
            title="測試影片",
            duration_sec=12.5,
        )
        db.save_transcript(
            conn,
            video_id,
            language="zh",
            text_full="這是逐字稿",
            segments_json="[]",
            whisper_model="mock-whisper",
            duration_sec=12.5,
        )
        db.save_analysis(
            conn,
            video_id,
            summary="重點摘要",
            topics_json='["測試"]',
            hooks_json='["開頭鉤子"]',
            style_json='{"format":"talking-head"}',
            engagement_signals_json='["caption"]',
            full_json='{"summary":"重點摘要"}',
        )
    finally:
        conn.close()

    output_dir = os.path.join(TMP_ROOT, "export_json")
    shutil.rmtree(output_dir, ignore_errors=True)
    os.makedirs(output_dir, exist_ok=True)
    result = tools.call_tool("export", {"format": "json", "output": output_dir})
    payload = _parse_result(result)
    try:
        assert payload["count"] == 1
        assert os.path.exists(os.path.join(output_dir, "%s.json" % video_id))
    finally:
        shutil.rmtree(output_dir, ignore_errors=True)
