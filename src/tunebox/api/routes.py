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
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request, Response, status

from .. import __version__
from ..config import Config
from ..core.ports import MusicSource
from ..jobs.models import Job, JobState
from ..jobs.store import JobStore
from ..jobs.worker import Worker
from ..sources import urls
from .schemas import HealthView, IngestRequest, JobView, SearchItem

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


def _view(job: Job) -> JobView:
    return JobView(**job.as_dict())


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
        return _view(job)

    worker = _worker(request)
    if worker is not None:
        worker.notify()
    return _view(job)


@router.get("/jobs", response_model=list[JobView], summary="List jobs, newest first")
def list_jobs(
    request: Request,
    state: JobState | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[JobView]:
    jobs = _store(request).list_jobs(states=[state] if state else None, limit=limit)
    return [_view(job) for job in jobs]


@router.get("/jobs/{job_id}", response_model=JobView, summary="One job's state and result")
def get_job(job_id: str, request: Request) -> JobView:
    job = _store(request).get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such job")
    return _view(job)


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
    return _view(cancelled)
