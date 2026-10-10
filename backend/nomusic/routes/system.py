"""System endpoints: liveness, engine/server capabilities, and cache stats/clear.

Split out of ``server.create_app``. Handlers read the shared engine, cache, and
registry from ``request.app.state``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from nomusic.security import require_operator

from . import JsonDict

router = APIRouter()


@router.get("/healthz")
def healthz() -> dict[str, bool]:
    return {"ok": True}


@router.get("/readyz")
def readyz(request: Request, _operator=Depends(require_operator)) -> JSONResponse:
    services = getattr(request.app.state, "services", None)
    status = services.readiness() if services is not None else {"ok": False, "state": "not_started"}
    return JSONResponse(status, status_code=200 if status["ok"] else 503,
                        headers={"Cache-Control": "no-store"})


@router.get("/capabilities")
def get_capabilities(request: Request, _operator=Depends(require_operator)) -> JsonDict:
    engine = request.app.state.engine
    settings = request.app.state.settings
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
            "keep_stems": list(settings.default_keep_stems),
            "chunk_seconds": settings.chunk_seconds,
            "chunk_overlap_seconds": settings.chunk_overlap_seconds,
        },
        "cache": {
            "ttl_days": settings.cache_ttl_days,
            "keep_source_after_complete": settings.keep_source_after_complete,
        },
    }
