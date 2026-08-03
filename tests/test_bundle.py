"""Take-home bundle: one self-contained file per reel."""
from __future__ import annotations

import json
import os
import re
import sqlite3

from reel_scout import bundle, config, db


def _tiny_jpeg(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:  # enough of a JPEG for base64 embedding
        f.write(b"\xff\xd8\xff\xdb" + b"\x00" * 32 + b"\xff\xd9")


def _seed(conn, video_bytes=b"\x00\x01\x02\x03", title="Reel One", text_full="測試逐字稿", segments_json=None):
    vid = db.upsert_video(
        conn, platform="youtube", platform_id="bundle1",
        url="https://youtube.com/shorts/bundle1", title=title, duration_sec=12.0,
    )
    vpath = os.path.join(config.VIDEOS_DIR, "bundle1.mp4")
    os.makedirs(config.VIDEOS_DIR, exist_ok=True)
    with open(vpath, "wb") as f:
        f.write(video_bytes)
    conn.execute("UPDATE videos SET file_path=?, status='analyzed' WHERE id=?", (vpath, vid))

    kf = os.path.join(config.KEYFRAMES_DIR, vid, "f0.jpg")
    _tiny_jpeg(kf)
    db.save_keyframes(conn, vid, [{"frame_index": 0, "timestamp_sec": 1.0,
                                   "file_path": kf, "strategy": "scene"}])
    db.save_transcript(conn, vid, language="zh", text_full=text_full,
                       segments_json=(segments_json if segments_json is not None
                                      else json.dumps([{"start": 0.0, "end": 2.0,
                                                        "text": "測試逐字稿"}])),
                       whisper_model="test", duration_sec=12.0)
    db.save_analysis(conn, vid, summary="摘要", topics_json='["a"]', hooks_json="{}",
                     style_json="{}", engagement_signals_json="{}",
                     full_json=json.dumps({"summary": "摘要",
                                           "content_structure": "hook-body-cta"}))
    conn.commit()
    return vid


def test_slugify_handles_cjk_and_collisions():
    assert bundle.slugify("Latte art in STARBUCKS", "x") == "latte-art-in-starbucks"
    assert bundle.slugify("", "fallback") == "fallback"
    # CJK survives (\w is unicode-aware), so filenames stay readable
    assert "剪輯" in bundle.slugify("不會剪輯也能做", "x")


def test_reel_page_is_self_contained(temp_db):
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    vid = _seed(conn)
    res = bundle.build_reel_page(conn, vid)
    conn.close()

    assert res["ok"], res.get("reason")
    html = res["html"]
    # every asset inlined
    assert "data:video/mp4;base64," in html
    assert "data:image/jpeg;base64," in html
    assert "data:font/woff2;base64," in html
    # nothing points back at a server
    assert 'src="/keyframe/' not in html
    assert "/api/stream/" not in html
    assert "url(/font/" not in html
    # waveform travels inside the page (file:// blocks fetch)
    boot = json.loads(re.search(r'<script id="boot"[^>]*>(.*?)</script>', html, re.S).group(1))
    assert "peaks" in boot


def test_oversized_video_is_skipped_with_a_reason(temp_db):
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    vid = _seed(conn, video_bytes=b"\x00" * 4096)
    res = bundle.build_reel_page(conn, vid, max_bytes=1024)
    conn.close()
    assert not res["ok"]
    assert "too big to inline" in res["reason"]


def test_build_bundle_writes_files_and_index(temp_db, tmp_path):
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    _seed(conn)
    out = str(tmp_path / "course")
    summary = bundle.build_bundle(conn, out)
    conn.close()

    assert len(summary["written"]) == 1
    entry = summary["written"][0]
    assert os.path.exists(os.path.join(out, entry["file"]))
    index = open(os.path.join(out, "index.html"), encoding="utf-8").read()
    assert entry["file"] in index
    # the index is self-contained too
    assert "data:font/woff2;base64," in index


def test_bundle_reports_skips_without_aborting(temp_db, tmp_path):
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    _seed(conn, video_bytes=b"\x00" * 4096)
    summary = bundle.build_bundle(conn, str(tmp_path / "c2"), max_bytes=1024)
    conn.close()
    # one skipped, still wrote an index rather than blowing up the whole export
    assert summary["written"] == []
    assert len(summary["skipped"]) == 1
    assert os.path.exists(os.path.join(str(tmp_path / "c2"), "index.html"))


def test_bundle_pages_link_back_to_index(temp_db):
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    vid = _seed(conn)
    with_back = bundle.build_reel_page(conn, vid, back_href="index.html")
    without = bundle.build_reel_page(conn, vid)
    conn.close()
    assert 'class="back" href="index.html"' in with_back["html"]
    # a standalone page gets no dangling link
    assert 'class="back"' not in without["html"]


def test_a_silent_reel_page_still_says_why(temp_db):
    """The bundle is the artifact a student keeps after the course -- the one
    file nobody can correct in person later. It inherits the inspector renderer,
    so this test exists to prove that inheritance actually holds."""
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    try:
        vid = _seed(conn, text_full="", segments_json="[]")
        page = bundle.build_reel_page(conn, vid)
    finally:
        conn.close()
    assert page["ok"] is True
    assert 'data-i18n="noTranscriptNote"' in page["html"]
