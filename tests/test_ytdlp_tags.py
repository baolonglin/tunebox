"""Tag mapping: recorded yt-dlp JSON -> TrackTags."""

from __future__ import annotations

import pytest

from tunebox.sources.ytdlp_tags import (
    clean_channel_name,
    clean_title,
    search_results_from_playlist,
    split_artist_title,
    tags_from_info,
)


class TestPlainUpload:
    """'Kraftwerk - Autobahn (Single version 1974)' by channel 'EddieAlda', no music metadata."""

    def test_artist_and_title_come_from_the_title(self, plain_upload_info):
        tags = tags_from_info(plain_upload_info)
        assert tags.artist == "Kraftwerk"
        assert tags.title == "Autobahn (Single version 1974)"

    def test_uploader_is_not_used_as_artist_when_the_title_splits(self, plain_upload_info):
        assert tags_from_info(plain_upload_info).artist != "EddieAlda"

    def test_no_album_no_year_rather_than_guessed_ones(self, plain_upload_info):
        tags = tags_from_info(plain_upload_info)
        assert tags.album is None
        assert tags.album_artist is None
        # upload_date is 2010-07-09; the recording is from 1974, so no year is the honest answer
        assert tags.year is None

    def test_source_identity_is_carried(self, plain_upload_info):
        tags = tags_from_info(plain_upload_info)
        assert tags.source == "youtube"
        assert tags.source_id == "iukUMRlaBBE"
        assert tags.source_url == "https://www.youtube.com/watch?v=iukUMRlaBBE"
        assert tags.duration == pytest.approx(207.0)


class TestArtTrack:
    """Real music metadata present: believe it rather than re-deriving it."""

    def test_uses_music_metadata(self, art_track_info):
        tags = tags_from_info(art_track_info)
        assert tags.artist == "Kraftwerk"
        assert tags.title == "Autobahn (2009 Remaster)"
        assert tags.album == "Autobahn"

    def test_album_artist_defaults_to_artist_when_an_album_exists(self, art_track_info):
        assert tags_from_info(art_track_info).album_artist == "Kraftwerk"

    def test_title_is_not_split_on_the_dash_inside_the_track_name(self, art_track_info):
        info = dict(art_track_info, track="Autobahn - Remaster", title="Kraftwerk - Autobahn")
        assert tags_from_info(info).title == "Autobahn - Remaster"


class TestTitleSplitting:
    @pytest.mark.parametrize(
        "raw, artist, title",
        [
            ("Kraftwerk - Autobahn", "Kraftwerk", "Autobahn"),
            ("Kraftwerk – Autobahn", "Kraftwerk", "Autobahn"),  # en dash
            ("Kraftwerk — Autobahn", "Kraftwerk", "Autobahn"),  # em dash
            ("Boards of Canada | Dayvan Cowboy", "Boards of Canada", "Dayvan Cowboy"),
            ("Artist - Song - Live Version", "Artist", "Song - Live Version"),
            ("SingleWordTitle", None, "SingleWordTitle"),
            ("A song about hyphens", None, "A song about hyphens"),
        ],
    )
    def test_split(self, raw, artist, title):
        assert split_artist_title(raw) == (artist, title)

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Autobahn (Official Video)", "Autobahn"),
            ("Autobahn [Official Music Video]", "Autobahn"),
            ("Autobahn (Official Audio) (HD)", "Autobahn"),
            ("Autobahn (Lyrics)", "Autobahn"),
            ("Autobahn - Official Video", "Autobahn"),
            ("Autobahn (Live at Wembley)", "Autobahn (Live at Wembley)"),  # meaningful, kept
            ("Autobahn (Single version 1974)", "Autobahn (Single version 1974)"),
            ("(Official Video)", "(Official Video)"),  # nothing left: keep the original
        ],
    )
    def test_noise_removal(self, raw, expected):
        assert clean_title(raw) == expected

    def test_artist_falls_back_to_the_channel_name(self):
        tags = tags_from_info({"id": "x" * 11, "title": "Some Instrumental", "uploader": "Aphex Twin - Topic"})
        assert tags.artist == "Aphex Twin"
        assert tags.title == "Some Instrumental"

    def test_unknown_artist_is_the_last_resort(self):
        tags = tags_from_info({"id": "y" * 11, "title": "Untitled"})
        assert tags.artist == "Unknown Artist"

    @pytest.mark.parametrize(
        "channel, expected",
        [("Kraftwerk - Topic", "Kraftwerk"), ("RickAstleyVEVO", "RickAstley"), (None, None), ("", None)],
    )
    def test_channel_cleaning(self, channel, expected):
        assert clean_channel_name(channel) == expected


class TestReleaseYear:
    def test_release_year_is_used(self, plain_upload_info):
        info = dict(plain_upload_info, release_year=1974)
        assert tags_from_info(info).year == 1974

    def test_release_date_is_used(self, plain_upload_info):
        info = dict(plain_upload_info, release_date="19741101")
        assert tags_from_info(info).year == 1974

    def test_upload_date_is_never_used(self, plain_upload_info):
        info = dict(plain_upload_info, upload_date="20100709")
        assert tags_from_info(info).year is None


class TestSearchParsing:
    def test_parses_recorded_search(self, search_payload):
        results = search_results_from_playlist(search_payload)
        assert [r.id for r in results] == ["iukUMRlaBBE", "x-G28iyPtz0"]
        first = results[0]
        assert first.title == "Kraftwerk - Autobahn (Single version 1974)"
        assert first.url == "https://www.youtube.com/watch?v=iukUMRlaBBE"
        assert first.uploader == "EddieAlda"
        assert first.duration == pytest.approx(207.0)
        assert first.thumbnail and first.thumbnail.startswith("https://i.ytimg.com/")

    def test_entries_without_an_id_are_skipped_not_fatal(self, search_payload):
        payload = dict(search_payload, entries=[{"title": "unavailable"}, *search_payload["entries"]])
        assert len(search_results_from_playlist(payload)) == 2

    def test_missing_duration_and_thumbnail_are_tolerated(self):
        payload = {"entries": [{"id": "a" * 11, "title": "T"}]}
        result = search_results_from_playlist(payload)[0]
        assert (result.duration, result.thumbnail) == (None, None)
        assert result.url == f"https://www.youtube.com/watch?v={'a' * 11}"

    def test_empty_payload(self):
        assert search_results_from_playlist({}) == []

    def test_search_results_are_json_serialisable(self, search_payload):
        import json

        results = search_results_from_playlist(search_payload)
        assert json.loads(json.dumps([r.as_dict() for r in results]))[0]["id"] == "iukUMRlaBBE"
