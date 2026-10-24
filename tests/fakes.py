"""Fakes shared by the Phase 2 and Phase 3 tests.

The point of the ports in :mod:`tunebox.core.ports` is that the queue, the worker and the API can
be exercised without yt-dlp, ffmpeg or a network — so these tests run the real store, the real
worker threads and the real FastAPI app, and only the *source* and the *player* are fake.

:class:`FakePlayer` matters for a second reason. Once OwnTone is installed on the development host
it is listening on loopback, which the no-network fixture in ``conftest.py`` deliberately permits;
a test that built the real :class:`~tunebox.player.owntone.OwnTonePlayer` would then quietly start
re-selecting speakers and triggering rescans on the machine running the suite.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path

from tunebox.core.models import (
    REPEAT_MODES,
    FetchedMedia,
    Output,
    PlayerStatus,
    Progress,
    QueueItem,
    SearchResult,
    TrackTags,
)
from tunebox.errors import PlayerUnavailable, SourceError
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


#: The speaker the real system plays to (PLAN.md Phase 0), used as the fake's default so tests read
#: like the thing they describe.
SOUNDBAR = "Living Room speaker"


@dataclass
class FakePlayer:
    """An in-memory :class:`~tunebox.core.ports.Player`.

    Everything a test wants to assert is a counter or a list here rather than a log line, and the
    two interesting failure modes have switches: ``unavailable`` makes every call raise (OwnTone not
    started), and ``present=False`` removes the speaker from the output list (soundbar unplugged).
    """

    name: str = "fake-player"
    base_url: str = "http://player.invalid:3689"
    output_name: str = SOUNDBAR
    output_id: str = "9999"
    output_type: str = "Chromecast"
    #: Is the speaker visible on the network at all?
    present: bool = True
    selected: bool = False
    #: Is the daemon itself answering?
    unavailable: bool = False
    #: Listing the output works but selecting it fails — the race a speaker dropping off Wi-Fi
    #: between two requests actually produces.
    fail_select: bool = False
    #: A specific exception for :meth:`select_output` to raise, for the cases where *which* failure
    #: it is decides the status code: a speaker in standby (``OutputUnreachable``, 503) is not the
    #: same answer as a daemon that is down (``PlayerUnavailable``, also 503) or an absent one (404).
    select_error: Exception | None = None
    state: str = "stop"
    volume: int = 30
    #: One of :data:`tunebox.core.models.REPEAT_MODES`, as the daemon would report it.
    repeat: str = "off"
    shuffle: bool = False
    items: list[QueueItem] = field(default_factory=list)
    #: The queue item the fake considers loaded. ``None`` means "the first one", which is what a
    #: daemon that has never been told otherwise does; :meth:`play_item` sets it.
    current: str | None = None
    rescans: int = 0
    scan_waits: int = 0
    #: ``None`` means "every path is already indexed". A set makes anything outside it unknown to
    #: the player, which is what a file ingested a second ago looks like.
    indexed: set[str] | None = None
    selections: list[tuple[str, int | None]] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    #: Raised by :meth:`rescan` when set, so the "a rescan failure is not an ingest failure" rule
    #: can be tested rather than asserted in a docstring.
    rescan_error: Exception | None = None
    #: Extra outputs beyond the configured one, so the device selector can be tested against a
    #: household that has more than one speaker — which is what this one turned out to have: two
    #: Chromecasts and two AirPlay devices (measured 2026-10-24).
    others: list[Output] = field(default_factory=list)
    #: Which of :attr:`others` are currently enabled. A set rather than a flag on each ``Output``
    #: because ``Output`` is immutable, and rebuilding the list on every read is what keeps the
    #: fake's answer and its state from drifting apart.
    selected_others: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ----------------------------------------------------------------- plumbing

    def _guard(self) -> None:
        if self.unavailable:
            raise PlayerUnavailable(f"player daemon is not answering at {self.base_url}")

    def _outputs(self) -> list[Output]:
        if not self.present:
            return []
        import dataclasses

        return [
            Output(
                id=self.output_id,
                name=self.output_name,
                type=self.output_type,
                selected=self.selected,
                volume=self.volume,
            ),
            *(
                dataclasses.replace(other, selected=other.id in self.selected_others)
                for other in self.others
            ),
        ]

    # ------------------------------------------------------------------- status

    def status(self) -> PlayerStatus:
        self._guard()
        now = next((item for item in self.items if item.id == self.current), None)
        if now is None:
            now = self.items[0] if self.items else None
        return PlayerStatus(
            state=self.state,
            volume=self.volume,
            repeat=self.repeat,
            shuffle=self.shuffle,
            item_id=now.id if now else None,
            now_playing=now,
            outputs=tuple(self._outputs()),
        )

    def outputs(self) -> list[Output]:
        self._guard()
        return self._outputs()

    def target_output(self) -> Output:
        from tunebox.errors import OutputNotFound

        self._guard()
        found = self._outputs()
        if not found:
            raise OutputNotFound(f"no output named {self.output_name!r}; visible now: (none)")
        return found[0]

    def find_output(self, *, output_id: str | None = None, name: str | None = None) -> Output:
        return self.target_output()

    def select_output(self, output_id: str, *, volume: int | None = None) -> None:
        self._guard()
        if self.select_error is not None:
            raise self.select_error
        if self.fail_select:
            raise PlayerUnavailable("the speaker went away between listing and selecting")
        with self._lock:
            self.selections.append((output_id, volume))
            if any(other.id == output_id for other in self.others):
                self.selected_others.add(output_id)
            else:
                # The configured output, or — for the many tests that pass an id they never
                # registered — whatever was asked for. Lenient on purpose: this fake predates
                # having more than one output, and tightening it here would fail a dozen tests
                # about something else entirely.
                self.selected = True
            if volume is not None:
                self.volume = volume

    def deselect_output(self, output_id: str) -> None:
        self._guard()
        if output_id in self.selected_others:
            self.selected_others.discard(output_id)
            return
        self.selected = False

    def select_only(self, output_id: str, *, volume: int | None = None) -> list[str]:
        """Select one output and switch the rest off — the real adapter's ordering, kept.

        Target first, then the others, so a failing enable leaves the previous speaker playing.
        """
        self.select_output(output_id, volume=volume)
        turned_off: list[str] = []
        for output in self._outputs():
            if output.id == output_id or not output.selected:
                continue
            self.deselect_output(output.id)
            turned_off.append(output.id)
        return turned_off

    def set_volume(self, volume: int, *, output_id: str | None = None) -> None:
        self._guard()
        self.volume = max(0, min(100, int(volume)))

    def change_volume(self, step: int, *, output_id: str | None = None) -> None:
        self._guard()
        self.set_volume(self.volume + int(step))

    # ---------------------------------------------------------------- transport

    def play(self) -> None:
        self._guard()
        self.commands.append("play")
        self.state = "play"

    def pause(self) -> None:
        self._guard()
        self.commands.append("pause")
        self.state = "pause"

    def stop(self) -> None:
        self._guard()
        self.commands.append("stop")
        self.state = "stop"

    def toggle(self) -> None:
        self._guard()
        self.commands.append("toggle")
        self.state = "pause" if self.state == "play" else "play"

    def next_track(self) -> None:
        self._guard()
        self.commands.append("next")
        if self.items:
            self.items = self.items[1:]

    def previous_track(self) -> None:
        self._guard()
        self.commands.append("previous")

    def seek(self, position_ms: int) -> None:
        self._guard()
        self.commands.append(f"seek:{position_ms}")

    # -------------------------------------------------------------------- modes

    def set_repeat(self, mode: str) -> None:
        """As strict as :class:`~tunebox.player.owntone.OwnTonePlayer`, deliberately.

        A permissive fake would let a route test pass a mode the real adapter refuses, which is the
        one thing a shared test double must not do — the daemon is the permissive one here (it takes
        ``state=bogus`` as "off"), so the fake being permissive too would leave nobody strict.
        """
        self._guard()
        if mode not in REPEAT_MODES:
            raise ValueError(f"repeat mode must be one of {', '.join(REPEAT_MODES)}, not {mode!r}")
        self.commands.append(f"repeat:{mode}")
        self.repeat = mode

    def set_shuffle(self, enabled: bool) -> None:
        self._guard()
        if not isinstance(enabled, bool):
            raise ValueError(f"shuffle takes a bool, not {type(enabled).__name__}")
        self.commands.append(f"shuffle:{'true' if enabled else 'false'}")
        self.shuffle = enabled

    # -------------------------------------------------------------------- queue

    def queue(self) -> list[QueueItem]:
        self._guard()
        return list(self.items)

    def play_item(self, item_id: str) -> None:
        """Jump to one queue item, and refuse an id that is not in the queue.

        As strict as the real adapter, for the reason :meth:`set_repeat` gives: OwnTone answers a
        bad id with ``400``, so a fake that shrugged would let a route test pass against a request
        the daemon rejects. The route is supposed to have looked in the queue first.
        """
        self._guard()
        if not any(item.id == item_id for item in self.items):
            from tunebox.errors import PlayerError

            raise PlayerError(f"no queue item with id {item_id!r}")
        self.commands.append(f"play_item:{item_id}")
        self.current = item_id
        self.state = "play"

    def clear_queue(self) -> None:
        self._guard()
        self.items = []
        self.current = None

    def enqueue_paths(
        self, paths, *, clear: bool = False, start: bool = False
    ) -> list[QueueItem]:
        self._guard()
        if clear:
            self.items = []
        added: list[QueueItem] = []
        for path in paths:
            path = Path(path)
            item = QueueItem(
                id=str(len(self.items) + len(added) + 1),
                position=len(self.items) + len(added),
                title=path.stem,
                artist=path.parent.parent.name,
                album=path.parent.name,
                path=str(path),
                track_id=str(abs(hash(str(path))) % 100000),
            )
            added.append(item)
        self.items = [*self.items, *added]
        if start:
            self.state = "play"
        return added

    def resolve_track(self, path, *, timeout: float = 0.0) -> dict:
        self._guard()
        if self.indexed is not None and str(Path(path)) not in self.indexed:
            from tunebox.errors import PlayerError

            raise PlayerError(f"{path} is not in the player's library.")
        return {"id": str(abs(hash(str(path))) % 100000), "path": str(Path(path))}

    def wait_for_scan(self, timeout: float = 30.0, poll: float = 0.25) -> bool:
        self._guard()
        self.scan_waits += 1
        return True

    # ------------------------------------------------------------------ library

    def rescan(self) -> None:
        self._guard()
        with self._lock:
            self.rescans += 1
        if self.rescan_error is not None:
            raise self.rescan_error

    def library_info(self) -> dict:
        self._guard()
        return {"songs": len(self.items), "updating": False}

    def ping(self) -> dict:
        self._guard()
        return {"version": "fake"}
