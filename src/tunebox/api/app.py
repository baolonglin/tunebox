"""The application factory.

A factory rather than a module-level ``app``: the tests build one against a temporary music root,
a fake source and an in-memory queue, and nothing in this package reaches for global state to do
its job. ``tunebox serve`` is then a thin caller (see :mod:`tunebox.cli`).

Three things happen here that are not "wiring":

* **the token middleware is installed before anything else**, so it also covers ``/docs``,
  ``/openapi.json`` and 404s — see :mod:`tunebox.api.auth` for why that is middleware and not a
  dependency, and for the one Phase 4 exemption (the app shell, which carries no data);
* **the exception handlers translate the one exception tree** in :mod:`tunebox.errors` into status
  codes, so no handler needs a try/except and a yt-dlp failure never reads as a bug in tunebox;
* **the worker's lifetime is the app's lifetime** — started on startup, drained on shutdown, and
  from Phase 3 the :class:`~tunebox.player.selection.OutputKeeper` alongside it, so that the
  soundbar is re-selected without anybody being asked to.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .. import __version__
from ..config import Config
from ..core.ports import MusicSource, Player
from ..errors import (
    ConfigError,
    InvalidURLError,
    LibraryError,
    OutputNotFound,
    OutputUnreachable,
    PlayerError,
    PlayerUnavailable,
    PlaylistError,
    PlaylistExists,
    PlaylistNotFound,
    SourceError,
    SourceTimeout,
    TuneboxError,
)
from ..jobs.store import JobStore
from ..jobs.worker import Worker
from ..player.owntone import OwnTonePlayer
from ..player.selection import OutputKeeper
from ..sources.ytdlp import YtDlpSource
from ..tagging import write_tags
from . import ui
from .auth import TokenAuthMiddleware
from .playlists import router as playlists_router
from .remote import router as remote_router
from .routes import router

log = logging.getLogger(__name__)

DESCRIPTION = (
    "Home-network-only ingest API and playback remote. Every route requires the shared token in "
    "an `X-Tunebox-Token` header (or `Authorization: Bearer`) — except the web app shell, which "
    "carries no data and asks for the token itself."
)


def create_app(
    config: Config | None = None,
    *,
    store: JobStore | None = None,
    source: MusicSource | None = None,
    tag_writer=write_tags,
    run_worker: bool = True,
    player: Player | None = None,
    keep_output: bool = True,
) -> FastAPI:
    """Build the API. ``config.validate_for_serve()`` decides whether this is allowed to exist."""
    config = config or Config.from_env()
    token = config.validate_for_serve()

    store = store or JobStore(config.database_path)
    source = source or YtDlpSource(config)
    player = player or OwnTonePlayer(config)
    worker = Worker(
        store=store, config=config, source=source, tag_writer=tag_writer, player=player
    )
    # Duck-typed rather than ``isinstance(player, OwnTonePlayer)``: the keeper only needs
    # ``target_output`` and ``select_output``, and a fallback-A player would supply both.
    keeper = OutputKeeper(player=player, config=config) if hasattr(player, "target_output") else None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if run_worker:
            worker.start()
        # The keeper is started even when the player is unreachable: it is what notices OwnTone
        # coming up later, and PLAN.md 3.1 explicitly wants ingest to keep working with no player
        # installed at all.
        if keeper is not None and keep_output:
            keeper.start()
        try:
            yield
        finally:
            if keeper is not None and keep_output:
                keeper.stop()
            if run_worker:
                worker.stop()
            store.close()

    app = FastAPI(
        title="tunebox",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.store = store
    app.state.source = source
    app.state.worker = worker
    app.state.player = player
    app.state.output_keeper = keeper

    # Phase 4. Built before the middleware so that a wheel with no assets in it fails here, with the
    # missing filename, rather than on a phone in a kitchen.
    ui_router = ui.create_ui_router(config) if config.ui_enabled else None
    app.add_middleware(
        TokenAuthMiddleware,
        token=token,
        exempt_paths=ui.exempt_paths() if ui_router is not None else (),
        exempt_prefixes=ui.exempt_prefixes() if ui_router is not None else (),
    )
    _install_error_handlers(app)
    app.include_router(router)
    app.include_router(remote_router)
    app.include_router(playlists_router)
    if ui_router is not None:
        # Last, so a hand-written asset path can never shadow an API route: FastAPI matches in
        # registration order, and the API is the part that must not move.
        app.include_router(ui_router)
    return app


def _install_error_handlers(app: FastAPI) -> None:
    def error(status_code: int, message: str) -> JSONResponse:
        return JSONResponse(status_code=status_code, content={"error": message})

    @app.exception_handler(InvalidURLError)
    async def _invalid_url(request: Request, exc: InvalidURLError) -> JSONResponse:
        # The allowlist rejected it: a client mistake, not a server problem.
        return error(400, str(exc))

    @app.exception_handler(SourceTimeout)
    async def _timeout(request: Request, exc: SourceTimeout) -> JSONResponse:
        return error(504, str(exc))

    @app.exception_handler(SourceError)
    async def _source_failed(request: Request, exc: SourceError) -> JSONResponse:
        # Upstream failed, usually a bot challenge or a YouTube change (PLAN.md 3.3).
        log.warning("source error: %s", exc)
        return error(502, str(exc))

    @app.exception_handler(ConfigError)
    async def _misconfigured(request: Request, exc: ConfigError) -> JSONResponse:
        return error(500, str(exc))

    @app.exception_handler(PlayerUnavailable)
    async def _player_down(request: Request, exc: PlayerUnavailable) -> JSONResponse:
        # The daemon is not answering. Retryable by the client once OwnTone is up, which is what
        # 503 means and 502 does not.
        return error(503, str(exc))

    @app.exception_handler(OutputNotFound)
    async def _no_output(request: Request, exc: OutputNotFound) -> JSONResponse:
        # A genuinely missing resource: an unplugged soundbar is not on the network to be found.
        return error(404, str(exc))

    @app.exception_handler(OutputUnreachable)
    async def _output_asleep(request: Request, exc: OutputUnreachable) -> JSONResponse:
        # The speaker is listed and will not come up — a Cast device in standby, most often. 503
        # rather than 502 because the client's move is to try again, and rather than 404 because the
        # thing it named is right there in the output list.
        log.info("output would not come up: %s", exc)
        return error(503, str(exc))

    @app.exception_handler(PlayerError)
    async def _player_failed(request: Request, exc: PlayerError) -> JSONResponse:
        log.warning("player error: %s", exc)
        return error(502, str(exc))

    @app.exception_handler(PlaylistNotFound)
    async def _no_playlist(request: Request, exc: PlaylistNotFound) -> JSONResponse:
        # It names a resource, and "it is not there" is the whole answer.
        return error(404, str(exc))

    @app.exception_handler(PlaylistExists)
    async def _playlist_exists(request: Request, exc: PlaylistExists) -> JSONResponse:
        # Well-formed request, blocked by state the caller can see with GET /playlists.
        return error(409, str(exc))

    @app.exception_handler(PlaylistError)
    async def _bad_playlist(request: Request, exc: PlaylistError) -> JSONResponse:
        # A name that sanitises to nothing, or a list that is too long: the caller's input.
        return error(400, str(exc))

    @app.exception_handler(LibraryError)
    async def _bad_library_path(request: Request, exc: LibraryError) -> JSONResponse:
        """A request named a file it may not, or one that is not there.

        Registered as a handler rather than left to the ``TuneboxError`` catch-all, which answered
        ``500``: ``LibraryError`` is raised by :func:`tunebox.library.resolve_track_path`, and
        everything that calls it is validating something a *client* sent. The routes that already catch
        it inline keep their own message; this is the backstop for the ones where it can only arrive via
        a path parameter, where a 500 would have reported a refused traversal as a server fault.
        """
        log.info("refused a library path: %s", exc)
        return error(400, str(exc))

    @app.exception_handler(TuneboxError)
    async def _tunebox_error(request: Request, exc: TuneboxError) -> JSONResponse:
        log.warning("%s: %s", type(exc).__name__, exc)
        return error(500, f"{type(exc).__name__}: {exc}")

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
        # Same ``{"error": ...}`` shape as everything else, including the 401 from the middleware.
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.detail},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # jsonable_encoder because pydantic puts exception objects in ``ctx``.
        return JSONResponse(
            status_code=422,
            content={"error": "invalid request", "detail": jsonable_encoder(exc.errors())},
        )
