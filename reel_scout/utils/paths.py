"""Resolving stored media paths independently of the working directory.

``DATA_DIR`` defaults to ``./data`` — relative to whatever cwd the process
happened to start in. Rows written from the repo root therefore recorded
``./data/videos/x.mp4`` while rows written from anywhere else recorded an
absolute path, and a live database holds both shapes.

Reading either one back with ``os.path.exists()`` from a different cwd — an MCP
server launched elsewhere, a cron job, a student running the CLI from their home
directory — reports the file as missing. The pipeline treats that as "not
downloaded yet" and silently downloads it again.

Resolution here tries every shape a stored path can legitimately have, anchored
to the *data root* rather than the cwd, and keeps the basename fallback that
lets a file survive being moved between data directories.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from .. import config


def data_root() -> Path:
    """Absolute data root. Read lazily so ``--data`` and tests can reassign it."""
    return Path(config.DATA_DIR).expanduser().resolve()


def _strip_dot(path: str) -> str:
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def candidates(stored: str) -> List[Path]:
    """Every location a stored path might legitimately refer to, in priority order."""
    rel = _strip_dot(stored)
    root = data_root()
    return [
        root / rel,                                  # portable form: videos/x.mp4
        root.parent / rel,                           # legacy form:   ./data/videos/x.mp4
        Path.cwd() / rel,                            # what abspath() used to assume
        Path(config.VIDEOS_DIR).expanduser().resolve() / os.path.basename(rel),
    ]                                                # moved between data dirs


def resolve_media_path(stored: Optional[str]) -> str:
    """Resolve a stored path to something openable from any working directory.

    Absolute paths that exist pass through untouched. Everything else is tried
    against the data root, its parent, the cwd, and finally the videos directory
    by basename; the first hit wins. When nothing exists the data-root form is
    returned, so failures name a concrete location instead of a bare relative
    fragment.
    """
    if not stored:
        return ""
    p = Path(stored).expanduser()
    if p.is_absolute():
        if p.exists():
            return str(p)
        # An absolute path that has moved still deserves the basename fallback.
        moved = Path(config.VIDEOS_DIR).expanduser().resolve() / p.name
        return str(moved) if moved.exists() else str(p)

    for candidate in candidates(stored):
        if candidate.exists():
            return str(candidate.resolve())

    return str(data_root() / _strip_dot(stored))


def exists(stored: Optional[str]) -> bool:
    """True when a stored path resolves to a file that is really there."""
    return bool(stored) and os.path.exists(resolve_media_path(stored))


def to_storage_path(path: Optional[str]) -> str:
    """The portable form to record in the database.

    Paths under the data root are stored relative to it so the whole directory
    can be moved or synced; anything outside stays absolute, since a relative
    form would not survive the move.
    """
    if not path:
        return ""
    p = Path(path).expanduser()
    root = data_root()
    try:
        resolved = p.resolve() if p.is_absolute() else (root / _strip_dot(path)).resolve()
    except OSError:
        return str(p)
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return str(resolved)
