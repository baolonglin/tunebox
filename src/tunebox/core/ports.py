"""Ports: the interfaces the core depends on. Adapters live in :mod:`tunebox.sources`,
:mod:`tunebox.tagging`, :mod:`tunebox.library` and :mod:`tunebox.player`.

Keeping the source behind one protocol is what keeps Alternative E (a licence-clean source)
reachable and what lets the yt-dlp adapter be replaced without touching the use case.

:class:`Player` exists for the same reason, and the reason is written down in PLAN.md 3.1 as a
risk: OwnTone's Chromecast output is the less-travelled half of OwnTone, and if it disappoints,
fallback A is to drive Cast ourselves with ``pychromecast``. That swap is only cheap if nothing
above the adapter knows which daemon is playing, so everything above it talks to this protocol.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol, Sequence

from .models import FetchedMedia, Output, PlayerStatus, Progress, QueueItem, SearchResult, TrackTags

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


class Player(Protocol):
    """The host's playback session: it owns the queue, so the phone does not (PLAN.md 2).

    Every method here is a *command to a daemon that keeps running without us*. Nothing in this
    protocol streams audio, holds a session, or needs the caller to stay alive — that is the
    property the whole of Phase 3 is about, and it is why the interface is this small.

    Paths, not track ids, are the vocabulary: :mod:`tunebox.ingest` returns a path, the filesystem
    is the library database (PLAN.md 3.1), and mapping a path to whatever the daemon calls that
    track is the adapter's problem rather than the caller's.
    """

    name: str

    def status(self) -> PlayerStatus:
        """Transport state, volume, and the track currently loaded."""

    def outputs(self) -> Sequence[Output]:
        """Every speaker the daemon can see right now, selected or not."""

    def select_output(self, output_id: str, *, volume: int | None = None) -> None:
        """Enable one output, leaving others alone."""

    def set_volume(self, volume: int, *, output_id: str | None = None) -> None: ...

    def play(self) -> None:
        """Start or resume. Must not require anything to be enqueued first."""

    def pause(self) -> None: ...

    def stop(self) -> None: ...

    def next_track(self) -> None: ...

    def previous_track(self) -> None: ...

    def set_repeat(self, mode: str) -> None:
        """Repeat nothing, the whole queue, or one song — one of :data:`~tunebox.core.models.REPEAT_MODES`.

        A *mode*, not a toggle, because the caller is stateless: two phones and the daemon's own web
        UI share this session (PLAN.md 2), so "set single" converges where "cycle" would ping-pong.

        Must reject anything outside :data:`~tunebox.core.models.REPEAT_MODES` rather than passing it
        on. OwnTone answers ``204`` to ``state=bogus`` and quietly takes it as ``off`` (measured
        2026-09-27), so a daemon-side check is not a check at all.
        """

    def set_shuffle(self, enabled: bool) -> None:
        """Shuffle on or off. Takes a real ``bool``, for the reason :meth:`set_repeat` gives."""

    def queue(self) -> Sequence[QueueItem]: ...

    def play_item(self, item_id: str) -> None:
        """Start playback at the queue item with this id, wherever it sits in the queue.

        The one method here whose vocabulary is an id rather than a path, and deliberately so: the
        caller is naming something *already in the queue*, which this protocol has always identified
        by :attr:`~tunebox.core.models.QueueItem.id` and reported as
        :attr:`~tunebox.core.models.PlayerStatus.item_id`. Nothing about which daemon is playing
        leaks out that was not already out, and a path would be worse than indirect — the same file
        can legitimately be in the queue twice, so a path names no single item.

        An id and not a position, for the reason :meth:`set_repeat` takes a mode and not a cycle:
        this session is shared (PLAN.md 2), so a position is relative to a queue snapshot another
        phone can invalidate between the poll and the tap, while an id either names the track
        somebody meant or names nothing at all.

        Callers are expected to have the item in hand. What a player does with an id that has since
        been removed is its own business — see :mod:`tunebox.api.remote`, which checks the queue
        first so that a stale remote is answered ``404`` rather than whatever the daemon says.
        """

    def clear_queue(self) -> None: ...

    def enqueue_paths(
        self, paths: Sequence[Path], *, clear: bool = False, start: bool = False
    ) -> Sequence[QueueItem]:
        """Add library files to the queue, optionally replacing it and starting playback."""

    def rescan(self) -> None:
        """Ask the daemon to pick up files that appeared under the music root.

        Called after every successful ingest (PLAN.md Phase 3). Must be cheap and must be safe to
        call when nothing changed.
        """

