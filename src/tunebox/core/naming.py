"""Turning tags into a safe path under the music root.

Two jobs, both security-relevant (PLAN.md 3.2 "Path safety"):

1. produce the conventional ``Artist/Album/Track.ext`` layout every music daemon understands,
2. guarantee that nothing derived from remote metadata can write outside the music root.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path, PurePosixPath

from ..errors import LibraryError
from .models import TrackTags

#: Album directory used when a track has no album — keeps the tree two levels deep everywhere.
SINGLES_DIR = "Singles"

UNKNOWN_ARTIST = "Unknown Artist"
UNKNOWN_TITLE = "Unknown Title"

#: Characters no path component may contain. Wider than POSIX needs: the library is likely to be
#: shared over SMB to a NAS or read by a Windows client, and ``:`` also confuses some scanners.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')

#: Tabs and newlines become spaces rather than underscores — they are whitespace in intent, and
#: "tab\tand\nnewline" should read as "tab and newline", not "tab_and_newline".
_WHITESPACE_CONTROLS = str.maketrans({c: " " for c in "\t\n\r\f\v"})

#: Windows reserved device names. Cheap to avoid, painful to discover later.
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

#: Per-component limit, in **bytes**: ext4 (the Raspberry Pi target) enforces 255 bytes for
#: NAME_MAX, and one CJK or accented character costs 2–4 of them. 120 leaves room for an
#: extension, a " (2)" collision suffix and a ".partial" staging prefix.
MAX_COMPONENT = 120


def _truncate_bytes(text: str, limit: int) -> str:
    """Cut ``text`` to at most ``limit`` UTF-8 bytes without splitting a character."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", "ignore")


def sanitize_component(value: str | None, fallback: str) -> str:
    """Make ``value`` safe to use as a single path component.

    Never returns an empty string, a traversal component, or a name with a trailing dot or
    space (both of which some filesystems silently strip, breaking later lookups).
    """
    if value is None:
        return fallback
    text = unicodedata.normalize("NFC", value)
    text = text.translate(_WHITESPACE_CONTROLS)
    text = _ILLEGAL.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(". ")
    if text in {"", ".", ".."}:
        return fallback
    if text.upper() in _RESERVED or text.upper().split(".")[0] in _RESERVED:
        text = f"_{text}"
    if len(text.encode("utf-8")) > MAX_COMPONENT:
        text = _truncate_bytes(text, MAX_COMPONENT).strip(". ") or fallback
    return text


def relative_path(tags: TrackTags, extension: str) -> PurePosixPath:
    """``Artist/Album/[NN - ]Title.ext`` — the layout OwnTone, Navidrome, Jellyfin and MPD share."""
    ext = extension.lower().lstrip(".")
    if not ext or _ILLEGAL.search(ext) or "." in ext:
        raise LibraryError(f"unsafe file extension: {extension!r}")

    artist = sanitize_component(tags.artist, UNKNOWN_ARTIST)
    album = sanitize_component(tags.album, SINGLES_DIR) if tags.album else SINGLES_DIR
    title = sanitize_component(tags.title, UNKNOWN_TITLE)

    if tags.track_number:
        stem = f"{int(tags.track_number):02d} - {title}"
    else:
        stem = title

    return PurePosixPath(artist) / album / f"{stem}.{ext}"


def resolve_within(root: Path, relative: PurePosixPath | Path) -> Path:
    """Resolve ``relative`` under ``root``, refusing anything that escapes it.

    Absolute paths, ``..`` and symlinked components are all rejected rather than clamped: a
    silent rewrite would hide the fact that remote metadata tried to leave the library.
    """
    rel = PurePosixPath(relative)
    if rel.is_absolute():
        raise LibraryError(f"refusing absolute library path: {rel}")
    if any(part == ".." for part in rel.parts):
        raise LibraryError(f"refusing path traversal in library path: {rel}")

    root_resolved = root.resolve()
    candidate = (root_resolved / Path(*rel.parts)).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise LibraryError(f"path escapes music root: {candidate} not under {root_resolved}")
    return candidate


def unique_path(path: Path) -> Path:
    """Return ``path``, or ``name (2).ext``-style variant if it is taken.

    Only used when the caller has decided the existing file is a *different* track; dedupe of
    the same track is the caller's business.
    """
    if not path.exists():
        return path
    for n in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise LibraryError(f"too many name collisions for {path}")
