"""Shared test fixtures.

No test in this suite touches the network: source metadata comes from the recorded JSON in
``tests/fixtures`` (re-record with ``tools/record_fixtures.py``), and the ingest tests use a
fake source. That is the Phase 1 requirement "no network in CI".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
def plain_upload_info() -> dict:
    """An ordinary upload: metadata must be inferred from the title."""
    return load_fixture("video_plain_upload.json")


@pytest.fixture
def art_track_info() -> dict:
    """A YouTube Music art track: real track/artist/album fields."""
    return load_fixture("video_art_track.json")


@pytest.fixture
def search_payload() -> dict:
    """``--flat-playlist --dump-single-json`` output for ``ytsearch2:kraftwerk autobahn``."""
    return load_fixture("search_kraftwerk_autobahn.json")


@pytest.fixture
def cjk_search_payload() -> dict:
    """Search results for ``k歌之王`` — CJK query, CJK titles, full-width punctuation."""
    return load_fixture("search_cjk.json")


@pytest.fixture
def cjk_label_upload_info() -> dict:
    """``陳奕迅 Eason Chan《K歌之王》[Official MV]`` on the record label's channel."""
    return load_fixture("video_cjk_label_upload.json")


@pytest.fixture
def cjk_inverted_info() -> dict:
    """``《K歌之王 AIR (Day Version)》陳奕迅 ...【Official MV】`` — title first, artist after."""
    return load_fixture("video_cjk_inverted.json")


@pytest.fixture
def cjk_lyric_channel_info() -> dict:
    """``K歌之王 陈奕迅 (歌词版)`` — no separator at all, so the artist is genuinely unknowable."""
    return load_fixture("video_cjk_lyric_channel.json")


@pytest.fixture
def cjk_mv_suffix_info() -> dict:
    """``陳奕迅 - 好久不見MV`` — a bare "MV" glued to the title, outside any bracket."""
    return load_fixture("video_cjk_mv_suffix.json")


@pytest.fixture
def cjk_hashtag_info() -> dict:
    """``#单依纯 《好久不见》...`` on a TV show's channel: the leading hashtag is the performer."""
    return load_fixture("video_cjk_hashtag.json")


@pytest.fixture
def cjk_square_bracket_info() -> dict:
    """``JAMES 楊永聰 [好久不見 Long Time No See] Official MV`` — label house style."""
    return load_fixture("video_cjk_square_brackets.json")


@pytest.fixture
def cjk_clickbait_info() -> dict:
    """``开口就碎了！陈奕迅深情演绎《好久不见》...`` — prose that must not become an artist."""
    return load_fixture("video_cjk_clickbait.json")


@pytest.fixture
def cjk_lyric_teaser_info() -> dict:
    """``單依純 - 好久不見 (Live)『你會不會忽然地出現...』【動態歌詞MV】`` — quote after the title."""
    return load_fixture("video_cjk_lyric_teaser.json")
