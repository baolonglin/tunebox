"""Shared-token authentication.

PLAN.md 3.2: *"Put a password on the ingest API even though it is LAN-only — a single shared
token in a header is enough at this scale, checked on **every** route."* The emphasis is the
reason this is ASGI middleware rather than a FastAPI dependency: a dependency has to be attached
to each route (and to the docs routes, which FastAPI mounts outside the router), so "every route"
would be a habit. Middleware makes it a property — including 404s, ``/docs`` and anything a later
phase adds without thinking about auth.

Everything is compared as **bytes**, never as decoded text. A header carrying 0xFF is not valid
UTF-8 and is not ASCII, and ``secrets.compare_digest`` raises ``TypeError`` on a non-ASCII
``str`` — which, in middleware that sits outside the exception handlers, would turn an
unauthenticated request into a 500. Comparing the raw bytes makes a garbage token simply wrong.

Rejections log the client IP, which is the thing worth knowing about on a home network (3.2
again); the token itself is never logged.

Phase 3 will need one exception: Cast pulls media over plain HTTP and cannot send headers, so
``/media/{id}`` will need a capability URL. ``exempt_prefixes`` is where that goes — deliberately
empty here, so the exception has to be added on purpose.
"""

from __future__ import annotations

import logging
import secrets
from typing import Iterable, Sequence

from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)

#: Primary header. ``Authorization: Bearer <token>`` is accepted too, because curl and every
#: HTTP client already know how to send it.
TOKEN_HEADER = b"x-tunebox-token"
_BEARER_PREFIX = b"bearer "

_UNAUTHORIZED_BODY = b'{"error":"unauthorized"}'

#: Policy-violation close code: the handshake was well-formed, the credentials were not.
_WS_UNAUTHORIZED_CODE = 1008


class TokenAuthMiddleware:
    """Reject every request that does not carry the shared token."""

    def __init__(
        self,
        app: ASGIApp,
        token: str,
        *,
        exempt_prefixes: Sequence[str] = (),
    ) -> None:
        if not token:
            # Failing here rather than allowing everything: a blank token must never mean
            # "no authentication" on a service that runs yt-dlp and ffmpeg.
            raise ValueError("TokenAuthMiddleware requires a non-empty token")
        self._app = app
        self._token = token.encode("utf-8")
        self._exempt = tuple(exempt_prefixes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            await self._app(scope, receive, send)  # lifespan and anything else non-request
            return
        if self._is_exempt(scope.get("path", "")):
            await self._app(scope, receive, send)
            return

        presented = _presented_token(scope.get("headers") or [])
        if presented is not None and secrets.compare_digest(presented, self._token):
            await self._app(scope, receive, send)
            return

        client = scope.get("client") or ("?", 0)
        log.warning(
            "rejected unauthenticated %s %s from %s",
            scope.get("method", kind),
            scope.get("path", "?"),
            client[0],
        )
        if kind == "websocket":
            await send({"type": "websocket.close", "code": _WS_UNAUTHORIZED_CODE})
            return
        await _send_unauthorized(send)

    def _is_exempt(self, path: str) -> bool:
        return any(path.startswith(prefix) for prefix in self._exempt)


def _presented_token(headers: Iterable[tuple[bytes, bytes]]) -> bytes | None:
    """The token the caller sent, as raw bytes. ``None`` when no candidate was present."""
    bearer: bytes | None = None
    for raw_name, raw_value in headers:
        name = raw_name.lower()
        if name == TOKEN_HEADER:
            value = raw_value.strip()
            if value:
                return value
        elif name == b"authorization":
            value = raw_value.strip()
            if value[:7].lower() == _BEARER_PREFIX:
                candidate = value[7:].strip()
                if candidate:
                    bearer = candidate
    return bearer


async def _send_unauthorized(send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_UNAUTHORIZED_BODY)).encode()),
                (b"www-authenticate", b"Bearer"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _UNAUTHORIZED_BODY})
