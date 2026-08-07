"""Stored media paths must resolve from any working directory.

``DATA_DIR`` defaults to ``./data``, so rows written from the repo root recorded
``./data/videos/x.mp4`` and rows written elsewhere recorded absolute paths — a
live database holds both. Resolving either with ``os.path.exists()`` from a
different cwd reported the file as missing, and the pipeline read that as "not
downloaded yet" and fetched it again.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from reel_scout import config, db
from reel_scout.utils import paths as media_paths


@pytest.fixture
def data_tree(temp_db, tmp_path):
    """A configured data root holding one real video file."""
    videos = os.path.join(config.DATA_DIR, "videos")
    os.makedirs(videos, exist_ok=True)
    video = os.path.join(videos, "yt_abc.mp4")
    with open(video, "wb") as f:
        f.write(b"\x00")
    return temp_db, video


def _conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# --- resolution -------------------------------------------------------------

def test_portable_form_resolves(data_tree):
    _, video = data_tree
    assert media_paths.resolve_media_path("videos/yt_abc.mp4") == os.path.realpath(video)


def test_legacy_dot_slash_form_resolves(data_tree):
    """The shape written whenever the process ran from the repo root."""
    _, video = data_tree
    root_name = os.path.basename(config.DATA_DIR)
    stored = "./%s/videos/yt_abc.mp4" % root_name
    assert media_paths.resolve_media_path(stored) == os.path.realpath(video)


def test_absolute_form_passes_through(data_tree):
    _, video = data_tree
    assert media_paths.resolve_media_path(video) == video


def test_resolution_survives_a_change_of_cwd(data_tree, tmp_path, monkeypatch):
    """The defect itself: right from the repo root, silently wrong elsewhere."""
    _, video = data_tree
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert os.path.exists("videos/yt_abc.mp4") is False   # the naive check
    assert media_paths.exists("videos/yt_abc.mp4") is True
    assert media_paths.resolve_media_path("videos/yt_abc.mp4") == os.path.realpath(video)


def test_moved_file_found_by_basename(data_tree):
    """A path pointing at an old data dir still resolves through VIDEOS_DIR."""
    _, video = data_tree
    stale = "/some/old/data/videos/yt_abc.mp4"
    assert media_paths.resolve_media_path(stale) == os.path.realpath(video)


def test_missing_file_names_a_checkable_location(data_tree):
    out = media_paths.resolve_media_path("videos/nope.mp4")
    assert out.endswith(os.path.join("videos", "nope.mp4"))
    assert os.path.isabs(out)
    assert media_paths.exists("videos/nope.mp4") is False


def test_empty_inputs_are_safe(data_tree):
    assert media_paths.resolve_media_path("") == ""
    assert media_paths.resolve_media_path(None) == ""
    assert media_paths.to_storage_path(None) == ""
    assert media_paths.exists(None) is False


# --- storage form -----------------------------------------------------------

def test_to_storage_path_is_relative_to_the_data_root(data_tree):
    _, video = data_tree
    assert media_paths.to_storage_path(video) == "videos/yt_abc.mp4"


def test_to_storage_path_keeps_outside_paths_absolute(data_tree, tmp_path):
    outside = tmp_path / "outside" / "clip.mp4"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"\x00")
    assert media_paths.to_storage_path(str(outside)) == os.path.realpath(str(outside))


def test_storage_round_trip(data_tree):
    _, video = data_tree
    stored = media_paths.to_storage_path(video)
    assert media_paths.resolve_media_path(stored) == os.path.realpath(video)


# --- migration --------------------------------------------------------------

def test_normalize_rewrites_legacy_rows(data_tree):
    db_path, _ = data_tree
    conn = _conn(db_path)
    root_name = os.path.basename(config.DATA_DIR)
    vid = db.upsert_video(
        conn, platform="youtube", platform_id="abc", url="u",
        file_path="./%s/videos/yt_abc.mp4" % root_name,
    )

    changed, missing = db.normalize_media_paths(conn)

    assert changed == 1
    assert missing == []
    row = conn.execute("SELECT file_path FROM videos WHERE id=?", (vid,)).fetchone()
    assert row["file_path"] == "videos/yt_abc.mp4"
    conn.close()


def test_normalize_leaves_unresolvable_rows_alone(data_tree):
    """Rewriting a path we cannot verify would turn a recoverable row into a
    confidently wrong one."""
    db_path, _ = data_tree
    conn = _conn(db_path)
    vid = db.upsert_video(
        conn, platform="youtube", platform_id="gone", url="u2",
        file_path="./data/videos/vanished.mp4",
    )

    changed, missing = db.normalize_media_paths(conn)

    assert changed == 0
    assert len(missing) == 1
    row = conn.execute("SELECT file_path FROM videos WHERE id=?", (vid,)).fetchone()
    assert row["file_path"] == "./data/videos/vanished.mp4"
    conn.close()


def test_normalize_dry_run_writes_nothing(data_tree):
    db_path, _ = data_tree
    conn = _conn(db_path)
    root_name = os.path.basename(config.DATA_DIR)
    stored = "./%s/videos/yt_abc.mp4" % root_name
    vid = db.upsert_video(
        conn, platform="youtube", platform_id="abc", url="u", file_path=stored,
    )

    changed, _ = db.normalize_media_paths(conn, dry_run=True)

    assert changed == 1
    row = conn.execute("SELECT file_path FROM videos WHERE id=?", (vid,)).fetchone()
    assert row["file_path"] == stored
    conn.close()
