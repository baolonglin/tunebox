"""Handing the shared token to a phone without typing it.

The token is 43 characters of ``secrets.token_urlsafe(32)``. It is meant to be pasted once per
device and then forgotten — ``web/app.js`` puts it in ``localStorage`` and strips it from the
address bar — but "pasted once" still assumes the link can *reach* the phone, and on a machine
whose only output is a terminal it cannot. A QR code closes that gap with no typing at all and
without weakening anything: the same secret, in the same URL fragment, just encoded for a camera.

**The fragment, not a query string.** :func:`pair_url` builds ``http://host:port/#token=…`` because
a fragment is never sent to the server and so never reaches uvicorn's access log — the audit trail
PLAN.md 3.2 asks for on a network full of devices nobody is proud of. Encoding the *query string*
form into a QR would quietly undo that, which is why the URL is built here rather than by the
caller.

**Why not a short pairing code.** Six digits typed into the phone would be the nicer UX, and it is
what Jellyfin and Plex do. It also requires an endpoint that answers *without* the token, plus
expiry, single-use and rate limiting — a new unauthenticated surface on the one service PLAN.md 3.2
spends its length arguing must not have one. The QR needs no new route at all.

**A QR code is a credential.** It is exactly as sensitive as the link, so :func:`render` is written
to be shown on a terminal and nowhere else; the CLI refuses to print it into a pipe for the same
reason ``deploy/install.sh`` guards its link with ``[ -t 1 ]``.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from urllib.parse import quote

from .config import Config, binds_every_interface
from .errors import TuneboxError

#: Error correction level. ``m`` (~15%) rather than segno's default ``h`` (~30%): the payload is a
#: URL of known length read from a bright screen at 20cm, not a label on a dusty crate, and the
#: lower level keeps the symbol small enough to stay legible in an 80-column terminal.
_ERROR_LEVEL = "m"


class PairingError(TuneboxError):
    """The pairing URL cannot be built, or cannot be shown."""


@dataclass(frozen=True)
class Pairing:
    """What a phone needs, and what to say about it."""

    url: str
    #: Non-fatal things the operator should know — a loopback bind, most importantly.
    warnings: tuple[str, ...] = ()


def pair_url(config: Config) -> Pairing:
    """Build the URL that hands ``config``'s token to a browser.

    The token is validated by :meth:`Config.validate_for_serve`, the same check ``serve`` makes, so
    that a token this command is willing to print is a token the service is willing to start with.
    A pairing link for a service that would refuse to boot is worse than an error.
    """
    token = config.validate_for_serve()
    host = (config.api_host or "").strip()
    warnings: list[str] = []
    if binds_every_interface(host):
        # validate_for_serve permits this when TUNEBOX_ALLOW_ALL_INTERFACES is set, but there is no
        # address here to put in a URL: 0.0.0.0 is not somewhere a phone can go.
        raise PairingError(
            f"{host or '(empty)'} is every interface, not an address a phone can open. "
            "Set TUNEBOX_API_HOST to this host's LAN address"
        )
    if host in ("localhost", "127.0.0.1", "::1"):
        warnings.append(
            f"{host} is this machine only — a phone cannot reach it. Set TUNEBOX_API_HOST to the "
            "LAN address (deploy/install.sh works it out) before pairing a device."
        )
    # quote() and not urlencode(): the token is the whole fragment, not a form field, so a '+' must
    # stay a '+' rather than becoming a space when app.js decodeURIComponent()s it. token_urlsafe
    # never emits one, but the token may have been set by hand.
    return Pairing(
        url=f"http://{host}:{config.api_port}/#token={quote(token, safe='')}",
        warnings=tuple(warnings),
    )


def qr(url: str) -> str:
    """The URL as a QR code drawn in half-height block characters.

    ``compact=True`` packs two rows of modules into each line of text, which is what makes a symbol
    this size fit on a terminal at all — 23 lines rather than 45 for a typical pairing URL.
    """
    try:
        import segno
    except ModuleNotFoundError as cause:  # pragma: no cover - a broken install, not a user error
        raise PairingError(
            "segno is not installed, so no QR code can be drawn. Reinstall tunebox, or use "
            "'tunebox pair --no-qr' and paste the link instead"
        ) from cause

    buffer = io.StringIO()
    segno.make(url, error=_ERROR_LEVEL).terminal(buffer, compact=True)
    return buffer.getvalue().rstrip("\n")


def render(config: Config, *, with_qr: bool = True) -> str:
    """The whole thing a person at a terminal should see."""
    pairing = pair_url(config)
    blocks: list[str] = []
    if with_qr:
        blocks.append(qr(pairing.url))
        blocks.append("Scan this with the phone's camera, then use \"Add to Home Screen\".")
    else:
        blocks.append("Open this on the phone, then use \"Add to Home Screen\":")
    blocks.append(f"    {pairing.url}")
    blocks.append(
        "The token travels in the URL fragment, which is never sent to the server, so it stays out\n"
        "of the access log. The phone stores it after the first load and will not ask again."
    )
    if pairing.warnings:
        blocks.append("\n".join(f"warning: {text}" for text in pairing.warnings))
    return "\n\n".join(blocks)
