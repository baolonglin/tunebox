"""Fakes shared by the Phase 2 tests.

The point of the ports in :mod:`tunebox.core.ports` is that the queue, the worker and the API can
be exercised without yt-dlp, ffmpeg or a network — so these tests run the real store, the real
worker threads and the real FastAPI app, and only the *source* is fake.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path

from tunebox.core.models import FetchedMedia, Progress, SearchResult, TrackTags
from tunebox.errors import SourceError
from tunebox.sources import urls


def video_id(index: int) -> str:
    """A syntactically valid 11-character YouTube id, distinct per index."""
    return f"vid{index:08d}"


def tags_for(vid: str) -> TrackTags:
    """Distinct artist and title per video, so each ingest lands at its own path."""
    return TrackTags(
        title=f"Track {vid}",
        artist=f"Artist {vid}",
        duration=123.0,
        source="fake",
        source_id=vid,
        source_url=f"https://www.youtube.com/watch?v={vid}",
    )


@dataclass
class FakeSource:
    """A :class:`~tunebox.core.ports.MusicSource` that writes bytes instead of downloading."""

    name: str = "fake"
    with_cover: bool = True
    results: list[SearchResult] = field(default_factory=list)
    #: video id -> how many fetches should still fail before one succeeds (retry tests).
    fail_fetch_times: dict[str, int] = field(default_factory=dict)
    #: video ids whose metadata lookup always fails.
    fail_inspect: set[str] = field(default_factory=set)
    #: Waited on inside every fetch, so a test can hold a job mid-flight. ``None`` means no wait.
    gate: threading.Event | None = None
    search_calls: list[tuple[str, int | None]] = field(default_factory=list)
    inspect_calls: list[str] = field(default_factory=list)
    fetch_calls: list[str] = field(default_factory=list)
    _mutex: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def search(self, query: str, limit: int | None = None) -> list[SearchResult]:
        self.search_calls.append((query, limit))
        return list(self.results)

    def inspect(self, url: str) -> TrackTags:
        vid, _ = urls.canonical_video_url(url)
        with self._mutex:
            self.inspect_calls.append(vid)
        if vid in self.fail_inspect:
            raise SourceError(f"inspect {vid} failed on purpose")
        return tags_for(vid)

    def fetch(
        self,
        url: str,
        dest_dir: Path,
        audio_format: str = "m4a",
        on_progress=None,
    ) -> FetchedMedia:
        vid, _ = urls.canonical_video_url(url)
        with self._mutex:
            self.fetch_calls.append(vid)
            remaining = self.fail_fetch_times.get(vid, 0)
            if remaining:
                self.fail_fetch_times[vid] = remaining - 1
        if remaining:
            raise SourceError(f"fetch {vid} failed on purpose ({remaining} attempt(s) left)")
        if self.gate is not None:
            self.gate.wait(timeout=10)
        if on_progress is not None:
            on_progress(Progress(percent=50.0, downloaded_bytes=50, total_bytes=100))
            on_progress(Progress(percent=100.0, downloaded_bytes=100, total_bytes=100))
        audio = dest_dir / f"track.{audio_format}"
        audio.write_bytes(b"fake audio payload for " + vid.encode())
        cover = None
        if self.with_cover:
            cover = dest_dir / "track.jpg"
            cover.write_bytes(b"\xff\xd8\xfffake jpeg")
        return FetchedMedia(audio_path=audio, cover_path=cover)


class RecordingTagWriter:
    """Stands in for mutagen: the fake audio is not a real container."""

    def __init__(self) -> None:
        self.calls: list[tuple[Path, TrackTags, Path | None]] = []
        self._lock = threading.Lock()

    def __call__(self, audio_path: Path, tags: TrackTags, cover_path: Path | None = None) -> None:
        with self._lock:
            self.calls.append((audio_path, tags, cover_path))
