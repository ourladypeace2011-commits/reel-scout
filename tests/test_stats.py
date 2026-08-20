"""Corpus statistics (roadmap 3D)."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile

from reel_scout import db, stats


def _fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn, path


def _seed(conn, pid, uploader, full, score=None, model=None):
    vid = db.upsert_video(conn, platform="youtube", platform_id=pid,
                          url="https://y/%s" % pid, title=pid, uploader=uploader)
    db.save_analysis(conn, vid, summary="", topics_json="[]",
                     hooks_json=json.dumps(full.get("hook", {})),
                     style_json=json.dumps(full.get("style", {})),
                     engagement_signals_json="{}", full_json=json.dumps(full))
    if score is not None:
        conn.execute(
            "INSERT INTO scores (video_id, hook_strength, visual_storytelling, "
            "pacing, structure, overall, model_used) VALUES (?,?,?,?,?,?,?)",
            (vid, *score, model))
        conn.commit()
    return vid


def _corpus(conn):
    _seed(conn, "a", "Chan One",
          {"content_type": "educational", "content_structure": "listicle",
           "style": {"format": "talking_head", "pacing": "fast"},
           "hook": {"opening_type": "question", "cta_type": "follow"}},
          score=(8.0, 7.0, 8.0, 7.0, 7.5))
    _seed(conn, "b", "Chan One",
          {"content_type": "educational", "content_structure": "hook-body-cta",
           "style": {"format": "talking_head", "pacing": "medium"},
           "hook": {"opening_type": "statement", "cta_type": "follow"}},
          score=(6.0, 6.0, 6.0, 6.0, 6.0))
    _seed(conn, "c", "Chan Two",
          {"content_type": "story", "content_structure": "story-arc",
           "style": {"format": "vlog", "pacing": "slow"},
           "hook": {"opening_type": "visual", "cta_type": "visit"}},
          score=(4.0, 9.0, 5.0, 8.0, 6.5))


def test_global_distributions_and_score_aggregates():
    conn, path = _fresh_db()
    try:
        _corpus(conn)
        s = stats.compute_stats(conn)
        assert s["total_videos"] == 3
        assert s["analyzed_videos"] == 3
        assert s["tag_distributions"]["content_type"] == {"educational": 2, "story": 1}
        assert s["tag_distributions"]["style_format"]["talking_head"] == 2
        assert s["tag_distributions"]["content_structure"]["listicle"] == 1
        ov = s["score_aggregates"]["overall"]
        assert ov["count"] == 3
        assert ov["min"] == 6.0 and ov["max"] == 7.5
        assert abs(ov["avg"] - round((7.5 + 6.0 + 6.5) / 3, 2)) < 1e-9
    finally:
        conn.close()
        os.unlink(path)


def test_channel_scope_filters_by_uploader():
    conn, path = _fresh_db()
    try:
        _corpus(conn)
        s = stats.compute_stats(conn, channel="Chan One")
        assert s["total_videos"] == 2
        assert s["tag_distributions"]["content_type"] == {"educational": 2}
        assert s["score_aggregates"]["overall"]["count"] == 2
        assert s["score_aggregates"]["overall"]["max"] == 7.5
    finally:
        conn.close()
        os.unlink(path)


def test_empty_corpus_has_zero_counts_not_crash():
    conn, path = _fresh_db()
    try:
        s = stats.compute_stats(conn)
        assert s["total_videos"] == 0
        assert s["tag_distributions"]["content_type"] == {}
        assert s["score_aggregates"]["overall"]["avg"] is None
        assert s["score_aggregates"]["overall"]["count"] == 0
        # formatter must not crash on empty
        assert "Corpus stats" in stats.format_stats(s)
    finally:
        conn.close()
        os.unlink(path)


def test_csv_export_long_format():
    conn, path = _fresh_db()
    try:
        _corpus(conn)
        s = stats.compute_stats(conn)
        out = os.path.join(os.path.dirname(path), "stats_out.csv")
        n = stats.write_csv(s, out)
        with open(out) as f:
            content = f.read()
        assert content.startswith("metric,dimension,key,value")
        assert "tag,content_type,educational,2" in content
        assert "score,overall,count,3" in content
        assert n > 0
        os.unlink(out)
    finally:
        conn.close()
        os.unlink(path)


# --- grouping by model_used ------------------------------------------------
#
# A craft score is only meaningful against the model that produced it. The
# corpus below is the case the course actually creates: students hand in
# agent-scored videos (`ingest score --model ...`, stamped `agent:<model>`)
# alongside locally-scored ones. The two rulers do not overlap at all, so the
# pooled mean lands in the empty gap between them — a number that describes no
# video in the library.

AGENT = "agent:claude-opus-4-8"
LOCAL = "omlx:qwen3-vl:8b"

_AGENT_OVERALLS = [8.5, 8.0, 8.3]     # avg 8.27
_LOCAL_OVERALLS = [5.0, 5.5, 5.2]     # avg 5.23
_POOLED_AVG = 6.75                    # avg of all six


def _flat():
    """One uniform tag payload — these tests are about the score grouping, so
    the tag distributions are deliberately not the variable."""
    return {"content_type": "educational", "content_structure": "hook-body-cta",
            "style": {"format": "talking_head", "pacing": "fast"},
            "hook": {"opening_type": "question", "cta_type": "follow"}}


def _mixed_corpus(conn):
    """Six videos, two scoring sources, no overlap between the two scales."""
    seeded = []
    for i, ov in enumerate(_AGENT_OVERALLS):
        vid = _seed(conn, "ag%d" % i, "Chan Agent", _flat(),
                    score=(ov, ov, ov, ov, ov), model=AGENT)
        seeded.append((vid, ov, AGENT))
    for i, ov in enumerate(_LOCAL_OVERALLS):
        vid = _seed(conn, "lo%d" % i, "Chan Local", _flat(),
                    score=(ov, ov, ov, ov, ov), model=LOCAL)
        seeded.append((vid, ov, LOCAL))
    return seeded


def test_mixed_corpus_is_grouped_instead_of_blended():
    conn, path = _fresh_db()
    try:
        _mixed_corpus(conn)
        s = stats.compute_stats(conn)

        # (a) The blend is real and it is misleading: the pooled mean is more
        # than a full point away from every single video in the corpus.
        pooled = s["score_aggregates"]["overall"]
        assert pooled["count"] == 6
        assert pooled["avg"] == _POOLED_AVG
        assert pooled["min"] == 5.0 and pooled["max"] == 8.5
        every = _AGENT_OVERALLS + _LOCAL_OVERALLS
        assert all(abs(v - pooled["avg"]) > 1.0 for v in every), (
            "the pooled mean should sit in the gap between the two scales")

        # (b) Before this change, that number was the only thing `stats` said
        # about the corpus. Now the mixing is stated and the scales are split.
        assert s["mixed_score_sources"] is True
        assert s["score_sources"] == {AGENT: 3, LOCAL: 3}

        by_model = s["score_aggregates_by_model"]
        assert set(by_model) == {AGENT, LOCAL}
        assert by_model[AGENT]["overall"]["avg"] == 8.27
        assert by_model[AGENT]["overall"]["count"] == 3
        assert by_model[AGENT]["overall"]["min"] == 8.0
        assert by_model[AGENT]["overall"]["max"] == 8.5
        assert by_model[LOCAL]["overall"]["avg"] == 5.23
        assert by_model[LOCAL]["overall"]["count"] == 3
        assert by_model[LOCAL]["overall"]["min"] == 5.0
        assert by_model[LOCAL]["overall"]["max"] == 5.5

        # (c) Each grouped mean does describe its own group — within 0.3 of
        # every member, against >1.0 for the pooled one.
        for src, overalls in ((AGENT, _AGENT_OVERALLS), (LOCAL, _LOCAL_OVERALLS)):
            avg = by_model[src]["overall"]["avg"]
            assert all(abs(v - avg) < 0.3 for v in overalls)

        # Every craft dimension is grouped, not just `overall`.
        for col in stats.SCORE_COLUMNS:
            assert by_model[AGENT][col]["count"] == 3
            assert by_model[LOCAL][col]["count"] == 3
    finally:
        conn.close()
        os.unlink(path)


def test_mixed_corpus_formatter_says_the_average_is_pooled():
    conn, path = _fresh_db()
    try:
        _mixed_corpus(conn)
        out = stats.format_stats(stats.compute_stats(conn))
        assert "2 scoring sources" in out
        assert "POOLED across 2 sources" in out
        assert "%s n=3" % AGENT in out and "%s n=3" % LOCAL in out
        # Both per-source blocks, with their own (comparable) averages.
        assert "Score aggregates · %s" % AGENT in out
        assert "Score aggregates · %s" % LOCAL in out
        assert "8.3" in out and "5.2" in out
    finally:
        conn.close()
        os.unlink(path)


def test_single_source_corpus_is_not_flagged_as_mixed():
    """No false alarm: one ruler means the average is just the average, and the
    source is named rather than warned about."""
    conn, path = _fresh_db()
    try:
        for i, ov in enumerate(_LOCAL_OVERALLS):
            _seed(conn, "lo%d" % i, "Chan Local", _flat(),
                  score=(ov, ov, ov, ov, ov), model=LOCAL)
        s = stats.compute_stats(conn)
        assert s["mixed_score_sources"] is False
        assert s["score_sources"] == {LOCAL: 3}
        assert s["score_aggregates_by_model"][LOCAL]["overall"]["avg"] == 5.23
        out = stats.format_stats(s)
        assert "scoring sources in this corpus" not in out
        assert "POOLED" not in out
        assert "source: %s" % LOCAL in out
    finally:
        conn.close()
        os.unlink(path)


def test_null_and_empty_model_used_share_one_bucket():
    """A row with `model_used` NULL and one with '' both mean the same thing —
    origin never recorded — so they must not read as two different scales."""
    conn, path = _fresh_db()
    try:
        _seed(conn, "n1", "C", _flat(), score=(7.0,) * 5, model=None)
        _seed(conn, "n2", "C", _flat(), score=(7.0,) * 5, model="")
        s = stats.compute_stats(conn)
        assert s["score_sources"] == {stats.UNKNOWN_MODEL: 2}
        assert s["mixed_score_sources"] is False
    finally:
        conn.close()
        os.unlink(path)


def test_channel_scope_also_scopes_the_grouping():
    conn, path = _fresh_db()
    try:
        _mixed_corpus(conn)
        s = stats.compute_stats(conn, channel="Chan Agent")
        assert s["score_sources"] == {AGENT: 3}
        assert s["mixed_score_sources"] is False
        assert set(s["score_aggregates_by_model"]) == {AGENT}
        assert s["score_aggregates"]["overall"]["avg"] == 8.27
    finally:
        conn.close()
        os.unlink(path)


def test_per_video_reads_are_unchanged_by_the_grouping():
    """Regression guard for acceptance #3: grouping is an aggregate-layer
    change. A single video's score must read back exactly as written, with its
    origin intact, whichever model wrote it."""
    conn, path = _fresh_db()
    try:
        seeded = _mixed_corpus(conn)
        stats.compute_stats(conn)  # aggregating must not mutate anything
        for vid, ov, model in seeded:
            row = db.get_score(conn, vid)
            assert row is not None
            assert row["overall"] == ov
            assert row["hook_strength"] == ov
            assert row["visual_storytelling"] == ov
            assert row["pacing"] == ov
            assert row["structure"] == ov
            assert row["model_used"] == model
    finally:
        conn.close()
        os.unlink(path)


def test_pooled_csv_rows_are_unchanged_and_grouped_rows_are_added():
    """The old four-column schema still holds, the old `score,*` rows still say
    exactly what they said before, and the grouping rides along beside them."""
    conn, path = _fresh_db()
    try:
        _mixed_corpus(conn)
        s = stats.compute_stats(conn)
        out = os.path.join(os.path.dirname(path), "stats_grouped.csv")
        stats.write_csv(s, out)
        with open(out, encoding="utf-8") as f:
            content = f.read()
        assert content.startswith("metric,dimension,key,value")
        # unchanged pooled rows
        assert "score,overall,avg,6.75" in content
        assert "score,overall,count,6" in content
        # new rows
        assert "count,score_source,%s,3" % AGENT in content
        assert "count,score_source,%s,3" % LOCAL in content
        assert "flag,score_source,mixed,1" in content
        assert "score_by_model,%s,overall.avg,8.27" % AGENT in content
        assert "score_by_model,%s,overall.avg,5.23" % LOCAL in content
        os.unlink(out)
    finally:
        conn.close()
        os.unlink(path)


def test_csv_grouping_survives_a_non_ascii_model_label():
    """`--model` is free text and the course is Chinese-language, so a model
    label can be CJK. Assert the content that comes back, not just that the
    write returned — an encoding fault is invisible to an exit code."""
    conn, path = _fresh_db()
    try:
        cjk = "agent:自建視覺模型"
        _seed(conn, "z1", "C", _flat(), score=(7.0,) * 5, model=cjk)
        _seed(conn, "z2", "C", _flat(), score=(5.0,) * 5, model=LOCAL)
        s = stats.compute_stats(conn)
        assert s["mixed_score_sources"] is True
        assert cjk in s["score_aggregates_by_model"]
        out = os.path.join(os.path.dirname(path), "stats_cjk.csv")
        stats.write_csv(s, out)
        with open(out, encoding="utf-8") as f:
            content = f.read()
        assert "count,score_source,%s,1" % cjk in content
        assert "score_by_model,%s,overall.avg,7.0" % cjk in content
        assert cjk in stats.format_stats(s)
        os.unlink(out)
    finally:
        conn.close()
        os.unlink(path)
