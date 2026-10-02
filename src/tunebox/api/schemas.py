"""Request and response shapes.

Requests are validated by pydantic because they are the untrusted edge (PLAN.md 3.2): unknown
fields are rejected rather than ignored, so a typo in a client is a 422 instead of a silently
dropped ``--artist``. The URL itself is *not* validated here — that is
:func:`tunebox.sources.urls.canonical_video_url`'s job, and there is exactly one allowlist.

Responses mirror the dataclasses in :mod:`tunebox.jobs.models` and :mod:`tunebox.core.models`
rather than introducing a second vocabulary, so ``GET /jobs/{id}`` and the CLI's JSON describe
the same thing with the same names.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from ..config import SUPPORTED_FORMATS
from ..core.models import REPEAT_MODES


class IngestRequest(BaseModel):
    """``POST /ingest`` body. Tag overrides match the CLI's ``--artist`` family."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(description="YouTube URL or bare 11-character video id")
    format: Literal[SUPPORTED_FORMATS] | None = None  # type: ignore[valid-type]
    artist: str | None = None
    title: str | None = None
    album: str | None = None
    album_artist: str | None = None
    year: int | None = Field(default=None, ge=1000, le=2999)
    track_number: int | None = Field(default=None, ge=1, le=999)
    force: bool = Field(
        default=False,
        description="re-download even if this video is already in the library",
    )

    def overrides(self) -> dict[str, Any]:
        """Only the fields the caller actually set — ``None`` must not erase a mapped tag."""
        fields = ("artist", "title", "album", "album_artist", "year", "track_number")
        return {name: getattr(self, name) for name in fields if getattr(self, name) is not None}


class JobView(BaseModel):
    id: str
    state: Literal["queued", "running", "done", "failed"]
    url: str
    video_id: str
    audio_format: str
    force: bool
    attempts: int
    max_attempts: int
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    retry_after: str | None = None
    progress: float | None = None
    error: str | None = None
    path: str | None = None
    already_present: bool = False
    warnings: list[str] = Field(default_factory=list)
    tags: dict[str, Any] | None = None


class SearchItem(BaseModel):
    id: str
    title: str
    url: str
    uploader: str | None = None
    duration: float | None = None
    thumbnail: str | None = None


class PlayerHealth(BaseModel):
    """What ``GET /healthz`` can say about the playback session without being able to fail.

    Phase 3's exit criterion is "music plays with no phone involved at all", and the honest way to
    check that from a second device is to ask the host what it thinks is happening. ``reachable``
    is false rather than absent when OwnTone is down, because "no player" and "player not
    answering" need different fixes.
    """

    reachable: bool
    url: str
    state: str | None = None
    volume: int | None = None
    now_playing: str | None = None
    queue_length: int | None = None
    #: The configured speaker, and whether the daemon currently has it enabled.
    output: str | None = None
    output_selected: bool | None = None
    error: str | None = None


class HealthView(BaseModel):
    status: Literal["ok"]
    version: str
    music_root: str
    audio_format: str
    worker_running: bool
    workers: int
    jobs: dict[str, int]
    player: PlayerHealth | None = None


class ErrorView(BaseModel):
    error: str


# ------------------------------------------------------------------ Phase 4: the remote
#
# These mirror :mod:`tunebox.core.models` field for field, so the web UI, the CLI's JSON and
# ``/docs`` all describe the playback session with one vocabulary. The request bodies are the
# interesting half: they are the untrusted edge of the *player*, and everything a remote can ask
# for is enumerated here rather than forwarded.


class OutputView(BaseModel):
    id: str
    name: str
    type: str = ""
    selected: bool = False
    volume: int | None = None
    needs_auth_key: bool = False


class QueueItemView(BaseModel):
    id: str
    position: int
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    length_ms: int | None = None
    path: str | None = None
    track_id: str | None = None


class PlayerView(BaseModel):
    """Everything the remote needs to draw a now-playing screen, in one response.

    One response on purpose: the UI polls this while it is open, and two round trips per second
    would be two chances for the transport state and the queue to disagree on screen.
    """

    url: str
    state: str
    volume: int | None = None
    repeat: str | None = None
    shuffle: bool = False
    consume: bool = False
    item_id: str | None = None
    item_length_ms: int | None = None
    item_progress_ms: int | None = None
    now_playing: QueueItemView | None = None
    outputs: list[OutputView] = Field(default_factory=list)
    queue: list[QueueItemView] = Field(default_factory=list)
    #: The *configured* speaker as the daemon currently sees it, or ``None`` when it is not visible
    #: — which is what an unplugged soundbar looks like, and is why the UI can offer a "select"
    #: button instead of leaving someone wondering why there is no sound.
    output: OutputView | None = None
    output_selected: bool = False
    #: The daemon says it is playing and **no** output at all is enabled, so the audio is going
    #: nowhere. Its own field rather than something the client derives, because it is the one state
    #: where every other thing in this response looks healthy: measured on the real daemon, OwnTone
    #: logs ``All selected speakers failed to start`` and then keeps ``state: play`` with
    #: ``item_progress_ms`` advancing, so a remote that trusted the transport drew a moving progress
    #: bar for a silent room. Any selected output counts, not just the configured one — audio
    #: reaching the wrong speaker is a different complaint from audio reaching none.
    playing_without_output: bool = False


class VolumeRequest(BaseModel):
    """Absolute or relative, never both. A step is what a ``+``/``−`` button sends."""

    model_config = ConfigDict(extra="forbid")

    volume: int | None = Field(default=None, ge=0, le=100)
    step: int | None = Field(default=None, ge=-100, le=100)

    @model_validator(mode="after")
    def _exactly_one(self) -> "VolumeRequest":
        if (self.volume is None) == (self.step is None):
            raise ValueError("send exactly one of volume (0-100) or step (-100..100)")
        return self


class SeekRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position_ms: int = Field(ge=0)


class RepeatRequest(BaseModel):
    """``PUT /player/repeat`` body.

    The ``Literal`` is built from :data:`tunebox.core.models.REPEAT_MODES` rather than spelled again,
    so the API and the adapter cannot disagree about what a mode is. This is the *first* of the two
    checks and the only one a client sees: the daemon itself answers ``204`` to ``state=bogus`` and
    takes it as ``off``, so an unvalidated typo would read as success and change the mode.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal[REPEAT_MODES]  # type: ignore[valid-type]


class ShuffleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: ``StrictBool``, not ``bool``. pydantic's ordinary boolean also accepts ``"yes"``, ``"on"``,
    #: ``1`` and ``"True"``, which would make this edge *looser* than the adapter behind it — and the
    #: adapter is strict for a measured reason (``Player.set_shuffle``). One rule stated once: a JSON
    #: ``true`` or ``false``, anything else a 422.
    enabled: StrictBool


class QueueRequest(BaseModel):
    """Add library files to the queue.

    ``paths`` are library paths — either relative to the music root (what ``GET /library`` hands
    out) or the absolute path a finished job reports. Both are rebuilt and re-checked against the
    music root by :func:`tunebox.library.resolve_track_path`, so no request body can name a file
    outside the library (PLAN.md 3.2). The bound on the list is not politeness either — each entry
    costs one lookup in the player's index.
    """

    model_config = ConfigDict(extra="forbid")

    paths: list[str] = Field(min_length=1, max_length=100)
    #: ``replace`` is "play this now" and empties the queue first; ``append`` is "play it after
    #: whatever is on".
    mode: Literal["append", "replace"] = "append"
    start: bool = Field(default=False, description="begin playback once the files are added")


class QueueAddedView(BaseModel):
    added: list[QueueItemView] = Field(default_factory=list)
    queue: list[QueueItemView] = Field(default_factory=list)


class LibraryTrackView(BaseModel):
    #: Relative to the music root, POSIX-style. Send it back verbatim to play it.
    path: str
    title: str
    artist: str | None = None
    album: str | None = None
    track_number: int | None = None
    size: int | None = None
    modified: float | None = None


class LibraryView(BaseModel):
    tracks: list[LibraryTrackView] = Field(default_factory=list)
    #: Matches found, which may exceed the page returned.
    total: int = 0
    #: The walk hit its file ceiling and stopped; the answer is incomplete, not merely paged.
    truncated: bool = False


class LibraryDeleteRequest(BaseModel):
    """``DELETE /library`` body — the only destructive request tunebox accepts.

    A body rather than a path parameter, and the same ``{"paths": [...]}`` shape as
    ``POST /player/queue``: a library path contains slashes and, in this household, CJK, and putting
    one through URL encoding twice to build ``DELETE /library/{path}`` is how a delete ends up
    naming the wrong file. It also makes "delete these three" one request that either resolves
    entirely or changes nothing.
    """

    model_config = ConfigDict(extra="forbid")

    paths: list[str] = Field(min_length=1, max_length=100)


class LibraryDeletedView(BaseModel):
    """What was removed. Paths are relative to the music root, as everywhere else."""

    deleted: list[str] = Field(default_factory=list)
    #: Directories that emptied as a result and were removed — reported because they are visible in
    #: OwnTone's browse view even though ``GET /library`` never showed them.
    removed_directories: list[str] = Field(default_factory=list)
    #: Non-fatal problems, currently only "the player could not be told to re-index". The file is
    #: gone either way, which is why this is a field and not a status code.
    warnings: list[str] = Field(default_factory=list)
