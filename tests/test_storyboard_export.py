"""Storyboard project export (roadmap §Phase 6D/6E)."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from unittest.mock import patch

from reel_scout import config, db
from reel_scout.export import storyboard
from reel_scout.export.storyboard import REF_PREFIX, build_project, export_storyboard
from reel_scout.shots import Shot


def _video(**kw):
    base = {"id": "v1", "url": "https://y/abc", "title": "Someone Else's Clip",
            "file_path": None}
    base.update(kw)
    return base


SHOTS = [Shot(0, 0.0, 5.0, 5.0), Shot(1, 5.0, 12.0, 7.0)]
FRAMES = [{"id": 11, "timestamp_sec": 2.4}, {"id": 22, "timestamp_sec": 8.5}]
VO = [{"start": 1.0, "end": 9.0, "text": "a line that straddles the cut"}]
CAPS = [{"timestamp_sec": 6.0, "text": "TITLE CARD"}]
DESCS = {11: "a close-up of a hand", 22: "a wide shot of the street"}


def _project(**kw):
    return build_project(_video(), SHOTS, FRAMES, VO, CAPS,
                         descriptions=DESCS, **kw)


def test_one_cut_per_shot_in_order():
    p = _project()
    assert [c["id"] for c in p["cuts"]] == ["c1", "c2"]
    assert p["films"][0]["id"] == p["cuts"][0]["filmId"]


def test_every_cut_carries_the_source_url_and_its_timecode():
    # 🔴 The provenance rule. Somebody opening this file six weeks later has to
    # be able to tell it is a teardown without being told.
    for c in _project()["cuts"]:
        assert c["note"].startswith(REF_PREFIX)
        assert "https://y/abc" in c["note"]
        assert "–" in c["note"], "the in/out timecode has to be there too"


def test_the_project_title_and_client_both_say_it_is_a_reference():
    p = _project()
    assert p["meta"]["title"].startswith(REF_PREFIX)
    assert "teardown" in p["meta"]["client"]


def test_shot_size_comes_from_the_description_when_it_is_derivable():
    p = _project()
    assert p["cuts"][0]["shot"] == "CU"      # "a close-up of a hand"
    assert p["cuts"][1]["shot"] == "LS"      # "a wide shot of the street"


def test_shot_size_is_blank_rather_than_guessed():
    p = build_project(_video(), SHOTS, FRAMES, VO, CAPS,
                      descriptions={11: "a dimly lit alley", 22: "a room"})
    assert [c["shot"] for c in p["cuts"]] == ["", ""]


def test_vo_lands_under_every_shot_it_covers():
    cuts = _project()["cuts"]
    assert "straddles" in cuts[0]["vo"]
    assert "straddles" in cuts[1]["vo"]


def test_on_screen_text_goes_to_sup_not_vo():
    cuts = _project()["cuts"]
    assert cuts[1]["sup"] == "TITLE CARD"
    assert "TITLE CARD" not in cuts[1]["vo"]


def test_sub_second_cuts_omit_sec_rather_than_assert_zero():
    # The app totals `sec` in the page footer; a row reading "0 seconds" is
    # worse than a row with no duration.
    p = build_project(_video(), [Shot(0, 0.0, 0.3, 0.3)], [], [], [])
    assert "sec" not in p["cuts"][0]
    p2 = build_project(_video(), [Shot(0, 0.0, 4.0, 4.0)], [], [], [])
    assert p2["cuts"][0]["sec"] == 4


def test_fields_only_the_set_can_fill_are_left_unset():
    # Estimating focal length from finished footage is a guess, and a guessed
    # number in a PPM is indistinguishable from a measured one downstream.
    c = _project()["cuts"][0]
    assert c["imageRef"] is None
    for absent in ("gear", "scoutRef", "scoutMeta"):
        assert absent not in c


def test_each_cut_is_its_own_group():
    # Consecutive-shot grouping is a judgement this export has no basis for.
    ids = [c["groupId"] for c in _project()["cuts"]]
    assert len(set(ids)) == len(ids)


def test_aspect_is_omitted_when_it_could_not_be_measured():
    assert "aspect" not in _project()
    assert _project(aspect="9:16")["aspect"] == "9:16"


def test_representative_frame_id_is_recorded_for_lookup():
    assert "frame id 11" in _project()["cuts"][0]["note"]


def _temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn, path


def test_export_skips_a_clip_with_no_shot_table():
    # One cut spanning four minutes is not a storyboard, and emitting it would
    # look like the export worked.
    conn, path = _temp_db()
    out = tempfile.mkdtemp()
    try:
        vid = db.upsert_video(conn, "youtube", "x1", "https://y/x1", title="No shots")
        conn.execute("UPDATE videos SET status='analyzed' WHERE id=?", (vid,))
        conn.commit()
        assert export_storyboard(conn, out) == 0
        assert os.listdir(out) == []
    finally:
        conn.close()
        os.unlink(path)


def test_export_writes_valid_json_for_a_clip_with_shots():
    conn, path = _temp_db()
    out = tempfile.mkdtemp()
    try:
        vid = db.upsert_video(conn, "youtube", "x2", "https://y/x2", title="Has shots")
        conn.execute("UPDATE videos SET status='analyzed' WHERE id=?", (vid,))
        conn.commit()
        db.save_shots(conn, vid, SHOTS)
        assert export_storyboard(conn, out, video_id=vid) == 1
        with open(os.path.join(out, "%s.project.json" % vid), encoding="utf-8") as f:
            p = json.load(f)
        assert len(p["cuts"]) == 2
        assert p["mode"] == "ppm"
        assert all(c["note"].startswith(REF_PREFIX) for c in p["cuts"])
    finally:
        conn.close()
        os.unlink(path)


def test_timecode_format_is_readable_against_a_scrub_bar():
    assert storyboard._tc(0) == "0:00.0"
    assert storyboard._tc(75.25) == "1:15.2"


def test_aspect_resolves_a_relative_media_path(monkeypatch, tmp_path):
    # Stored paths are mostly `./data/videos/...`, which exists only in the
    # checkout that wrote them. Reading that raw made every export warn "aspect
    # unknown (media missing)" and silently fall back to the app's 16:9 default
    # — wrong for a vertical clip, and nothing on screen said the check itself
    # had failed rather than the media being gone.
    root = str(tmp_path)
    videos = os.path.join(root, "data", "videos")
    os.makedirs(videos)
    open(os.path.join(videos, "v.mp4"), "wb").write(b"0")
    monkeypatch.setattr(config, "DATA_DIR", os.path.join(root, "data"))
    elsewhere = tmp_path / "elsewhere"; elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert not os.path.exists("./data/videos/v.mp4")

    with patch("reel_scout.export.storyboard.ffprobe.probe_dimensions",
               return_value=(1080, 1920)) as probe:
        assert storyboard._aspect("./data/videos/v.mp4") == "9:16"
    # ffprobe has to be handed something openable, not the stored string.
    assert os.path.isabs(probe.call_args[0][0])


def test_aspect_is_none_when_the_media_really_is_gone():
    assert storyboard._aspect("/definitely/not/here.mp4") is None
    assert storyboard._aspect(None) is None
