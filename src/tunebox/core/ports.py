"""Ports: the interfaces the core depends on. Adapters live in :mod:`tunebox.sources`,
:mod:`tunebox.tagging` and :mod:`tunebox.library`.

Keeping the source behind one protocol is what keeps Alternative E (a licence-clean source)
reachable and what lets the yt-dlp adapter be replaced without touching the use case.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol, Sequence

from .models import FetchedMedia, Progress, SearchResult, TrackTags

ProgressCallback = Callable[[Progress], None]


class MusicSource(Protocol):
    """A place tracks can be searched for and fetched from."""

    name: str

    def search(self, query: str, limit: int | None = None) -> Sequence[SearchResult]:
        """Return candidates for ``query``. Must not download media."""

    def inspect(self, url: str) -> TrackTags:
        """Return the tags tunebox will use for ``url``, without downloading media."""

    def fetch(
        self,
        url: str,
        dest_dir: Path,
        audio_format: str = "m4a",
        on_progress: ProgressCallback | None = None,
    ) -> FetchedMedia:
        """Download audio (and a cover image if available) into ``dest_dir``."""


class TagWriter(Protocol):
    """Writes :class:`TrackTags` into an audio file, optionally embedding cover art."""

    def __call__(
        self, audio_path: Path, tags: TrackTags, cover_path: Path | None = None
    ) -> None: ...
