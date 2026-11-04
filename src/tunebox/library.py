"""Placing a finished file into the library, and reading back what is in it.

Two properties matter for placement, both because a music daemon is watching this tree:

* the move is atomic, so a scanner never sees a half-written file under a real track name;
* every write is confined to the music root (PLAN.md 3.2).

:func:`list_tracks` is the other direction, added in Phase 4 so the web remote has something to
play. It walks the tree rather than querying a database, because there is no database to query:
"the filesystem is the database for audio" (PLAN.md 3.1), and duplicating it into SQLite would
create a second place for the library to be wrong.

:func:`delete_track` is the third, and the only destructive thing in the project. It is here rather
than in the route for the same reason placement is: the rule that nothing outside the music root can
be named by a request body belongs next to the code that enforces it, not spread across the callers.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unicodedata
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Iterator

from .core import naming
from .core.models import DeletedTrack, LibraryPage, LibraryTrack, TrackTags
from .errors import LibraryError

#: Extensions :func:`list_tracks` treats as music. Wider than :data:`tunebox.config.SUPPORTED_FORMATS`
#: on purpose: tunebox only ever *writes* m4a and mp3, but the music root is a plain directory and
#: anything a person copied into it should be playable from the remote too.
AUDIO_EXTENSIONS = frozenset(
    {".m4a", ".mp3", ".flac", ".ogg", ".oga", ".opus", ".wav", ".aiff", ".aif", ".wma", ".m4b"}
)


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


# ------------------------------------------------------------------ reading it back


def resolve_track_path(root: Path, relative: str) -> Path:
    """Turn a client-supplied library path into an absolute one under ``root``.

    The counterpart to :attr:`~tunebox.core.models.LibraryTrack.path`, and the only way a request
    body is allowed to name a file. Everything dangerous about accepting a path from a client is
    handled by :func:`tunebox.core.naming.resolve_within` — ``..`` and symlinks that leave the root
    are refused rather than clamped — so the remote cannot ask the player daemon to enqueue
    ``/etc/shadow`` even though the daemon would have refused it anyway. Defence in depth, and the
    error is ours rather than OwnTone's.

    Both spellings are accepted because the API hands out both: ``GET /library`` reports paths
    relative to the music root, while a finished job reports the absolute path it wrote. An absolute
    path is reduced to the relative form *first*, so there is exactly one containment check rather
    than one per shape — which is the sort of duplication a traversal bug hides in.
    """
    text = (relative or "").strip()
    if not text:
        raise LibraryError("empty library path")
    # Accept both separators: a client on Windows (or a hand-written curl) should not be a 500.
    raw = text.replace("\\", "/")
    if raw.startswith("/"):
        try:
            raw = Path(raw).resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError) as exc:
            raise LibraryError(f"path is outside the music root: {text}") from exc
    candidate = PurePosixPath(raw)
    naming.resolve_within(root, candidate)  # raises LibraryError if it escapes
    # Joined *unresolved* even though the check above resolved it. The player adapter looks a path
    # up in OwnTone's index by string, and OwnTone recorded whatever spelling its own `directories`
    # setting gave it — so on the symlinked external SSD that PLAN.md 3.1 actually deploys to,
    # handing over the resolved form would report an indexed file as missing.
    resolved = root / Path(*candidate.parts)
    if not resolved.is_file():
        raise LibraryError(f"no such track in the library: {text}")
    if resolved.suffix.lower() not in AUDIO_EXTENSIONS:
        raise LibraryError(f"not a music file: {text}")
    return resolved


def describe_track(root: Path, absolute: Path) -> LibraryTrack:
    """Describe one track exactly as :func:`list_tracks` would describe it.

    Here so that :mod:`tunebox.playlists` can report a playlist's entries in the same vocabulary a
    ``GET /library`` row uses — same title, same artist, same relative path — rather than deriving
    them a second time and disagreeing about, say, whether ``Singles`` is an album.

    A path that is somehow not under ``root`` falls back to its own filename. That cannot happen via
    :func:`resolve_track_path`, and this is not where it should be discovered if it ever does.
    """
    try:
        relative = absolute.relative_to(root)
    except ValueError:  # pragma: no cover - callers resolve through resolve_track_path first
        relative = Path(absolute.name)
    return _describe(relative, absolute)


# ------------------------------------------------------------------ taking it out again


def delete_track(root: Path, relative: str, *, prune: bool = True) -> DeletedTrack:
    """Delete one track from the library, plus any directories that emptied because of it.

    The only destructive operation tunebox has, and what it destroys is the only irreplaceable
    state the system holds — so three things about it are decisions rather than implementation:

    * **It is a hard delete, not a move into a trash directory.** A dot-directory under the music
      root would be recoverable from the phone and invisible to :func:`list_tracks` for free, but it
      would also sit *inside* the tree ``tunebox backup`` mirrors, so deleted music would keep
      occupying the backup disk until something expired it. The backup is the undo instead — which
      is only true while ``TUNEBOX_BACKUP_PRUNE`` is off, and off is its default for exactly this
      kind of reason (PLAN.md Phase 5: "the accident a home backup most often has to survive is
      somebody deleting an album").
    * **Empty parents are removed, upward, stopping at the music root.** Deleting the last track in
      ``Artist/Singles/`` otherwise leaves a directory that ``GET /library`` cannot see (it walks
      files) but OwnTone's browse view can, so they accumulate as phantom artists. The emptiness
      check is :meth:`~pathlib.Path.rmdir` itself failing rather than a prior ``iterdir``, because
      an ingest running in parallel can fill the directory between the two.
    * **The path goes through :func:`resolve_track_path` and nothing else.** It is the one place
      that reduces an absolute path to the relative form, refuses ``..`` and symlinks that leave the
      root, and rejects a non-audio extension — and a second path handler in the *delete* route is
      precisely where a traversal bug would be worth having.

    ``relative`` is whatever the client sent: either form ``GET /library`` or ``GET /jobs`` hands
    out. Raises :class:`~tunebox.errors.LibraryError` if it names anything else.
    """
    target = resolve_track_path(root, relative)
    try:
        target.unlink()
    except OSError as exc:
        raise LibraryError(f"could not delete {relative}: {exc}") from exc
    removed = _prune_empty_parents(root, target.parent) if prune else ()
    return DeletedTrack(path=relative_to_root(root, target), removed_directories=removed)


def _prune_empty_parents(root: Path, directory: Path) -> tuple[str, ...]:
    """Remove ``directory`` and its parents while they are empty, never touching ``root`` itself.

    A music root that holds nothing else must survive: an empty library is the state ``tunebox
    serve`` starts in, and :func:`list_tracks` already treats a missing root as an empty library
    rather than an error — but removing the directory OwnTone is configured to watch would be a
    surprise nobody asked a delete for.
    """
    removed: list[str] = []
    root_resolved = root.resolve()
    current = directory
    while True:
        try:
            resolved = current.resolve()
        except OSError:  # pragma: no cover - a parent that cannot be resolved is a parent to leave
            break
        # Stop at the root, and stop if we somehow left it: the same containment rule as
        # ``naming.resolve_within``, applied on the way *out* of the tree rather than into it.
        if resolved == root_resolved or root_resolved not in resolved.parents:
            break
        try:
            current.rmdir()
        except OSError:
            # Not empty (the common case, and the point), or not removable. Either way, stop here.
            break
        removed.append(relative_to_root(root, current))
        current = current.parent
    return tuple(removed)


def relative_to_root(root: Path, path: Path) -> str:
    """``path`` as a POSIX path relative to ``root`` — the only form a client is ever told.

    Public because the job view needs it as well: a finished ingest records the absolute path it
    wrote, and the phone needs the same spelling the library listing hands out. See
    ``tunebox.api.routes._view``.
    """
    try:
        return path.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover - callers pass paths built by joining onto root
        return path.name


# ------------------------------------------------------------------ listing it


def list_tracks(
    root: Path,
    *,
    query: str | None = None,
    limit: int = 200,
    max_files: int = 50_000,
) -> LibraryPage:
    """Every music file under ``root``, newest first, optionally filtered by ``query``.

    Newest first because of what this is *for*: the most likely thing anyone wants to play from the
    remote is the track they ingested a minute ago. Alphabetical browsing of a whole library is what
    OwnTone's own web UI is good at, and Phase 4 links to it rather than rebuilding it.

    Dot-directories are skipped, which is not a style choice — it is what keeps the scratch
    directory (``.tunebox-tmp``) and the queue database (``.tunebox``) out of the answer, the same
    reason :data:`tunebox.config.SCRATCH_DIRNAME` has a leading dot in the first place. Dot-*files*
    are skipped for the same reason: a cross-device placement stages through
    ``.Track.m4a.partial``, and a half-copied file must never be offered as something to play.
    """
    matches: list[LibraryTrack] = []
    seen = 0
    truncated = False
    needle = _fold(query) if query and query.strip() else None

    for path in _walk_audio(root):
        seen += 1
        if seen > max_files:
            truncated = True
            break
        try:
            relative = path.relative_to(root)
        except ValueError:  # pragma: no cover - _walk_audio only yields paths under root
            continue
        track = _describe(relative, path)
        if needle is not None and needle not in _fold(f"{track.path} {track.artist or ''}"):
            continue
        matches.append(track)

    matches.sort(key=lambda t: (-(t.modified or 0.0), t.path))
    return LibraryPage(
        tracks=tuple(matches[: max(0, limit)]), total=len(matches), truncated=truncated
    )


def _walk_audio(root: Path) -> Iterator[Path]:
    """Music files under ``root``, skipping dot-entries. Never raises for an unreadable tree."""
    if not root.is_dir():
        # A music root that does not exist yet is an empty library, not an error: `tunebox serve`
        # is allowed to start before the first ingest has created it.
        return
    for dirpath, dirnames, filenames in os.walk(root, onerror=None):
        # In-place, because that is how os.walk is told not to descend. Symlinked directories are
        # not followed (os.walk's default), which also means the walk cannot loop.
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            if Path(name).suffix.lower() in AUDIO_EXTENSIONS:
                yield Path(dirpath) / name


def _describe(relative: Path, absolute: Path) -> LibraryTrack:
    """Read a track's metadata out of its own path — the inverse of :func:`naming.relative_path`."""
    parts = relative.parts
    stem = relative.stem
    track_number, title = _split_track_number(stem)
    artist = parts[0] if len(parts) >= 2 else None
    album = parts[-2] if len(parts) >= 3 else None
    if album == naming.SINGLES_DIR:
        # Not a real album, just the directory this layout puts album-less tracks in. Reporting it
        # as one would put "Singles" on screen as if it were a record.
        album = None
    try:
        stat = absolute.stat()
        size: int | None = stat.st_size
        modified: float | None = stat.st_mtime
    except OSError:  # pragma: no cover - the file was removed between walk and stat
        size = modified = None
    return LibraryTrack(
        path=relative.as_posix(),
        title=title,
        artist=artist,
        album=album,
        track_number=track_number,
        size=size,
        modified=modified,
    )


def _split_track_number(stem: str) -> tuple[int | None, str]:
    """``"03 - Trans-Europe Express"`` -> ``(3, "Trans-Europe Express")``.

    Matches what :func:`naming.relative_path` writes, and only that: a title that merely happens to
    begin with digits and a dash keeps them, because two digits then " - " is the shape tunebox
    produced and a wider guess would eat real titles.
    """
    head, separator, tail = stem.partition(" - ")
    if separator and len(head) == 2 and head.isdigit() and tail:
        return int(head), tail
    return None, stem


def _fold(text: str) -> str:
    """Case- and composition-insensitive form, for matching a query against a filename.

    NFC because that is what :func:`naming.sanitize_component` writes, while a filesystem may hand
    back NFD — macOS notoriously does. Without normalising both sides, searching for a title with
    an accent in it would find nothing on one machine and everything on another.
    """
    return unicodedata.normalize("NFC", text).casefold()
