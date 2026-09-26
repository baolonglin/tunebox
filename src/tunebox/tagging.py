"""The tag writer: mutagen adapter.

This is the authority on what ends up inside the file (PLAN.md 3.4). yt-dlp's
``--embed-metadata`` is deliberately not used, for two reasons: the mapping in
:mod:`tunebox.sources.ytdlp_tags` must be the only place metadata is decided, and Phase 3 needs
to feed the *same* ``TrackTags`` into the Chromecast ``LOAD`` payload — the soundbar never reads
the file's tags, so a second, divergent source of truth would be a bug waiting to happen.
"""

from __future__ import annotations

import logging
from pathlib import Path

from mutagen import MutagenError
from mutagen.id3 import APIC, TALB, TDRC, TIT2, TPE1, TPE2, TRCK, TXXX, ID3, ID3NoHeaderError
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover

from .core.models import TrackTags
from .errors import TaggingError

log = logging.getLogger(__name__)

#: Custom atom/frame names, so a track's origin survives in the file itself. Phase 2 dedupes on
#: the source id; having it in the file means the library alone is enough to rebuild that state.
SOURCE_ID_KEY = "TUNEBOX_SOURCE_ID"
SOURCE_URL_KEY = "TUNEBOX_SOURCE_URL"

_MP4_FREEFORM = "----:com.apple.iTunes:{}"


def write_tags(audio_path: Path, tags: TrackTags, cover_path: Path | None = None) -> None:
    """Write ``tags`` into ``audio_path``, embedding ``cover_path`` when given."""
    suffix = audio_path.suffix.lower()
    try:
        if suffix in (".m4a", ".mp4", ".m4b", ".aac"):
            _write_mp4(audio_path, tags, cover_path)
        elif suffix == ".mp3":
            _write_mp3(audio_path, tags, cover_path)
        else:
            raise TaggingError(f"no tag writer for {suffix or audio_path.name!r}")
    except MutagenError as exc:
        raise TaggingError(f"mutagen failed on {audio_path.name}: {exc}") from exc


def read_tags(audio_path: Path) -> dict[str, object]:
    """Read back the fields we write. Used by tests and by manual verification."""
    suffix = audio_path.suffix.lower()
    if suffix in (".m4a", ".mp4", ".m4b", ".aac"):
        audio = MP4(audio_path)
        raw = audio.tags or {}
        track = raw.get("trkn") or []
        return {
            "title": _one(raw.get("\xa9nam")),
            "artist": _one(raw.get("\xa9ART")),
            "album": _one(raw.get("\xa9alb")),
            "album_artist": _one(raw.get("aART")),
            "year": _one(raw.get("\xa9day")),
            "track_number": track[0][0] if track else None,
            "source_id": _one_bytes(raw.get(_MP4_FREEFORM.format(SOURCE_ID_KEY))),
            "source_url": _one_bytes(raw.get(_MP4_FREEFORM.format(SOURCE_URL_KEY))),
            "has_cover": bool(raw.get("covr")),
            "duration": getattr(audio.info, "length", None),
            "codec": getattr(audio.info, "codec", None),
        }
    if suffix == ".mp3":
        audio = MP3(audio_path)
        raw = audio.tags
        txxx = {frame.desc: frame.text[0] for frame in (raw.getall("TXXX") if raw else [])}
        return {
            "title": _text(raw, "TIT2"),
            "artist": _text(raw, "TPE1"),
            "album": _text(raw, "TALB"),
            "album_artist": _text(raw, "TPE2"),
            "year": _text(raw, "TDRC"),
            "track_number": _text(raw, "TRCK"),
            "source_id": txxx.get(SOURCE_ID_KEY),
            "source_url": txxx.get(SOURCE_URL_KEY),
            "has_cover": bool(raw and raw.getall("APIC")),
            "duration": getattr(audio.info, "length", None),
            "codec": "mp3",
        }
    raise TaggingError(f"no tag reader for {suffix or audio_path.name!r}")


# --------------------------------------------------------------------- writers


def _write_mp4(audio_path: Path, tags: TrackTags, cover_path: Path | None) -> None:
    audio = MP4(audio_path)
    if audio.tags is None:
        audio.add_tags()
    mp4 = audio.tags
    assert mp4 is not None

    # Replace rather than merge: whatever the source embedded is not authoritative.
    for key in ("\xa9nam", "\xa9ART", "\xa9alb", "aART", "\xa9day", "trkn", "covr"):
        mp4.pop(key, None)

    mp4["\xa9nam"] = [tags.title]
    mp4["\xa9ART"] = [tags.artist]
    if tags.album:
        mp4["\xa9alb"] = [tags.album]
    if tags.album_artist:
        mp4["aART"] = [tags.album_artist]
    if tags.year:
        mp4["\xa9day"] = [str(tags.year)]
    if tags.track_number:
        mp4["trkn"] = [(int(tags.track_number), 0)]
    if tags.source_id:
        mp4[_MP4_FREEFORM.format(SOURCE_ID_KEY)] = [tags.source_id.encode("utf-8")]
    if tags.source_url:
        mp4[_MP4_FREEFORM.format(SOURCE_URL_KEY)] = [tags.source_url.encode("utf-8")]

    cover = _load_cover(cover_path)
    if cover is not None:
        data, image_format = cover
        mp4["covr"] = [MP4Cover(data, imageformat=image_format)]

    audio.save()


def _write_mp3(audio_path: Path, tags: TrackTags, cover_path: Path | None) -> None:
    try:
        id3 = ID3(audio_path)
    except ID3NoHeaderError:
        id3 = ID3()

    for key in ("TIT2", "TPE1", "TPE2", "TALB", "TDRC", "TRCK", "APIC"):
        id3.delall(key)
    for frame in list(id3.getall("TXXX")):
        if frame.desc in (SOURCE_ID_KEY, SOURCE_URL_KEY):
            id3.delall(f"TXXX:{frame.desc}")

    id3.add(TIT2(encoding=3, text=[tags.title]))
    id3.add(TPE1(encoding=3, text=[tags.artist]))
    if tags.album:
        id3.add(TALB(encoding=3, text=[tags.album]))
    if tags.album_artist:
        id3.add(TPE2(encoding=3, text=[tags.album_artist]))
    if tags.year:
        id3.add(TDRC(encoding=3, text=[str(tags.year)]))
    if tags.track_number:
        id3.add(TRCK(encoding=3, text=[str(int(tags.track_number))]))
    if tags.source_id:
        id3.add(TXXX(encoding=3, desc=SOURCE_ID_KEY, text=[tags.source_id]))
    if tags.source_url:
        id3.add(TXXX(encoding=3, desc=SOURCE_URL_KEY, text=[tags.source_url]))

    cover = _load_cover(cover_path)
    if cover is not None:
        data, image_format = cover
        mime = "image/png" if image_format == MP4Cover.FORMAT_PNG else "image/jpeg"
        id3.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))

    id3.save(audio_path, v2_version=3)


# --------------------------------------------------------------------- helpers


def _load_cover(cover_path: Path | None) -> tuple[bytes, int] | None:
    """Read cover art, returning ``None`` (with a warning) rather than failing the ingest."""
    if cover_path is None or not cover_path.is_file():
        return None
    data = cover_path.read_bytes()
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data, MP4Cover.FORMAT_PNG
    if data[:3] == b"\xff\xd8\xff":
        return data, MP4Cover.FORMAT_JPEG
    log.warning("ignoring cover %s: not a JPEG or PNG", cover_path.name)
    return None


def _one(values: object) -> str | None:
    if isinstance(values, list) and values:
        return str(values[0])
    return None


def _one_bytes(values: object) -> str | None:
    if isinstance(values, list) and values:
        value = values[0]
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value)
    return None


def _text(tags: object, key: str) -> str | None:
    if tags is None:
        return None
    frame = tags.get(key)  # type: ignore[union-attr]
    if frame is None:
        return None
    return str(frame.text[0]) if getattr(frame, "text", None) else None
