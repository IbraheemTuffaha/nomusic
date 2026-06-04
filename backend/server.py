"""FastAPI entrypoint for the nomusic backend.

Run directly with ``python backend/server.py`` (no uvicorn CLI needed).

Endpoints:
  GET  /healthz
  GET  /capabilities
  POST /process              {url, model?, keep_stems?} -> {job_id, ...}
  GET  /status/{job_id}      -> JobStatus
  GET  /events/{job_id}      -> text/event-stream (SSE status updates)
  GET  /chunk/{job_id}/{idx} -> audio/wav (404 if not yet ready)
  GET  /audio/{job_id}       -> audio/wav (the concatenated full track)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

# Make sibling modules importable when this file is invoked as a script.
_BACKEND_DIR = Path(__file__).resolve().parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from fastapi import FastAPI, HTTPException, Request, Response  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from pydantic import BaseModel, Field, field_validator  # noqa: E402

from config import SETTINGS  # noqa: E402
from engines import get_engine  # noqa: E402
from engines.base import DEMUCS_STEMS  # noqa: E402
from jobs import JobRegistry  # noqa: E402
from pipeline.cache import CHUNK_MEDIA_TYPE, JobCache  # noqa: E402
from pipeline.processor import Processor  # noqa: E402

# NOMUSIC_DEBUG=1 raises the level to DEBUG, surfacing the verbose diagnostics
# (e.g. the progressive download/gate logs) that are otherwise hidden.
_DEBUG = os.environ.get("NOMUSIC_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")
logging.basicConfig(
    level=logging.DEBUG if _DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("nomusic.server")


class ProcessRequest(BaseModel):
    url: str = Field(..., min_length=1)
    model: Optional[str] = None
    keep_stems: Optional[list[str]] = None

    @field_validator("keep_stems")
    @classmethod
    def _validate_stems(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is None:
            return v
        bad = [s for s in v if s not in DEMUCS_STEMS]
        if bad:
            raise ValueError(f"unknown stems: {bad}; allowed: {DEMUCS_STEMS}")
        if not v:
            raise ValueError("keep_stems must not be empty")
        return v


class PrioritizeRequest(BaseModel):
    from_chunk: int = Field(..., ge=0)


def _start_cache_ttl_sweeper(cache: JobCache) -> None:
    """Run an initial sweep, then schedule one every
    ``cache_sweep_interval_seconds``. Skipped entirely when TTL is 0.

    Lives in a daemon thread so it doesn't block server shutdown."""
    if SETTINGS.cache_ttl_days <= 0 or SETTINGS.cache_sweep_interval_seconds <= 0:
        log.info("Cache TTL sweep disabled (ttl_days=%s)", SETTINGS.cache_ttl_days)
        return

    ttl_seconds = SETTINGS.cache_ttl_days * 86400.0

    def _loop() -> None:
        import time

        while True:
            try:
                removed, freed = cache.sweep_older_than(ttl_seconds)
                if removed:
                    log.info(
                        "TTL sweep: removed %d entries, freed %d bytes",
                        removed,
                        freed,
                    )
            except Exception:
                log.exception("TTL sweep failed")
            time.sleep(SETTINGS.cache_sweep_interval_seconds)

    t = threading.Thread(target=_loop, name="nomusic-cache-ttl", daemon=True)
    t.start()


def _start_memory_gc(registry: JobRegistry) -> None:
    """Periodically reclaim in-memory job entries whose disk cache is gone.

    Runs alongside the disk TTL sweeper on its own daemon thread, so the
    in-memory JobStatus map can't grow without bound on a long-lived server.
    Keyed to its own interval so a hosted deployment can GC aggressively
    without touching the disk-sweep cadence. ``0`` disables it."""
    interval = SETTINGS.memory_gc_interval_seconds
    if interval <= 0:
        log.info("Memory GC disabled (interval=%s)", interval)
        return

    def _loop() -> None:
        import time

        while True:
            time.sleep(interval)
            try:
                dropped = registry.memory_gc()
                if dropped:
                    log.info("Memory GC dropped %d stale in-memory job(s)", dropped)
            except Exception:
                log.exception("Memory GC failed")

    t = threading.Thread(target=_loop, name="nomusic-memory-gc", daemon=True)
    t.start()


def _start_engine_warmup(engine) -> None:
    """Load the default model weights on a background thread at startup.

    The first separation otherwise pays a one-off model-load cost (weights
    fetch + MPS init, several seconds) on the user's first chunk. Warming up
    in the background means that cost is usually already paid by the time the
    first job reaches its separation phase, so first-chunk latency matches
    steady state. Best-effort: a failure here just falls back to lazy loading.
    """

    def _loop() -> None:
        try:
            engine.warmup()
            log.info("Engine warmup complete")
        except Exception:
            log.exception("Engine warmup failed; will load lazily on first job")

    t = threading.Thread(target=_loop, name="nomusic-engine-warmup", daemon=True)
    t.start()


# SSE responses must not be buffered: ``no-cache`` stops the browser caching
# the stream, ``X-Accel-Buffering: no`` tells nginx-style proxies (relevant
# once this runs behind a real server) to flush each event immediately.
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

# How often a quiet stream wakes to check whether the client has disconnected.
# Kept well below the keep-alive gap so unsubscribe() — which starts the
# idle-abandon clock — fires within ~1s of a pause/tab-close, rather than
# lagging up to a full keep-alive interval.
_SSE_DISCONNECT_POLL_SECONDS = 1.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Capture the running event loop once at startup. Worker threads use it
    # (via call_soon_threadsafe) to push status snapshots onto SSE queues.
    app.state.registry.attach_loop(asyncio.get_running_loop())
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="nomusic", version="0.2.0", lifespan=lifespan)

    # ``allow_private_network=True`` opts into Chrome's Private Network
    # Access flow: a fetch from a public origin (youtube.com) to a private
    # IP (127.0.0.1) gets an extra preflight with
    # ``Access-Control-Request-Private-Network: true`` and the response
    # must echo ``Access-Control-Allow-Private-Network: true``. Without it
    # Chrome silently drops the request even when regular CORS is correct.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(SETTINGS.allow_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        allow_private_network=True,
    )

    engine = get_engine(SETTINGS.engine_name)
    cache = JobCache(SETTINGS.cache_dir)
    processor = Processor(
        engine=engine,
        cache=cache,
        chunk_seconds=SETTINGS.chunk_seconds,
        chunk_overlap_seconds=SETTINGS.chunk_overlap_seconds,
        keep_source_after_complete=SETTINGS.keep_source_after_complete,
        progressive=SETTINGS.progressive_download,
    )
    registry = JobRegistry(processor=processor, cache=cache)

    # Stash on app.state so tests can poke at it without re-importing.
    app.state.engine = engine
    app.state.cache = cache
    app.state.registry = registry

    _start_cache_ttl_sweeper(cache)
    _start_memory_gc(registry)
    _start_engine_warmup(engine)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/capabilities")
    def capabilities() -> dict:
        caps = engine.capabilities()
        return {
            "server_version": app.version,
            "engine": {
                "name": caps.name,
                "device": caps.device,
                "supported_models": list(caps.supported_models),
                "default_model": caps.default_model,
                "supported_stems": list(caps.supported_stems),
            },
            "defaults": {
                "keep_stems": list(SETTINGS.default_keep_stems),
                "chunk_seconds": SETTINGS.chunk_seconds,
                "chunk_overlap_seconds": SETTINGS.chunk_overlap_seconds,
            },
            "cache": {
                "ttl_days": SETTINGS.cache_ttl_days,
                "keep_source_after_complete": SETTINGS.keep_source_after_complete,
            },
        }

    @app.post("/process")
    def process(req: ProcessRequest) -> dict:
        caps = engine.capabilities()
        model = req.model or caps.default_model
        if model not in caps.supported_models:
            # Fall back instead of erroring: a client may carry a stale model in
            # its saved settings (e.g. one we've since dropped). Log it and use
            # the default rather than failing the job.
            log.warning(
                "unknown model %r requested; falling back to default %r",
                model, caps.default_model,
            )
            model = caps.default_model
        keep_stems = list(req.keep_stems or SETTINGS.default_keep_stems)
        try:
            status = registry.submit(req.url, model=model, keep_stems=keep_stems)
        except Exception as exc:
            log.exception("submit failed")
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return status.to_dict()

    @app.post("/process/{job_id}/prioritize")
    def prioritize(job_id: str, req: PrioritizeRequest) -> dict:
        """Re-order the worker's pending chunks so ``from_chunk`` is next.

        Fire-and-forget from the client's perspective. Returns ``applied``
        so a curious caller can tell whether the job was still mutable
        (it isn't once the job is fully done), but a normal seek doesn't
        need to inspect the response.
        """
        ok = registry.prioritize(job_id, req.from_chunk)
        return {"applied": ok}

    @app.get("/status/{job_id}")
    def status(job_id: str) -> dict:
        status = registry.get(job_id)
        if status is None:
            raise HTTPException(status_code=404, detail="unknown job_id")
        return status.to_dict()

    @app.get("/events/{job_id}")
    async def events(job_id: str, request: Request):
        """Server-Sent Events stream of a job's status.

        Replaces the extension's old /status polling: the client opens one
        EventSource and receives a snapshot on connect plus a push on every
        state change, ending with a terminal ``ready``/``error`` event.

        Three response shapes:
          * 204 No Content for an unknown job — per the SSE spec this tells
            EventSource to stop reconnecting (vs. a 404, which it would retry).
          * a single-event stream for a job that's already terminal (e.g. a
            fully-cached replay) — no subscription, so nothing to leak.
          * a live subscription for an in-flight job.
        """
        initial = registry.get(job_id)
        if initial is None:
            return Response(status_code=204)

        if initial.state.value in ("ready", "error"):
            payload = json.dumps(initial.to_dict())

            async def one_shot():
                yield f"data: {payload}\n\n"

            return StreamingResponse(
                one_shot(), media_type="text/event-stream", headers=_SSE_HEADERS
            )

        queue = registry.subscribe(job_id)

        async def stream():
            try:
                # Emit the current state right away so the client paints
                # without waiting for the next change. Re-read after subscribe
                # to close the gap where the job finished mid-handshake.
                cur = registry.get(job_id)
                if cur is not None:
                    yield f"data: {json.dumps(cur.to_dict())}\n\n"
                    if cur.state.value in ("ready", "error"):
                        return
                # Poll for disconnect every _SSE_DISCONNECT_POLL_SECONDS but
                # only emit a keep-alive comment every sse_keepalive_seconds, so
                # a paused/closed client is noticed promptly (starting the idle
                # clock) without spamming the wire with keep-alives.
                polls_per_keepalive = max(
                    1,
                    round(
                        SETTINGS.sse_keepalive_seconds / _SSE_DISCONNECT_POLL_SECONDS
                    ),
                )
                idle_polls = 0
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        update = await asyncio.wait_for(
                            queue.get(), timeout=_SSE_DISCONNECT_POLL_SECONDS
                        )
                    except asyncio.TimeoutError:
                        idle_polls += 1
                        if idle_polls >= polls_per_keepalive:
                            idle_polls = 0
                            yield ":\n\n"  # keep-alive comment; ignored by EventSource
                        continue
                    idle_polls = 0
                    yield f"data: {json.dumps(update)}\n\n"
                    if update.get("state") in ("ready", "error"):
                        return
            finally:
                registry.unsubscribe(job_id, queue)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers=_SSE_HEADERS
        )

    @app.get("/chunk/{job_id}/{chunk_idx}")
    def chunk(job_id: str, chunk_idx: int) -> FileResponse:
        meta = cache.load_meta(job_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="unknown job_id")
        if chunk_idx < 0 or chunk_idx >= meta.total_chunks:
            raise HTTPException(status_code=404, detail="chunk index out of range")
        path = cache.chunk_path(job_id, chunk_idx)
        if not path.exists():
            # 425 Too Early: client should poll /status and retry.
            # no-store is critical — without it, the browser can cache the
            # 425 and serve it forever, so a chunk that landed on disk a
            # second later would still appear "not ready" to the client.
            raise HTTPException(
                status_code=425,
                detail="chunk not ready",
                headers={"Cache-Control": "no-store"},
            )
        return FileResponse(
            str(path),
            media_type=CHUNK_MEDIA_TYPE,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/cache")
    def cache_stats() -> dict:
        return {"root": str(cache.root), **cache.stats()}

    @app.post("/cache/clear")
    def cache_clear() -> dict:
        # Ask any in-flight workers to abandon at their next chunk boundary
        # (releases the GPU lock + runs their own cleanup) and drop the
        # in-memory status map so a freshly cleared cache doesn't surface stale
        # "ready" status. Doing this before clear_all() means a live worker
        # unwinds cleanly instead of crashing on a chunk write into a
        # just-deleted directory.
        registry.abandon_all()
        freed = cache.clear_all()
        return {"deleted_bytes": freed}

    @app.get("/audio/{job_id}")
    def audio(job_id: str):
        """On-demand concatenation of every chunk into a single OGG stream.

        We no longer keep a precomputed full file on disk (cut storage in
        half), so this endpoint stitches the per-chunk OGG/Opus files
        together as bytes flow out. OGG containers concatenate cleanly:
        writing one file's bytes after another produces a valid combined
        stream that Web Audio, VLC, and ffplay all decode as one track.
        """
        from fastapi.responses import StreamingResponse

        meta = cache.load_meta(job_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="unknown job_id")
        if not meta.complete:
            raise HTTPException(status_code=425, detail="full audio not ready")

        # Snapshot the contiguous run of chunk files ONCE. The advertised
        # Content-Length and the streamed body must come from the same view of
        # disk; computing them in two passes lets a gap (or a concurrent TTL
        # sweep / cache clear) advertise more bytes than _gen actually yields,
        # which clients read as a truncated/hung response.
        chunk_files = []
        for idx in range(meta.total_chunks):
            p = cache.chunk_path(job_id, idx)
            try:
                size = p.stat().st_size
            except FileNotFoundError:
                break  # serve only the contiguous prefix that exists
            chunk_files.append((p, size))

        def _gen():
            for p, _ in chunk_files:
                try:
                    f = open(p, "rb")
                except FileNotFoundError:
                    return  # deleted after the snapshot; stop short
                with f:
                    while True:
                        block = f.read(64 * 1024)
                        if not block:
                            break
                        yield block

        total = sum(size for _, size in chunk_files)
        headers = {"Cache-Control": "public, max-age=86400"}
        if total:
            headers["Content-Length"] = str(total)
        return StreamingResponse(
            _gen(), media_type=CHUNK_MEDIA_TYPE, headers=headers
        )

    return app


app = create_app()


def main() -> None:
    import os

    import uvicorn

    # Dev convenience: NOMUSIC_RELOAD=1 watches backend/*.py and restarts on
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
        # uvicorn's reloader needs an import string (not the app object) so its
        # watcher subprocess can re-import on change. Put the backend dir on
        # PYTHONPATH so that subprocess resolves "server" no matter which cwd
        # the script was launched from (the README runs it from the repo root).
        os.environ["PYTHONPATH"] = (
            str(_BACKEND_DIR) + os.pathsep + os.environ.get("PYTHONPATH", "")
        )
        uvicorn.run(
            "server:app",
            host=SETTINGS.host,
            port=SETTINGS.port,
            reload=True,
            reload_dirs=[str(_BACKEND_DIR)],
            log_level="info",
            access_log=False,
        )
    else:
        uvicorn.run(
            app,
            host=SETTINGS.host,
            port=SETTINGS.port,
            log_level="info",
            access_log=False,
        )


if __name__ == "__main__":
    main()
