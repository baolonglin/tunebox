"""URL validation.

PLAN.md 3.2: *"Validate URLs against an allowlist before handing them to yt-dlp."* yt-dlp
supports hundreds of extractors; this project wants exactly one family of them. Anything else
— including ``file://``, ``javascript:`` and generic-extractor HTTP pages — is refused here,
before any subprocess exists.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from ..errors import InvalidURLError

ALLOWED_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }
)

#: YouTube ids are 11 chars of URL-safe base64. Being strict here means the id is safe to use
#: in a filename, a log line or a URL without further escaping.
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def is_video_id(value: str) -> bool:
    return bool(VIDEO_ID.match(value))


def canonical_video_url(raw: str) -> tuple[str, str]:
    """Validate ``raw`` and return ``(video_id, canonical_watch_url)``.

    Accepts a bare 11-character video id as a convenience, plus ``watch?v=``, ``youtu.be/``,
    ``/shorts/`` and ``/embed/`` forms. Query strings are dropped: a canonical URL keeps
    playlist and timestamp parameters — which change what yt-dlp downloads — out of the picture.
    """
    value = (raw or "").strip()
    if not value:
        raise InvalidURLError("empty URL")
    if any(c in value for c in "\r\n\t"):
        raise InvalidURLError("URL contains control characters")

    if is_video_id(value):
        return value, f"https://www.youtube.com/watch?v={value}"

    parsed = urlparse(value if "//" in value else f"https://{value}")
    if parsed.scheme not in ("http", "https"):
        raise InvalidURLError(f"unsupported URL scheme: {parsed.scheme or '(none)'}")

    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise InvalidURLError(
            f"host not allowed: {host or '(none)'} "
            f"(allowed: {', '.join(sorted(ALLOWED_HOSTS))})"
        )

    video_id = _extract_id(host, parsed.path, parsed.query)
    if not video_id:
        raise InvalidURLError(f"no video id found in URL: {value}")
    return video_id, f"https://www.youtube.com/watch?v={video_id}"


def _extract_id(host: str, path: str, query: str) -> str | None:
    if host in ("youtu.be", "www.youtu.be"):
        candidate = path.lstrip("/").split("/")[0]
        return candidate if is_video_id(candidate) else None

    if path in ("/watch", "/watch/"):
        values = parse_qs(query).get("v", [])
        return values[0] if values and is_video_id(values[0]) else None

    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[0] in ("shorts", "embed", "v", "live"):
        return parts[1] if is_video_id(parts[1]) else None
    return None
