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

    ``type`` is the daemon's own word for the transport — on this household's hardware the only
    one that matters is ``Chromecast``, because the SN9YG offers no AirPlay (PLAN.md 3.1).
    """

    id: str
    name: str
    type: str = ""
    selected: bool = False
    volume: int | None = None
    #: Cast devices never ask for this, but AirPlay 2 does, and a speaker stuck waiting for a PIN
    #: looks exactly like one that refuses to play. Surfaced so the CLI can say which it is.
    needs_auth_key: bool = False

    @property
    def is_cast(self) -> bool:
        return self.type.lower() in ("chromecast", "cast")

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "selected": self.selected,
            "volume": self.volume,
            "needs_auth_key": self.needs_auth_key,
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


@dataclass(frozen=True)
class PlayerStatus:
    """What the host's playback session is doing right now.

    This is the answer to "is music playing with no phone involved", which is the Phase 3 exit
    criterion, so it deliberately carries the queue item as well as the transport state: "playing"
    with nothing loaded and "playing track 3 of 5" are different facts.
    """

    state: str  # "play", "pause" or "stop"
    volume: int | None = None
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
