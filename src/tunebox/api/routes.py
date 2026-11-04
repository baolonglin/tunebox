"""The endpoints Phase 2 promises: ``POST /ingest``, ``GET /jobs/{id}``, ``GET /search``.

Every handler here is a plain ``def``, not ``async def``, on purpose: ``GET /search`` blocks on a
yt-dlp subprocess for a second or two and the queue writes block on SQLite, so Starlette running
them in its threadpool is exactly what we want. An ``async def`` handler doing either would stall
the event loop for every other request.

The division of labour with the worker is deliberate:

* the request does only what must be synchronous — validate the URL, look for an already-queued
  job for the same video, write one row — and returns a job id;
* everything slow, retryable, or network-shaped happens in :mod:`tunebox.jobs.worker`.

That is what lets a phone queue three URLs and lock itself.

``GET /library`` joins them in Phase 4, and is here rather than in :mod:`tunebox.api.remote`
because it is a fact about *our* library rather than a command to the player: it walks the music
root and needs no daemon running at all. Its handler blocks on a directory walk, which is the third
reason these are not ``async def``.

``DELETE /library`` is here for the same reason and is the one destructive route in the project — it
unlinks files from the music root. Everything that makes that safe is in its own docstring below;
the reason it is not in :mod:`tunebox.api.remote` is that it does not go through the ``Player`` port
at all. It only *tells* the player afterwards, and cannot fail because of it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request, Response, status

from .. import __version__
from ..config import Config
from ..core.ports import MusicSource
from ..errors import LibraryError, TuneboxError
from ..jobs.models import Job, JobState
from ..jobs.store import JobStore
from ..jobs.worker import Worker
from ..library import delete_track, list_tracks, relative_to_root, resolve_track_path
from ..player.hooks import rescan_library
from ..playlists import forget_tracks as forget_playlist_tracks
from ..sources import urls
from .remote import configured_output
from .schemas import (
    HealthView,
    IngestRequest,
    JobView,
    LibraryDeletedView,
    LibraryDeleteRequest,
    LibraryTrackView,
    LibraryView,
    PlayerHealth,
    SearchItem,
)

log = logging.getLogger(__name__)

router = APIRouter()


# --------------------------------------------------------------------- plumbing


def _config(request: Request) -> Config:
    return request.app.state.config


def _store(request: Request) -> JobStore:
    return request.app.state.store


def _source(request: Request) -> MusicSource:
    return request.app.state.source


def _worker(request: Request) -> Worker | None:
    return getattr(request.app.state, "worker", None)


def _view(job: Job, music_root: Path) -> JobView:
    """A job, plus the library-relative spelling of whatever file it wrote.

    The store keeps the absolute path, which is the right thing to record: it is where the file
    actually is. But the page puts a finished track straight into *This device*'s queue, and that
    queue is pruned on the phone by comparing paths against what ``GET /library`` hands out — so a
    second spelling of the same file is a row that survives the delete of its own audio. Derived
    here rather than stored, because it is a function of the music root and nothing else.
    """
    data = job.as_dict()
    written = data.get("path")
    data["library_path"] = (
        relative_to_root(music_root, Path(written)) if isinstance(written, str) and written else None
    )
    return JobView(**data)


# ---------------------------------------------------------------------- routes


@router.get("/healthz", response_model=HealthView, summary="Liveness and queue depth")
def health(request: Request) -> HealthView:
    config = _config(request)
    worker = _worker(request)
    return HealthView(
        status="ok",
        version=__version__,
        music_root=str(config.music_root),
        audio_format=config.audio_format,
        worker_running=bool(worker and worker.running),
        workers=config.worker_concurrency,
        jobs=_store(request).counts(),
        player=_player_health(request),
    )


def _player_health(request: Request) -> PlayerHealth | None:
    """Describe the playback session, or why it cannot be described.

    Wrapped in a broad ``except`` on purpose: ``/healthz`` is what a monitor polls, and a health
    check that answers 503 because the *soundbar* is unplugged reports the wrong outage. The player
    being down is a field in the answer, not the answer's status code.
    """
    player = getattr(request.app.state, "player", None)
    if player is None:
        return None
    config = _config(request)
    url = getattr(player, "base_url", config.player_base_url)
    try:
        status = player.status()
        queue = player.queue()
    except TuneboxError as exc:
        return PlayerHealth(reachable=False, url=url, error=str(exc))
    except Exception as exc:  # pragma: no cover - an adapter bug must not take /healthz down
        log.exception("player health check raised")
        return PlayerHealth(reachable=False, url=url, error=f"{type(exc).__name__}: {exc}")

    # Same precedence as OwnTonePlayer.find_output, and shared with the Phase 4 remote so the two
    # can never disagree about which speaker is "the" speaker: an id is unambiguous, a name is what
    # somebody renamed in the Google Home app last week. Matching on *either* would let a name
    # collision report the wrong speaker as selected while a configured id points somewhere else.
    target = configured_output(status.outputs, config)
    now = status.now_playing
    return PlayerHealth(
        reachable=True,
        url=url,
        state=status.state,
        volume=status.volume,
        now_playing=f"{now.artist} — {now.title}" if now and now.title else None,
        queue_length=len(queue),
        output=target.name if target else (config.output_id or config.output_name),
        output_selected=target.selected if target else False,
    )


@router.get("/search", response_model=list[SearchItem], summary="Search the source for candidates")
def search(
    request: Request,
    q: str = Query(min_length=1, max_length=200, description="search terms"),
    limit: int | None = Query(default=None, ge=1, le=50),
) -> list[SearchItem]:
    """Synchronous on purpose: a phone showing a result list has nothing to do until it arrives.

    This is the only route that talks to the network during the request, which is also why it is
    the one to cache first if searching ever feels slow (PLAN.md 3.1).
    """
    results = _source(request).search(q, limit=limit or _config(request).search_limit)
    return [SearchItem(**result.as_dict()) for result in results]


@router.get("/library", response_model=LibraryView, summary="Music files under the music root")
def library(
    request: Request,
    q: str | None = Query(default=None, max_length=200, description="filter on path and artist"),
    limit: int = Query(default=100, ge=1, le=500),
) -> LibraryView:
    """List the library from the filesystem, newest first.

    Newest first because of what the remote uses it for: the track somebody wants to play is
    overwhelmingly the one they ingested a minute ago. Whole-library browsing by artist and album is
    what OwnTone's own web UI already does well, and Phase 4's second bullet decided to link to that
    rather than rebuild it.

    No tags are read — the layout ``Artist/Album/Track.ext`` already carries them, because
    :mod:`tunebox.core.naming` put them there. Opening every file with mutagen to display a list
    would make browsing a thousand tracks a thousand reads off an SD card.

    Paths come back **relative** to the music root, and that is the form ``POST /player/queue``
    expects. It means no absolute host path ever reaches a client, and no client-supplied path is
    ever used as one.
    """
    config = _config(request)
    page = list_tracks(
        config.music_root, query=q, limit=limit, max_files=config.library_max_files
    )
    if page.truncated:
        log.warning(
            "library listing truncated at %d files; TUNEBOX_MUSIC_ROOT may be wider than intended",
            config.library_max_files,
        )
    return LibraryView(
        tracks=[LibraryTrackView(**track.as_dict()) for track in page.tracks],
        total=page.total,
        truncated=page.truncated,
    )


@router.delete(
    "/library",
    response_model=LibraryDeletedView,
    summary="Delete tracks from the library, and the directories they empty",
)
def delete_library(payload: LibraryDeleteRequest, request: Request) -> LibraryDeletedView:
    """Remove files from the music root. **Destructive, and there is no trash.**

    The remote is otherwise a one-way door — a mistagged ingest, a wrong search result or a
    duplicate can be added from a phone and then only removed by finding a terminal — so this
    exists; and because it is the one route that destroys the only irreplaceable state in the
    system (PLAN.md 3.1), four things about it are deliberate:

    * **Every path is resolved before anything is deleted.** A typo in the third entry is a ``400``
      that removed nothing, the same all-or-nothing property ``POST /player/queue`` has. Duplicate
      spellings of one file (the relative form and the absolute form) collapse to one delete rather
      than making the second a "no such track".
    * **The track the player has loaded is refused with a ``409``.** Pulling the file out from under
      the daemon mid-song is the one case with an audible failure mode, and refusing is cheaper to
      explain than whatever OwnTone does about it. Later queue entries are *not* refused: the daemon
      discovers a missing file on its next ``LOAD``, and a queue is not a claim on the library.
    * **A player that cannot be asked does not block the delete.** If the daemon is down there is
      nothing playing to protect, and the file is on our disk, not its. Same for the rescan
      afterwards — :func:`~tunebox.player.hooks.rescan_library` cannot fail an operation, it can
      only add a warning to it.
    * **No confirmation lives here.** An API that asked twice would be a protocol nobody follows;
      the confirmation step is the remote's job, and it has one.

    Since playlists arrived there is a fifth: **entries naming a deleted track are removed from every
    playlist, and the playlists that changed are reported.** It happens *before* the unlink, which is
    what makes it exact rather than a guess — while the file still exists its entry resolves, so a
    line that was already dangling for an unrelated reason is left alone. Pruning rather than merely
    reporting, because OwnTone skips an unresolvable entry silently: the alternative is a playlist that
    is quietly one track shorter forever and nothing on any screen having mentioned it.
    """
    config = _config(request)
    player = getattr(request.app.state, "player", None)

    # Resolve first, delete second. Keyed by the resolved path so that ["a.m4a", "/music/a.m4a"]
    # is one delete and not one delete followed by a 400 about a file we just removed.
    targets: dict[Path, str] = {}
    try:
        for entry in payload.paths:
            targets.setdefault(resolve_track_path(config.music_root, entry), entry)
    except LibraryError as exc:
        # A client naming a file it should not, or one that is not there: its mistake, not ours.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    loaded = _loaded_track_path(player)
    if loaded is not None:
        for target in targets:
            if _same_file(target, loaded):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"{target.name} is the track the player has loaded; "
                        "stop playback or skip past it first"
                    ),
                )

    store = _store(request)
    # Before the unlink, deliberately: see the docstring. Cannot raise — a playlist that could not be
    # rewritten must not fail a delete that has already been authorised.
    playlists_updated = list(
        forget_playlist_tracks(
            config.music_root, targets, directory=config.playlists_dirname
        )
    )
    deleted: list[str] = []
    removed: list[str] = []
    for target, entry in targets.items():
        record = delete_track(config.music_root, entry)
        deleted.append(record.path)
        removed.extend(record.removed_directories)
        _forget_ingest_history(store, target)
        log.info("deleted %s from the library", record.path)

    warnings: list[str] = []
    if player is not None:
        # Unconditional, unlike the ingest hook's ``TUNEBOX_RESCAN_AFTER_INGEST``: that setting is
        # about not making the daemon re-index after every download, while an index that still
        # offers a file which no longer exists is a track that fails when somebody taps it.
        warning = rescan_library(player)
        if warning:
            warnings.append(warning)
    return LibraryDeletedView(
        deleted=deleted,
        removed_directories=removed,
        playlists_updated=playlists_updated,
        warnings=warnings,
    )


def _loaded_track_path(player: object | None) -> Path | None:
    """The file the player currently has loaded, or ``None`` if it cannot be asked.

    Broad ``except`` on purpose, and it is the same reasoning as ``_player_health`` above: this is a
    *courtesy* check on the way to deleting a file from our own disk. A daemon that is down is not
    playing anything, so failing the delete because the question could not be asked would refuse an
    operation for the sake of a risk that is not there.
    """
    if player is None:
        return None
    try:
        now = player.status().now_playing  # type: ignore[attr-defined]
    except TuneboxError as exc:
        log.debug("could not ask the player what is loaded: %s", exc)
        return None
    except Exception:  # pragma: no cover - an adapter bug must not block a delete either
        log.exception("player status raised while checking what is loaded")
        return None
    if now is None or not now.path:
        return None
    return Path(now.path)


def _same_file(one: Path, other: Path) -> bool:
    """Whether two paths name the same file, allowing for a symlinked music root.

    PLAN.md 3.1 deploys the library through a symlink to an external SSD, and OwnTone reports
    whatever spelling its own ``directories`` setting gave it — so string equality alone would miss
    the match in the deployment that matters.
    """
    if one == other:
        return True
    try:
        return one.resolve() == other.resolve()
    except OSError:  # pragma: no cover - resolve() of a plain path does not need the file to exist
        return False


def _forget_ingest_history(store: JobStore, path: Path) -> None:
    """Drop the video-id history entry for a deleted file, in both spellings of its path.

    Not load-bearing: the worker already treats an entry whose file has gone as no entry at all, so
    a deleted track is re-ingestable with or without this. Which is also why a failure here is
    logged and swallowed — the file is already gone, and an index is an optimisation.
    """
    spellings = {str(path), str(path.resolve())}
    try:
        for spelling in spellings:
            store.forget_path(spelling)
    except Exception:  # pragma: no cover - a store failure must not fail a completed delete
        log.exception("could not drop the ingest history entry for %s", path)


@router.post(
    "/ingest",
    response_model=JobView,
    status_code=status.HTTP_201_CREATED,
    summary="Queue a URL for ingest",
)
def enqueue_ingest(payload: IngestRequest, request: Request, response: Response) -> JobView:
    """Validate, dedupe against in-flight work, enqueue. Returns immediately.

    Two of the three dedupe layers meet here:

    * an identical video already **queued or running** returns that job with ``200`` instead of
      creating a second one — double-tapping "add" on a phone must not download twice, and the
      check happens inside the insert's transaction so two simultaneous taps cannot both win;
    * a video already **in the library** is caught by the worker, before it spends a metadata
      request, so there is one code path for it rather than a second check inlined here.

    (The third layer is the destination-path check inside the Phase 1 ingest use case, which
    still catches files that arrived by some other route.)
    """
    config = _config(request)
    store = _store(request)

    # Allowlist first: nothing untrusted reaches the database, let alone a subprocess.
    video_id, canonical = urls.canonical_video_url(payload.url)

    job, created = store.enqueue(
        url=canonical,
        video_id=video_id,
        audio_format=payload.format or config.audio_format,
        overrides=payload.overrides(),
        force=payload.force,
        max_attempts=config.job_max_attempts,
        dedupe_active=not payload.force,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
        log.info("ingest of %s already queued as job %s", video_id, job.id)
        return _view(job, _config(request).music_root)

    worker = _worker(request)
    if worker is not None:
        worker.notify()
    return _view(job, _config(request).music_root)


@router.get("/jobs", response_model=list[JobView], summary="List jobs, newest first")
def list_jobs(
    request: Request,
    state: JobState | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[JobView]:
    jobs = _store(request).list_jobs(states=[state] if state else None, limit=limit)
    root = _config(request).music_root
    return [_view(job, root) for job in jobs]


@router.get("/jobs/{job_id}", response_model=JobView, summary="One job's state and result")
def get_job(job_id: str, request: Request) -> JobView:
    job = _store(request).get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such job")
    return _view(job, _config(request).music_root)


@router.delete(
    "/jobs/{job_id}",
    response_model=JobView,
    summary="Cancel a job that has not started yet",
)
def cancel_job(job_id: str, request: Request) -> JobView:
    """Queued jobs only. A running yt-dlp is left to finish: half-killing a download to satisfy
    a DELETE would leave scratch files and no track."""
    store = _store(request)
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such job")
    if not store.cancel(job_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"job is {job.state.value}, only queued jobs can be cancelled",
        )
    cancelled = store.get(job_id)
    assert cancelled is not None
    return _view(cancelled, _config(request).music_root)
