"""The application factory.

A factory rather than a module-level ``app``: the tests build one against a temporary music root,
a fake source and an in-memory queue, and nothing in this package reaches for global state to do
its job. ``tunebox serve`` is then a thin caller (see :mod:`tunebox.cli`).

Three things happen here that are not "wiring":

* **the token middleware is installed before anything else**, so it also covers ``/docs``,
  ``/openapi.json`` and 404s — see :mod:`tunebox.api.auth` for why that is middleware and not a
  dependency;
* **the exception handlers translate the one exception tree** in :mod:`tunebox.errors` into status
  codes, so no handler needs a try/except and a yt-dlp failure never reads as a bug in tunebox;
* **the worker's lifetime is the app's lifetime** — started on startup, drained on shutdown.
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
from ..core.ports import MusicSource
from ..errors import ConfigError, InvalidURLError, SourceError, SourceTimeout, TuneboxError
from ..jobs.store import JobStore
from ..jobs.worker import Worker
from ..sources.ytdlp import YtDlpSource
from ..tagging import write_tags
from .auth import TokenAuthMiddleware
from .routes import router

log = logging.getLogger(__name__)

DESCRIPTION = (
    "Home-network-only ingest API. Every route requires the shared token in an "
    "`X-Tunebox-Token` header (or `Authorization: Bearer`)."
)


def create_app(
    config: Config | None = None,
    *,
    store: JobStore | None = None,
    source: MusicSource | None = None,
    tag_writer=write_tags,
    run_worker: bool = True,
) -> FastAPI:
    """Build the API. ``config.validate_for_serve()`` decides whether this is allowed to exist."""
    config = config or Config.from_env()
    token = config.validate_for_serve()

    store = store or JobStore(config.database_path)
    source = source or YtDlpSource(config)
    worker = Worker(store=store, config=config, source=source, tag_writer=tag_writer)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if run_worker:
            worker.start()
        try:
            yield
        finally:
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

    app.add_middleware(TokenAuthMiddleware, token=token)
    _install_error_handlers(app)
    app.include_router(router)
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
