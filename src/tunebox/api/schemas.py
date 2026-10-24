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
    #: The daemon has established that this output wants a PIN or a password, so selecting it will
    #: fail until somebody pairs it in OwnTone's own web UI. Discovered rather than known — see
    #: :attr:`tunebox.core.models.Output.requires_auth` — so ``false`` is not a promise it will work.
    requires_auth: bool = False
    #: ``requires_auth`` or ``needs_auth_key``: one field for the UI, because the two spellings have
    #: the same cause and exactly the same fix.
    needs_pairing: bool = False
    #: May the remote switch playback to this output? ``TUNEBOX_OUTPUT_ALLOW`` decides, and the
    #: answer is computed here rather than in the browser so that the rule lives on the side of the
    #: wire that enforces it — the device selector greys out what ``PUT /player/output`` would
    #: refuse, instead of offering it and reporting a 403 after the tap.
    selectable: bool = True


class QueueItemView(BaseModel):
    id: str
    position: int
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    length_ms: int | None = None
    path: str | None = None
    track_id: str | None = None


class OutputSelectRequest(BaseModel):
    """``PUT /player/output`` body — which speaker the music should come out of.

    The one request body in this project that takes an identifier the *daemon* owns, which is why
    the route around it does two checks the others do not need: the id must be in the daemon's
    current output list (so a made-up one is a ``404`` from here and is never forwarded), and it
    must pass ``TUNEBOX_OUTPUT_ALLOW``. OwnTone lists every Cast and AirPlay device it can see over
    mDNS, and mDNS does not stop at the front door.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=200, description="an id from GET /player's `outputs`")


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
    #: Playlists that named one of the deleted tracks and had the entry removed. Reported for the
    #: same reason as ``removed_directories``: it is the part of the delete nobody asked for, and a
    #: playlist that silently got shorter is exactly the sort of thing a UI should be able to mention.
    playlists_updated: list[str] = Field(default_factory=list)
    #: Non-fatal problems, currently only "the player could not be told to re-index". The file is
    #: gone either way, which is why this is a field and not a status code.
    warnings: list[str] = Field(default_factory=list)


# ------------------------------------------------- playing on the phone itself
#
# The one thing a browser needs from the server in order to be an output: a URL it can put in
# ``<audio src>``. See :mod:`tunebox.api.media` for why that cannot be a token-protected URL and
# :mod:`tunebox.core.tickets` for what makes a capability URL an acceptable substitute.


class MediaTicketRequest(BaseModel):
    """``POST /library/tickets`` body — the same ``{"paths": [...]}`` shape as everything else.

    A list rather than one path per request because the caller is building a queue: a phone that
    minted one ticket per track would make a request per tap, and the whole point of this route is
    that it is the *only* thing phone-local playback asks the server for.
    """

    model_config = ConfigDict(extra="forbid")

    paths: list[str] = Field(min_length=1, max_length=100)


class MediaTicketView(BaseModel):
    """One capability URL. ``path`` is echoed so a caller can match it to what it asked for."""

    path: str
    #: Relative to this service, e.g. ``/media/eyJ…``. Relative on purpose: the page already knows
    #: the host it loaded from, and an absolute URL would have to guess at it from the server side —
    #: the same mistake ``TUNEBOX_OWNTONE_UI`` exists to avoid.
    url: str
    #: What the response will be labelled, so a client can decide whether it can play the file at
    #: all before fetching a note of it.
    content_type: str


class MediaTicketsView(BaseModel):
    tickets: list[MediaTicketView] = Field(default_factory=list)
    #: Seconds these are good for. Advisory — the ticket carries its own expiry, signed.
    expires_in: int


# ------------------------------------------------------------------ playlists
#
# A playlist is an M3U file in the music root (see :mod:`tunebox.playlists`), so there is no id here
# and no database behind it: the name *is* the filename. These mirror
# :class:`tunebox.core.models.Playlist`, with one addition that the model deliberately does not carry
# — ``skipped``, which is about one request rather than about the playlist.


class PlaylistEntryView(BaseModel):
    """One line of a playlist. ``position`` is how an edit names it — see the request bodies below."""

    position: int
    #: The raw text of the line in the file. The only thing there is to show for a missing entry.
    entry: str
    missing: bool = False
    track: LibraryTrackView | None = None


class PlaylistView(BaseModel):
    name: str
    #: The ``.m3u`` file, relative to the music root.
    path: str
    #: Number of entries that resolve to a real track. ``GET /playlists`` leaves ``entries`` empty and
    #: fills this, so a summary and an empty playlist are distinguishable.
    count: int = 0
    #: Entries that resolve to nothing — the half OwnTone skips without saying so. A number of its own
    #: so that a listing can say "4 tracks · 1 missing" without carrying every entry.
    missing_count: int = 0
    modified: float | None = None
    #: Every line that names something, in file order — including the ones that resolve to nothing,
    #: flagged rather than dropped. The order *is* the playlist, and ``position`` is what
    #: ``DELETE /playlists/{name}/tracks`` and ``PUT /playlists/{name}/tracks/{position}`` take.
    entries: list[PlaylistEntryView] = Field(default_factory=list)
    #: Only ever non-empty from ``from_queue``: queue items that are not library tracks (a stream, a
    #: file deleted since it was queued) and were left out instead of failing the save.
    skipped: list[str] = Field(default_factory=list)


class PlaylistCreateRequest(BaseModel):
    """``POST /playlists`` — from explicit paths, from the queue, or empty.

    ``paths`` and ``from_queue`` are mutually exclusive, and **neither is required**: a name on its own
    creates an empty playlist. That is the entry point for assembling one a track at a time from the
    Library panel, and its absence was the complaint that "only the queue can create a playlist".

    Where they differ is what an unusable entry means. ``paths`` came from a client that read them out
    of ``GET /library``, so a path that does not resolve is that client's bug and the whole request
    fails. The *queue* is shared state that tunebox did not necessarily fill (OwnTone's own web UI can
    add to it), so an item that is not a library track is skipped and reported — the alternative is a
    "save the queue" button that cannot save this queue and cannot say which entry is the problem.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    paths: list[str] | None = Field(default=None, max_length=1000)
    from_queue: bool = False
    #: Overwrite a playlist of the same name. Off by default, so creating twice is a ``409`` rather
    #: than silently discarding whatever was in the first one.
    replace: bool = False

    @model_validator(mode="after")
    def _at_most_one_source(self) -> "PlaylistCreateRequest":
        if self.paths is not None and self.from_queue:
            raise ValueError("send at most one of paths or from_queue")
        if self.paths is not None and not self.paths:
            raise ValueError("paths must not be empty; omit it to create an empty playlist")
        return self


class PlaylistTracksRequest(BaseModel):
    """``POST /playlists/{name}/tracks`` — append. Same ``paths`` shape as the queue route."""

    model_config = ConfigDict(extra="forbid")

    paths: list[str] = Field(min_length=1, max_length=1000)


class PlaylistRemoveRequest(BaseModel):
    """``DELETE /playlists/{name}/tracks`` — take entries out. Exactly one of two ways to say which.

    ``positions`` names **lines**: ``[1]`` is the second entry and nothing else. It is the only form
    that can express "remove the second copy of this track" or "remove that dangling entry", so it is
    what the remote sends. ``paths`` names **tracks** and removes every entry resolving to one of them,
    which is what a terminal wants to type.

    A body rather than a path parameter for the same reason ``DELETE /library`` has one: this removes
    several at once, all-or-nothing, and a library path in a URL is how a delete names the wrong thing.
    """

    model_config = ConfigDict(extra="forbid")

    positions: list[int] | None = Field(default=None, max_length=1000)
    paths: list[str] | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _exactly_one(self) -> "PlaylistRemoveRequest":
        if (self.positions is None) == (self.paths is None):
            raise ValueError("send exactly one of positions or paths")
        if self.positions is not None:
            if not self.positions:
                raise ValueError("positions must not be empty")
            if any(position < 0 for position in self.positions):
                raise ValueError("positions are zero-based and must not be negative")
        elif not self.paths:
            raise ValueError("paths must not be empty")
        return self


class PlaylistMoveRequest(BaseModel):
    """``PUT /playlists/{name}/tracks/{position}`` — the reorder primitive, one entry at a time.

    A move rather than a whole new order, for the reason ``PUT /player/repeat`` takes a mode rather
    than a cycle: two phones can be looking at this list, and an order computed from a two-second-old
    reading would silently undo whatever the other one just did.

    ``to`` is where the entry lands **after** being lifted out, so ``to: 0`` is "put it first" and no
    caller has to reason about whether its own removal shifted the target index.
    """

    model_config = ConfigDict(extra="forbid")

    to: int = Field(ge=0)


class PlaylistRenameRequest(BaseModel):
    """``PATCH /playlists/{name}`` — the new name, which is sanitised because it names a file.

    Here because nothing else can do it: the same ``allow_modifying_stored_playlists = false`` that
    stops OwnTone writing a playlist stops its web UI renaming one. The answer says the name it used.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)


class PlaylistPlayRequest(BaseModel):
    """``PUT /playlists/{name}/play``.

    The defaults are the verb's whole point: *replace the queue and start*, which is what tapping a
    playlist means. ``mode: append`` and ``start: false`` are there so the same route serves "add
    this playlist to what is already on", which is the other thing anybody wants from a list.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["append", "replace"] = "replace"
    start: bool = True


class PlaylistDeletedView(BaseModel):
    #: The file that was removed, relative to the music root.
    deleted: str
    #: Non-fatal problems, currently only a rescan the daemon refused.
    warnings: list[str] = Field(default_factory=list)
