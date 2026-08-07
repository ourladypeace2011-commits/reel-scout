"""Structured fields out of a VLM reply, and the backfill that recovers them.

The bug these guard against was silent: both backends returned
`FrameDescription(description=text)`, so `text_in_frame` was "" for every frame
ever described, `collect_captions` returned nothing, and `ocr_captions` stayed
empty -- with no error anywhere. `test_ocr.py` did not catch it because it mocks
`get_keyframes_with_descriptions` and hands back rows whose `text_in_frame` is
already populated, i.e. it starts testing downstream of the break. The backend
tests here close that gap: they assert on what `describe_frame` actually
returns.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch

from reel_scout import db, ocr
from reel_scout.vision import parse
from reel_scout.vision.ollama import OllamaVLM
from reel_scout.vision.omlx import OmlxVLM


# --- prose reader -----------------------------------------------------------

def test_prose_reads_quoted_text_when_marked_as_overlaid():
    d = ('Black background with centered Chinese text overlay "如果你有在關注 AI" '
         "in a dark box. Minimalist mood.")
    assert parse.text_from_prose(d) == "如果你有在關注 AI"


def test_prose_joins_several_spans_in_order():
    d = ('On-screen text: "客訴回信例子" (top) and "單一 prompt 寫法" (bottom). '
         "Mood: professional.")
    assert parse.text_from_prose(d) == "客訴回信例子 / 單一 prompt 寫法"


def test_prose_drops_in_world_text():
    """A shop front is text the camera found, not text the editor added, and
    ocr_captions means the latter. No indicator word -> nothing."""
    d = ('The frame shows the exterior of a ramen shop with large Japanese '
         'characters and the words "Cyun Ramen Shop" in English. Motorcycles '
         "are parked outside.")
    assert parse.text_from_prose(d) == ""


def test_prose_empty_when_frame_has_no_text():
    d = ("A person holds a vinyl record cover against a backdrop of turntables. "
         "No on-screen text; mood is casual.")
    assert parse.text_from_prose(d) == ""


def test_prose_handles_curly_and_cjk_quotes():
    assert parse.text_from_prose('the caption reads “JUST GET STARTED.”') == \
        "JUST GET STARTED."
    assert parse.text_from_prose("字卡「開場白」overlaid at the top") == "開場白"


def test_prose_dedupes_a_caption_repeated_in_one_frame():
    d = 'Text overlay "SALE" at top; the same "sale" appears again below.'
    assert parse.text_from_prose(d) == "SALE"


def test_prose_ignores_an_unclosed_quote():
    """An unterminated quote must not swallow the rest of the description."""
    d = 'The overlay text is "' + ("x" * 400)
    assert parse.text_from_prose(d) == ""


# --- structured tail --------------------------------------------------------

def test_tail_is_preferred_and_stripped_from_the_prose():
    raw = ("A dark frame with a glowing cube.\n"
           "ON_SCREEN_TEXT: RAG 流程 #1：建立索引\n"
           "OBJECTS: cube, envelope, chart")
    prose, text, objects = parse.parse_frame_reply(raw)
    assert text == "RAG 流程 #1：建立索引"
    assert objects == ["cube", "envelope", "chart"]
    assert "ON_SCREEN_TEXT" not in prose
    assert "OBJECTS" not in prose
    assert prose.startswith("A dark frame")


def test_tail_none_is_trusted_and_does_not_fall_back_to_prose():
    """NONE is the model answering, not the model staying silent -- a quoted
    span left in the prose must not override it."""
    raw = ('A poster reading "MORO" hangs on the wall.\n'
           "ON_SCREEN_TEXT: NONE\n"
           "OBJECTS: poster, wall")
    _, text, objects = parse.parse_frame_reply(raw)
    assert text == ""
    assert objects == ["poster", "wall"]


def test_missing_tail_falls_back_to_prose():
    raw = 'The text "BUT THE PROBLEM" is displayed in the center.'
    prose, text, objects = parse.parse_frame_reply(raw)
    assert text == "BUT THE PROBLEM"
    assert objects == []
    assert prose == raw


def test_tail_tolerates_markdown_decoration():
    raw = ("Some frame.\n"
           "**ON-SCREEN TEXT:** Hello world\n"
           "- OBJECTS: a, b")
    _, text, objects = parse.parse_frame_reply(raw)
    assert text == "Hello world"
    assert objects == ["a", "b"]


def test_tail_words_inside_the_prose_are_not_a_tail():
    """The regexes are line-anchored so a sentence mentioning on-screen text
    cannot be mistaken for the structured answer."""
    raw = 'This frame has on-screen text: the overlay reads "GO".'
    _, text, _ = parse.parse_frame_reply(raw)
    assert text == "GO"


def test_parse_never_raises():
    for bad in (None, "", "\x00", "ON_SCREEN_TEXT:"):
        prose, text, objects = parse.parse_frame_reply(bad)
        assert isinstance(prose, str) and isinstance(text, str)
        assert isinstance(objects, list)


# --- backends: the assertion the original tests never made ------------------

def _ollama_reply(text):
    resp = MagicMock()
    resp.read.return_value = json.dumps({"response": text}).encode("utf-8")
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: False
    return resp


def _omlx_reply(text):
    body = {"choices": [{"message": {"content": text}}]}
    resp = MagicMock()
    resp.read.return_value = json.dumps(body).encode("utf-8")
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: False
    return resp


def test_ollama_backend_populates_text_in_frame():
    raw = ("A title card.\nON_SCREEN_TEXT: 開場白\nOBJECTS: card, background")
    with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
        f.write(b"\xff\xd8\xff")
        f.flush()
        with patch("reel_scout.vision.ollama.urllib.request.urlopen",
                   return_value=_ollama_reply(raw)):
            desc = OllamaVLM("http://x", "m").describe_frame(f.name)
    assert desc.text_in_frame == "開場白"
    assert desc.objects == ["card", "background"]
    assert "ON_SCREEN_TEXT" not in desc.description


def test_omlx_backend_populates_text_in_frame():
    raw = 'The overlay text reads "SOUND DESIGN" at the bottom.'
    with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
        f.write(b"\xff\xd8\xff")
        f.flush()
        with patch("reel_scout.vision.omlx.urllib.request.urlopen",
                   return_value=_omlx_reply(raw)):
            desc = OmlxVLM("http://x", "m").describe_frame(f.name)
    assert desc.text_in_frame == "SOUND DESIGN"


# --- backfill ---------------------------------------------------------------

def _temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn, path


def _seed(conn, descriptions, text_in_frame=""):
    vid = db.upsert_video(
        conn, platform="youtube", platform_id="bf",
        url="https://youtube.com/shorts/bf", title="T", duration_sec=20.0,
    )
    kf_ids = db.save_keyframes(conn, vid, [
        {"frame_index": i, "timestamp_sec": float(i),
         "file_path": "/f%d.jpg" % i, "strategy": "interval"}
        for i in range(len(descriptions))
    ])
    for kf_id, d in zip(kf_ids, descriptions):
        db.save_vision_description(
            conn, kf_id, description=d, objects_json="[]",
            text_in_frame=text_in_frame, vlm_backend="ollama", vlm_model="m",
        )
    return vid


def test_backfill_fills_frames_and_writes_captions():
    conn, path = _temp_db()
    try:
        vid = _seed(conn, [
            'Text overlay "FIRST CARD" on black.',
            "A wide shot of a street. No on-screen text.",
            'The caption reads "SECOND CARD".',
        ])
        r = ocr.backfill_from_descriptions(conn)
        assert r["scanned"] == 3
        assert r["filled"] == 2
        assert r["videos"] == 1
        caps = [c["text"] for c in db.get_ocr_captions(conn, vid)]
        assert caps == ["FIRST CARD", "SECOND CARD"]
        assert r["captions"] == 2
    finally:
        conn.close()
        os.unlink(path)


def test_backfill_dry_run_writes_nothing():
    conn, path = _temp_db()
    try:
        vid = _seed(conn, ['Text overlay "ONLY CARD".'])
        r = ocr.backfill_from_descriptions(conn, dry_run=True)
        assert r["filled"] == 1
        assert r["captions"] == 1
        assert db.get_ocr_captions(conn, vid) == []
        row = conn.execute(
            "SELECT text_in_frame FROM vision_descriptions"
        ).fetchone()
        assert (row["text_in_frame"] or "") == ""
    finally:
        conn.close()
        os.unlink(path)


def test_backfill_never_overwrites_a_real_read():
    conn, path = _temp_db()
    try:
        _seed(conn, ['Text overlay "FROM PROSE".'], text_in_frame="FROM BACKEND")
        r = ocr.backfill_from_descriptions(conn)
        assert r["scanned"] == 0
        assert r["filled"] == 0
        row = conn.execute(
            "SELECT text_in_frame FROM vision_descriptions"
        ).fetchone()
        assert row["text_in_frame"] == "FROM BACKEND"
    finally:
        conn.close()
        os.unlink(path)


def test_backfill_is_idempotent():
    conn, path = _temp_db()
    try:
        vid = _seed(conn, ['Text overlay "CARD".', 'The caption reads "TWO".'])
        first = ocr.backfill_from_descriptions(conn)
        second = ocr.backfill_from_descriptions(conn)
        assert first["filled"] == 2
        assert second["filled"] == 0          # nothing left empty to fill
        caps = [c["text"] for c in db.get_ocr_captions(conn, vid)]
        assert caps == ["CARD", "TWO"]        # not doubled
    finally:
        conn.close()
        os.unlink(path)
