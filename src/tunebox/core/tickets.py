"""Capability tickets: short-lived, path-scoped, signed URLs for one audio file.

This exists because of one hard browser constraint. ``<audio src="...">`` is a request the
*browser* makes, and a page cannot attach a header to it — so the shared token that guards every
other route (:mod:`tunebox.api.auth`) cannot be used for the one request that carries the music.
The three ways out of that are a cookie, the token in a query string, or a capability URL, and
only the third is compatible with what this project already decided:

* **A cookie is out.** The remote sets none and says so (``credentials: "omit"`` in ``app.js``),
  and a cookie would be attached by the browser to every request a third-party page could cause,
  which is the one property the header token was chosen *for*.
* **The token in a query string is out.** PLAN.md 3.2 wants uvicorn's access log to be the audit
  trail on a network full of devices nobody is proud of, and that is why the remote carries the
  token in a URL *fragment* and strips it on arrival. Putting the master credential in
  ``?token=`` would write it into that log once per track, forever.
* **A capability URL is what is left**, and it is what the auth middleware's ``exempt_prefixes``
  was reserved for in Phase 3 — "the capability URL a Cast-pulled ``/media/{id}`` would need".
  The Cast path never needed it; the phone-as-speaker path does.

What makes the capability safe enough to be in a log line is that it is *not* the token:

* **It names one file.** The relative library path is inside the signed payload, so a ticket for
  one track cannot be re-pointed at another — and the path is still re-resolved against the music
  root by :func:`tunebox.library.resolve_track_path` on the way out, so a ticket is not trusted to
  have been minted for something sane.
* **It expires** (``TUNEBOX_MEDIA_TICKET_TTL``, six hours by default — long enough for an evening's
  queue, short enough that a URL in a log is not a key to the library).
* **It is read-only.** There is nothing a ticket can do but stream bytes of one audio file.
* **It dies with the token.** The signing key is derived from ``TUNEBOX_TOKEN``, so rotating the
  token invalidates every ticket ever minted, with no state to clear.

No state is kept, deliberately. A table of outstanding tickets would be a second database (PLAN.md
3.1 has opinions about that), would need pruning, and would not survive the restart that an upgrade
performs mid-song. A signature plus an expiry needs none of it.

Nothing here touches the filesystem or the network, which is why it is in ``core``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

#: Bumped if the payload shape or the signature ever changes. It is mixed into the key derivation
#: *and* into the signed bytes, so a v1 ticket cannot be made to verify as a v2 one.
TICKET_VERSION = "v1"

#: Domain separation for the derived key: the signing key is not the token, so a signature can
#: never be mistaken for (or replayed as) a credential for anything else.
_KEY_INFO = b"tunebox media ticket " + TICKET_VERSION.encode("ascii")

#: What gets signed, prefixed. A NUL separator cannot occur in base64url, so payload and prefix
#: can never be confused with each other however the payload is chosen.
_SIGN_PREFIX = b"tunebox-media-" + TICKET_VERSION.encode("ascii") + b"\x00"

#: Truncated to 128 bits. This authenticates a read-only URL for one file on a home network for a
#: few hours; 16 bytes is beyond forgeable and keeps the URL short enough to read in a log.
_SIGNATURE_BYTES = 16

#: A ticket is ``payload.signature``. Both halves are base64url without padding, so the whole
#: thing is one URL path segment with nothing to escape.
_SEPARATOR = "."

#: Expiries are rounded *up* to a multiple of this, in seconds.
#:
#: Which makes a ticket for the same track **stable for a minute at a time**, and that is the point:
#: the page mints a fresh ticket every time it loads a track, and a URL that changed on every call
#: would mean the browser's HTTP cache never recognised a file it had just played — so replaying the
#: previous song, or coming back to a track after a skip, would be a re-download. Rounded *up* rather
#: than down, so quantising can only ever lengthen a ticket's life by under a minute and never cut
#: the configured TTL short.
_EXPIRY_BUCKET = 60


class TicketError(ValueError):
    """A ticket that cannot be trusted: malformed, wrongly signed, or expired.

    One class for all three on purpose. The route answers the same ``403`` for each, because
    telling a caller *which* of "you made that up" and "that was valid an hour ago" applies is
    telling it something only a real ticket-holder could otherwise know.

    A :class:`ValueError` rather than a :class:`~tunebox.errors.TuneboxError`: nothing in tunebox
    failed. The caller presented a bad credential, which is an ordinary thing for a route at the
    edge to be told.
    """


@dataclass(frozen=True)
class Ticket:
    """A verified ticket: which library path it names, and when it stops being valid."""

    path: str
    expires_at: int

    @property
    def expires_in(self) -> int:
        """Whole seconds left, floored at zero — what a ``max-age`` can be built from."""
        return max(0, self.expires_at - int(time.time()))


def signing_key(token: str) -> bytes:
    """Derive the ticket key from the API token.

    Derived rather than used directly so that a leaked signature is not a step towards the token,
    and so that the two have independent lengths and alphabets. Rotating ``TUNEBOX_TOKEN``
    invalidates every outstanding ticket as a side effect, which is the behaviour anyone rotating a
    token wants and would otherwise have to remember to ask for.
    """
    if not token:
        # Signing with an empty key would produce tickets anybody could mint. The same refusal as
        # TokenAuthMiddleware's, for the same reason.
        raise ValueError("a media ticket key cannot be derived from an empty token")
    return hmac.new(token.encode("utf-8"), _KEY_INFO, hashlib.sha256).digest()


def mint(path: str, *, key: bytes, ttl: float, now: float | None = None) -> str:
    """Sign a ticket for ``path``, valid for ``ttl`` seconds.

    ``path`` is a library path relative to the music root — the same string ``GET /library`` hands
    out. It is *not* validated here: containment is :func:`tunebox.library.resolve_track_path`'s
    one job and it is applied again when the ticket is spent, so this module never has to be
    trusted to have been called correctly.
    """
    if ttl <= 0:
        raise ValueError(f"media ticket ttl must be positive, got {ttl}")
    moment = time.time() if now is None else now
    expires_at = -(-int(moment + ttl) // _EXPIRY_BUCKET) * _EXPIRY_BUCKET
    payload = _encode({"p": path, "e": expires_at})
    return f"{payload}{_SEPARATOR}{_sign(payload, key)}"


def verify(ticket: str, *, key: bytes, now: float | None = None) -> Ticket:
    """Check a ticket and return what it names. Raises :class:`TicketError` if it is not good.

    The order matters: the signature is checked **before** the payload is believed, so an expiry
    somebody edited is rejected as a forgery rather than read as a date.
    """
    raw = (ticket or "").strip()
    if not raw:
        raise TicketError("empty media ticket")
    payload, separator, signature = raw.partition(_SEPARATOR)
    if not separator or not payload or not signature:
        raise TicketError("malformed media ticket")
    expected = _sign(payload, key)
    # compare_digest on the ASCII signatures: both are base64url, so both are ASCII by
    # construction, and comparing the encoded form avoids decoding attacker-controlled bytes.
    if not hmac.compare_digest(signature, expected):
        raise TicketError("media ticket signature does not match")
    try:
        claims = _decode(payload)
    except (ValueError, UnicodeDecodeError) as exc:  # pragma: no cover - signed, so ours
        # Only reachable if we minted something unparseable, since the signature already matched.
        raise TicketError("media ticket payload is not readable") from exc
    path = claims.get("p")
    expires_at = claims.get("e")
    if not isinstance(path, str) or not path or not isinstance(expires_at, int):
        raise TicketError("media ticket is missing its path or expiry")
    moment = time.time() if now is None else now
    if expires_at <= moment:
        raise TicketError("media ticket has expired")
    return Ticket(path=path, expires_at=expires_at)


def _sign(payload: str, key: bytes) -> str:
    digest = hmac.new(key, _SIGN_PREFIX + payload.encode("ascii"), hashlib.sha256).digest()
    return _b64(digest[:_SIGNATURE_BYTES])


def _encode(claims: dict[str, object]) -> str:
    # Compact and key-sorted, so the same claims always produce the same bytes. Together with
    # _EXPIRY_BUCKET that is what makes a ticket for one track stable rather than new on every call.
    text = json.dumps(claims, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    return _b64(text.encode("utf-8"))


def _decode(payload: str) -> dict[str, object]:
    claims = json.loads(_unb64(payload).decode("utf-8"))
    if not isinstance(claims, dict):
        raise ValueError("ticket payload is not an object")
    return claims


def _b64(raw: bytes) -> str:
    """base64url with the padding stripped: URL-safe, and no ``=`` to escape in a path segment."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except Exception as exc:  # binascii.Error, and anything else the decoder decides to raise
        raise ValueError("not base64url") from exc
