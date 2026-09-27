"""Serving the one web page — PLAN.md Phase 4.

    Single page served by the API: search, ingest progress, now-playing, transport controls.
    Installable to the home screen.

Four decisions here are load-bearing.

**The shell is served from an allowlist, not a static-files mount.** ``StaticFiles`` would be one
line, and it would also mean the set of things this service hands out unauthenticated is "whatever
is in a directory" — a property that is true today and enforced by nothing. Every file below is
named, with its content type and its cache policy decided by us. A request for anything else is a
404 from a route that never touched the filesystem, so there is no traversal to get wrong.

**The shell is the one thing exempt from the token, and it contains no data.** A browser cannot put
a header on the document request it makes on your behalf, so a page that needed the token to load
could never load. What is exempted is therefore exactly the bytes that are already in this
repository: markup, CSS, script, manifest, icons. The token gate is the page's first action, every
route that can read the library or touch the player still requires it, and
:mod:`tunebox.api.auth` describes the exemption from the middleware's side. Setting
``TUNEBOX_UI=0`` removes the routes and the exemption together, for a host where the only client is
``curl``.

**Security headers on every shell response, and a CSP strict enough to be worth having.**
``default-src 'none'`` with ``script-src 'self'`` is only possible because the page has no inline
script and loads nothing from a third party — no CDN, no font, not even YouTube's thumbnails.
``form-action 'none'`` is the one that earns its place twice: the forms are submitted by JavaScript,
so blocking a native submission costs nothing, and it means a broken script cannot put the shared
token into a URL — and therefore into uvicorn's access log — by falling back to a GET of the
enclosing page.

**Templated once, at startup.** Four placeholders are substituted — the version (displayed in the
footer), the **build id** (which busts the phone's cache and names the service worker's cache) and
two facts about where OwnTone's own web UI can be found. Doing it when the app is built rather than
per request means the shell costs one dictionary lookup to serve, which is what a Raspberry Pi
should spend on a static file.

**The build id is a digest of the assets, not the version** — see :func:`build_id`. The version is
what a release is called; the build id is what the *bytes* are, and the phone's cache has to be keyed
on the bytes. Keying it on `__version__` was a bug found in use: an upgrade that edited ``app.js``
without releasing a new version left every browser that had loaded the page in the previous hour
running the old script, because ``/ui/app.js?v=0.1.0`` was still the same URL and still inside its
``max-age``. The document itself is never cached (``no-cache`` below), so a new build id always
reaches the phone on the next load, and it then asks for a URL it cannot have cached.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response, status

from .. import __version__
from ..config import Config

log = logging.getLogger(__name__)

#: Where the hand-written assets live. Shipped inside the wheel: hatchling includes every file under
#: ``src/tunebox``, so ``pip install tunebox`` carries the UI with it.
WEB_ROOT = Path(__file__).resolve().parent.parent / "web"

#: Served at the site root rather than under ``/ui/``, each for a concrete reason:
#:
#: * ``/`` is what somebody types into a phone, and what the manifest's ``start_url`` points at;
#: * ``/sw.js`` **must** be at the root — a service worker's default scope is its own directory, so
#:   one served from ``/ui/`` could not control the page at ``/`` (the alternative, a
#:   ``Service-Worker-Allowed`` header, is a second thing to get right for no gain);
#: * ``/manifest.webmanifest`` is conventional and is what the ``<link rel=manifest>`` points at.
ROOT_ASSETS = {
    "/": "index.html",
    "/sw.js": "sw.js",
    "/manifest.webmanifest": "manifest.webmanifest",
}

#: Everything else, under one prefix so the auth exemption can be one prefix.
UI_PREFIX = "/ui/"
UI_ASSETS = (
    "app.css",
    "app.js",
    "icon.svg",
    "icon-180.png",
    "icon-192.png",
    "icon-512.png",
    "icon-maskable.png",
)

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}

#: File types the version placeholders are substituted into. A PNG must never be string-processed.
_TEMPLATED = {".html", ".js", ".webmanifest", ".css"}

#: ``no-cache`` means "revalidate before use", not "do not store". The document, the manifest and
#: especially the service worker have stable URLs, so a phone that cached them for an hour would
#: keep running last version's app after an upgrade.
_REVALIDATE = "no-cache"
#: Assets under ``/ui/`` are requested with ``?v=<version>`` by the shell, so an upgrade changes
#: their URL. An hour is a compromise for the two that are not versioned (the icons): long enough to
#: be free on a reload, short enough that a redrawn icon appears the same day.
_CACHE_UI = "public, max-age=3600"

_SECURITY_HEADERS = {
    "Content-Security-Policy": "; ".join(
        (
            "default-src 'none'",
            "script-src 'self'",
            "style-src 'self'",
            "img-src 'self'",
            "connect-src 'self'",
            "manifest-src 'self'",
            "base-uri 'none'",
            # The forms are submitted by JavaScript. Blocking a native submission is free, and it is
            # what stops a broken script from ever putting the token in a query string.
            "form-action 'none'",
            "frame-ancestors 'none'",
        )
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # Belt and braces with frame-ancestors, for a browser old enough to lack CSP level 2.
    "X-Frame-Options": "DENY",
}


@dataclass(frozen=True)
class Asset:
    """One file, read and templated once, ready to become a response."""

    body: bytes
    content_type: str
    cache_control: str

    def response(self) -> Response:
        return Response(
            content=self.body,
            media_type=self.content_type,
            headers={"Cache-Control": self.cache_control, **_SECURITY_HEADERS},
        )


def render(name: str, config: Config, *, cache_control: str) -> Asset:
    """Read ``name`` from :data:`WEB_ROOT`, substituting the three shell placeholders."""
    path = WEB_ROOT / name
    suffix = path.suffix.lower()
    raw = path.read_bytes()
    if suffix in _TEMPLATED:
        text = raw.decode("utf-8")
        for placeholder, value in _substitutions(config).items():
            text = text.replace(placeholder, value)
        raw = text.encode("utf-8")
    return Asset(
        body=raw,
        content_type=_CONTENT_TYPES.get(suffix, "application/octet-stream"),
        cache_control=cache_control,
    )


def _substitutions(config: Config) -> dict[str, str]:
    return {
        "__TUNEBOX_VERSION__": __version__,
        # What the phone's cache is keyed on. See build_id, and the module docstring for the bug.
        "__TUNEBOX_BUILD__": build_id(),
        # Empty is the normal answer and not a missing setting: the player is on loopback, so the
        # only address that could work for a phone is the one the phone used to reach *us*, which
        # only the browser knows. See Config.owntone_ui_link.
        "__OWNTONE_URL__": config.owntone_ui_link,
        "__PLAYER_PORT__": str(config.player_port),
    }


def build_id() -> str:
    """Short digest of the shell's own bytes, plus the version. The cache key for ``/ui/*``.

    Content-addressed rather than version-addressed, because the thing a browser must not keep is a
    *file* it no longer matches — and `__version__` does not change when a file does. Two properties
    make it usable as a cache key:

    * **Deterministic**, so two workers, a restart and an upgraded copy of the same code all serve the
      same URL. Nothing per-process (a start timestamp, ``os.urandom``) would be, and a build id that
      changed on every restart would re-download the shell for no reason.
    * **Computed from the raw files**, before templating, so substituting it into them cannot change
      it. The digest covers every shell asset rather than only the two that carry it, which means a
      redrawn icon also bumps it — cheap, and the alternative is deciding which changes "count".

    Eight hex characters: this keys a browser cache on a home network, not a content-addressed store,
    and the failure mode of a collision is one stale reload rather than a wrong file.
    """
    digest = hashlib.sha256(__version__.encode("utf-8"))
    for name in sorted({*ROOT_ASSETS.values(), *UI_ASSETS}):
        digest.update(name.encode("utf-8"))
        digest.update((WEB_ROOT / name).read_bytes())
    return digest.hexdigest()[:8]


def load_assets(config: Config) -> dict[str, Asset]:
    """Every servable path -> its bytes. Missing files fail here, at startup, not on a phone."""
    assets: dict[str, Asset] = {}
    for route, filename in ROOT_ASSETS.items():
        assets[route] = render(filename, config, cache_control=_REVALIDATE)
    for filename in UI_ASSETS:
        assets[f"{UI_PREFIX}{filename}"] = render(filename, config, cache_control=_CACHE_UI)
    return assets


def create_ui_router(config: Config) -> APIRouter:
    """The shell's routes, with every file already read and templated.

    Reading at build time rather than per request is also a small correctness win: a wheel missing
    its assets fails ``create_app`` with a ``FileNotFoundError`` naming the file, instead of serving
    a 500 to somebody standing in a kitchen.
    """
    assets = load_assets(config)
    router = APIRouter(tags=["ui"])

    @router.get(
        "/",
        include_in_schema=False,
        response_class=Response,
        summary="The tunebox remote (no token required: it carries no data)",
    )
    def index() -> Response:
        return assets["/"].response()

    @router.get("/sw.js", include_in_schema=False, response_class=Response)
    def service_worker() -> Response:
        return assets["/sw.js"].response()

    @router.get("/manifest.webmanifest", include_in_schema=False, response_class=Response)
    def manifest() -> Response:
        return assets["/manifest.webmanifest"].response()

    @router.get("/ui/{name}", include_in_schema=False, response_class=Response)
    def asset(name: str) -> Response:
        # ``{name}`` cannot contain a slash (that needs the ``:path`` converter), and the lookup is a
        # dictionary of names decided above — so this is an allowlist twice over, and neither check
        # touches the filesystem.
        found = assets.get(f"{UI_PREFIX}{name}")
        if found is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such asset")
        return found.response()

    log.debug("serving the web remote: %s", ", ".join(sorted(assets)))
    return router


def exempt_paths() -> tuple[str, ...]:
    """Exact paths the token middleware must let through — see the module docstring."""
    return tuple(ROOT_ASSETS)


def exempt_prefixes() -> tuple[str, ...]:
    return (UI_PREFIX,)
