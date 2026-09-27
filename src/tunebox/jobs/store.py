"""SQLite job queue and ingest history.

PLAN.md 3.1: *"Start with SQLite-backed jobs (one table, a worker loop) — no Redis until there's
a reason."* Two tables in the end, because dedupe by video id needs an index over what has
already landed; the audio itself is still just files on disk.

Two properties this file exists to guarantee:

* **A job is claimed exactly once.** ``claim_next`` does its read and its write inside one
  ``BEGIN IMMEDIATE`` transaction, so two worker threads (or a second process) cannot both take
  the same row. This is why the state transition is a conditional ``UPDATE ... WHERE state =
  'queued'`` rather than a plain write.
* **A crash loses no job.** Anything left ``running`` when the process died is recovered by
  ``requeue_running`` at startup instead of sitting in the queue forever.

Threading: one connection per thread (``sqlite3`` objects are not shareable), WAL so a reader
never blocks the writer, and a busy timeout so the rare write collision waits instead of raising.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable, Sequence

from .models import ACTIVE_STATES, Job, JobState, TrackRecord

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    state         TEXT NOT NULL,
    url           TEXT NOT NULL,
    video_id      TEXT NOT NULL,
    audio_format  TEXT NOT NULL,
    overrides     TEXT NOT NULL DEFAULT '{}',
    force         INTEGER NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 3,
    created_at    REAL NOT NULL,
    available_at  REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL,
    progress      REAL,
    error         TEXT,
    path          TEXT,
    already_present INTEGER NOT NULL DEFAULT 0,
    warnings      TEXT NOT NULL DEFAULT '[]',
    tags          TEXT
);
CREATE INDEX IF NOT EXISTS jobs_ready ON jobs (state, available_at);
CREATE INDEX IF NOT EXISTS jobs_video ON jobs (video_id, state);

CREATE TABLE IF NOT EXISTS tracks (
    video_id     TEXT PRIMARY KEY,
    path         TEXT NOT NULL,
    title        TEXT,
    artist       TEXT,
    tags         TEXT,
    ingested_at  REAL NOT NULL
);
"""

_JOB_COLUMNS = (
    "id, state, url, video_id, audio_format, overrides, force, attempts, max_attempts, "
    "created_at, available_at, started_at, finished_at, progress, error, path, "
    "already_present, warnings, tags"
)


def new_job_id() -> str:
    """Unguessable job id.

    Sequential integers would let anyone who gets one token-less request through enumerate the
    whole history; there is no cost to making them opaque.
    """
    return secrets.token_hex(8)


class JobStore:
    """The queue. Safe to share between the API threadpool and the worker threads."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._all_connections: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self._closed = False
        # Belt and braces on top of BEGIN IMMEDIATE: this keeps threads of *this* process from
        # ever hitting SQLITE_BUSY, while the transaction is what protects against a second
        # process. Reentrant so a write inside a write is a deadlock we never have to debug.
        self._write_lock = threading.RLock()
        # An in-memory store must share one connection, or each thread gets its own empty
        # database. Only tests use it, but a silently empty queue is a bad way to find out.
        self._shared: sqlite3.Connection | None = None
        if str(self.path) == ":memory:":
            self._shared = self._connect()
        # Not inside _writing(): executescript() commits any open transaction itself, so an
        # explicit BEGIN/COMMIT around it fails with "no transaction is active".
        with self._write_lock:
            self._conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ plumbing

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,  # explicit transactions; see _writing()
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        with self._lock:
            self._all_connections.append(conn)
        return conn

    @property
    def _conn(self) -> sqlite3.Connection:
        if self._closed:
            raise sqlite3.ProgrammingError("job store is closed")
        if self._shared is not None:
            return self._shared
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    class _Writing:
        """``BEGIN IMMEDIATE`` ... ``COMMIT``, so a claim is atomic against other workers."""

        def __init__(self, conn: sqlite3.Connection, lock: "threading.RLock") -> None:
            self._conn = conn
            self._lock = lock

        def __enter__(self) -> sqlite3.Connection:
            self._lock.acquire()
            try:
                self._conn.execute("BEGIN IMMEDIATE")
            except BaseException:
                # __exit__ never runs if __enter__ raises, so the lock has to be released here
                # or every later write in the process deadlocks on it.
                self._lock.release()
                raise
            return self._conn

        def __exit__(self, exc_type, exc, tb) -> None:
            try:
                if exc_type is None:
                    self._conn.execute("COMMIT")
                else:
                    self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                # A failed COMMIT leaves the transaction open and would poison every later
                # BEGIN on this connection; roll back so the connection stays usable.
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:  # pragma: no cover - nothing left to try
                    pass
                raise
            finally:
                self._lock.release()

    def _writing(self) -> "JobStore._Writing":
        return self._Writing(self._conn, self._write_lock)

    def close(self) -> None:
        """Close every connection and refuse to open another.

        Final on purpose: a store that transparently reopened would let a worker thread outlive
        the shutdown that closed it and write to a database nobody is reading any more.
        """
        self._closed = True
        with self._lock:
            connections = list(self._all_connections)
            self._all_connections.clear()
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover - closing twice is not interesting
                pass
        self._shared = None
        self._local = threading.local()

    # --------------------------------------------------------------------- jobs

    def enqueue(
        self,
        *,
        url: str,
        video_id: str,
        audio_format: str,
        overrides: dict[str, Any] | None = None,
        force: bool = False,
        max_attempts: int = 3,
        dedupe_active: bool = False,
        now: float | None = None,
    ) -> tuple[Job, bool]:
        """Add a job. Returns ``(job, created)``.

        With ``dedupe_active``, an existing queued/running job for the *same request* — same
        video, same container, same tag overrides — is returned instead of a second job being
        created, and the lookup happens inside the same transaction as the insert. Checking first
        and inserting after would leave a window where two simultaneous taps on a phone create two
        jobs; matching on the video id alone would silently discard the second request's
        ``--artist`` or its ``format``.
        """
        now = time.time() if now is None else now
        overrides = dict(overrides or {})
        job = Job(
            id=new_job_id(),
            state=JobState.QUEUED,
            url=url,
            video_id=video_id,
            audio_format=audio_format,
            overrides=overrides,
            force=force,
            max_attempts=max_attempts,
            created_at=now,
            available_at=now,
        )
        with self._writing() as conn:
            if dedupe_active:
                rows = conn.execute(
                    f"SELECT {_JOB_COLUMNS} FROM jobs WHERE video_id = ? AND state IN (?,?)"
                    " ORDER BY created_at",
                    (video_id, *[s.value for s in ACTIVE_STATES]),
                ).fetchall()
                for row in rows:
                    candidate = _job_from_row(row)
                    if (
                        candidate.audio_format == audio_format
                        and candidate.overrides == overrides
                    ):
                        return candidate, False
            conn.execute(
                "INSERT INTO jobs (id, state, url, video_id, audio_format, overrides, force,"
                " attempts, max_attempts, created_at, available_at, warnings)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,'[]')",
                (
                    job.id,
                    job.state.value,
                    job.url,
                    job.video_id,
                    job.audio_format,
                    json.dumps(job.overrides, ensure_ascii=False),
                    int(job.force),
                    0,
                    job.max_attempts,
                    job.created_at,
                    job.available_at,
                ),
            )
        log.info("queued job %s for %s", job.id, job.video_id)
        return job, True

    def get(self, job_id: str) -> Job | None:
        row = self._conn.execute(
            f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return _job_from_row(row) if row else None

    def list_jobs(self, *, states: Sequence[JobState] | None = None, limit: int = 50) -> list[Job]:
        sql = f"SELECT {_JOB_COLUMNS} FROM jobs"
        params: list[Any] = []
        if states:
            sql += f" WHERE state IN ({','.join('?' * len(states))})"
            params += [s.value for s in states]
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        return [_job_from_row(row) for row in self._conn.execute(sql, params)]

    def claim_next(self, now: float | None = None) -> Job | None:
        """Take the oldest due job, atomically. Returns ``None`` when there is nothing to do."""
        now = time.time() if now is None else now
        with self._writing() as conn:
            row = conn.execute(
                f"SELECT {_JOB_COLUMNS} FROM jobs WHERE state = ? AND available_at <= ?"
                " ORDER BY available_at, created_at LIMIT 1",
                (JobState.QUEUED.value, now),
            ).fetchone()
            if row is None:
                return None
            job = _job_from_row(row)
            cursor = conn.execute(
                "UPDATE jobs SET state = ?, attempts = attempts + 1, started_at = ?,"
                " progress = NULL, error = NULL WHERE id = ? AND state = ?",
                (JobState.RUNNING.value, now, job.id, JobState.QUEUED.value),
            )
            if cursor.rowcount != 1:  # pragma: no cover - lost the race despite the transaction
                return None
        return replace(job, state=JobState.RUNNING, attempts=job.attempts + 1, started_at=now)

    def update_progress(self, job_id: str, percent: float | None) -> None:
        with self._writing() as conn:
            conn.execute(
                "UPDATE jobs SET progress = ? WHERE id = ? AND state = ?",
                (percent, job_id, JobState.RUNNING.value),
            )

    def mark_done(
        self,
        job_id: str,
        *,
        path: str,
        tags: dict[str, Any] | None,
        already_present: bool,
        warnings: Iterable[str] = (),
        now: float | None = None,
    ) -> None:
        now = time.time() if now is None else now
        with self._writing() as conn:
            conn.execute(
                "UPDATE jobs SET state = ?, finished_at = ?, progress = 100.0, path = ?,"
                " tags = ?, already_present = ?, warnings = ?, error = NULL WHERE id = ?",
                (
                    JobState.DONE.value,
                    now,
                    path,
                    json.dumps(tags, ensure_ascii=False) if tags is not None else None,
                    int(already_present),
                    json.dumps(list(warnings), ensure_ascii=False),
                    job_id,
                ),
            )

    def mark_failed(self, job_id: str, error: str, *, now: float | None = None) -> None:
        """Record a terminal failure.

        A job already recorded as ``done`` is left alone: if the outcome was written and something
        downstream then threw, the file really is in the library, and calling that job failed
        would be a lie the client acts on.
        """
        now = time.time() if now is None else now
        with self._writing() as conn:
            conn.execute(
                "UPDATE jobs SET state = ?, finished_at = ?, error = ?"
                " WHERE id = ? AND state != ?",
                (JobState.FAILED.value, now, error, job_id, JobState.DONE.value),
            )

    def reschedule(self, job_id: str, *, error: str, available_at: float) -> None:
        """Put a failed attempt back in the queue, not before ``available_at``."""
        with self._writing() as conn:
            conn.execute(
                "UPDATE jobs SET state = ?, available_at = ?, error = ?, started_at = NULL,"
                " progress = NULL WHERE id = ?",
                (JobState.QUEUED.value, available_at, error, job_id),
            )

    def cancel(self, job_id: str) -> bool:
        """Fail a job that has not started. Running jobs are left alone: a half-killed yt-dlp
        subprocess is worse than a completed download nobody wanted."""
        with self._writing() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET state = ?, finished_at = ?, error = ? WHERE id = ? AND state = ?",
                (
                    JobState.FAILED.value,
                    time.time(),
                    "cancelled",
                    job_id,
                    JobState.QUEUED.value,
                ),
            )
            return cursor.rowcount == 1

    def requeue_running(self, *, now: float | None = None) -> int:
        """Crash recovery: reclaim jobs a dead worker left ``running``.

        Attempts are already counted, so a job that keeps killing the process still stops after
        ``max_attempts`` rather than becoming a restart loop.
        """
        now = time.time() if now is None else now
        with self._writing() as conn:
            requeued = conn.execute(
                "UPDATE jobs SET state = ?, available_at = ?, started_at = NULL,"
                " error = 'interrupted; requeued at startup'"
                " WHERE state = ? AND attempts < max_attempts",
                (JobState.QUEUED.value, now, JobState.RUNNING.value),
            ).rowcount
            abandoned = conn.execute(
                "UPDATE jobs SET state = ?, finished_at = ?,"
                " error = 'interrupted and out of attempts' WHERE state = ?",
                (JobState.FAILED.value, now, JobState.RUNNING.value),
            ).rowcount
        if requeued or abandoned:
            log.warning("recovered %d interrupted job(s), abandoned %d", requeued, abandoned)
        return requeued

    def reclaim_stale(
        self,
        *,
        started_before: float,
        exclude: Iterable[str] = (),
        now: float | None = None,
    ) -> list[str]:
        """Requeue (or fail) jobs that have been ``running`` too long, returning their ids.

        ``exclude`` is the set of jobs the caller is *actually* working on right now. Without it
        this would reclaim a live download from underneath the thread performing it and then run
        it twice. With it, the only jobs this can touch are ones whose worker thread died — which
        is the situation it exists for, since a thread that dies between the download and
        ``mark_done`` would otherwise leave a job ``running`` forever.
        """
        now = time.time() if now is None else now
        skip = set(exclude)
        reclaimed: list[str] = []
        with self._writing() as conn:
            rows = conn.execute(
                f"SELECT {_JOB_COLUMNS} FROM jobs WHERE state = ? AND started_at < ?",
                (JobState.RUNNING.value, started_before),
            ).fetchall()
            for row in rows:
                job = _job_from_row(row)
                if job.id in skip:
                    continue
                if job.attempts < job.max_attempts:
                    conn.execute(
                        "UPDATE jobs SET state = ?, available_at = ?, started_at = NULL,"
                        " error = 'abandoned while running; requeued' WHERE id = ?",
                        (JobState.QUEUED.value, now, job.id),
                    )
                else:
                    conn.execute(
                        "UPDATE jobs SET state = ?, finished_at = ?,"
                        " error = 'abandoned while running and out of attempts' WHERE id = ?",
                        (JobState.FAILED.value, now, job.id),
                    )
                reclaimed.append(job.id)
        if reclaimed:
            log.warning("reclaimed %d stale running job(s)", len(reclaimed))
        return reclaimed

    def counts(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state")
        counts = {state.value: 0 for state in JobState}
        for row in rows:
            counts[row["state"]] = row["n"]
        return counts

    # ------------------------------------------------------------------ tracks

    def record_track(
        self,
        *,
        video_id: str,
        path: str,
        title: str | None = None,
        artist: str | None = None,
        tags: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> None:
        now = time.time() if now is None else now
        with self._writing() as conn:
            conn.execute(
                "INSERT INTO tracks (video_id, path, title, artist, tags, ingested_at)"
                " VALUES (?,?,?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET"
                " path = excluded.path, title = excluded.title, artist = excluded.artist,"
                " tags = excluded.tags, ingested_at = excluded.ingested_at",
                (
                    video_id,
                    path,
                    title,
                    artist,
                    json.dumps(tags, ensure_ascii=False) if tags is not None else None,
                    now,
                ),
            )

    def find_track(self, video_id: str) -> TrackRecord | None:
        row = self._conn.execute(
            "SELECT video_id, path, title, artist, tags, ingested_at FROM tracks"
            " WHERE video_id = ?",
            (video_id,),
        ).fetchone()
        if row is None:
            return None
        return TrackRecord(
            video_id=row["video_id"],
            path=row["path"],
            title=row["title"],
            artist=row["artist"],
            tags=json.loads(row["tags"]) if row["tags"] else None,
            ingested_at=row["ingested_at"],
        )

    def forget_track(self, video_id: str) -> None:
        """Drop a history entry whose file has disappeared, so the next ingest re-downloads."""
        with self._writing() as conn:
            conn.execute("DELETE FROM tracks WHERE video_id = ?", (video_id,))


def _job_from_row(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        state=JobState(row["state"]),
        url=row["url"],
        video_id=row["video_id"],
        audio_format=row["audio_format"],
        overrides=json.loads(row["overrides"] or "{}"),
        force=bool(row["force"]),
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        created_at=row["created_at"],
        available_at=row["available_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        progress=row["progress"],
        error=row["error"],
        path=row["path"],
        already_present=bool(row["already_present"]),
        warnings=tuple(json.loads(row["warnings"] or "[]")),
        tags=json.loads(row["tags"]) if row["tags"] else None,
    )
