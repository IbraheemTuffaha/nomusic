"""FastAPI entrypoint for the nomusic backend.

Run directly with ``python backend/server.py`` (no uvicorn CLI needed).

Endpoints:
  GET  /healthz
  GET  /capabilities
  POST /process              {url, model?, keep_stems?} -> {job_id, ...}
  GET  /status/{job_id}      -> JobStatus
  GET  /chunk/{job_id}/{idx} -> audio/wav (404 if not yet ready)
  GET  /audio/{job_id}       -> audio/wav (the concatenated full track)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

# Make sibling modules importable when this file is invoked as a script.
_BACKEND_DIR = Path(__file__).resolve().parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from pydantic import BaseModel, Field, field_validator  # noqa: E402

from config import SETTINGS  # noqa: E402
from engines import get_engine  # noqa: E402
from engines.base import DEMUCS_STEMS  # noqa: E402
from jobs import JobRegistry  # noqa: E402
from pipeline.cache import JobCache  # noqa: E402
from pipeline.processor import Processor  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
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


def create_app() -> FastAPI:
    app = FastAPI(title="nomusic", version="0.1.0")

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
        allow_methods=["GET", "POST", "OPTIONS"],
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
    )
    registry = JobRegistry(processor=processor, cache=cache)

    # Stash on app.state so tests can poke at it without re-importing.
    app.state.engine = engine
    app.state.cache = cache
    app.state.registry = registry

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
        }

    @app.post("/process")
    def process(req: ProcessRequest) -> dict:
        caps = engine.capabilities()
        model = req.model or caps.default_model
        if model not in caps.supported_models:
            raise HTTPException(
                status_code=400,
                detail=f"unsupported model {model!r}; "
                f"supported: {list(caps.supported_models)}",
            )
        keep_stems = list(req.keep_stems or SETTINGS.default_keep_stems)
        try:
            status = registry.submit(req.url, model=model, keep_stems=keep_stems)
        except Exception as exc:
            log.exception("submit failed")
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return status.to_dict()

    @app.get("/status/{job_id}")
    def status(job_id: str) -> dict:
        status = registry.get(job_id)
        if status is None:
            raise HTTPException(status_code=404, detail="unknown job_id")
        return status.to_dict()

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
            raise HTTPException(status_code=425, detail="chunk not ready")
        return FileResponse(
            str(path),
            media_type="audio/wav",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/audio/{job_id}")
    def audio(job_id: str) -> FileResponse:
        meta = cache.load_meta(job_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="unknown job_id")
        path = cache.full_path(job_id)
        if not path.exists():
            raise HTTPException(status_code=425, detail="full audio not ready")
        return FileResponse(
            str(path),
            media_type="audio/wav",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    return app


app = create_app()


def main() -> None:
    import uvicorn

    log.info(
        "Starting nomusic backend on http://%s:%d (engine=%s)",
        SETTINGS.host,
        SETTINGS.port,
        SETTINGS.engine_name,
    )
    uvicorn.run(
        app,
        host=SETTINGS.host,
        port=SETTINGS.port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
