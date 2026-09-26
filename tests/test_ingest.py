"""The ingest use case, driven by a fake source. No network, no yt-dlp, no ffmpeg."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tunebox.config import Config
from tunebox.core.models import FetchedMedia, Progress, TrackTags
from tunebox.errors import TaggingError
from tunebox.ingest import ingest, plan_destination
from tunebox.sources.ytdlp_tags import tags_from_info


@dataclass
class FakeSource:
    """Stands in for :class:`~tunebox.sources.ytdlp.YtDlpSource`."""

    tags: TrackTags
    name: str = "fake"
    extension: str = "m4a"
    with_cover: bool = True
    fetch_calls: list[str] = field(default_factory=list)
    inspect_calls: list[str] = field(default_factory=list)
    fail_fetch: bool = False

    def search(self, query, limit):  # pragma: no cover - not used by these tests
        return []

    def inspect(self, url: str) -> TrackTags:
        self.inspect_calls.append(url)
        return self.tags

    def fetch(self, url, dest_dir: Path, audio_format="m4a", on_progress=None) -> FetchedMedia:
        self.fetch_calls.append(url)
        if self.fail_fetch:
            raise RuntimeError("extraction exploded")
        if on_progress:
            on_progress(Progress(percent=50.0, downloaded_bytes=50, total_bytes=100))
            on_progress(Progress(percent=100.0, downloaded_bytes=100, total_bytes=100))
        audio = dest_dir / f"track.{self.extension}"
        audio.write_bytes(b"fake audio payload")
        cover = None
        if self.with_cover:
            cover = dest_dir / "track.jpg"
            cover.write_bytes(b"\xff\xd8\xfffake jpeg")
        return FetchedMedia(audio_path=audio, cover_path=cover)


class RecordingTagWriter:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[Path, TrackTags, Path | None]] = []
        self.fail = fail

    def __call__(self, audio_path: Path, tags: TrackTags, cover_path: Path | None = None) -> None:
        self.calls.append((audio_path, tags, cover_path))
        if self.fail:
            raise TaggingError("no tag writer for '.xyz'")


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(music_root=tmp_path / "music", audio_format="m4a", ytdlp=("false",))


@pytest.fixture
def source(plain_upload_info) -> FakeSource:
    return FakeSource(tags=tags_from_info(plain_upload_info))


URL = "https://www.youtube.com/watch?v=iukUMRlaBBE"


def test_file_lands_at_the_conventional_path(config, source):
    writer = RecordingTagWriter()
    result = ingest(URL, source=source, config=config, tag_writer=writer)

    assert result.path == config.music_root / "Kraftwerk" / "Singles" / "Autobahn (Single version 1974).m4a"
    assert result.path.read_bytes() == b"fake audio payload"
    assert result.already_present is False
    assert result.warnings == ()


def test_tags_are_written_before_placement_with_the_cover(config, source):
    writer = RecordingTagWriter()
    ingest(URL, source=source, config=config, tag_writer=writer)

    audio_path, tags, cover = writer.calls[0]
    assert tags.artist == "Kraftwerk"
    assert cover is not None and cover.name == "track.jpg"
    # tagging happens in scratch, not in the library
    assert config.music_root / ".tunebox-tmp" in audio_path.parents


def test_scratch_directory_is_cleaned_up(config, source):
    ingest(URL, source=source, config=config, tag_writer=RecordingTagWriter())
    assert list(config.scratch_root.iterdir()) == []


def test_second_ingest_is_deduped_without_downloading(config, source):
    ingest(URL, source=source, config=config, tag_writer=RecordingTagWriter())
    result = ingest(URL, source=source, config=config, tag_writer=RecordingTagWriter())

    assert result.already_present is True
    assert len(source.fetch_calls) == 1
    assert len(source.inspect_calls) == 2


def test_force_redownloads_and_overwrites_in_place(config, source):
    first = ingest(URL, source=source, config=config, tag_writer=RecordingTagWriter())
    second = ingest(URL, source=source, config=config, tag_writer=RecordingTagWriter(), force=True)

    assert second.path == first.path
    assert second.already_present is False
    assert len(source.fetch_calls) == 2
    # no "Autobahn (2).m4a" left behind
    assert len(list(second.path.parent.iterdir())) == 1


def test_overrides_change_both_tags_and_destination(config, source):
    result = ingest(
        URL,
        source=source,
        config=config,
        tag_writer=RecordingTagWriter(),
        overrides={"artist": "Kraftwerk", "album": "Autobahn", "title": "Autobahn", "track_number": 1},
    )
    assert result.path == config.music_root / "Kraftwerk" / "Autobahn" / "01 - Autobahn.m4a"
    assert result.tags.album == "Autobahn"


def test_adding_an_album_derives_the_album_artist(config, source):
    """Otherwise the library shows a one-track album with no artist to group it under."""
    result = ingest(URL, source=source, config=config, tag_writer=RecordingTagWriter(),
                    overrides={"album": "Autobahn"})
    assert result.tags.album_artist == "Kraftwerk"


def test_an_explicit_album_artist_is_not_overwritten(config, source):
    result = ingest(URL, source=source, config=config, tag_writer=RecordingTagWriter(),
                    overrides={"album": "Trans-Europe Express", "album_artist": "Various Artists"})
    assert result.tags.album_artist == "Various Artists"


def test_unknown_override_is_rejected(config, source):
    with pytest.raises(ValueError, match="unknown tag override"):
        ingest(URL, source=source, config=config, tag_writer=RecordingTagWriter(),
               overrides={"genre": "krautrock"})


def test_tagging_failure_keeps_the_audio_and_reports_a_warning(config, source):
    result = ingest(URL, source=source, config=config, tag_writer=RecordingTagWriter(fail=True))

    assert result.path.is_file()
    assert any("tagging failed" in w for w in result.warnings)


def test_unexpected_container_is_filed_under_its_real_extension(config, plain_upload_info):
    source = FakeSource(tags=tags_from_info(plain_upload_info), extension="opus")
    result = ingest(URL, source=source, config=config, tag_writer=RecordingTagWriter())

    assert result.path.suffix == ".opus"
    assert any("expected .m4a" in w for w in result.warnings)


def test_failed_fetch_leaves_no_library_file_and_no_scratch(config, plain_upload_info):
    source = FakeSource(tags=tags_from_info(plain_upload_info), fail_fetch=True)
    with pytest.raises(RuntimeError):
        ingest(URL, source=source, config=config, tag_writer=RecordingTagWriter())

    assert not (config.music_root / "Kraftwerk").exists()
    assert list(config.scratch_root.iterdir()) == []


def test_missing_cover_is_not_an_error(config, plain_upload_info):
    source = FakeSource(tags=tags_from_info(plain_upload_info), with_cover=False)
    writer = RecordingTagWriter()
    ingest(URL, source=source, config=config, tag_writer=writer)
    assert writer.calls[0][2] is None


def test_dry_run_reports_the_destination_without_fetching(config, source):
    tags, destination = plan_destination(URL, source=source, config=config)
    assert tags.artist == "Kraftwerk"
    assert destination.name == "Autobahn (Single version 1974).m4a"
    assert source.fetch_calls == []
    assert not destination.exists()


def test_dry_run_applies_the_same_overrides_as_a_real_ingest(config, source):
    """A dry run that ignored --artist/--album would advertise a path ingest never writes."""
    overrides = {"artist": "陳奕迅", "album": "打得火熱", "title": "K歌之王", "track_number": 3}
    tags, destination = plan_destination(URL, source=source, config=config, overrides=overrides)

    assert destination == config.music_root / "陳奕迅" / "打得火熱" / "03 - K歌之王.m4a"
    assert tags.album_artist == "陳奕迅"
    assert source.fetch_calls == []
