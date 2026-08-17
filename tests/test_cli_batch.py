"""`reel-scout batch` has to tell the shell when it did not do the job.

Until now every path in `_cmd_batch` printed an explanation and returned None,
so the process exited 0 -- including the path where every single item failed.
A wrapper, a CI step, or a student reading `echo $?` was told the batch was
done. That is the same silent-success family as the empty database and the 403
that never retried: the run reports fine and the result is quietly missing.

The command had no test coverage at all before this file.
"""

from __future__ import annotations

import pytest

from reel_scout import batch, cli


def _src(tmp_path, n=2):
    p = tmp_path / "list.txt"
    p.write_text(
        "\n".join("clip%d\thttps://www.instagram.com/reel/AbCdEfG%04d/" % (i, i)
                  for i in range(n)),
        encoding="utf-8",
    )
    return str(p)


@pytest.fixture
def full_mode(monkeypatch):
    """A machine that can do everything, so mode resolution is never the story."""
    monkeypatch.setattr(batch, "probe", lambda: {"vlm": True, "whisper": True})


def _result(done=0, failed=0, pending=0, **extra):
    out = {
        "mode": "full",
        "done": [{"label": "d%d" % i, "url": "u", "video_id": "v%d" % i}
                 for i in range(done)],
        "failed": [{"label": "f%d" % i, "url": "u", "reason": "analyze exited non-zero"}
                   for i in range(failed)],
        "pending_completion": [{"label": "p%d" % i, "url": "u", "video_id": "w%d" % i}
                               for i in range(pending)],
    }
    out.update(extra)
    return out


def test_a_batch_with_a_failed_item_does_not_exit_zero(tmp_path, monkeypatch, full_mode):
    monkeypatch.setattr(batch, "run_batch",
                        lambda *a, **k: _result(done=1, failed=1))
    with pytest.raises(SystemExit) as exc:
        cli.main(["batch", "--file", _src(tmp_path), "--out", str(tmp_path / "o")])
    assert exc.value.code == 1


def test_a_batch_where_everything_landed_exits_zero(tmp_path, monkeypatch, full_mode):
    monkeypatch.setattr(batch, "run_batch", lambda *a, **k: _result(done=2))
    cli.main(["batch", "--file", _src(tmp_path), "--out", str(tmp_path / "o")])


def test_work_still_waiting_for_a_visual_layer_is_not_a_failure(
    tmp_path, monkeypatch, full_mode, capsys
):
    # `--mode agent` is the path SKILL.md recommends, and leaving items in
    # pending_completion is its designed terminal state -- the operator is meant
    # to read the keyframes and write findings back. Exiting non-zero here would
    # paint the recommended path permanently red.
    monkeypatch.setattr(batch, "run_batch",
                        lambda *a, **k: _result(done=2, pending=2))
    cli.main(["batch", "--file", _src(tmp_path), "--out", str(tmp_path / "o")])
    assert "still need a visual layer" in capsys.readouterr().out


def test_dry_run_exits_zero_because_the_listing_is_the_deliverable(
    tmp_path, monkeypatch, full_mode, capsys
):
    def _boom(*a, **k):
        raise AssertionError("--dry-run must not analyze anything")
    monkeypatch.setattr(batch, "run_batch", _boom)
    cli.main(["batch", "--file", _src(tmp_path), "--dry-run", "--out", str(tmp_path / "o")])
    assert "--dry-run: nothing was analyzed" in capsys.readouterr().out


def test_a_source_that_could_not_be_fetched_does_not_exit_zero(tmp_path, monkeypatch):
    def _fail(url):
        raise RuntimeError("got a Google sign-in page instead of the file.")
    monkeypatch.setattr(batch, "fetch", _fail)
    with pytest.raises(SystemExit) as exc:
        cli.main(["batch", "--doc", "https://docs.google.com/document/d/x/edit"])
    assert exc.value.code == 1


def test_a_source_with_no_links_does_not_exit_zero(tmp_path, monkeypatch):
    empty = tmp_path / "empty.txt"
    empty.write_text("nothing to see here\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        cli.main(["batch", "--file", str(empty)])
    assert exc.value.code == 1


def test_no_mode_and_no_vlm_exits_non_zero_but_keeps_its_explanation(
    tmp_path, monkeypatch, capsys
):
    # resolve_mode's own message says "That is a choice, not an error". It is
    # right about blame and silent about state: nothing ran. The status has to
    # say so, and the explanation has to survive -- a bare exit code on the path
    # a first-time user hits would be a downgrade.
    monkeypatch.setattr(batch, "probe", lambda: {"vlm": False, "whisper": True})
    with pytest.raises(SystemExit) as exc:
        cli.main(["batch", "--file", _src(tmp_path), "--out", str(tmp_path / "o")])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "That is a choice, not an error" in out, "the explanation must not be dropped"
    assert "exits non-zero" in out, "message and status must not contradict each other"


def test_asking_for_full_mode_on_a_box_without_a_vlm_does_not_exit_zero(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(batch, "probe", lambda: {"vlm": False, "whisper": True})
    with pytest.raises(SystemExit) as exc:
        cli.main(["batch", "--file", _src(tmp_path), "--mode", "full",
                  "--out", str(tmp_path / "o")])
    assert exc.value.code == 1
    assert "needs a reachable VLM backend" in capsys.readouterr().out


def test_items_never_attempted_are_reported_and_are_not_exit_zero(
    tmp_path, monkeypatch, full_mode, capsys
):
    # Stopping early is not the same as failing: these URLs were never tried,
    # so they must not be counted against the crawler or the source.
    monkeypatch.setattr(batch, "run_batch", lambda *a, **k: _result(
        done=1, deadline_exceeded=True,
        not_attempted=[{"label": "later", "url": "https://example.com/x"}]))
    with pytest.raises(SystemExit) as exc:
        cli.main(["batch", "--file", _src(tmp_path), "--out", str(tmp_path / "o")])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "never attempted" in out
    assert "0 failed" not in out, "not-attempted items must not be printed as failures"
