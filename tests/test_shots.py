from __future__ import annotations

from unittest.mock import MagicMock, patch

from reel_scout import shots
from reel_scout.shots import compute_shot_metrics, metrics_from_cuts, parse_cut_count


def test_parse_cut_count():
    stderr = (
        "[Parsed_showinfo] n:0 pts_time:1.5 ...\n"
        "[Parsed_showinfo] n:1 pts_time:3.2 ...\n"
        "[Parsed_showinfo] n:2 pts_time:9.9 ...\n"
    )
    assert parse_cut_count(stderr) == 3
    assert parse_cut_count("no scene changes here") == 0


def test_metrics_from_cuts_static_clip():
    # A single-shot clip: 0 cuts -> 1 shot spanning the whole duration.
    m = metrics_from_cuts(0, 30.0)
    assert m.shot_count == 1
    assert m.cuts_per_minute == 0.0
    assert m.avg_shot_sec == 30.0
    assert m.duration_sec == 30.0


def test_metrics_from_cuts_multi():
    # 9 cuts in 30s -> 18 cuts/min, 10 shots, 3.0s avg shot.
    m = metrics_from_cuts(9, 30.0)
    assert m.shot_count == 10
    assert m.cuts_per_minute == 18.0
    assert m.avg_shot_sec == 3.0


def test_compute_shot_metrics_mocked():
    fake = MagicMock()
    fake.returncode = 0
    fake.stderr = "pts_time:1.0\npts_time:2.0\npts_time:3.0\n"
    with patch("reel_scout.shots.subprocess.run", return_value=fake), \
         patch("reel_scout.shots._probe_duration", return_value=60.0):
        m = compute_shot_metrics("/fake.mp4")
    assert m is not None
    assert m.shot_count == 4        # 3 cuts + 1
    assert m.cuts_per_minute == 3.0  # 3 cuts in 1 minute
    assert m.duration_sec == 60.0


def test_compute_shot_metrics_uses_passed_duration():
    fake = MagicMock()
    fake.returncode = 0
    fake.stderr = "pts_time:1.0\n"
    with patch("reel_scout.shots.subprocess.run", return_value=fake) as run, \
         patch("reel_scout.shots._probe_duration") as probe:
        m = compute_shot_metrics("/fake.mp4", duration_sec=120.0)
    probe.assert_not_called()  # no re-probe when duration is supplied
    assert m.cuts_per_minute == 0.5  # 1 cut in 2 minutes
    assert run.called


def test_compute_shot_metrics_ffmpeg_nonzero_returns_none():
    # A corrupt video makes ffmpeg exit nonzero; must NOT be read as a 0-cut clip.
    fake = MagicMock()
    fake.returncode = 1
    fake.stderr = "moov atom not found\n"
    with patch("reel_scout.shots._probe_duration", return_value=30.0), \
         patch("reel_scout.shots.subprocess.run", return_value=fake):
        assert compute_shot_metrics("/corrupt.mp4") is None


def test_compute_shot_metrics_no_duration_returns_none():
    with patch("reel_scout.shots._probe_duration", return_value=None):
        assert compute_shot_metrics("/fake.mp4") is None


def test_compute_shot_metrics_ffmpeg_failure_returns_none():
    with patch("reel_scout.shots._probe_duration", return_value=30.0), \
         patch("reel_scout.shots.subprocess.run", side_effect=OSError("no ffmpeg")):
        assert compute_shot_metrics("/fake.mp4") is None


# --- per-shot spans (roadmap §Phase 6A) -------------------------------------

def test_parse_cut_times_sorts_and_dedupes():
    stderr = "pts_time:3.2 x pts_time:1.5 y pts_time:3.2 z pts_time:9.9"
    assert shots.parse_cut_times(stderr) == [1.5, 3.2, 9.9]
    assert shots.parse_cut_times("no scene changes here") == []


def test_parse_cut_count_counts_the_same_list_it_partitions_on():
    # The count and the table must never come from different sets. A repeated
    # timestamp is one boundary, so it is one shot, so it is one cut.
    stderr = "pts_time:1.5 pts_time:1.5 pts_time:4.0"
    assert parse_cut_count(stderr) == len(shots.parse_cut_times(stderr)) == 2


def test_shots_from_cuts_partitions_the_whole_clip():
    sh = shots.shots_from_cuts([2.0, 7.5], 10.0)
    assert [(s.index, s.start_sec, s.end_sec, s.dur_sec) for s in sh] == [
        (0, 0.0, 2.0, 2.0),
        (1, 2.0, 7.5, 5.5),
        (2, 7.5, 10.0, 2.5),
    ]
    # No gaps, no overlaps, and the spans add up to the clip.
    assert abs(sum(s.dur_sec for s in sh) - 10.0) < 1e-9


def test_shot_count_and_span_count_cannot_disagree():
    # The invariant the whole design rests on: `n` boundaries -> `n + 1` shots,
    # for both the aggregate and the table.
    for cuts in ([], [1.0], [1.0, 2.0], [0.5, 0.6, 9.9]):
        sh = shots.shots_from_cuts(cuts, 10.0)
        assert len(sh) == metrics_from_cuts(len(cuts), 10.0).shot_count


def test_boundary_at_zero_yields_a_zero_length_shot_rather_than_vanishing():
    # Dropping it would break the n+1 invariant and hide a detector artifact.
    sh = shots.shots_from_cuts([0.0, 5.0], 10.0)
    assert len(sh) == 3
    assert sh[0].dur_sec == 0.0
    assert sh[1].start_sec == 0.0


def test_boundary_past_the_end_is_clamped_not_negative():
    sh = shots.shots_from_cuts([5.0, 99.0], 10.0)
    assert len(sh) == 3
    assert all(s.dur_sec >= 0 for s in sh)
    assert sh[-1].end_sec == 10.0


def test_unsorted_boundaries_never_produce_negative_durations():
    sh = shots.shots_from_cuts([7.0, 2.0], 10.0)
    assert all(s.dur_sec >= 0 for s in sh)
    assert [s.start_sec for s in sh] == sorted(s.start_sec for s in sh)


def test_no_duration_means_no_partition():
    assert shots.shots_from_cuts([1.0], 0) == []
    assert shots.shots_from_cuts([1.0], None) == []


def test_compute_shot_table_returns_metrics_and_spans_from_one_pass():
    fake = MagicMock()
    fake.returncode = 0
    fake.stderr = "pts_time:1.0\npts_time:2.0\npts_time:3.0\n"
    with patch("reel_scout.shots.subprocess.run", return_value=fake) as run, \
         patch("reel_scout.shots._probe_duration", return_value=60.0):
        table = shots.compute_shot_table("/fake.mp4")
    assert run.call_count == 1  # one ffmpeg pass, not one per output
    m, sh = table
    assert m.shot_count == 4 and len(sh) == 4
    assert sh[-1].end_sec == 60.0


def test_compute_shot_table_returns_none_when_the_pass_fails():
    fake = MagicMock()
    fake.returncode = 1
    fake.stderr = "pts_time:1.0\n"
    with patch("reel_scout.shots.subprocess.run", return_value=fake), \
         patch("reel_scout.shots._probe_duration", return_value=60.0):
        assert shots.compute_shot_table("/fake.mp4") is None
