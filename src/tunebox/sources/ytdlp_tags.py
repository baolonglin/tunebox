"""yt-dlp info dict -> tunebox domain objects.

The only module that knows the shape of yt-dlp's JSON. Pure functions, so the whole of this
file is testable against recorded fixtures with no network (Phase 1 exit criterion).

Two kinds of YouTube item matter in practice, and they need different treatment:

* **art tracks / official music uploads** carry real music metadata (``track``, ``artist``,
  ``album``) — use it verbatim, it is better than anything we could infer;
* **ordinary uploads** carry only a ``title`` like ``"Kraftwerk - Autobahn (Single version
  1974)"`` — split it, and strip the marketing noise YouTube titles collect.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

from ..core.models import SearchResult, TrackTags

SOURCE_NAME = "youtube"

#: Separators used between artist and title in upload titles, longest first.
_SEPARATORS = (" -- ", " — ", " – ", " - ", " | ", " ~ ", " · ")

#: Separators without surrounding spaces. Only tried on titles that contain CJK text, where
#: "陈奕迅-K歌之王" is idiomatic and "Blink-182" is not a risk.
_TIGHT_SEPARATORS = ("－", "—", "–", "-", "｜", "|", "：", "・", "·")

#: Chinese, Japanese and Korean ranges, plus full-width punctuation.
_CJK = re.compile(r"[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef\uac00-\ud7af]")

#: Hashtags. YouTube shows them inline in the title ("#单依纯 《好久不见》..."), and they are not
#: part of a song name — but a *leading* hashtag is very often the artist, so it is kept aside.
_HASHTAG = re.compile(r"(?<!\S)#[^\s#]+")
_LEADING_HASHTAG = re.compile(r"^\s*#([^\s#]+)")

#: A quotation group this long is a lyric teaser, not a song title:
#: "單依純 - 好久不見 (Live)『你會不會忽然地出現，在街角的咖啡店...』".
_LONG_QUOTE = re.compile(r"\s*[《〈「『]\s*[^》〉」』]{26,}\s*[》〉」』]")

#: Sentence punctuation marks clickbait prose ("开口就碎了！陈奕迅深情演绎《好久不见》..."), which
#: must never become an artist name — and never a directory name.
_SENTENCE_PUNCTUATION = re.compile(r"[！？。，；、…!?;]")

#: Absolute sanity cap for an artist inferred from a title.
_ARTIST_MAX_CHARS = 60

#: CJK title quotation marks. Unlike the brackets in ``_NOISE_BRACKETED`` these *contain the
#: title*: "陳奕迅 Eason Chan《K歌之王》" is the dominant convention on Chinese-language uploads,
#: and treating it as noise (or failing to split on it) yields the record label as the artist.
_TITLE_BRACKETS = (("《", "》"), ("〈", "〉"), ("『", "』"), ("「", "」"))

#: Tried only when no primary bracket matches: "JAMES 楊永聰 [好久不見 Long Time No See]" is the
#: house style of several Taiwanese labels. Square brackets are ambiguous (they also hold noise),
#: hence second place and a length limit.
_SECONDARY_TITLE_BRACKETS = (("[", "]"), ("【", "】"))
_SECONDARY_MAX_CHARS = 40

#: Punctuation left dangling once the title has been lifted out of its brackets.
_DANGLING = " \t-–—－|｜·・~,，、:："

#: Parenthesised/bracketed noise removed from titles. Conservative on purpose: a wrong removal
#: loses real information ("(Live at Wembley)" and "(現場版)" must survive), so only unambiguous
#: marketing boilerplate is listed.
_NOISE_PATTERNS = (
    r"official\s+music\s+video",
    r"official\s+video",
    r"official\s+audio",
    r"official\s+lyric[s]?\s+video",
    r"official\s+visuali[sz]er",
    r"official\s*m/?v",
    r"lyric[s]?\s+video",
    r"lyric[s]?",
    r"audio\s+only",
    r"visuali[sz]er",
    r"full\s+hd",
    r"hd\s+remaster(ed)?",
    r"\d{3,4}p",
    r"4k",
    r"hq",
    r"hd",
    r"m/?v",
    # CJK equivalents of the same boilerplate.
    r"官方\s*(完整版|版|m/?v|音樂視頻|音乐视频)?",
    r"[动動]態歌[词詞]\s*(?:m/?v)?",
    r"歌[词詞]\s*(版|m/?v)?",
    r"中文字幕",
    r"字幕\s*版",
    r"高清\s*(版)?",
    r"超清",
)

#: Bracket pairs that mark noise, including the full-width forms used in CJK text.
#: ``《》`` is deliberately absent — see ``_TITLE_BRACKETS``.
_NOISE_BRACKETED = re.compile(
    r"\s*[\(\[\{【〔［（｛]\s*(?:" + "|".join(_NOISE_PATTERNS) + r")\s*[\)\]\}】〕］）｝]",
    re.IGNORECASE,
)
_NOISE_BARE = re.compile(
    r"\s*[-–—|]?\s*\b(?:official\s+(?:music\s+)?video|official\s+audio|official\s*m/?v)\b\s*$",
    re.IGNORECASE,
)
#: "好久不見MV" — a bare MV suffix glued to CJK text, which no bracket rule can catch.
_NOISE_MV_SUFFIX = re.compile(r"(?<=[\u3400-\u9fff])\s*m/?v\s*$", re.IGNORECASE)
_TOPIC_CHANNEL = re.compile(r"\s*-\s*Topic$", re.IGNORECASE)
_VEVO_CHANNEL = re.compile(r"VEVO$", re.IGNORECASE)
#: "单依纯Official", "... Official Channel", "…官方频道" — channel decoration, not part of a name.
_CHANNEL_SUFFIX = re.compile(
    r"\s*(?:-\s*)?(?:official(?:\s+(?:channel|music|audio))?|官方(?:頻道|频道|音樂頻道|音乐频道)?)\s*$",
    re.IGNORECASE,
)

#: "陳奕迅 2003演唱會 - K歌之王" names a concert, not a band. Stripped from artists *derived from
#: the title* only — real metadata is never second-guessed.
_CONCERT_SUFFIX = re.compile(r"\s*(?:19|20)?\d{0,2}\s*(?:演唱[會会]|live\s*concert)\s*$", re.IGNORECASE)


def clean_derived_artist(name: str | None) -> str | None:
    """Tidy an artist that was inferred from an upload title."""
    if not name:
        return None
    cleaned = _CONCERT_SUFFIX.sub("", name).strip(_DANGLING).strip()
    return cleaned or name.strip() or None


def clean_title(raw: str) -> str:
    """Strip boilerplate from an upload title without touching meaningful parentheses."""
    text = _HASHTAG.sub(" ", raw or "")
    text = _LONG_QUOTE.sub(" ", text)
    text = _NOISE_BRACKETED.sub("", text)
    text = _NOISE_BARE.sub("", text)
    text = _NOISE_MV_SUFFIX.sub("", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text.strip(_DANGLING).strip() or (raw or "").strip()


def leading_hashtag(raw: str) -> str | None:
    """``"#单依纯 《好久不见》..."`` -> ``"单依纯"``.

    A hashtag at the very start of a title is, in practice, almost always the performer. Used
    only as a last resort before the channel name, never over real metadata.
    """
    match = _LEADING_HASHTAG.match(raw or "")
    return match.group(1) if match else None


def plausible_artist(candidate: str | None) -> bool:
    """Reject artist candidates that are obviously prose rather than a name.

    Clickbait titles ("开口就碎了！陈奕迅深情演绎《好久不见》...") otherwise become an artist tag and,
    worse, a directory. Sentence punctuation is the giveaway; length alone is not, because
    "陳奕迅 Eason Chan with The Heritage Orchestra" is a legitimate credit.
    """
    if not candidate:
        return False
    if _SENTENCE_PUNCTUATION.search(candidate):
        return False
    return len(candidate) <= _ARTIST_MAX_CHARS


def _first_separator_index(text: str) -> int | None:
    positions = [text.find(sep) for sep in _SEPARATORS]
    found = [p for p in positions if p != -1]
    return min(found) if found else None


def _bracket_groups(text: str, pairs: tuple[tuple[str, str], ...], max_len: int) -> list[tuple[int, int, int]]:
    """Return ``(start, end, pair_index)`` for each usable bracket group, in title order."""
    groups: list[tuple[int, int, int]] = []
    for index, (open_char, close_char) in enumerate(pairs):
        start = text.find(open_char)
        if start == -1:
            continue
        end = text.find(close_char, start + 1)
        if end == -1:
            continue
        inside = text[start + 1 : end].strip()
        if not inside or len(inside) > max_len:
            continue
        groups.append((start, end, index))
    return sorted(groups)


def split_bracketed_title(raw: str) -> tuple[str | None, str | None]:
    """``"陳奕迅 Eason Chan《K歌之王》[Official MV]"`` -> ``("陳奕迅 Eason Chan", "K歌之王")``.

    Returns ``(artist_or_None, title_or_None)``; ``(None, None)`` when the title uses no CJK
    quotation marks. The artist is whichever side of the brackets has text — both
    ``Artist《Title》`` and ``《Title》Artist`` are in common use.

    A spaced separator that appears *before* the first bracket wins instead, because then the
    brackets hold something else — typically a lyric quote:
    ``"單依純 - 好久不見 (Live)『你會不會忽然地出現...』"``.
    """
    text = clean_title(raw)

    groups = _bracket_groups(text, _TITLE_BRACKETS, max_len=_ARTIST_MAX_CHARS)
    if not groups:
        groups = _bracket_groups(text, _SECONDARY_TITLE_BRACKETS, max_len=_SECONDARY_MAX_CHARS)
        # A secondary bracket only names the title when something precedes it.
        groups = [g for g in groups if text[: g[0]].strip(_DANGLING)]
    if not groups:
        return None, None

    start, end, _pair_index = groups[0]
    separator_at = _first_separator_index(text)
    if separator_at is not None and separator_at < start:
        return None, None

    inside = text[start + 1 : end].strip()
    before = text[:start].strip(_DANGLING)
    after = text[end + 1 :].strip(_DANGLING)
    return (before or after or None), inside


def split_artist_title(raw: str) -> tuple[str | None, str]:
    """``"Artist - Title"`` -> ``("Artist", "Title")``; unsplittable input -> ``(None, title)``.

    Splits on the *first* separator only: "Artist - Song - Remix" belongs to one artist.
    Refuses splits that would produce an implausible artist (empty, or absurdly long), which is
    what stops sentence-like titles from being mangled.
    """
    title = clean_title(raw)
    for sep in _SEPARATORS:
        if sep in title:
            left, right = title.split(sep, 1)
            artist = left.strip(" \"'")
            rest = right.strip()
            if artist and rest and len(artist) <= 80:
                return artist, rest

    # CJK uploads usually omit the spaces: "陈奕迅-K歌之王". Only attempted when the title
    # actually contains CJK, so Western names with hyphens are left alone.
    if _CJK.search(title):
        for sep in _TIGHT_SEPARATORS:
            if sep in title:
                left, right = title.split(sep, 1)
                artist = left.strip(_DANGLING).strip(" \"'")
                rest = right.strip(_DANGLING).strip()
                if artist and rest and len(artist) <= 20:
                    return artist, rest
    return None, title


def clean_channel_name(name: str | None) -> str | None:
    """YouTube's auto-generated and label channels carry decoration: ``"Artist - Topic"``,
    ``"ArtistVEVO"``, ``"单依纯Official"``, ``"…官方频道"``."""
    if not name:
        return None
    cleaned = _TOPIC_CHANNEL.sub("", name).strip()
    cleaned = _VEVO_CHANNEL.sub("", cleaned).strip()
    cleaned = _CHANNEL_SUFFIX.sub("", cleaned).strip()
    return cleaned or None


def _first(value: Any) -> str | None:
    """yt-dlp exposes both ``artist`` (str) and ``artists`` (list) depending on extractor."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _release_year(info: dict[str, Any]) -> int | None:
    """Only trust real release dates.

    ``upload_date`` is deliberately *not* used: an upload in 2017 of a 1974 record would tag
    the library with a year that is simply wrong, and a missing year is easier to live with
    (and easier to fix later via MusicBrainz) than a confidently wrong one.
    """
    year = _as_int(info.get("release_year"))
    if year:
        return year
    date = info.get("release_date")
    if isinstance(date, str) and len(date) >= 4 and date[:4].isdigit():
        return int(date[:4])
    return None


def tags_from_info(info: dict[str, Any]) -> TrackTags:
    """Map a yt-dlp info dict to the tags tunebox will write.

    Precedence for artist: real music metadata, then whatever the title structure implies, then
    the channel name. The channel is last because on Chinese-language uploads it is usually the
    record label ("英皇娛樂 eeg music"), which is not what belongs in an artist tag.
    """
    raw_title = (info.get("title") or "").strip()
    track = _first(info.get("track"))
    artist = _first(info.get("artist")) or _first(info.get("artists"))
    if not artist:
        artist = _first(info.get("creator")) or _first(info.get("creators"))

    title_from_structure: str | None = None
    artist_from_structure: str | None = None
    if track:
        # Music metadata present: believe it. Only "(2009 Remaster)"-style noise is cleaned.
        title_from_structure = clean_title(track)
    else:
        bracket_artist, bracket_title = split_bracketed_title(raw_title)
        if bracket_title:
            title_from_structure = clean_title(bracket_title)
            artist_from_structure = clean_title(bracket_artist) if bracket_artist else None
        else:
            artist_from_structure, title_from_structure = split_artist_title(raw_title)

    channel = clean_channel_name(info.get("uploader") or info.get("channel"))
    derived = clean_derived_artist(artist_from_structure)
    if not plausible_artist(derived):
        derived = None
    artist = artist or derived or leading_hashtag(raw_title) or channel or "Unknown Artist"
    title = title_from_structure or raw_title or (info.get("id") or "Unknown Title")

    album = _first(info.get("album"))
    album_artist = _first(info.get("album_artist")) or (artist if album else None)

    video_id = info.get("id")
    return TrackTags(
        title=title,
        artist=artist,
        album=album,
        album_artist=album_artist,
        year=_release_year(info),
        track_number=_as_int(info.get("track_number")),
        duration=float(info["duration"]) if _as_int(info.get("duration")) is not None else None,
        source=SOURCE_NAME,
        source_id=video_id,
        source_url=info.get("webpage_url") or (f"https://www.youtube.com/watch?v={video_id}" if video_id else None),
    )


def _thumbnail_url(entry: dict[str, Any]) -> str | None:
    thumb = entry.get("thumbnail")
    if isinstance(thumb, str) and thumb:
        return thumb
    thumbs = entry.get("thumbnails")
    if isinstance(thumbs, list):
        for candidate in reversed(thumbs):  # yt-dlp orders worst -> best
            if isinstance(candidate, dict) and candidate.get("url"):
                return str(candidate["url"])
    return None


def search_results_from_playlist(payload: dict[str, Any]) -> list[SearchResult]:
    """Map ``--flat-playlist --dump-single-json`` output to search results.

    Entries without a usable id are skipped rather than raising: one unavailable video must not
    cost the user the whole result list.
    """
    entries: Iterable[Any] = payload.get("entries") or []
    results: list[SearchResult] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        video_id = entry.get("id")
        if not isinstance(video_id, str) or not video_id:
            continue
        duration = entry.get("duration")
        results.append(
            SearchResult(
                id=video_id,
                title=(entry.get("title") or "").strip() or video_id,
                url=entry.get("url") or f"https://www.youtube.com/watch?v={video_id}",
                uploader=clean_channel_name(entry.get("uploader") or entry.get("channel")),
                duration=float(duration) if isinstance(duration, (int, float)) else None,
                thumbnail=_thumbnail_url(entry),
            )
        )
    return results
