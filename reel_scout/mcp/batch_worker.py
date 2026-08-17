"""Run one batch to completion, detached from whoever asked for it.

Spawned by the `batch_start` MCP tool. A separate process rather than a thread
inside the server, for one decisive reason: `contextlib.redirect_stdout` -- the
convention every noisy handler in tools.py follows -- rebinds sys.stdout for the
whole process and is not thread-safe. A worker thread holding that redirect
while the main loop answered a request would send JSON-RPC frames to stderr; a
worker thread without it would put run_batch's print() calls straight into the
NDJSON stream. Both kill the session. Here stdout is /dev/null at spawn and the
question does not arise.

The second reason is that the MCP process dies with the client and has no
shutdown hook. A thread would die mid-video; this finishes the twenty videos
the user waited for.

Everything it needs comes from the database, not argv: that keeps a 20-URL
command line off Windows' CreateProcess limit, and it is also what makes the
row the single source of truth about a job nobody is watching.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from typing import Any, Dict, List, Tuple

from .. import batch as batch_mod
from .. import config, db

#: How `batch_start` marks the rows it owns.
BATCH_SOURCE = "mcp-batch"


def load_job(conn: Any, batch_id: str) -> Tuple[List[Tuple[str, str]], Dict[str, Any]]:
    """(entries, meta) for a batch, rebuilt from its rows."""
    row = db.get_batch(conn, batch_id)
    if row is None:
        raise RuntimeError("no such batch: %s" % batch_id)
    items = db.get_batch_items(conn, batch_id)
    entries = [(item["label"] or "", item["url"]) for item in items]
    return entries, {
        "mode": row["mode"] or "agent",
        "out_root": row["out_root"] or config.DATA_DIR,
        "status": row["status"],
    }


def make_progress_sink(conn: Any, batch_id: str):
    """Turn run_batch's events into rows, and carry the cancel flag back.

    Intermediate states go through set_batch_item_progress, never
    update_batch_item: that one moves the batch counters and would count a video
    once per stage it passes through. Only the two terminal events call it, once
    each.
    """
    import sqlite3

    def sink(event: Dict[str, Any]) -> Any:
        kind = event.get("event")
        url = event.get("url")
        try:
            db.touch_batch_heartbeat(conn, batch_id)
            if kind == "item_start":
                db.set_batch_item_progress(
                    conn, batch_id, url, status="analyzing",
                    label=event.get("label"), slug=event.get("slug"))
                if db.batch_cancel_requested(conn, batch_id):
                    return "cancel"
            elif kind == "item_analyzed":
                db.set_batch_item_progress(
                    conn, batch_id, url, video_id=event.get("video_id"))
            elif kind == "item_needs_vision":
                db.set_batch_item_progress(conn, batch_id, url, status="needs_vision")
            elif kind == "item_exporting":
                db.set_batch_item_progress(
                    conn, batch_id, url, status="exporting",
                    bundle_dir=event.get("bundle_dir"))
            elif kind == "item_done":
                db.set_batch_item_progress(
                    conn, batch_id, url, bundle_dir=event.get("bundle_dir"))
                _retry(db.update_batch_item, conn, batch_id, url, "done",
                       video_id=event.get("video_id"))
            elif kind == "item_failed":
                _retry(db.update_batch_item, conn, batch_id, url, "error",
                       error=event.get("reason"))
        except sqlite3.OperationalError:
            # A dropped progress row costs one poll's accuracy. Raising here
            # would cost the rest of the batch.
            return None
        return None

    return sink


def _retry(fn, *args, **kwargs):
    """Terminal transitions move the counters, so losing one is not cosmetic."""
    import sqlite3
    import time

    for attempt in range(3):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError:
            if attempt == 2:
                raise
            time.sleep(1)


def run(batch_id: str) -> int:
    config.ensure_dirs()
    conn = db.get_connection(timeout=30)
    try:
        entries, meta = load_job(conn, batch_id)
        db.set_batch_meta(conn, batch_id, status="running")
        db.touch_batch_heartbeat(conn, batch_id)
        sink = make_progress_sink(conn, batch_id)
        try:
            result = batch_mod.run_batch(
                entries, meta["out_root"], meta["mode"],
                verbose=False, on_progress=sink)
        except BaseException as exc:  # noqa: BLE001
            # A worker that dies without marking the row leaves a batch that is
            # "running" forever and a user who is told to keep waiting.
            db.set_batch_meta(conn, batch_id, status="failed")
            sys.stderr.write("batch %s failed: %s\n%s\n"
                             % (batch_id, exc, traceback.format_exc()))
            return 1
        if result.get("cancelled"):
            db.set_batch_meta(conn, batch_id, status="cancelled")
        elif result.get("deadline_exceeded"):
            db.set_batch_meta(conn, batch_id, status="incomplete")
        else:
            # mark_batch_completed owns the 'completed' status; calling
            # set_batch_meta after it would just overwrite what it set.
            #
            # Deliberately still unconditional on failures. The question this
            # answers is "did the run finish", and whether the *items* succeeded
            # is a different question the reader derives from the counters --
            # see `_batch_state`. One status cannot honestly carry both.
            db.mark_batch_completed(conn, batch_id)
        return 0
    finally:
        conn.close()


def main(argv: List[str] = None) -> None:
    parser = argparse.ArgumentParser(prog="reel-scout-batch-worker")
    parser.add_argument("--batch-id", required=True)
    args = parser.parse_args(argv)
    raise SystemExit(run(args.batch_id))


if __name__ == "__main__":
    main()
