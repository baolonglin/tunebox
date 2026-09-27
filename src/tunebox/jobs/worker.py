"""The worker loop: claim a job, run the Phase 1 ingest use case, record what happened.

Threads, not processes and not asyncio. Every second of a job is spent waiting on a yt-dlp
subprocess, so the GIL is irrelevant, and at the concurrency cap PLAN.md 3.1 sets (1–2) a thread
pool is the whole of the machinery needed.

Where the Phase 2 requirements live:

* **concurrency cap** — ``config.worker_concurrency``, validated to 1–2 in :mod:`tunebox.config`,
  and an exclusive lock file next to the database so a second ``tunebox serve`` on the same queue
  cannot quietly double it (which would also mean downloading one video twice).
* **timeouts** — the yt-dlp adapter kills the whole process group on ``inspect_timeout`` /
  ``fetch_timeout``, and a hung download surfaces here as
  :class:`~tunebox.errors.SourceTimeout`, which is retryable. On top of that,
  :meth:`JobStore.reclaim_stale` requeues anything left ``running`` past
  ``config.job_timeout`` without a live thread behind it, so a job cannot get stuck forever if a
  thread dies at the wrong moment.
* **bounded retries** — ``config.job_max_attempts`` with exponential backoff expressed as the
  job's ``available_at``, so a waiting retry does not occupy a worker thread.
* **dedupe by video id** — checked here, before ``inspect``, so a repeat costs zero network
  requests rather than one. The path-based dedupe inside ``ingest()`` remains as the backstop
  for files that arrived some other way.

And where Phase 3's requirement lives:

* **library rescan hook** — a successful ingest that actually produced a *new* file tells the
  player daemon to index it (PLAN.md Phase 3). It runs just *before* the outcome is recorded, not
  after, so that a rescan warning can still reach ``GET /jobs/{id}``; it is skipped when the file was
  already present; and it can only ever add a warning — see :mod:`tunebox.player.hooks`.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import replace
from pathlib import Path

from .. import ingest as ingest_module
from .. import tagging
from ..config import Config
from ..core.models import IngestResult, Progress, TrackTags
from ..core.ports import MusicSource, Player, TagWriter
from ..errors import InvalidURLError, LibraryError, SourceError, TuneboxError
from ..player.hooks import rescan_library
from .models import Job
from .store import JobStore

log = logging.getLogger(__name__)

#: Transient by nature: a bot challenge, a dead network, a full disk. Worth another attempt.
RETRYABLE = (SourceError, OSError)

#: Deterministic: retrying cannot change the outcome, so failing now is the honest answer.
NON_RETRYABLE = (InvalidURLError, LibraryError, ValueError)

#: Don't write a progress row for every packet: one per percent is plenty for a phone UI and
#: keeps the queue database from becoming the busiest thing on the disk.
_PROGRESS_STEP = 1.0

#: Name of the lock file, kept beside the database it protects.
LOCK_SUFFIX = ".lock"


class WorkerBusyError(TuneboxError):
    """Another process is already draining this queue."""


class Worker:
    """A pool of ingest threads over one :class:`~tunebox.jobs.store.JobStore`."""

    def __init__(
        self,
        *,
        store: JobStore,
        config: Config,
        source: MusicSource,
        tag_writer: TagWriter = tagging.write_tags,
        player: Player | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._source = source
        self._tag_writer = tag_writer
        #: Optional: ingest works perfectly well with no player installed, and Phases 1 and 2 did
        #: exactly that. When one is present, a finished ingest tells it to rescan.
        self._player = player
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._wake = threading.Event()
        #: Jobs this process is working on right now, so the stale reaper never reclaims one.
        self._in_flight: set[str] = set()
        self._in_flight_lock = threading.Lock()
        self._lock_file: int | None = None
        self._next_reap = 0.0

    # ----------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._threads:
            raise RuntimeError("worker already started")
        self._acquire_queue_lock()
        # Anything left running belongs to a previous process that died mid-download. Safe to do
        # unconditionally only because the lock above proves no other worker is live.
        self._store.requeue_running()
        self._stop.clear()
        self._next_reap = time.monotonic() + self._config.job_timeout
        for index in range(self._config.worker_concurrency):
            thread = threading.Thread(
                target=self._loop, name=f"tunebox-worker-{index}", daemon=True
            )
            thread.start()
            self._threads.append(thread)
        log.info("worker started with %d thread(s)", len(self._threads))

    def notify(self) -> None:
        """Wake an idle thread. Called after an enqueue so a queued job starts immediately."""
        self._wake.set()

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the threads to finish and exit, waiting at most ``timeout`` for each.

        An in-flight ingest is *not* interrupted — killing a download to shave a second off
        shutdown would trade correctness for nothing — but neither is shutdown held open for a
        ten-minute download. A job still running when the process exits is left ``running``, and
        the next start requeues it with its attempt already counted.
        """
        self._stop.set()
        self._wake.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        alive = [t for t in self._threads if t.is_alive()]
        self._threads = alive
        if alive:
            log.warning(
                "%d worker thread(s) still downloading at shutdown; their jobs will be requeued",
                len(alive),
            )
        else:
            log.info("worker stopped")
        self._release_queue_lock()

    @property
    def running(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    # ----------------------------------------------------- one worker per queue

    def _lock_path(self) -> Path:
        return self._store.path.with_name(self._store.path.name + LOCK_SUFFIX)

    def _acquire_queue_lock(self) -> None:
        """Take an exclusive lock on the queue, or refuse to start.

        Two ``tunebox serve`` processes on one database would each requeue the other's in-flight
        jobs at startup and then both download the same video. An advisory file lock is enough:
        it is released by the kernel if the holder dies, so a crashed process never leaves the
        queue permanently locked.
        """
        if str(self._store.path) == ":memory:":
            return  # a private database cannot be shared, so there is nothing to protect
        try:
            import fcntl
        except ImportError:  # pragma: no cover - POSIX only; the target hosts are Pi and Mac
            log.warning("no fcntl on this platform: not guarding against a second worker")
            return

        path = self._lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise WorkerBusyError(
                f"another tunebox worker is already using {self._store.path} "
                f"(lock held on {path}). Stop it, or point TUNEBOX_DB somewhere else."
            ) from exc
        os.truncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._lock_file = fd

    def _release_queue_lock(self) -> None:
        if self._lock_file is None:
            return
        try:
            os.close(self._lock_file)  # closing drops the flock
        except OSError:  # pragma: no cover - nothing useful to do at shutdown
            pass
        self._lock_file = None

    # ---------------------------------------------------------------------- loop

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._store.claim_next()
            except Exception:
                # A broken or closed store must not kill the thread silently; back off and retry,
                # and let shutdown be what ends the loop.
                log.exception("failed to claim a job; backing off")
                self._stop.wait(self._config.job_poll_interval)
                continue

            if job is None:
                self._reap_stale()
                # Idle: wait to be poked, but wake on a timer anyway so retries that became due
                # are picked up without an enqueue.
                self._wake.wait(self._config.job_poll_interval)
                self._wake.clear()
                continue

            with self._in_flight_lock:
                self._in_flight.add(job.id)
            try:
                self.run_job(job)
            finally:
                with self._in_flight_lock:
                    self._in_flight.discard(job.id)

    def _reap_stale(self) -> None:
        """Occasionally look for jobs stuck ``running`` with no thread behind them."""
        now = time.monotonic()
        if now < self._next_reap:
            return
        self._next_reap = now + max(self._config.job_timeout / 4, 1.0)
        with self._in_flight_lock:
            mine = set(self._in_flight)
        try:
            self._store.reclaim_stale(
                started_before=time.time() - self._config.job_timeout, exclude=mine
            )
        except Exception:  # pragma: no cover - housekeeping must never break the loop
            log.debug("stale-job sweep failed", exc_info=True)

    # ----------------------------------------------------------------- one job

    def run_job(self, job: Job) -> None:
        """Execute one claimed job to a terminal state (or back into the queue).

        Public because it is exactly what a test wants to drive: no threads, no timing.

        Everything — including recording the result — is inside the error handling. An exception
        while writing the outcome used to escape and kill the worker thread, leaving the job
        ``running`` forever and the queue behind it unserved.
        """
        log.info("job %s attempt %d/%d: %s", job.id, job.attempts, job.max_attempts, job.url)
        try:
            result = self._dedupe(job)
            if result is None:
                result = ingest_module.ingest(
                    job.url,
                    source=self._source,
                    config=self._job_config(job),
                    tag_writer=self._tag_writer,
                    on_progress=_ProgressWriter(self._store, job.id),
                    overrides=dict(job.overrides),
                    force=job.force,
                )
            self._finish(job, result)
        except NON_RETRYABLE as exc:
            log.warning("job %s failed permanently: %s", job.id, exc)
            self._fail(job, f"{type(exc).__name__}: {exc}")
        except RETRYABLE as exc:
            self._retry_or_fail(job, exc)
        except Exception as exc:  # a bug in our code, not a bad download
            log.exception("job %s hit an unexpected error", job.id)
            self._fail(job, f"internal error: {type(exc).__name__}: {exc}")

    def _job_config(self, job: Job) -> Config:
        """Per-job format override, so ``POST /ingest {"format": "mp3"}`` works."""
        if job.audio_format == self._config.audio_format:
            return self._config
        return replace(self._config, audio_format=job.audio_format)

    def _dedupe(self, job: Job) -> IngestResult | None:
        """Video-id dedupe: has this exact video already landed, in the format asked for, and is
        the file still there?"""
        if job.force:
            return None
        record = self._store.find_track(job.video_id)
        if record is None:
            return None
        path = Path(record.path)
        if path.suffix.lstrip(".").lower() != job.audio_format:
            # Asking for mp3 when only the m4a exists is a different request, not a duplicate.
            log.info(
                "job %s wants .%s but %s is on disk; ingesting anyway",
                job.id,
                job.audio_format,
                path.suffix,
            )
            return None
        if not path.is_file():
            # The library is the source of truth; a stale index entry must not block a re-ingest.
            log.info("history entry for %s points at a missing file, re-ingesting", job.video_id)
            self._store.forget_track(job.video_id)
            return None
        log.info("job %s deduped by video id %s -> %s", job.id, job.video_id, path)
        return _deduped_result(path, record.tags)

    def _finish(self, job: Job, result: IngestResult) -> None:
        # Before the outcome is written, not after: the rescan can add a warning, and a warning
        # that lands after ``mark_done`` would never reach ``GET /jobs/{id}``.
        result = self._rescan(result)
        tags = result.tags.as_dict()
        self._store.mark_done(
            job.id,
            path=str(result.path),
            tags=tags,
            already_present=result.already_present,
            warnings=result.warnings,
        )
        try:
            # The video-id index is an optimisation, not the outcome: losing it costs one metadata
            # request next time and must not turn a finished ingest into a failed job.
            self._store.record_track(
                video_id=job.video_id,
                path=str(result.path),
                title=tags.get("title"),
                artist=tags.get("artist"),
                tags=tags,
            )
        except Exception:  # pragma: no cover - the path check inside ingest() still dedupes
            log.exception("could not index %s for dedupe", job.video_id)
        log.info("job %s done: %s", job.id, result.path)

    def _rescan(self, result: IngestResult) -> IngestResult:
        """Tell the player daemon about a newly placed file (PLAN.md Phase 3).

        Skipped when nothing new landed: a deduped job changed no file, so asking OwnTone to walk
        the library for it would be a request that can only find what it already knows.
        """
        if self._player is None or not self._config.rescan_after_ingest:
            return result
        if result.already_present:
            return result
        warning = rescan_library(self._player)
        if warning is None:
            return result
        return replace(result, warnings=(*result.warnings, warning))

    def _fail(self, job: Job, message: str) -> None:
        try:
            self._store.mark_failed(job.id, message)
        except Exception:  # pragma: no cover - the stale sweep is the backstop for this
            log.exception("could not record the failure of job %s", job.id)

    def _retry_or_fail(self, job: Job, exc: BaseException) -> None:
        message = f"{type(exc).__name__}: {exc}"
        if job.attempts >= job.max_attempts:
            log.warning("job %s out of attempts (%d): %s", job.id, job.attempts, message)
            self._fail(job, message)
            return
        delay = self._config.retry_delay(job.attempts)
        log.warning(
            "job %s attempt %d/%d failed, retrying in %.0fs: %s",
            job.id,
            job.attempts,
            job.max_attempts,
            delay,
            message,
        )
        try:
            self._store.reschedule(job.id, error=message, available_at=time.time() + delay)
        except Exception:  # pragma: no cover - the stale sweep is the backstop for this
            log.exception("could not reschedule job %s", job.id)


def _deduped_result(path: Path, tags: dict | None) -> IngestResult:
    """An :class:`IngestResult`-shaped answer for a job that needed no work.

    Uses the tags recorded at the original ingest, so a deduped job returns the same payload a
    fresh one would rather than a special case the client has to know about.
    """
    known = tags or {}
    restored = TrackTags(
        title=known.get("title") or path.stem,
        artist=known.get("artist") or path.parent.parent.name,
        album=known.get("album"),
        album_artist=known.get("album_artist"),
        year=known.get("year"),
        track_number=known.get("track_number"),
        duration=known.get("duration"),
        source=known.get("source"),
        source_id=known.get("source_id"),
        source_url=known.get("source_url"),
    )
    return IngestResult(path=path, tags=restored, already_present=True)


class _ProgressWriter:
    """Throttled progress into the job row, so ``GET /jobs/{id}`` can show a percentage."""

    def __init__(self, store: JobStore, job_id: str) -> None:
        self._store = store
        self._job_id = job_id
        self._last = -1.0

    def __call__(self, progress: Progress) -> None:
        percent = progress.percent
        if percent is None:
            return
        if percent - self._last < _PROGRESS_STEP and percent < 100.0:
            return
        self._last = percent
        try:
            self._store.update_progress(self._job_id, round(percent, 1))
        except Exception:  # pragma: no cover - progress is cosmetic, never fatal
            log.debug("could not record progress for job %s", self._job_id, exc_info=True)
