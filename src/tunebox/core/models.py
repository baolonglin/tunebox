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
