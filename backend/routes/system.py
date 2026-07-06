"""System endpoints: liveness, engine/server capabilities, and cache stats/clear.

Split out of ``server.create_app``. Handlers read the shared engine, cache, and
registry from ``request.app.state``.

Two routers are exported: ``router`` (public read-only health/capabilities) and
``admin`` (cache stats + clear), the latter gated behind the private admin token
in public mode so a client can't read global usage or wipe everyone's data.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from config import SETTINGS
from security import require_admin, require_edge

from . import JsonDict

router = APIRouter()

# Destructive / global-state routes. In public mode every route here requires the
# admin token (require_admin) AND the tunnel-secret gate (require_edge), so a
# LAN-direct probe of the loopback socket sees the same "looks absent" 404 the
# jobs/media routers already return instead of being able to hammer the admin
# token off-tunnel. In dev both are no-ops so behavior is unchanged. (/healthz is
# left ungated as a liveness probe that hits loopback directly and leaks nothing.)
admin = APIRouter(dependencies=[Depends(require_edge), Depends(require_admin)])


@router.get("/healthz")
def healthz() -> dict[str, bool]:
    return {"ok": True}


@router.get("/capabilities", dependencies=[Depends(require_edge)])
def get_capabilities(request: Request) -> JsonDict:
    engine = request.app.state.engine
    caps = engine.capabilities()
    engine_info: JsonDict = {
        "name": caps.name,
        "supported_models": list(caps.supported_models),
        "default_model": caps.default_model,
        "supported_stems": list(caps.supported_stems),
    }
    body: JsonDict = {
        "engine": engine_info,
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
    # Don't fingerprint the host to anonymous clients in public mode: the
    # extension only consumes models/stems/defaults (F21).
    if not SETTINGS.public:
        body["server_version"] = request.app.version
        engine_info["device"] = caps.device
    return body


@admin.get("/cache")
def cache_stats(request: Request) -> JsonDict:
    # Drop the on-disk root path (F19): it's an internal detail with no client
    # consumer, and now admin-only besides.
    cache = request.app.state.cache
    return cache.stats()


@admin.post("/cache/clear")
def cache_clear(request: Request) -> dict[str, int]:
    # Ask any in-flight workers to abandon at their next chunk boundary
    # (releases their admission slot + runs their own cleanup) and drop the
    # in-memory status map so a freshly cleared cache doesn't surface stale
    # "ready" status. Doing this before clear_all() means a live worker
    # unwinds cleanly instead of crashing on a chunk write into a
    # just-deleted directory.
    registry = request.app.state.registry
    cache = request.app.state.cache
    registry.abandon_all()
    freed = cache.clear_all()
    return {"deleted_bytes": freed}
