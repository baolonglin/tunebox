"""Playlists — M3U files tunebox writes into the music root itself.

**A playlist here is a file, not a table**, and that is the whole design. OwnTone *can* keep
playlists (``/api/library/playlists``), and it refuses to write one unless its config is changed:
it ships ``allow_modifying_stored_playlists = false`` with an empty ``default_playlist_directory``,
both commented out, and the binary carries the matching refusals. Measured against the running
daemon, 2026-09-27: an ``.m3u`` dropped into the music root and followed by ``PUT /api/update`` was
indexed as ``{"type": "plain", "item_count": 2}`` and expanded into the queue in order, with both
relative and absolute entries resolving — including one with CJK in it.

So tunebox writes the file. Not as a workaround; it is the better side of four trades that were
already made elsewhere:

* **No new database.** The filesystem is the library database (PLAN.md 3.1), so a playlist is backed
  up by ``tunebox backup``, survives ``tunebox restore``, and is readable by Navidrome or MPD — with
  no new code in any of those three places.
* **No OwnTone config change**, so the daemon's write side stays locked down.
* **Playback needs no new daemon integration at all.** This module reads the M3U, resolves the
  entries and hands absolute paths to the existing :meth:`~tunebox.core.ports.Player.enqueue_paths`.
  Building on ``uris=library:playlist:N`` instead would have leaked OwnTone's ids through the
  ``Player`` port and broken the drop-in property the port exists for (PLAN.md 3.4).
* **The write path already exists.** Every entry goes through
  :func:`tunebox.library.resolve_track_path`, which is the one place that refuses ``..``, a symlink
  out of the tree and a non-audio file — so a playlist cannot name a file outside the music root,
  which is the entire risk of accepting a list of paths from a phone.

Three decisions in the file format, each cheap and each load-bearing somewhere:

* **Entries are relative to the playlist file**, so ``Playlists/x.m3u`` names tracks as
  ``../Artist/Album/Track.m4a``. Relative because it survives the library moving — the symlinked
  external SSD of PLAN.md 3.1, and a restore to a different path — and relative *to the file* because
  that is what M3U means everywhere. It is also why tunebox resolves the entries itself for playback
  rather than relying on the daemon to: the one thing the measurement above did **not** cover is a
  playlist in a subdirectory, so nothing in the playback path depends on OwnTone agreeing with us.
* **An absolute entry is read but never written.** Hand-edited files and playlists from elsewhere
  work; what tunebox produces is portable.
* **No ``#EXTINF``.** It would carry a title for players that do not read tags, and OwnTone reads
  tags. What it would cost is a second copy of each track's metadata inside the playlist, going stale
  the moment somebody retags a file. tunebox does not write them — and does not remove the ones it
  finds either: every edit here carries the existing file across line for line (:func:`_lines`), so
  appending a track to a playlist another program wrote leaves that program's lines alone. The one
  exception is deliberate: pruning an entry takes its own ``#EXTINF`` with it, because that line
  describes the track that just went and would otherwise be read as belonging to the next one.

Concurrency is **last-write-wins**, stated rather than left implicit: every edit is a
read-modify-write, and two phones editing one playlist at the same moment means one of the two edits
is lost. The write itself is atomic (staged beside the target, then :func:`os.replace`), so a scanner
never sees a half-written list and a crash cannot truncate one — it is only the *merge* that is not
attempted, because at one household's scale the machinery to do better would be the largest moving
part in the feature.

Two distinctions in this module are load-bearing, and both were bugs before they were rules:

* **Creating a playlist sanitises its name; addressing one does not** —
  :func:`normalise_name` against :func:`exact_name`. Sanitising a lookup means ``a:b`` and ``a_b``
  address the same file, so a delete can remove somebody else's playlist.
* **Reading is never capped; writing is.** :data:`MAX_ENTRIES` applies to what tunebox writes. Applying
  it to a read meant a 1005-entry playlist was *read* as 1000 and then rewritten from that reading by
  the next unrelated delete.
"""

from __future__ import annotations

import logging
import os
import unicodedata
from dataclasses import dataclass
from dataclasses import replace as _replace
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

from .config import DEFAULT_PLAYLISTS_DIRNAME
from .core import naming
from .core.models import Playlist, PlaylistEntry
from .errors import LibraryError, PlaylistError, PlaylistExists, PlaylistNotFound
from .library import describe_track, resolve_track_path

log = logging.getLogger(__name__)

#: The one extension tunebox reads and writes. ``.m3u8`` is the same format declared UTF-8, which
#: every player assumes of ``.m3u`` anyway and OwnTone indexes identically — so one spelling, and the
#: file is written UTF-8 regardless.
SUFFIX = ".m3u"

#: First line of every file tunebox writes. Not required by any reader here (the entries are plain
#: paths), but it is what makes the file recognisable as a playlist rather than a stray text file.
HEADER = "#EXTM3U"

#: Comment line naming the playlist, written into files tunebox creates. Files it did not create keep
#: whatever header they came with — see :func:`_rewrite`.
_COMMENT = "# tunebox playlist: "

#: Ceiling on a playlist tunebox **writes**. Generous: it is forty albums. Deliberately *not* applied
#: when reading, which was a measured data-loss bug: capping the read meant a hand-written playlist of
#: 1005 entries came back as 1000, and then the first ``DELETE /library`` that touched it rewrote the
#: file from that truncated reading and destroyed the last five lines. A read is bounded by the file,
#: and a file nobody wrote through this module is not ours to shorten.
MAX_ENTRIES = 1000


# --------------------------------------------------------------------- naming


def exact_name(name: str) -> str:
    """The name as an **addressing** key: used verbatim, validated, never rewritten.

    The counterpart to :func:`normalise_name`, and the two must not be confused — conflating them was
    a measured bug. Sanitising on the way *in* to a lookup means two different files can be addressed
    by one name: ``a:b.m3u`` and ``a_b.m3u`` both sanitise to ``a_b``, so ``DELETE /playlists/a:b``
    deleted ``a_b.m3u`` — somebody else's playlist. So creating sanitises, and everything that names an
    existing playlist (read, append, play, delete) requires a name that is already a usable component
    and refuses anything else.

    The trade is that a playlist whose filename tunebox would not have written cannot be addressed
    through the API. It is listed (:func:`list_playlists` reports the real stem) and a ``404`` is what
    comes back, which is the right failure: the alternative is silently acting on a different file.
    """
    text = unicodedata.normalize("NFC", name or "").strip()
    if text.lower().endswith(SUFFIX):
        # Accept the filename as well as the name: `tunebox playlist show "Road trip.m3u"` is what
        # somebody who just looked in the directory will type.
        text = text[: -len(SUFFIX)]
    if not text or text in (".", ".."):
        raise PlaylistError(f"not a usable playlist name: {name!r}")
    if "/" in text or "\\" in text:
        raise PlaylistError(
            f"a playlist name is one name, not a path: {name!r} (create one to have it sanitised)"
        )
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise PlaylistError(f"playlist name contains a control character: {name!r}")
    if text != text.strip(". "):
        # Some filesystems silently strip these, so a name ending in one would not be findable again.
        raise PlaylistError(f"playlist name may not begin or end with a dot or a space: {name!r}")
    if len(text.encode("utf-8")) > naming.MAX_COMPONENT:
        raise PlaylistError(
            f"playlist name is too long: {len(text.encode('utf-8'))} bytes, "
            f"the limit is {naming.MAX_COMPONENT}"
        )
    return text


def normalise_name(name: str) -> str:
    """The name as it will be **stored**: one safe path component, without the extension.

    Through :func:`tunebox.core.naming.sanitize_component`, the same function that names every track —
    so a playlist called ``Road trip: 2026/07`` becomes one filename rather than two directories, and
    a name of ``..`` is refused rather than becoming a path. The fallback is empty on purpose: there is
    no sensible "Unknown Playlist", so a name that sanitises to nothing is an error the caller must
    report.

    Only ``write_playlist`` uses this. See :func:`exact_name` for why looking one up does not.
    """
    text = unicodedata.normalize("NFC", name or "").strip()
    if text.lower().endswith(SUFFIX):
        text = text[: -len(SUFFIX)]
    safe = naming.sanitize_component(text, "")
    if not safe:
        raise PlaylistError(f"not a usable playlist name: {name!r}")
    return safe


def playlist_path(root: Path, name: str, *, directory: str = DEFAULT_PLAYLISTS_DIRNAME) -> Path:
    """Absolute path of the ``.m3u`` file for ``name``. Does not require it to exist.

    ``name`` is taken verbatim (:func:`exact_name`), which is what makes this safe to use for a delete.
    Containment is checked by :func:`tunebox.core.naming.resolve_within` and the *unresolved* join is
    what comes back — the same pairing :func:`tunebox.library.resolve_track_path` uses, and for the
    same reason: on the symlinked music root PLAN.md 3.1 deploys to, the resolved spelling is not the
    one the daemon has in its index.
    """
    relative = PurePosixPath(_directory_name(directory)) / f"{exact_name(name)}{SUFFIX}"
    naming.resolve_within(root, relative)  # raises LibraryError if it escapes the music root
    return root / Path(*relative.parts)


def _directory_name(directory: str) -> str:
    """The playlists directory as a single safe component.

    Through the same sanitiser as everything else, which is also what makes a *hidden* playlists
    directory unreachable rather than merely discouraged: ``sanitize_component`` strips leading dots,
    so ``.lists`` becomes ``lists`` and the files cannot end up somewhere a music scanner skips.
    :meth:`tunebox.config.Config.__post_init__` refuses such a name outright, which is the better place
    to *tell* somebody; this is the second line of the same defence, for a ``Config`` built in a test by
    hand or a caller passing a directory directly.
    """
    return naming.sanitize_component(directory, DEFAULT_PLAYLISTS_DIRNAME)


def _playlists_dir(root: Path, directory: str) -> Path:
    """The playlists directory, **checked for containment** before anything iterates it.

    Not redundant with :func:`playlist_path`, which was the gap this closes: the two functions that
    walk the directory rather than naming a file in it (:func:`list_playlists` and
    :func:`forget_tracks`) used to join the name and call ``iterdir`` with no check at all. With
    ``Playlists/`` replaced by a symlink pointing out of the tree, a ``DELETE /library`` then read *and
    rewrote* a file outside the music root — measured, and a direct violation of PLAN.md 3.2. One
    ``resolve_within`` here refuses it, because ``resolve_within`` resolves symlinks before comparing.
    """
    relative = PurePosixPath(_directory_name(directory))
    naming.resolve_within(root, relative)  # raises LibraryError if the directory leaves the root
    return root / Path(*relative.parts)


def _playlist_files(root: Path, directory: str) -> list[Path]:
    """Every playlist file in the directory, in name order, each one inside the music root.

    The per-file check is for the same reason as :func:`_playlists_dir`: a single ``.m3u`` that is a
    symlink to something outside the tree would otherwise be read (leaking its contents into a
    ``missing`` list) and, on a prune, rewritten.
    """
    try:
        base = _playlists_dir(root, directory)
    except LibraryError as exc:
        log.warning("not reading playlists: %s", exc)
        return []
    if not base.is_dir():
        # No directory yet is no playlists, not an error: it is created by the first write, exactly as
        # the music root itself is created by the first ingest.
        return []
    found: list[Path] = []
    for path in sorted(base.iterdir()):
        if path.name.startswith(".") or path.suffix.lower() != SUFFIX or not path.is_file():
            continue
        try:
            naming.resolve_within(root, PurePosixPath(_directory_name(directory)) / path.name)
        except LibraryError as exc:
            log.warning("skipping playlist %s: %s", path.name, exc)
            continue
        found.append(path)
    return found


def _relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover - every caller joins onto root
        return path.name


# --------------------------------------------------------------------- reading


def read_playlist(root: Path, name: str, *, directory: str = DEFAULT_PLAYLISTS_DIRNAME) -> Playlist:
    """One playlist, with its entries resolved against the music root.

    Entries that do not resolve are reported in :attr:`~tunebox.core.models.Playlist.missing` rather
    than raising: a playlist is a text file in a directory anyone in the house can write to, so a line
    naming a track that has been deleted by hand is an ordinary state and must not make the whole
    playlist unreadable.
    """
    target = playlist_path(root, name, directory=directory)
    if not target.is_file():
        raise PlaylistNotFound(f"no playlist called {exact_name(name)!r}")
    return _describe(root, target)


def _describe(root: Path, target: Path) -> Playlist:
    """Build the model from the file, taking the name from the **filename**.

    From the filename rather than from whatever the caller asked for, which matters for
    :func:`list_playlists`: deriving the name by sanitising the stem hid any file whose stem would not
    survive the sanitiser (``Road:trip.m3u`` appeared in no listing at all) and reported two different
    files under one name.
    """
    _, blocks = _parse(root, target)
    entries = tuple(
        PlaylistEntry(
            position=position,
            entry=block.entry,
            track=describe_track(root, block.absolute) if block.absolute else None,
        )
        for position, block in enumerate(blocks)
    )
    try:
        modified: float | None = target.stat().st_mtime
    except OSError:  # pragma: no cover - it was a file one statement ago
        modified = None
    return Playlist(
        name=target.stem,
        path=_relative(root, target),
        count=sum(1 for entry in entries if entry.track is not None),
        missing_count=sum(1 for entry in entries if entry.track is None),
        modified=modified,
        entries=entries,
    )


def list_playlists(
    root: Path, *, directory: str = DEFAULT_PLAYLISTS_DIRNAME
) -> tuple[Playlist, ...]:
    """Every playlist, in name order, **without** its tracks.

    Name order rather than the newest-first that :func:`tunebox.library.list_tracks` uses, and the
    difference is deliberate: newest-first is right for a library because the track somebody wants is
    the one they just ingested, while a playlist is a thing with a name that somebody is looking
    *for*. A list that reordered itself whenever one was edited would be a list nobody could point at.

    Tracks are left out (and :attr:`~tunebox.core.models.Playlist.count` carries the length) so that
    drawing a sidebar does not mean sending the whole library back. Reading each file is unavoidable
    — the count is in it — but each is a few hundred bytes.
    """
    found: list[Playlist] = []
    for path in _playlist_files(root, directory):
        try:
            # Counts kept, entries dropped: see Playlist for why both numbers are fields.
            found.append(_replace(_describe(root, path), entries=()))
        except (PlaylistError, LibraryError, OSError) as exc:
            # One unreadable file must not hide the rest of somebody's playlists.
            log.warning("skipping playlist %s: %s", path.name, exc)
    found.sort(key=lambda item: (unicodedata.normalize("NFC", item.name).casefold(), item.name))
    return tuple(found)


def resolved_paths(root: Path, playlist: Playlist) -> list[Path]:
    """Absolute paths for the tracks of ``playlist``, in order — what the player is handed.

    Re-resolved through :func:`tunebox.library.resolve_track_path` rather than carried along from the
    read, so that there is exactly one function in the project that turns a stored path into a path
    on disk. A playlist read a moment ago whose file has since gone raises
    :class:`~tunebox.errors.LibraryError` here, which is the same answer ``POST /player/queue`` gives
    for the same situation.
    """
    return [resolve_track_path(root, track.path) for track in playlist.tracks]


def _lines(target: Path) -> list[str]:
    """Every line of the file, in order, without its line endings. Comments and blanks included.

    Kept whole because every edit is a rewrite, and a rewrite that only knew about the *entries* threw
    the rest away: appending one track to a playlist written by another program deleted its
    ``#EXTINF`` lines, which are that program's copy of the metadata. tunebox does not write them and
    has no business removing them either.
    """
    return target.read_text(encoding="utf-8", errors="replace").splitlines()


@dataclass(frozen=True)
class _Block:
    """One item of a playlist, as the **lines** it occupies in the file.

    Lines rather than a path, because every edit here is a rewrite of the whole file and an item is
    not always one line: ``#EXTINF:213,Kraftwerk - Autobahn`` above an entry is that entry's own
    metadata, written by whichever program produced the file. Remove the entry and leave the
    ``#EXTINF`` and it now describes the *next* track; move the entry and leave it and the file is
    simply wrong. So the unit an edit moves or drops is this, not a line.
    """

    lines: tuple[str, ...]
    #: The raw entry text, stripped — the line that names something.
    entry: str
    #: Where it resolves under the music root, or ``None`` if it names no library track.
    absolute: Path | None


def _parse(root: Path, target: Path) -> tuple[list[str], list[_Block]]:
    """Split a playlist into its header and its items: ``(preamble, blocks)``.

    The rule, stated once because three operations depend on it:

    * an item **starts** at its ``#EXTINF`` if one sits immediately above it, otherwise at its own
      line, and **ends** where the next item starts (so a stray comment after an entry travels with
      that entry, and the last item takes any trailing blank lines with it);
    * everything before the first item is the **preamble** — ``#EXTM3U``, a ``#PLAYLIST:`` directive,
      a note somebody left at the top — and it never moves and is never removed.

    That second clause is the one worth having: it is what lets a file keep its own header through
    every edit, and what stops "delete the first track" from also deleting the comment above it.
    """
    lines = _lines(target)
    starts: list[int] = []
    texts: list[str] = []
    for index, line in enumerate(lines):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        start = index
        if index and lines[index - 1].strip().upper().startswith("#EXTINF"):
            start = index - 1
        starts.append(start)
        texts.append(text)

    blocks: list[_Block] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        blocks.append(
            _Block(
                lines=tuple(lines[start:end]),
                entry=texts[position],
                absolute=_resolve_entry(root, target.parent, texts[position]),
            )
        )
    preamble = lines[: starts[0]] if starts else lines
    return preamble, blocks


def _body(preamble: Sequence[str], blocks: Sequence[_Block]) -> list[str]:
    """The file again, from its header and its items — the inverse of :func:`_parse`."""
    return [*preamble, *(line for block in blocks for line in block.lines)]


def _resolve_entry(root: Path, base: Path, entry: str) -> Path | None:
    """Turn one M3U line into an absolute path under ``root``, or ``None`` if it is not one.

    Relative entries are resolved against ``base`` — the playlist file's own directory, which is what
    M3U means — and then reduced to a root-relative path so that the single containment check in
    :func:`tunebox.library.resolve_track_path` applies to both spellings. ``..`` is therefore allowed
    *into* the music root (``../Artist/Track.m4a`` is the normal case from a subdirectory) and refused
    out of it.

    Anything that is not a library track comes back ``None``: a remote URL, a line of prose, a
    deleted file, a ``.cue`` sheet. Notably a URL is **not** fetched or passed to the player — a
    playlist is a list of files in this library and nothing else.
    """
    text = entry.replace("\\", "/").strip()
    if not text:
        return None
    try:
        if text.startswith("/"):
            return resolve_track_path(root, text)
        joined = os.path.normpath(os.path.join(base, *PurePosixPath(text).parts))
        relative = Path(os.path.relpath(joined, start=root)).as_posix()
        return resolve_track_path(root, relative)
    except (LibraryError, OSError, ValueError):
        return None


# --------------------------------------------------------------------- writing


def write_playlist(
    root: Path,
    name: str,
    entries: Sequence[str],
    *,
    directory: str = DEFAULT_PLAYLISTS_DIRNAME,
    overwrite: bool = False,
) -> Playlist:
    """Create (or replace) a playlist from library paths.

    ``entries`` are library paths in either spelling ``GET /library`` and ``GET /jobs`` hand out, and
    they are resolved **before anything is written**: a typo in the third one is an error that created
    no file, which is the same all-or-nothing property ``POST /player/queue`` and ``DELETE /library``
    have. Order is kept and duplicates are kept — the same track twice in one playlist is a thing
    people do deliberately, and silently collapsing it would be a surprise in the one file somebody
    arranged by hand.

    **An empty list is allowed**, and that is a deliberate reversal: v1 refused one, which made "save
    the queue" the only way a playlist could come into existence and left somebody who wanted to
    assemble one track by track with nowhere to put the first track. An empty playlist is a valid M3U
    and a usable destination for :func:`append_tracks`.

    This is the one function that *names* a file, so it is the one that sanitises
    (:func:`normalise_name`); everything else addresses an existing one verbatim.
    """
    safe = normalise_name(name)
    target = playlist_path(root, safe, directory=directory)
    if target.exists() and not overwrite:
        raise PlaylistExists(
            f"a playlist called {safe!r} already exists; ask to replace it to overwrite"
        )
    paths = _resolve_all(root, entries)
    _write(target, [HEADER, f"{_COMMENT}{safe}", *(_entry_for(target.parent, p) for p in paths)])
    log.info("wrote playlist %s with %d track(s)", safe, len(paths))
    return read_playlist(root, safe, directory=directory)


def append_tracks(
    root: Path,
    name: str,
    entries: Sequence[str],
    *,
    directory: str = DEFAULT_PLAYLISTS_DIRNAME,
) -> Playlist:
    """Add tracks to the end of an existing playlist.

    Read-modify-write, so last writer wins (see the module docstring). The existing file is carried
    across **line for line** — comments, ``#EXTINF`` and entries that resolve to nothing all stay
    exactly where they are. This verb is "add something", and a user who has never been told about a
    dangling entry (or whose playlist came from another program) must not discover that adding a track
    quietly edited the rest of the file.
    """
    target = playlist_path(root, name, directory=directory)
    if not target.is_file():
        raise PlaylistNotFound(f"no playlist called {exact_name(name)!r}")
    existing = _lines(target)
    additions = [_entry_for(target.parent, path) for path in _resolve_all(root, entries)]
    if not additions:
        raise PlaylistError("nothing to add")
    _, blocks = _parse(root, target)
    if len(blocks) + len(additions) > MAX_ENTRIES:
        raise PlaylistError(
            f"playlist {exact_name(name)!r} would exceed {MAX_ENTRIES} entries "
            f"({len(blocks)} already there, {len(additions)} more)"
        )
    _write(target, [*existing, *additions])
    log.info("appended %d track(s) to playlist %s", len(additions), target.stem)
    return read_playlist(root, name, directory=directory)


def remove_entries(
    root: Path,
    name: str,
    *,
    positions: Sequence[int] | None = None,
    paths: Sequence[str] | None = None,
    directory: str = DEFAULT_PLAYLISTS_DIRNAME,
) -> Playlist:
    """Take entries out of a playlist. No audio is touched.

    Two ways to say which, and exactly one of them per call, because they answer different questions:

    * **``positions``** names lines — ``[1]`` is the second entry and nothing else. This is what a
      remote sends, and it is the only form that can express "remove the *second* copy of this track"
      or "remove this dangling entry", neither of which a path identifies.
    * **``paths``** names tracks, and removes **every** entry that resolves to one of them. This is
      what a terminal wants, and what ``DELETE /library`` needs internally.

    A position that is not in the playlist is an error rather than a silent no-op: the remote reads the
    list two seconds before the tap, so a stale index means somebody else has edited the playlist and
    the right answer is to say so, not to delete whatever is at that index now.

    An entry's ``#EXTINF`` goes with it (see :class:`_Block`), and the file's header and any line that
    is not an entry's own are left exactly where they are.
    """
    if (positions is None) == (paths is None):
        raise PlaylistError("give exactly one of positions or paths")
    target = playlist_path(root, name, directory=directory)
    if not target.is_file():
        raise PlaylistNotFound(f"no playlist called {exact_name(name)!r}")
    preamble, blocks = _parse(root, target)

    if positions is not None:
        wanted = set(positions)
        if not wanted:
            raise PlaylistError("no positions given")
        outside = sorted(p for p in wanted if not 0 <= p < len(blocks))
        if outside:
            raise PlaylistError(
                f"playlist {target.stem!r} has {len(blocks)} entr{'y' if len(blocks) == 1 else 'ies'}, "
                f"so there is nothing at position {', '.join(str(p) for p in outside)}"
            )
        drop = wanted
    else:
        assert paths is not None
        doomed = {
            key for path in paths if (key := _identity(resolve_track_path(root, path))) is not None
        }
        drop = {
            position
            for position, block in enumerate(blocks)
            if block.absolute is not None and _identity(block.absolute) in doomed
        }
        if not drop:
            raise PlaylistError(
                f"playlist {target.stem!r} does not hold "
                + ", ".join(str(path) for path in paths)
            )

    kept = [block for position, block in enumerate(blocks) if position not in drop]
    _write(target, _body(preamble, kept))
    log.info("removed %d entr(y/ies) from playlist %s", len(drop), target.stem)
    return read_playlist(root, name, directory=directory)


def move_entry(
    root: Path,
    name: str,
    position: int,
    to: int,
    *,
    directory: str = DEFAULT_PLAYLISTS_DIRNAME,
) -> Playlist:
    """Move one entry to another position, closing the gap behind it.

    The primitive the remote's ↑ and ↓ buttons send, and the only reordering verb there is. A *move*
    rather than a whole new order, for the reason ``PUT /player/repeat`` takes a mode rather than a
    cycle: two phones can be looking at this list, and an order computed from a two-second-old reading
    would silently undo whatever the other one just did. One move is small enough to be the last
    writer without erasing an edit it never saw.

    ``to`` is the index the entry ends up at **after** it has been lifted out, which is what makes
    ``move(3, 0)`` "put it first" and ``move(0, n - 1)`` "put it last" without the caller having to
    reason about whether its own removal shifted the target.
    """
    target = playlist_path(root, name, directory=directory)
    if not target.is_file():
        raise PlaylistNotFound(f"no playlist called {exact_name(name)!r}")
    preamble, blocks = _parse(root, target)
    for label, index in (("position", position), ("to", to)):
        if not 0 <= index < len(blocks):
            raise PlaylistError(
                f"{label} {index} is outside playlist {target.stem!r}, which has "
                f"{len(blocks)} entr{'y' if len(blocks) == 1 else 'ies'}"
            )
    if position == to:
        # Not an error: a ↑ on the first row is a tap somebody made, and the honest answer is the
        # playlist unchanged rather than a message about a move that would have changed nothing.
        return read_playlist(root, name, directory=directory)

    reordered = list(blocks)
    reordered.insert(to, reordered.pop(position))
    _write(target, _body(preamble, reordered))
    log.info("moved entry %d to %d in playlist %s", position, to, target.stem)
    return read_playlist(root, name, directory=directory)


def rename_playlist(
    root: Path,
    name: str,
    new_name: str,
    *,
    directory: str = DEFAULT_PLAYLISTS_DIRNAME,
) -> Playlist:
    """Rename a playlist. The name is the filename, so this is a rename on disk.

    Here because nothing else can do it: the same ``allow_modifying_stored_playlists = false`` that
    stops OwnTone writing a playlist stops its web UI renaming one, so without this the only way to
    fix a typo in a name from a phone is to rebuild the list under a new one.

    The *new* name is sanitised (:func:`normalise_name`) — it is being used to name a file, which is
    the one situation that sanitises — while the old one is taken verbatim, like every other lookup.
    The contents are not touched at all: the entries are relative to the playlist file's directory and
    the file has not left it.
    """
    target = playlist_path(root, name, directory=directory)
    if not target.is_file():
        raise PlaylistNotFound(f"no playlist called {exact_name(name)!r}")
    safe = normalise_name(new_name)
    destination = playlist_path(root, safe, directory=directory)
    if destination == target:
        return read_playlist(root, safe, directory=directory)
    if destination.exists():
        raise PlaylistExists(f"a playlist called {safe!r} already exists")
    # Our own comment line carries the name, so it would otherwise go stale — a file somebody opens
    # after a rename should not disagree with its own filename. Rewritten *before* the rename so that
    # the worst outcome of a failure here is an edited comment rather than a renamed file with a
    # stale one. Only the line tunebox wrote is touched; a header from another program is left alone.
    preamble, blocks = _parse(root, target)
    refreshed = [
        f"{_COMMENT}{safe}" if line.startswith(_COMMENT) else line for line in preamble
    ]
    if refreshed != preamble:
        _write(target, _body(refreshed, blocks))
    # os.rename and not os.replace: replace would silently overwrite the destination, and the check
    # above is a check, not a lock. Losing a playlist to a race on a name is not a trade worth making.
    os.rename(target, destination)
    log.info("renamed playlist %s to %s", target.stem, safe)
    return read_playlist(root, safe, directory=directory)



def delete_playlist(root: Path, name: str, *, directory: str = DEFAULT_PLAYLISTS_DIRNAME) -> str:
    """Remove the playlist file. Returns its path relative to the music root.

    Nowhere near as serious as :func:`tunebox.library.delete_track` — no audio is touched, and the
    playlist can be rebuilt from a queue in a few taps — so there is no ``409`` for the playlist
    currently playing: once the queue is loaded it holds tracks, not a reference to this file.

    The playlists *directory* is left behind when the last playlist goes, unlike the empty artist
    directories a track delete prunes. It is a fixture of the library rather than a by-product of one:
    it will be wanted again by the next create, and an empty directory shows up in no browse view.
    """
    target = playlist_path(root, name, directory=directory)
    if not target.is_file():
        raise PlaylistNotFound(f"no playlist called {exact_name(name)!r}")
    target.unlink()
    log.info("deleted playlist %s", target.stem)
    return _relative(root, target)


def forget_tracks(
    root: Path,
    paths: Iterable[Path],
    *,
    directory: str = DEFAULT_PLAYLISTS_DIRNAME,
) -> tuple[str, ...]:
    """Drop entries naming any of ``paths`` from every playlist. Returns the ones changed.

    **Call this before the files are unlinked.** That ordering is the whole trick: while the track
    still exists its entry resolves, so it can be matched by *identity* rather than by guessing which
    spelling the file used — and a line that was already dangling for some unrelated reason is left
    exactly where it is instead of being swept up by somebody else's delete.

    Matched on ``(device, inode)`` and not on the path string, which was a measured bug on the
    filesystem this project runs on: macOS is case-insensitive, so an entry spelled
    ``../A/SINGLES/t.m4a`` resolves to the file being deleted while comparing unequal to it as text —
    and the entry survived into a playlist that could never play it. Inode identity also covers the
    NFC/NFD spellings of a CJK filename and a hard link.

    An entry's ``#EXTINF`` line goes with it, since that is the line's own metadata and leaving it
    behind would attach it to the next track.

    Pruning rather than merely reporting, because of what the alternative looks like from the sofa:
    OwnTone skips an entry it cannot resolve and says nothing, so "deleted one track" would silently
    become "that playlist is one shorter forever, and nothing on screen ever mentioned it". The names
    of the playlists that changed are returned so ``DELETE /library`` can say so — the same way it
    already reports the directories it removed.

    Never raises: it is called from the middle of a delete that has already been authorised, and a
    playlist that cannot be rewritten must not fail an unlink. Same rule as the rescan hook.
    """
    doomed = {key for path in paths if (key := _identity(Path(path))) is not None}
    if not doomed:
        return ()

    changed: list[str] = []
    for target in _playlist_files(root, directory):
        try:
            preamble, blocks = _parse(root, target)
            kept = [
                block
                for block in blocks
                if block.absolute is None or _identity(block.absolute) not in doomed
            ]
            if len(kept) == len(blocks):
                continue
            _write(target, _body(preamble, kept))
        except (PlaylistError, LibraryError, OSError) as exc:
            log.warning("could not prune playlist %s: %s", target.name, exc)
            continue
        log.info("pruned %d entr(y/ies) from playlist %s", len(blocks) - len(kept), target.stem)
        changed.append(_relative(root, target))
    return tuple(changed)


def _identity(path: Path) -> tuple[int, int] | None:
    """``(device, inode)`` for a file that exists, else ``None``.

    The one comparison :func:`forget_tracks` trusts. ``None`` for a file that is already gone, which
    is why ``forget_tracks`` must run *before* the unlink: afterwards there would be nothing to
    identify and every match would be a guess about spelling.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_dev, stat.st_ino)


def _resolve_all(root: Path, entries: Sequence[str]) -> list[Path]:
    """Every entry resolved to a real track, or :class:`~tunebox.errors.LibraryError` and no file.

    An empty list comes back empty rather than raising. ``write_playlist`` wants that — an empty
    playlist is a valid destination to add tracks to one at a time — and ``append_tracks`` refuses it
    itself, where "nothing to add" is the accurate message.
    """
    if len(entries) > MAX_ENTRIES:
        raise PlaylistError(f"a playlist may hold at most {MAX_ENTRIES} tracks, got {len(entries)}")
    return [resolve_track_path(root, entry) for entry in entries]


def _entry_for(playlist_dir: Path, track: Path) -> str:
    """One M3U line for ``track``: relative to the playlist file, POSIX separators.

    ``os.path.relpath`` rather than :meth:`pathlib.Path.relative_to`, because the answer has to be
    allowed to climb: from ``<root>/Playlists`` to ``<root>/Artist/Album/Track.m4a`` is
    ``../Artist/Album/Track.m4a``, and ``relative_to`` raises for anything that is not a descendant.
    Neither path is resolved first — a symlinked music root must not be baked into the file, since a
    playlist outliving the symlink is exactly the case relative entries exist for.
    """
    return Path(os.path.relpath(track, start=playlist_dir)).as_posix()


def _write(target: Path, lines: Sequence[str]) -> None:
    """Write the file atomically: staged beside the target, then :func:`os.replace`.

    Atomic because a music daemon is watching this tree — the same requirement
    :func:`tunebox.library.place` has, and for the same reason: a scanner must never read a
    half-written playlist and index half of it. The staging name is dot-prefixed so that the scanner
    skips it even in the instant it exists, which is the same convention a cross-device placement
    uses for ``.Track.m4a.partial``.

    An :class:`OSError` is **not** translated into a :class:`~tunebox.errors.PlaylistError`, and that
    is a deliberate choice about status codes: a read-only music volume or a full disk is not the
    caller's mistake, and answering ``400`` for it would send somebody looking for a typo in their
    request. It is left to surface as the unexpected failure it is.
    """
    body = "\n".join(lines) + "\n"
    staged = target.with_name(f".{target.name}.partial")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(body, encoding="utf-8")
        os.replace(staged, target)
    except OSError:
        staged.unlink(missing_ok=True)  # never leave a half-written list in the library
        raise
