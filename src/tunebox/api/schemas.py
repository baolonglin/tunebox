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

from pydantic import BaseModel, ConfigDict, Field

from ..config import SUPPORTED_FORMATS


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


class HealthView(BaseModel):
    status: Literal["ok"]
    version: str
    music_root: str
    audio_format: str
    worker_running: bool
    workers: int
    jobs: dict[str, int]


class ErrorView(BaseModel):
    error: str
