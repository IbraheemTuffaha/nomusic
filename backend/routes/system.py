"""System endpoints: liveness, engine/server capabilities, and cache stats/clear.

Split out of ``server.create_app``. Handlers read the shared engine, cache, and
registry from ``request.app.state``.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from config import SETTINGS

from . import JsonDict

router = APIRouter()


@router.get("/healthz")
def healthz() -> dict[str, bool]:
    return {"ok": True}


@router.get("/capabilities")
def get_capabilities(request: Request) -> JsonDict:
    engine = request.app.state.engine
    caps = engine.capabilities()
    return {
        "server_version": request.app.version,
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


@router.get("/cache")
def cache_stats(request: Request) -> JsonDict:
    cache = request.app.state.cache
    return {"root": str(cache.root), **cache.stats()}


@router.post("/cache/clear")
def cache_clear(request: Request) -> dict[str, int]:
    # Ask any in-flight workers to abandon at their next chunk boundary
    # (releases the GPU lock + runs their own cleanup) and drop the
    # in-memory status map so a freshly cleared cache doesn't surface stale
    # "ready" status. Doing this before clear_all() means a live worker
    # unwinds cleanly instead of crashing on a chunk write into a
    # just-deleted directory.
    registry = request.app.state.registry
    cache = request.app.state.cache
    registry.abandon_all()
    freed = cache.clear_all()
    return {"deleted_bytes": freed}
