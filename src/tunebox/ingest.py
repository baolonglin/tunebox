"""The ingest use case: URL in, correctly tagged file in the library out.

This is the one piece of orchestration in Phase 1, and it is deliberately dependency-injected:
it talks to a :class:`~tunebox.core.ports.MusicSource` and a
:class:`~tunebox.core.ports.TagWriter`, never to yt-dlp or mutagen directly. That is what makes
it testable without a network and swappable if the source changes (PLAN.md 1.E, 3.4).

Order of operations, and why:

1. ``inspect`` first — metadata is cheap, and it tells us the destination path.
2. dedupe on that path before downloading, so a repeated ingest costs one metadata request.
3. fetch into scratch, ``tag``, then ``place`` last, so the library only ever gains complete,
   already-tagged files.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from . import library, tagging
from .config import Config
from .core.models import IngestResult, TrackTags
from .core.ports import MusicSource, ProgressCallback, TagWriter
from .errors import TaggingError

log = logging.getLogger(__name__)


def ingest(
    url: str,
    *,
    source: MusicSource,
    config: Config,
    tag_writer: TagWriter = tagging.write_tags,
    on_progress: ProgressCallback | None = None,
    overrides: dict[str, object] | None = None,
    force: bool = False,
) -> IngestResult:
    """Fetch ``url`` into the library and return where it landed.

    ``overrides`` replaces mapped tag fields (used by ``--artist``/``--album``/``--title``),
    applied before the destination path is computed so a correction also fixes the filename.
    ``force`` re-downloads even when the destination already exists.
    """
    tags = source.inspect(url)
    if overrides:
        tags = _apply_overrides(tags, overrides)

    destination = library.destination_for(config.music_root, tags, config.audio_format)
    if destination.exists() and not force:
        log.info("already in library, skipping download: %s", destination)
        return IngestResult(path=destination, tags=tags, already_present=True)

    warnings: list[str] = []
    with library.scratch_dir(config.scratch_root) as work:
        media = source.fetch(
            url,
            work,
            audio_format=config.audio_format,
            on_progress=on_progress,
        )

        # The container is whatever the source actually produced — trust the file, not the
        # request, so an unexpected extension cannot silently land at the wrong path.
        actual_ext = media.audio_path.suffix.lstrip(".").lower()
        if actual_ext != config.audio_format:
            warnings.append(
                f"expected .{config.audio_format} but got .{actual_ext}; "
                "filing under the actual extension"
            )
            destination = library.destination_for(config.music_root, tags, actual_ext)

        try:
            tag_writer(media.audio_path, tags, media.cover_path)
        except TaggingError as exc:
            # A missing cover or an odd container must not cost us the audio; the file is still
            # playable and the tags can be fixed in place later.
            log.warning("tagging failed, keeping untagged audio: %s", exc)
            warnings.append(f"tagging failed: {exc}")

        final = library.place(media.audio_path, destination, overwrite=force)

    log.info("ingested %s -> %s", tags.source_id or url, final)
    return IngestResult(path=final, tags=tags, already_present=False, warnings=tuple(warnings))


def _apply_overrides(tags: TrackTags, overrides: dict[str, object]) -> TrackTags:
    allowed = {"title", "artist", "album", "album_artist", "year", "track_number"}
    unknown = set(overrides) - allowed
    if unknown:
        raise ValueError(f"unknown tag override(s): {', '.join(sorted(unknown))}")
    clean = {k: v for k, v in overrides.items() if v is not None}
    if not clean:
        return tags
    updated = replace(tags, **clean)
    # ``--album`` on a track that had none leaves album_artist unset; derive it here so the
    # library groups correctly instead of showing a one-track album with no artist.
    if updated.album and not updated.album_artist:
        updated = replace(updated, album_artist=updated.artist)
    return updated


def plan_destination(
    url: str,
    *,
    source: MusicSource,
    config: Config,
    overrides: dict[str, object] | None = None,
) -> tuple[TrackTags, Path]:
    """Metadata-only dry run: what would be written, and where.

    Applies ``overrides`` exactly as :func:`ingest` does — a dry run that ignored them would
    report a path the real ingest never uses, which is worse than no dry run at all.
    """
    tags = source.inspect(url)
    if overrides:
        tags = _apply_overrides(tags, overrides)
    return tags, library.destination_for(config.music_root, tags, config.audio_format)
