"""URL allowlist. This runs before any subprocess exists, so it is the first line of defence."""

from __future__ import annotations

import pytest

from tunebox.errors import InvalidURLError
from tunebox.sources.urls import canonical_video_url

VIDEO_ID = "iukUMRlaBBE"
CANONICAL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


@pytest.mark.parametrize(
    "raw",
    [
        f"https://www.youtube.com/watch?v={VIDEO_ID}",
        f"http://youtube.com/watch?v={VIDEO_ID}",
        f"https://m.youtube.com/watch?v={VIDEO_ID}&feature=share",
        f"https://music.youtube.com/watch?v={VIDEO_ID}&list=RDAMVM123",
        f"https://youtu.be/{VIDEO_ID}",
        f"https://youtu.be/{VIDEO_ID}?t=42",
        f"https://www.youtube.com/shorts/{VIDEO_ID}",
        f"https://www.youtube.com/embed/{VIDEO_ID}",
        f"www.youtube.com/watch?v={VIDEO_ID}",
        VIDEO_ID,
    ],
)
def test_accepted_forms_all_canonicalise(raw):
    assert canonical_video_url(raw) == (VIDEO_ID, CANONICAL)


def test_playlist_and_timestamp_parameters_are_dropped():
    """Extra parameters change what yt-dlp downloads; the canonical URL must not carry them."""
    _, url = canonical_video_url(f"https://www.youtube.com/watch?v={VIDEO_ID}&list=PL123&index=4")
    assert url == CANONICAL


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "https://evil.example.com/watch?v=" + VIDEO_ID,
        "https://youtube.com.evil.example/watch?v=" + VIDEO_ID,
        "file:///etc/passwd",
        "javascript:alert(1)",
        "ftp://youtube.com/watch?v=" + VIDEO_ID,
        "https://www.youtube.com/watch?v=short",
        "https://www.youtube.com/watch?v=" + "a" * 12,
        "https://www.youtube.com/",
        "https://www.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/@channel",
        f"https://www.youtube.com/watch?v={VIDEO_ID}\nrm -rf /",
        f"https://www.youtube.com/watch?v={VIDEO_ID}; rm -rf /",
    ],
)
def test_rejected(raw):
    with pytest.raises(InvalidURLError):
        canonical_video_url(raw)
