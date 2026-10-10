"""FastAPI entrypoint for the nomusic backend.

Run directly with ``python backend/server.py`` (no uvicorn CLI needed).

``create_app`` only assembles routes and middleware. Its lifespan creates and
owns the engine, cache, registry and background work, and joins normal work
before releasing them on shutdown. Importing this module starts no services.
The endpoints live in :mod:`nomusic.routes` (system / jobs / media):

  GET  /healthz
  GET  /readyz
  GET  /capabilities
  POST /process              {url, model?, keep_stems?, client_id?} -> {job_id, ...}
  POST /process/{job_id}/prioritize {from_chunk} -> {applied}
  POST /process/{job_id}/interest {client_id, lease_seconds?} -> lease
  DELETE /process/{job_id}/interest?client_id=... -> release that client lease
  GET  /status/{job_id}      -> JobStatus
  GET  /events/{job_id}      -> text/event-stream (SSE status updates)
  GET  /chunk/{job_id}/{idx} -> audio/ogg (425 if not yet ready)
  GET  /audio/{job_id}       -> audio/ogg (concatenated track)
  POST /exports              -> asynchronous export preparation
  GET  /exports/{export_id}  -> export status
  DELETE /exports/{export_id} -> cancel an export
  GET  /exports/{export_id}/download -> prepared artifact
  GET  /cache                -> cache stats
  POST /cache/clear          -> {deleted_bytes}
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from nomusic.auth import AuthStore
from nomusic.config import SETTINGS, Settings
from nomusic.engines import get_engine
from nomusic.routes.jobs import router as jobs_router
from nomusic.routes.exports import router as exports_router
from nomusic.routes.media import router as media_router
from nomusic.routes.system import router as system_router
from nomusic.services import Services

# Directory watched by the optional development reloader. All entry points use
# the same package import so there is only one server module identity.
_BACKEND_DIR = Path(__file__).resolve().parent

log = logging.getLogger("nomusic.server")


def _raise_open_file_limit() -> None:
    """Lift this process's open-file soft limit toward its hard limit.

    The MP3/MP4 export opens one ffmpeg ``-i`` input per chunk (pipeline/export.py),
    so a long video — a 45-min track is ~285 chunks at the default 9.5 s stride —
    can blow past the macOS default soft limit of 256 file descriptors and fail
    the export with an opaque ffmpeg error. ffmpeg inherits this process's
    rlimits, so raising the limit here covers the spawned subprocess too.
    Best-effort: any failure just leaves the default in place.
    """
    try:
        import resource
    except ImportError:
        return  # non-POSIX (Windows): no rlimits, and unsupported anyway.
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (ValueError, OSError):
        return
    # 8192 is comfortably above any realistic chunk count and well under macOS's
    # per-process ceiling (kern.maxfilesperproc); macOS also rejects an infinite
    # NOFILE, so cap to the concrete hard limit when it isn't unlimited.
    target = 8192
    if hard != resource.RLIM_INFINITY:
        target = min(target, hard)
    if soft >= target:
        return
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        log.info("Raised open-file soft limit %d -> %d", soft, target)
    except (ValueError, OSError):
        log.debug("could not raise open-file limit from %d", soft, exc_info=True)


def _configure_logging() -> None:
    """Set up root logging once, at app/CLI startup rather than import time.

    NOMUSIC_DEBUG=1 raises the level to DEBUG, surfacing the verbose diagnostics
    (e.g. the progressive download/gate logs) that are otherwise hidden.
    """
    debug = os.environ.get("NOMUSIC_DEBUG", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    _raise_open_file_limit()
    settings: Settings = app.state.settings
    auth_store: AuthStore = app.state.auth_store
    auth_store.validate()
    # The CLI selects process ownership before startup, including bind failures
    # that make Uvicorn enter lifespan shutdown without its normal drain hook.
    prepare_shutdown = getattr(app.state, "prepare_process_shutdown", None)
    services = Services(
        settings, engine_factory=get_engine,
        wait_for_warmup=prepare_shutdown is None,
    )
    app.state.services = services
    try:
        services.start(asyncio.get_running_loop())
        app.state.engine = services.engine
        app.state.cache = services.cache
        app.state.registry = services.registry
        app.state.exports = services.exports
        yield
    finally:
        if prepare_shutdown is not None:
            prepare_shutdown(services)
        services.begin_shutdown()
        # Joining synchronously here would block final worker-to-SSE callbacks.
        await asyncio.to_thread(services.shutdown)
        for name in (
            "engine", "cache", "registry", "exports", "services"
        ):
            if hasattr(app.state, name):
                delattr(app.state, name)


def create_app(
    *, settings: Settings | None = None, auth_store: AuthStore | None = None
) -> FastAPI:
    configured = SETTINGS if settings is None else settings
    app = FastAPI(title="nomusic", version="0.2.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = configured
    app.state.auth_store = auth_store or AuthStore(configured.auth_file)

    # Preserve the existing local browser transport. Loopback and CORS do not
    # authenticate callers; remote deployment requires separate authorization
    # and origin restrictions.
    #
    # ``allow_private_network=True`` opts into Chrome's Private Network
    # Access flow: a fetch from a public origin (youtube.com) to a private
    # IP (127.0.0.1) gets an extra preflight with
    # ``Access-Control-Request-Private-Network: true`` and the response
    # must echo ``Access-Control-Allow-Private-Network: true``. Without it
    # Chrome silently drops the request even when regular CORS is correct.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(configured.allow_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        allow_private_network=True,
    )

    app.include_router(system_router)
    app.include_router(jobs_router)
    app.include_router(media_router)
    app.include_router(exports_router)
    return app


app = create_app()


def main() -> None:
    _configure_logging()

    from nomusic.serving import serve

    # Dev convenience: NOMUSIC_RELOAD=1 watches the imported package and restarts on
    # save, so you don't re-run the server by hand on every change. Off by
    # default (the reloader spawns a watcher subprocess + re-imports the app,
    # which reloads the model — fine for dev, wasteful for normal use).
    reload = os.environ.get("NOMUSIC_RELOAD", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    log.info(
        "Starting nomusic backend on http://%s:%d (engine=%s%s)",
        SETTINGS.host,
        SETTINGS.port,
        SETTINGS.engine_name,
        " · auto-reload" if reload else "",
    )
    if reload:
        log.warning(
            "Auto-reload watches %s; use an editable install to watch checkout edits "
            "(see docs/installation.md)", _BACKEND_DIR,
        )
    serve(
        "nomusic.server:app",
        host=SETTINGS.host,
        port=SETTINGS.port,
        reload=reload,
        reload_dirs=[str(_BACKEND_DIR)] if reload else None,
    )


if __name__ == "__main__":
    main()
