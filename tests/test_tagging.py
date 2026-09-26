"""Tag writing round-trips through real audio files.

Needs ffmpeg to synthesise a tiny AAC/MP3 file — no network. Skipped where ffmpeg is absent,
because tunebox cannot work without ffmpeg anyway (PLAN.md 0.3) and a skip here is honest.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tunebox.core.models import TrackTags
from tunebox.errors import TaggingError
from tunebox.tagging import read_tags, write_tags

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

TAGS = TrackTags(
    title="Autobahn",
    artist="Kraftwerk",
    album="Autobahn",
    album_artist="Kraftwerk",
    year=1974,
    track_number=1,
    duration=1.0,
    source="youtube",
    source_id="iukUMRlaBBE",
    source_url="https://www.youtube.com/watch?v=iukUMRlaBBE",
)

# 1x1 pixel JPEG — enough to prove cover embedding without shipping a binary fixture.
TINY_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300"
    "0806060706050807070709090808"
    + "0a" * 50
    + "ffc00011080001000101011100ffc4001f0000010501010101010100000000000000000102030405060708090a0b"
    + "ffc40014100100000000000000000000000000000000ffda0008010100003f00d2cf20ffd9"
)


def _make_audio(path: Path, codec: str) -> Path:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            *(["-c:a", "aac", "-b:a", "64k"] if codec == "aac" else ["-c:a", "libmp3lame"]),
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return path


@pytest.fixture
def cover(tmp_path: Path) -> Path:
    path = tmp_path / "cover.jpg"
    path.write_bytes(TINY_JPEG)
    return path


class TestM4A:
    def test_round_trip(self, tmp_path: Path, cover: Path):
        audio = _make_audio(tmp_path / "track.m4a", "aac")
        write_tags(audio, TAGS, cover)

        read = read_tags(audio)
        assert read["title"] == "Autobahn"
        assert read["artist"] == "Kraftwerk"
        assert read["album"] == "Autobahn"
        assert read["album_artist"] == "Kraftwerk"
        assert read["year"] == "1974"
        assert read["track_number"] == 1
        assert read["has_cover"] is True

    def test_source_identity_survives_in_the_file(self, tmp_path: Path):
        """The library alone must be enough to know where a track came from (Phase 2 dedupe)."""
        audio = _make_audio(tmp_path / "track.m4a", "aac")
        write_tags(audio, TAGS)
        read = read_tags(audio)
        assert read["source_id"] == "iukUMRlaBBE"
        assert read["source_url"].endswith("v=iukUMRlaBBE")

    def test_writing_twice_replaces_rather_than_appends(self, tmp_path: Path):
        audio = _make_audio(tmp_path / "track.m4a", "aac")
        write_tags(audio, TAGS)
        write_tags(audio, TrackTags(title="New Title", artist="New Artist"))

        read = read_tags(audio)
        assert read["title"] == "New Title"
        assert read["album"] is None  # stale album from the first write is gone

    def test_missing_cover_file_is_ignored(self, tmp_path: Path):
        audio = _make_audio(tmp_path / "track.m4a", "aac")
        write_tags(audio, TAGS, tmp_path / "does-not-exist.jpg")
        assert read_tags(audio)["has_cover"] is False

    def test_non_image_cover_is_ignored_not_embedded(self, tmp_path: Path):
        audio = _make_audio(tmp_path / "track.m4a", "aac")
        junk = tmp_path / "cover.jpg"
        junk.write_bytes(b"this is not an image")
        write_tags(audio, TAGS, junk)
        assert read_tags(audio)["has_cover"] is False

    def test_minimal_tags(self, tmp_path: Path):
        audio = _make_audio(tmp_path / "track.m4a", "aac")
        write_tags(audio, TrackTags(title="Untitled", artist="Unknown Artist"))
        read = read_tags(audio)
        assert (read["title"], read["artist"]) == ("Untitled", "Unknown Artist")
        assert read["album"] is None and read["track_number"] is None


class TestMP3:
    def test_round_trip(self, tmp_path: Path, cover: Path):
        audio = _make_audio(tmp_path / "track.mp3", "mp3")
        write_tags(audio, TAGS, cover)

        read = read_tags(audio)
        assert read["title"] == "Autobahn"
        assert read["artist"] == "Kraftwerk"
        assert read["album_artist"] == "Kraftwerk"
        assert read["year"] == "1974"
        assert read["track_number"] == "1"
        assert read["source_id"] == "iukUMRlaBBE"
        assert read["has_cover"] is True

    def test_writing_twice_replaces(self, tmp_path: Path):
        audio = _make_audio(tmp_path / "track.mp3", "mp3")
        write_tags(audio, TAGS)
        write_tags(audio, TrackTags(title="Second", artist="Second Artist"))
        read = read_tags(audio)
        assert read["title"] == "Second"
        assert read["album"] is None


def test_unsupported_container_raises_tagging_error(tmp_path: Path):
    path = tmp_path / "track.flac"
    path.write_bytes(b"not really flac")
    with pytest.raises(TaggingError):
        write_tags(path, TAGS)


def test_corrupt_file_raises_tagging_error_not_a_crash(tmp_path: Path):
    path = tmp_path / "track.m4a"
    path.write_bytes(b"definitely not an mp4 container")
    with pytest.raises(TaggingError):
        write_tags(path, TAGS)
