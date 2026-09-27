"""Job records. Pure data, like :mod:`tunebox.core.models` — the store and the worker own the I/O.

A job is the API's promise that an ingest will happen. It outlives the request that created it,
which is the whole point: a phone can queue three URLs and walk away.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in (JobState.DONE, JobState.FAILED)


#: States a job can still be in when a duplicate enqueue arrives — see
#: :meth:`tunebox.jobs.store.JobStore.enqueue` and its ``dedupe_active`` flag.
ACTIVE_STATES = (JobState.QUEUED, JobState.RUNNING)


def iso(timestamp: float | None) -> str | None:
    """Epoch seconds -> UTC ISO-8601. Storage is numeric so ordering is cheap; output is readable."""
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Job:
    id: str
    state: JobState
    url: str
    #: Canonical source id, resolved at enqueue time. This is the dedupe key (PLAN.md Phase 2).
    video_id: str
    audio_format: str = "m4a"
    overrides: dict[str, Any] = field(default_factory=dict)
    force: bool = False
    attempts: int = 0
    max_attempts: int = 3
    created_at: float = field(default_factory=time.time)
    #: Earliest time a worker may claim this job. Backoff is expressed here, not in a sleep.
    available_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    progress: float | None = None
    error: str | None = None
    path: str | None = None
    already_present: bool = False
    warnings: tuple[str, ...] = ()
    tags: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "state": self.state.value,
            "url": self.url,
            "video_id": self.video_id,
            "audio_format": self.audio_format,
            "force": self.force,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "created_at": iso(self.created_at),
            "started_at": iso(self.started_at),
            "finished_at": iso(self.finished_at),
            "retry_after": iso(self.available_at) if self.state is JobState.QUEUED else None,
            "progress": self.progress,
            "error": self.error,
            "path": self.path,
            "already_present": self.already_present,
            "warnings": list(self.warnings),
            "tags": self.tags,
        }


@dataclass(frozen=True)
class TrackRecord:
    """One row of ingest history: the video-id index over the library.

    The filesystem remains the library database (PLAN.md 3.1); this table exists only so a
    repeated ingest can be recognised *before* spending a metadata request on it. The tags are
    kept alongside so a deduped job can answer with the same shape as a fresh one.
    """

    video_id: str
    path: str
    title: str | None = None
    artist: str | None = None
    tags: dict[str, Any] | None = None
    ingested_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "path": self.path,
            "title": self.title,
            "artist": self.artist,
            "tags": self.tags,
            "ingested_at": iso(self.ingested_at),
        }
