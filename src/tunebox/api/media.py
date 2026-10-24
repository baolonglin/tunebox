"""Serving library audio to the *phone* — the second output tunebox has.

Phase 3 made the host own the playback session and kept the phone out of the audio path on purpose
(PLAN.md 2): music must keep playing when the phone sleeps, leaves or dies. That is still the
default and still the right one for the living room. What it does not cover is the case this module
exists for — **somebody wants to listen on the device in their hand**: headphones, a kitchen with
no speaker in it, a soundbar somebody else is using, or a household that has not set up OwnTone at
all.

So the browser becomes a second output, and the only thing the server has to provide for it is the
audio itself. Everything else about that mode — the queue, the transport, repeat, shuffle — lives
in the page, because in that mode the page *is* the player. There is deliberately no server-side
session for it: a mirrored queue would be state that outlived the only thing that could play it,
and two phones would then be fighting over a queue neither one was the output for.

Two routes, and the split between them is the whole security design:

``POST /library/tickets``
    Authenticated like everything else. Takes library paths, resolves each one under the music root,
    and hands back a signed capability URL per track. This is the route that decides what may be
    streamed.

``GET`` / ``HEAD /media/{ticket}``
    **Not** behind the shared token, because ``<audio src>`` is a request the browser makes and a
    page cannot put a header on it. The ticket is the credential: it names one file, it expires, and
    it can do nothing but read. :mod:`tunebox.core.tickets` has the reasoning, including why the
    alternatives (a cookie, ``?token=``) were both rejected.

Note what the ticket is *not* trusted for. The path inside it goes back through
:func:`tunebox.library.resolve_track_path` on every request, so containment under the music root is
enforced where it is enforced for every other path in this project, rather than being assumed
because a signature matched. A bug in the minting route therefore cannot become a file-read
primitive.

Byte ranges are implemented rather than inherited, which is not gold-plating:

* **iOS Safari requires them.** It answers a media element's load by asking for ``bytes=0-1`` first
  and will not play a response that comes back ``200`` with the whole file instead of ``206``.
* **Seeking is a range request.** Without one, dragging a progress bar re-downloads from zero.
* And Starlette's own ``FileResponse`` grew range support at a version this project does not pin,
  so doing it here is also how it works the same on the Pi and on the development Mac.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse

from ..config import Config
from ..core import tickets
from ..errors import LibraryError
from ..library import resolve_track_path
from .schemas import MediaTicketRequest, MediaTicketView, MediaTicketsView

log = logging.getLogger(__name__)

router = APIRouter(tags=["media"])

#: Prefix the token middleware must let through. A real prefix rather than an exact path, because
#: the ticket is in the path — and it is only exempted when local playback is enabled, so a host
#: that does not want this has no unauthenticated route at all.
MEDIA_PREFIX = "/media/"

#: 64 KiB per read. Big enough that a four-minute AAC file is a few dozen syscalls, small enough
#: that a range request for one byte (which is how iOS opens a track) does not allocate a megabyte.
CHUNK_BYTES = 64 * 1024

#: Content types for the extensions :data:`tunebox.library.AUDIO_EXTENSIONS` allows. Spelled out
#: rather than left to :mod:`mimetypes` alone because the answer there depends on the host's
#: ``/etc/mime.types``: macOS and Debian disagree about ``.m4a`` (``audio/m4a`` vs ``audio/mp4``),
#: and Safari refuses to play the former. A media element decides whether it can play a file from
#: this header, so a wrong one is silence rather than an error.
_AUDIO_TYPES = {
    ".m4a": "audio/mp4",
    ".m4b": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".aiff": "audio/aiff",
    ".aif": "audio/aiff",
    ".wma": "audio/x-ms-wma",
}

#: ``private`` so no proxy between the phone and the host may hold a copy, and a few minutes of
#: ``max-age`` so that replaying the track that just finished, or seeking back into it, is free.
#: Short rather than long because the URL is a credential with a lifetime of its own.
_MEDIA_CACHE = "private, max-age=300"

_MEDIA_HEADERS = {
    "Accept-Ranges": "bytes",
    "Cache-Control": _MEDIA_CACHE,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # A capability URL must not travel anywhere it was not put. There is no markup here to frame,
    # but this response is reachable without the token, so it says so for itself.
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; sandbox",
}


# --------------------------------------------------------------------- plumbing


def _config(request: Request) -> Config:
    return request.app.state.config


def _key(request: Request) -> bytes:
    """The ticket signing key, derived once at startup — see :func:`create_media_router`."""
    key = getattr(request.app.state, "media_key", None)
    if key is None:  # pragma: no cover - the router is only mounted with a key installed
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="local playback is not configured",
        )
    return key


def content_type_for(path: Path) -> str:
    """Audio content type for ``path``. Never ``None`` and never ``text/*``."""
    suffix = path.suffix.lower()
    known = _AUDIO_TYPES.get(suffix)
    if known:
        return known
    guessed, _ = mimetypes.guess_type(path.name)
    # Anything unrecognised is served as opaque bytes rather than as whatever the host's mime table
    # guessed: this route is reachable without the token, so it must never label a response in a way
    # a browser would try to *render*.
    return guessed if guessed and guessed.startswith("audio/") else "application/octet-stream"


# ---------------------------------------------------------------------- minting


@router.post(
    "/library/tickets",
    response_model=MediaTicketsView,
    status_code=status.HTTP_201_CREATED,
    summary="Capability URLs for streaming library tracks to this device",
)
def mint_tickets(payload: MediaTicketRequest, request: Request) -> MediaTicketsView:
    """Sign a streaming URL per path, all-or-nothing.

    All-or-nothing for the same reason ``POST /player/queue`` is: the caller is about to build a
    playlist out of the answer, and a response that silently omitted the third track would be a gap
    nobody could see. A path that is not a library track is a ``400`` naming it.

    Under ``/library/`` rather than ``/media/`` on purpose — ``/media/`` is the prefix the token
    middleware exempts, and a *minting* route inside it would be a route that hands out credentials
    to anyone who asks. The two halves of this module must not share a prefix.
    """
    config = _config(request)
    key = _key(request)
    ttl = config.media_ticket_ttl

    resolved: list[tuple[str, Path]] = []
    for entry in payload.paths:
        try:
            absolute = resolve_track_path(config.music_root, entry)
        except LibraryError as exc:
            # The client named something it may not, or something that is not there. Its mistake —
            # and the default handler for LibraryError is a 400, but saying which entry is the point.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
        resolved.append((_relative(config.music_root, absolute), absolute))

    minted = [
        MediaTicketView(
            path=relative,
            url=f"{MEDIA_PREFIX}{tickets.mint(relative, key=key, ttl=ttl)}",
            content_type=content_type_for(absolute),
        )
        for relative, absolute in resolved
    ]
    log.debug("minted %d media ticket(s)", len(minted))
    return MediaTicketsView(tickets=minted, expires_in=int(ttl))


def _relative(root: Path, absolute: Path) -> str:
    """The relative spelling, which is the one that goes in a ticket and comes back out of it."""
    try:
        return absolute.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover - resolve_track_path joins onto root
        return absolute.name


# --------------------------------------------------------------------- spending


@router.api_route(
    "/media/{ticket}",
    methods=["GET", "HEAD"],
    include_in_schema=False,
    response_class=Response,
)
def stream(ticket: str, request: Request) -> Response:
    """Stream one audio file to whoever holds a valid ticket for it.

    ``HEAD`` is answered as well as ``GET``, with the same headers and no body: a media element
    sends one to learn the length and the type before it commits to a download, and a ``405`` there
    makes some browsers give up on the source entirely.
    """
    config = _config(request)
    try:
        claim = tickets.verify(ticket, key=_key(request))
    except tickets.TicketError as exc:
        # One answer for forged, malformed and expired alike (see TicketError). 403 rather than 401
        # because there is no header to re-present: the URL either is the credential or is not.
        log.info("refused a media ticket from %s: %s", _client(request), exc)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid media ticket")

    try:
        # Re-resolved, every time. A signature proves the path came from us; it does not prove the
        # path is still a track under the music root, and that check belongs in one place.
        path = resolve_track_path(config.music_root, claim.path)
    except LibraryError as exc:
        # Deleted since the ticket was minted, most likely — the remote can delete tracks.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    try:
        size = path.stat().st_size
    except OSError as exc:  # pragma: no cover - resolve_track_path just stat'ed it
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="track is gone") from exc

    headers = {
        **_MEDIA_HEADERS,
        # The filename a download would get. ``inline`` because this is for a media element, not a
        # file dialog, and filename* is the RFC 5987 form — CJK track names are the normal case here.
        "Content-Disposition": _disposition(path.name),
    }
    media_type = content_type_for(path)

    requested = request.headers.get("range")
    try:
        span = _parse_range(requested, size)
    except _Unsatisfiable:
        # 416 must carry the real size, or a client cannot work out what it should have asked for.
        return Response(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            headers={**headers, "Content-Range": f"bytes */{size}"},
        )

    if span is None:
        start, end, code = 0, size - 1, status.HTTP_200_OK
    else:
        start, end = span
        code = status.HTTP_206_PARTIAL_CONTENT
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    length = 0 if size == 0 else end - start + 1
    headers["Content-Length"] = str(length)

    if request.method == "HEAD":
        # Same headers, no body. Content-Length still describes what a GET would return.
        return Response(status_code=code, headers=headers, media_type=media_type)

    return StreamingResponse(
        _read(path, start, length),
        status_code=code,
        headers=headers,
        media_type=media_type,
    )


def _client(request: Request) -> str:
    return request.client.host if request.client else "?"


def _disposition(name: str) -> str:
    """``inline`` with both filename forms, so a non-ASCII track name survives the header.

    The plain ``filename=`` is ASCII-only by specification and the fallback for an old client; the
    quoting is deliberately conservative because this string came off a disk anyone in the house can
    write to, and a filename containing a quote or a newline must not be able to inject a header.
    """
    safe = "".join(ch for ch in name if ch.isascii() and ch.isprintable() and ch not in '"\\;')
    fallback = safe or "track"
    return f'inline; filename="{fallback}"; filename*=UTF-8\'\'{quote(name, safe="")}'


class _Unsatisfiable(Exception):
    """The client asked for bytes that are not in the file."""


def _parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Parse a single-range ``Range`` header into inclusive ``(start, end)``.

    ``None`` means "serve the whole thing", which covers three cases that all want the same answer:
    no header at all, a unit that is not ``bytes``, and a *multi*-range request. The last is
    legitimate to decline — RFC 9110 lets a server answer any range request with the full
    representation — and declining is much better than the alternative, since a correct
    multipart/byteranges body is a pile of code that no audio element has ever asked this for.

    An empty file has no satisfiable range, so every range against one is refused.
    """
    raw = (header or "").strip()
    if not raw:
        return None
    unit, separator, spec = raw.partition("=")
    if not separator or unit.strip().lower() != "bytes":
        return None
    if "," in spec:
        return None
    first, separator, last = spec.strip().partition("-")
    if not separator:
        return None
    first, last = first.strip(), last.strip()

    if not first:
        # A suffix range: ``bytes=-500`` is "the last 500 bytes", not "from 0 to 500".
        if not last.isdigit():
            return None
        wanted = int(last)
        if wanted == 0 or size == 0:
            raise _Unsatisfiable
        return max(0, size - wanted), size - 1

    if not first.isdigit():
        return None
    start = int(first)
    if start >= size:
        raise _Unsatisfiable
    if not last:
        return start, size - 1
    if not last.isdigit():
        return None
    # Clamped rather than refused: asking for more than the file has is how a media element says
    # "the rest of it", and RFC 9110 requires the smaller of the two.
    return start, min(int(last), size - 1)


def _read(path: Path, start: int, length: int) -> Iterator[bytes]:
    """Yield ``length`` bytes from ``start``, in chunks, closing the file whatever happens.

    Opened per request, with no shared handle: two phones streaming the same track is the ordinary
    case, and a seek on a shared descriptor would hand each of them the other's position. A client
    that disconnects mid-track raises inside the generator, which is what the ``finally`` is for.
    """
    if length <= 0:
        return
    with path.open("rb") as handle:
        handle.seek(start)
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(CHUNK_BYTES, remaining))
            if not chunk:
                # Truncated under us — an ingest overwriting the file, or a disk going away. Stop
                # rather than spin: the client sees a short response, which is the truth.
                break
            remaining -= len(chunk)
            yield chunk


# ----------------------------------------------------------------------- wiring


def create_media_router(token: str) -> APIRouter:
    """The media routes, with the signing key derived from ``token``.

    A factory so the key is computed once at startup rather than per request, and so that mounting
    this router is the same act as deriving the key — there is no way to install the routes without
    one. :func:`tunebox.api.app.create_app` puts the key on ``app.state``.
    """
    # Derived eagerly so a bad token fails at startup rather than on the first track.
    tickets.signing_key(token)
    return router


def media_key(token: str) -> bytes:
    """The key :func:`create_media_router`'s routes will look for on ``app.state.media_key``."""
    return tickets.signing_key(token)


def exempt_prefixes() -> tuple[str, ...]:
    """What the token middleware must let through for ``<audio src>`` to work at all."""
    return (MEDIA_PREFIX,)


__all__ = [
    "CHUNK_BYTES",
    "MEDIA_PREFIX",
    "content_type_for",
    "create_media_router",
    "exempt_prefixes",
    "media_key",
    "router",
]
