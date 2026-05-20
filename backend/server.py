"""FastAPI entrypoint for the nomusic backend.

Run directly with ``python backend/server.py`` (no uvicorn CLI needed).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Make sibling modules importable when this file is invoked as a script.
_BACKEND_DIR = Path(__file__).resolve().parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# Vendored ``demucs-mlx`` checkout. install.sh clones it here.
_VENDOR_DEMUCS = _BACKEND_DIR.parent / "vendor" / "demucs-mlx"
if _VENDOR_DEMUCS.exists() and str(_VENDOR_DEMUCS) not in sys.path:
    sys.path.insert(0, str(_VENDOR_DEMUCS))

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from config import SETTINGS  # noqa: E402
from engines import get_engine  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("nomusic.server")


def create_app() -> FastAPI:
    app = FastAPI(title="nomusic", version="0.1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(SETTINGS.allow_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    # One engine instance for the process. Engines are responsible for their
    # own thread/process safety; today's MLXEngine is fine to share because
    # demucs_mlx serializes inference internally.
    engine = get_engine(SETTINGS.engine_name)

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

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

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
