"""CJK titles, end to end through mapping and naming.

Chinese-language uploads follow different conventions from the Western "Artist - Title" habit:
the title sits inside ``《》``, the channel is usually the record label rather than the artist,
and the marketing boilerplate is in Chinese. Getting this wrong fills the library with albums
named after labels, so it gets its own test file.

All metadata comes from recorded fixtures — no network.
"""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from tunebox.core import naming
from tunebox.sources.ytdlp_tags import (
    clean_channel_name,
    clean_derived_artist,
    clean_title,
    leading_hashtag,
    plausible_artist,
    search_results_from_playlist,
    split_artist_title,
    split_bracketed_title,
    tags_from_info,
)


class TestLabelUpload:
    """``陳奕迅 Eason Chan《K歌之王》[Official MV]``, uploaded by ``英皇娛樂 eeg music``."""

    def test_artist_comes_from_the_title_not_the_label_channel(self, cjk_label_upload_info):
        tags = tags_from_info(cjk_label_upload_info)
        assert tags.artist == "陳奕迅 Eason Chan"
        assert tags.artist != "英皇娛樂 eeg music"

    def test_title_is_what_is_inside_the_brackets(self, cjk_label_upload_info):
        assert tags_from_info(cjk_label_upload_info).title == "K歌之王"

    def test_path_is_built_from_the_real_artist(self, cjk_label_upload_info):
        tags = tags_from_info(cjk_label_upload_info)
        assert naming.relative_path(tags, "m4a") == PurePosixPath(
            "陳奕迅 Eason Chan/Singles/K歌之王.m4a"
        )

    def test_source_identity_is_carried(self, cjk_label_upload_info):
        tags = tags_from_info(cjk_label_upload_info)
        assert tags.source_id == "Sm_DVsixxsk"
        assert tags.duration == pytest.approx(229.0)


class TestInvertedTitle:
    """``《K歌之王 AIR (Day Version)》陳奕迅 Eason Chan with The Heritage Orchestra【Official MV】``."""

    def test_title_first_artist_after_is_handled(self, cjk_inverted_info):
        tags = tags_from_info(cjk_inverted_info)
        assert tags.title == "K歌之王 AIR (Day Version)"
        assert tags.artist == "陳奕迅 Eason Chan with The Heritage Orchestra"

    def test_full_width_bracket_noise_is_stripped(self, cjk_inverted_info):
        assert "Official MV" not in tags_from_info(cjk_inverted_info).artist
        assert "【" not in tags_from_info(cjk_inverted_info).title

    def test_a_real_release_year_is_kept(self, cjk_inverted_info):
        assert tags_from_info(cjk_inverted_info).year == 2025


class TestUnsplittableTitle:
    """``K歌之王 陈奕迅 (歌词版)`` on a lyric-video channel: honest fallback, no invention."""

    def test_chinese_boilerplate_is_removed_from_the_title(self, cjk_lyric_channel_info):
        assert tags_from_info(cjk_lyric_channel_info).title == "K歌之王 陈奕迅"

    def test_artist_falls_back_to_the_channel(self, cjk_lyric_channel_info):
        """Nothing in the metadata identifies the artist; `--artist` is the fix, not a guess."""
        assert tags_from_info(cjk_lyric_channel_info).artist == "GM Lyric"


class TestBracketSplitting:
    @pytest.mark.parametrize(
        "raw, artist, title",
        [
            ("陳奕迅《K歌之王》", "陳奕迅", "K歌之王"),
            ("陳奕迅 Eason Chan《K歌之王》[Official MV]", "陳奕迅 Eason Chan", "K歌之王"),
            ("《K歌之王》陳奕迅", "陳奕迅", "K歌之王"),
            ("陳奕迅《K歌之王(國)》", "陳奕迅", "K歌之王(國)"),
            ("五月天〈後來的我們〉", "五月天", "後來的我們"),
            ("宇多田ヒカル「First Love」", "宇多田ヒカル", "First Love"),
            ("周杰倫『稻香』", "周杰倫", "稻香"),
        ],
    )
    def test_split(self, raw, artist, title):
        assert split_bracketed_title(raw) == (artist, title)

    @pytest.mark.parametrize(
        "raw",
        ["Kraftwerk - Autobahn", "K歌之王 陈奕迅", "陳奕迅《》", "no brackets here"],
    )
    def test_no_bracketed_title(self, raw):
        assert split_bracketed_title(raw) == (None, None)


class TestTightSeparators:
    """CJK uploads usually omit the spaces around the separator."""

    @pytest.mark.parametrize(
        "raw, artist, title",
        [
            ("陈奕迅-K歌之王", "陈奕迅", "K歌之王"),
            ("陈奕迅－K歌之王", "陈奕迅", "K歌之王"),
            ("陳奕迅｜K歌之王", "陳奕迅", "K歌之王"),
            ("陳奕迅：K歌之王", "陳奕迅", "K歌之王"),
        ],
    )
    def test_split(self, raw, artist, title):
        assert split_artist_title(raw) == (artist, title)

    def test_western_hyphenated_names_are_left_alone(self):
        """The tight split is CJK-only, so 'Blink-182' must not become artist 'Blink'."""
        assert split_artist_title("Blink-182") == (None, "Blink-182")
        assert split_artist_title("Jay-Z") == (None, "Jay-Z")

    def test_a_spaced_separator_still_wins(self):
        assert split_artist_title("陳奕迅 2003演唱會 - K歌之王") == ("陳奕迅 2003演唱會", "K歌之王")


class TestChineseNoiseRemoval:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("K歌之王 (歌词版)", "K歌之王"),
            ("K歌之王（歌詞版）", "K歌之王"),
            ("K歌之王【官方MV】", "K歌之王"),
            ("K歌之王 [官方版]", "K歌之王"),
            ("K歌之王 (中文字幕)", "K歌之王"),
            ("K歌之王 (高清版)", "K歌之王"),
            # meaningful, must survive
            ("K歌之王 (現場版)", "K歌之王 (現場版)"),
            ("K歌之王 (超CD水準)", "K歌之王 (超CD水準)"),
            ("K歌之王 (國)", "K歌之王 (國)"),
        ],
    )
    def test_clean_title(self, raw, expected):
        assert clean_title(raw) == expected

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("陳奕迅 2003演唱會", "陳奕迅"),
            ("陳奕迅 演唱會", "陳奕迅"),
            ("五月天 2019演唱会", "五月天"),
            ("陳奕迅", "陳奕迅"),
            ("Kraftwerk", "Kraftwerk"),
        ],
    )
    def test_concert_designations_are_not_artists(self, raw, expected):
        assert clean_derived_artist(raw) == expected

    def test_a_concert_upload_maps_to_the_performer(self):
        info = {
            "id": "nH6a9-hKieA",
            "title": "陳奕迅 2003演唱會 - K歌之王 (超CD水準)",
            "uploader": "Elden Cat",
        }
        tags = tags_from_info(info)
        assert tags.artist == "陳奕迅"
        assert tags.title == "K歌之王 (超CD水準)"


class TestMessyRealTitles:
    """The eight results a search for ``好久不见`` actually returns.

    Recorded from live yt-dlp output: each one broke the mapping in a different way.
    """

    def test_a_bare_mv_suffix_is_not_part_of_the_song_name(self, cjk_mv_suffix_info):
        """``陳奕迅 - 好久不見MV`` — no bracket to key off, the MV is glued to the CJK text."""
        tags = tags_from_info(cjk_mv_suffix_info)
        assert (tags.artist, tags.title) == ("陳奕迅", "好久不見")

    def test_a_leading_hashtag_names_the_performer(self, cjk_hashtag_info):
        """The channel is a TV show ("中国好声音官方频道"); the hashtag is the singer."""
        tags = tags_from_info(cjk_hashtag_info)
        assert tags.artist == "单依纯"
        assert tags.title == "好久不见"

    def test_square_bracket_titles_beat_the_label_channel(self, cjk_square_bracket_info):
        """``JAMES 楊永聰 [好久不見 Long Time No See] Official MV`` on 華研國際's channel."""
        tags = tags_from_info(cjk_square_bracket_info)
        assert tags.artist == "JAMES 楊永聰"
        assert tags.title == "好久不見 Long Time No See"

    def test_clickbait_prose_never_becomes_an_artist(self, cjk_clickbait_info):
        """A directory named "开口就碎了！陈奕迅深情演绎" would be a lasting embarrassment."""
        tags = tags_from_info(cjk_clickbait_info)
        assert tags.title == "好久不见"
        assert "！" not in tags.artist
        assert tags.artist == "SMG音乐频道"  # the channel: honest, if unhelpful

    def test_a_lyric_teaser_is_not_the_title(self, cjk_lyric_teaser_info):
        """``單依純 - 好久不見 (Live)『你會不會忽然地出現...』【動態歌詞MV】``.

        The 『』 quote holds song lyrics, so the dash split must win over bracket extraction —
        otherwise the title becomes a sentence and the artist becomes "單依純 - 好久不見 (Live)".
        """
        tags = tags_from_info(cjk_lyric_teaser_info)
        assert tags.artist == "單依純"
        assert tags.title == "好久不見 (Live)"

    def test_every_messy_title_produces_a_sane_path(
        self,
        cjk_mv_suffix_info,
        cjk_hashtag_info,
        cjk_square_bracket_info,
        cjk_clickbait_info,
        cjk_lyric_teaser_info,
    ):
        for info in (
            cjk_mv_suffix_info,
            cjk_hashtag_info,
            cjk_square_bracket_info,
            cjk_clickbait_info,
            cjk_lyric_teaser_info,
        ):
            path = naming.relative_path(tags_from_info(info), "m4a")
            assert len(path.parts) == 3
            assert not any(len(part.encode("utf-8")) > 130 for part in path.parts)
            # no sentence punctuation, hashtags or stray brackets in the artist directory
            assert not set("！？，。#《》『』【】") & set(path.parts[0])


class TestArtistPlausibility:
    @pytest.mark.parametrize(
        "candidate, ok",
        [
            ("陳奕迅", True),
            ("陳奕迅 Eason Chan", True),
            ("陳奕迅 Eason Chan with The Heritage Orchestra", True),  # a long but real credit
            ("开口就碎了！陈奕迅深情演绎", False),
            ("「叙事感女王」每一句都是故事，听一遍哭一遍", False),
            ("这首歌太好听了吧？", False),
            ("x" * 61, False),
            ("", False),
            (None, False),
        ],
    )
    def test_plausible_artist(self, candidate, ok):
        assert plausible_artist(candidate) is ok


class TestChannelNameCleanup:
    @pytest.mark.parametrize(
        "channel, expected",
        [
            ("单依纯Official", "单依纯"),
            ("Eason Chan Official Channel", "Eason Chan"),
            ("中国好声音官方频道", "中国好声音"),
            ("五月天 - Official", "五月天"),
            ("Kraftwerk - Topic", "Kraftwerk"),
            ("RickAstleyVEVO", "RickAstley"),
            ("華研國際", "華研國際"),
        ],
    )
    def test_clean_channel_name(self, channel, expected):
        assert clean_channel_name(channel) == expected


class TestLeadingHashtag:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("#单依纯 《好久不见》", "单依纯"),
            ("  #JAMES 楊永聰 [好久不見]", "JAMES"),
            ("好久不见 #单依纯", None),  # trailing hashtags are topic spam, not credits
            ("好久不见", None),
        ],
    )
    def test_leading_hashtag(self, raw, expected):
        assert leading_hashtag(raw) == expected

    def test_hashtags_are_stripped_from_titles(self):
        assert clean_title("好久不见 #shanyichun #中国好声音") == "好久不见"


class TestLyricTeaserStripping:
    def test_a_long_quote_is_dropped(self):
        raw = "好久不見 (Live)『你會不會忽然地出現，在街角的咖啡店，我會帶着笑臉 揮手寒暄，和你坐着聊聊天。』"
        assert clean_title(raw) == "好久不見 (Live)"

    def test_a_short_quote_is_kept_as_a_title(self):
        assert split_bracketed_title("宇多田ヒカル「First Love」") == ("宇多田ヒカル", "First Love")

    def test_animated_lyrics_boilerplate_is_removed(self):
        assert clean_title("好久不見【動態歌詞MV】") == "好久不見"
        assert clean_title("好久不見【動態歌詞】") == "好久不見"


class TestCJKSearch:
    def test_results_survive_the_round_trip(self, cjk_search_payload):
        """Assertions are derived from the fixture: search rankings shift between recordings."""
        results = search_results_from_playlist(cjk_search_payload)
        entries = cjk_search_payload["entries"]

        assert len(results) == len(entries)
        assert [r.id for r in results] == [e["id"] for e in entries]
        assert [r.title for r in results] == [e["title"] for e in entries]
        assert all(r.duration and r.duration > 0 for r in results)
        assert any("《K歌之王》" in r.title for r in results)  # CJK brackets survive intact

    def test_the_label_channel_is_reported_verbatim_in_search_results(self, cjk_search_payload):
        """Search results describe the upload; interpretation happens at ingest."""
        results = search_results_from_playlist(cjk_search_payload)
        labels = [r.uploader for r in results]
        assert "英皇娛樂 eeg music" in labels

    def test_results_are_json_serialisable_without_escaping_loss(self, cjk_search_payload):
        import json

        results = search_results_from_playlist(cjk_search_payload)
        reloaded = json.loads(json.dumps([r.as_dict() for r in results], ensure_ascii=False))
        titles = [item["title"] for item in reloaded]
        assert any("《K歌之王》" in title for title in titles)


class TestCJKPathSafety:
    def test_cjk_components_are_preserved_not_transliterated(self):
        from tunebox.core.models import TrackTags

        path = naming.relative_path(TrackTags(title="K歌之王", artist="陳奕迅", album="打得火熱"), "m4a")
        assert path == PurePosixPath("陳奕迅/打得火熱/K歌之王.m4a")

    def test_full_width_slash_lookalikes_are_not_separators(self):
        """U+FF0F is a normal character, not a path separator — it must not split the path."""
        from tunebox.core.models import TrackTags

        path = naming.relative_path(TrackTags(title="A／B", artist="陳奕迅"), "m4a")
        assert len(path.parts) == 3
        assert path.name == "A／B.m4a"

    def test_long_cjk_titles_stay_within_the_byte_budget(self):
        from tunebox.core.models import TrackTags

        path = naming.relative_path(TrackTags(title="歌" * 200, artist="陳" * 200), "m4a")
        for part in path.parts:
            assert len(part.encode("utf-8")) <= naming.MAX_COMPONENT + len(".m4a")
