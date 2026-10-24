"""Domain models. Pure data, no I/O, no knowledge of yt-dlp."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SearchResult:
    """One candidate from a source search. Deliberately small: this is what a phone needs."""

    id: str
    title: str
    url: str
    uploader: str | None = None
    duration: float | None = None
    thumbnail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "uploader": self.uploader,
            "duration": self.duration,
            "thumbnail": self.thumbnail,
        }


@dataclass(frozen=True)
class TrackTags:
    """The metadata tunebox considers true for a track.

    This is the single source of truth (PLAN.md 3.4): it names the file on disk, it is written
    into the file by the tag writer, and in Phase 3 it is what populates the Cast ``LOAD``
    payload. Nothing downstream re-derives metadata from the source.
    """

    title: str
    artist: str
    album: str | None = None
    album_artist: str | None = None
    year: int | None = None
    track_number: int | None = None
    duration: float | None = None
    source: str | None = None  # adapter name, e.g. "youtube"
    source_id: str | None = None  # e.g. the video id — the Phase 2 dedupe key
    source_url: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "album_artist": self.album_artist,
            "year": self.year,
            "track_number": self.track_number,
            "duration": self.duration,
            "source": self.source,
            "source_id": self.source_id,
            "source_url": self.source_url,
        }


@dataclass(frozen=True)
class FetchedMedia:
    """What a source adapter leaves in a scratch directory: audio, and maybe a cover image."""

    audio_path: Path
    cover_path: Path | None = None


@dataclass(frozen=True)
class Progress:
    """A single progress sample from a fetch, normalised away from yt-dlp's formatting."""

    percent: float | None = None
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    speed: float | None = None
    eta: int | None = None


@dataclass(frozen=True)
class Output:
    """One speaker the player daemon can send audio to (PLAN.md Phase 3).

    ``type`` is the daemon's own word for the transport. Phase 3 only had to care about
    ``Chromecast``, because the household's soundbar offers no AirPlay — but the device selector
    lists everything the daemon can see, and on this network that is two Chromecasts *and* two
    AirPlay 2 devices, which fail in entirely different ways. Hence :attr:`needs_pairing`.
    """

    id: str
    name: str
    type: str = ""
    selected: bool = False
    volume: int | None = None
    #: Cast devices never ask for this, but AirPlay 2 does, and a speaker stuck waiting for a PIN
    #: looks exactly like one that refuses to play. Surfaced so the CLI can say which it is.
    needs_auth_key: bool = False
    #: The daemon has established that this output wants a PIN or a password. **Discovered rather
    #: than known**: measured against OwnTone 29.3 on 2026-10-24, a Mac advertising itself as an
    #: AirPlay receiver reported ``requires_auth: false`` until the first enable attempt failed, and
    #: ``true`` from then on. So this being false is not a promise that a speaker will work; this
    #: being true is a promise that it will not, until somebody pairs it.
    requires_auth: bool = False

    @property
    def is_cast(self) -> bool:
        return self.type.lower() in ("chromecast", "cast")

    @property
    def needs_pairing(self) -> bool:
        """Will this output refuse to start until somebody enters a PIN or a password?

        The distinction worth having, because the fix is completely different from the one for every
        other selection failure: retrying cannot help, and nothing tunebox does can help either —
        the pairing happens in OwnTone's own web UI (``POST /api/outputs/{id}/verification``), which
        is deliberately not proxied here (PLAN.md 3.2).
        """
        return self.requires_auth or self.needs_auth_key

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "selected": self.selected,
            "volume": self.volume,
            "needs_auth_key": self.needs_auth_key,
            "requires_auth": self.requires_auth,
        }


@dataclass(frozen=True)
class QueueItem:
    """One entry in the daemon's queue. ``path`` is what ties it back to a tunebox ingest."""

    id: str
    position: int
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    length_ms: int | None = None
    path: str | None = None
    track_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "position": self.position,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "length_ms": self.length_ms,
            "path": self.path,
            "track_id": self.track_id,
        }


#: The three repeat modes, in the daemon's own spelling: nothing, the whole queue, one song.
#:
#: Here rather than in the adapter because it is the *vocabulary* of a playback mode and three
#: layers need it — the adapter validates against it, ``tunebox.api.schemas`` builds its ``Literal``
#: from it, and :attr:`PlayerStatus.repeat` reports one of them. A second spelling of this list
#: would be a mode the API accepted and the player rejected, or worse, the reverse.
REPEAT_MODES = ("off", "all", "single")


@dataclass(frozen=True)
class PlayerStatus:
    """What the host's playback session is doing right now.

    This is the answer to "is music playing with no phone involved", which is the Phase 3 exit
    criterion, so it deliberately carries the queue item as well as the transport state: "playing"
    with nothing loaded and "playing track 3 of 5" are different facts.
    """

    state: str  # "play", "pause" or "stop"
    volume: int | None = None
    #: One of :data:`REPEAT_MODES`, or ``None`` from a daemon that did not say. Read *and* written
    #: (``Player.set_repeat``); ``consume`` below is read-only on purpose.
    repeat: str | None = None
    shuffle: bool = False
    consume: bool = False
    item_id: str | None = None
    item_length_ms: int | None = None
    item_progress_ms: int | None = None
    now_playing: QueueItem | None = None
    outputs: tuple[Output, ...] = ()

    @property
    def playing(self) -> bool:
        return self.state == "play"

    @property
    def selected_outputs(self) -> tuple[Output, ...]:
        return tuple(output for output in self.outputs if output.selected)

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "volume": self.volume,
            "repeat": self.repeat,
            "shuffle": self.shuffle,
            "consume": self.consume,
            "item_id": self.item_id,
            "item_length_ms": self.item_length_ms,
            "item_progress_ms": self.item_progress_ms,
            "now_playing": self.now_playing.as_dict() if self.now_playing else None,
            "outputs": [output.as_dict() for output in self.outputs],
        }


@dataclass(frozen=True)
class IngestResult:
    path: Path
    tags: TrackTags
    already_present: bool = False
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "already_present": self.already_present,
            "tags": self.tags.as_dict(),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class LibraryTrack:
    """One audio file under the music root, described by *where it is* (PLAN.md Phase 4).

    Deliberately derived from the path rather than by reading tags. The filesystem is the library
    database (PLAN.md 3.1) and :mod:`tunebox.core.naming` put the tags *into* the path on the way
    in, so the path already carries the answer — while opening every file with mutagen would turn
    browsing a thousand-track library into a thousand file reads on an SD card.

    :attr:`path` is **relative** to the music root, POSIX-style. That is not cosmetic: it is the
    only thing a remote ever sends back when asking to play something, so the absolute path is
    reconstructed server-side and traversal is structurally impossible rather than filtered.
    """

    path: str
    title: str
    artist: str | None = None
    album: str | None = None
    track_number: int | None = None
    size: int | None = None
    modified: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "track_number": self.track_number,
            "size": self.size,
            "modified": self.modified,
        }


@dataclass(frozen=True)
class DeletedTrack:
    """What one delete removed: the track, and the directories it left empty behind it.

    ``removed_directories`` is reported rather than merely done, because it is the part nobody
    asked for: deleting the last track in ``Artist/Singles/`` removes two directories, and a UI
    that said "1 track deleted" while three things disappeared from OwnTone's browse view would be
    hiding the interesting half. Both spellings are **relative** to the music root, for the same
    reason as :attr:`LibraryTrack.path`.
    """

    path: str
    removed_directories: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "removed_directories": list(self.removed_directories),
        }


@dataclass(frozen=True)
class PlaylistEntry:
    """One line of a playlist: where it is in the list, what it says, and what it resolves to.

    :attr:`position` is the field that makes editing possible at all, and it is why this type exists
    rather than a bare list of :class:`LibraryTrack`. A playlist may legitimately hold the same track
    twice, and it may hold entries that resolve to nothing — so neither a path nor an index into the
    *resolvable* tracks identifies a line. "Remove the second copy" and "move line four up" need the
    position of the entry in the **file**, and anything else is a client guessing.

    :attr:`track` is ``None`` for an entry that names no library track: a file deleted by hand, a
    playlist restored beside a library that moved, a stream URL, a line somebody typed. Reported with
    its :attr:`entry` text rather than dropped, because a playlist that silently shrinks is the
    failure worth naming — OwnTone's own behaviour is to skip such a line without saying so.
    """

    position: int
    #: The raw text of the line, as it appears in the file.
    entry: str
    track: LibraryTrack | None = None

    @property
    def missing(self) -> bool:
        return self.track is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "entry": self.entry,
            "missing": self.missing,
            "track": self.track.as_dict() if self.track else None,
        }


@dataclass(frozen=True)
class Playlist:
    """One playlist: a name, the file it is, and the entries it holds in order.

    A playlist in tunebox is **a file, not a table** (PLAN.md 3.1: the filesystem is the library
    database). So this carries no id — :attr:`name` is the identity, because it is the filename, and
    there is nowhere else for an id to come from or to drift out of step with.

    :attr:`entries` is the single ordered truth, one per line of the file that names something.
    :attr:`tracks` and :attr:`missing` are views onto it for the two callers that only want one half —
    playback, which needs the resolvable ones, and a UI that wants to say how many are not.

    :attr:`count` and :attr:`missing_count` are fields rather than lengths of :attr:`entries` because
    a **listing** of playlists leaves ``entries`` empty rather than sending every track in the library
    back for a sidebar — and both numbers are what a row on a phone says. Without them, a summary and
    an empty playlist would be the same document, and "4 tracks · 1 missing" would need the whole list.
    """

    name: str
    #: The ``.m3u`` file, relative to the music root — ``Playlists/Saturday morning.m3u``.
    path: str
    #: Entries that resolve to a real track: what will play.
    count: int = 0
    #: Entries that resolve to nothing. Its own number because it is the half OwnTone never mentions.
    missing_count: int = 0
    modified: float | None = None
    entries: tuple[PlaylistEntry, ...] = ()

    @property
    def tracks(self) -> tuple[LibraryTrack, ...]:
        """Just the entries that resolve, in order — what gets handed to the player."""
        return tuple(entry.track for entry in self.entries if entry.track is not None)

    @property
    def missing(self) -> tuple[str, ...]:
        """The raw text of every entry that resolves to nothing."""
        return tuple(entry.entry for entry in self.entries if entry.track is None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "count": self.count,
            "missing_count": self.missing_count,
            "modified": self.modified,
            "entries": [entry.as_dict() for entry in self.entries],
        }


@dataclass(frozen=True)
class LibraryPage:
    """A page of :class:`LibraryTrack`, plus how much of the library it is speaking for.

    ``truncated`` is a separate fact from ``total``: the first means "the walk gave up before
    seeing everything", the second "there are more matches than this page holds". A UI that
    conflates them would silently claim a small library.
    """

    tracks: tuple[LibraryTrack, ...] = ()
    total: int = 0
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "tracks": [track.as_dict() for track in self.tracks],
            "total": self.total,
            "truncated": self.truncated,
        }
