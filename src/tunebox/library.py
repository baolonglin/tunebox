"""Placing a finished file into the library.

Two properties matter here, both because a music daemon is watching this tree:

* the move is atomic, so a scanner never sees a half-written file under a real track name;
* every write is confined to the music root (PLAN.md 3.2).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .core import naming
from .core.models import TrackTags
from .errors import LibraryError


def destination_for(root: Path, tags: TrackTags, extension: str) -> Path:
    """Absolute path this track belongs at. Does not touch the filesystem beyond resolving."""
    return naming.resolve_within(root, naming.relative_path(tags, extension))


def place(source: Path, destination: Path, *, overwrite: bool = False) -> Path:
    """Move ``source`` to ``destination`` atomically, creating parent directories.

    Falls back to copy+replace when the two are on different filesystems, which is why the
    scratch directory lives inside the music root by default.
    """
    if not source.is_file():
        raise LibraryError(f"nothing to place: {source} is not a file")

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not overwrite:
        destination = naming.unique_path(destination)

    try:
        os.replace(source, destination)
    except OSError:
        # Cross-device: stage beside the destination, then swap in atomically.
        staged = destination.with_name(f".{destination.name}.partial")
        try:
            shutil.copy2(source, staged)
            os.replace(staged, destination)
        except OSError:
            staged.unlink(missing_ok=True)  # never leave a half-copied file in the library
            raise
        source.unlink(missing_ok=True)
    return destination


@contextmanager
def scratch_dir(parent: Path, prefix: str = "ingest-") -> Iterator[Path]:
    """Temporary working directory under ``parent``, removed even when the ingest fails."""
    parent.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
