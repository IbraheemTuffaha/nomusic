"""Runtime configuration.

Values can be overridden by environment variables prefixed ``NOMUSIC_`` for
local debugging; the extension never sets these. Keep this module dependency-
free so it can be imported by tests and tools.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(key: str, default: str) -> str:
    return os.environ.get(f"NOMUSIC_{key}", default)


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(f"NOMUSIC_{key}")
    return int(raw) if raw else default


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(f"NOMUSIC_{key}")
    return float(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    host: str = field(default_factory=lambda: _env("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("PORT", 8723))

    engine_name: str = field(default_factory=lambda: _env("ENGINE", "mlx"))

    cache_dir: Path = field(
        default_factory=lambda: Path(
            _env("CACHE_DIR", str(Path.home() / ".cache" / "nomusic"))
        )
    )

    # Stems to keep in the final mix. ``vocals`` covers speech; ``other`` covers
    # ambient sounds / sound effects. Drums and bass are the music we drop.
    default_keep_stems: tuple[str, ...] = ("vocals", "other")

    # 30 s chunks per the design doc, with a small overlap to absorb edge
    # artifacts from the separator. ``chunk_overlap_seconds`` is the *total*
    # overlap between adjacent chunks; the crossfade uses the same window.
    chunk_seconds: float = field(
        default_factory=lambda: _env_float("CHUNK_SECONDS", 30.0)
    )
    chunk_overlap_seconds: float = field(
        default_factory=lambda: _env_float("CHUNK_OVERLAP_SECONDS", 1.0)
    )

    # CORS: extensions hit us with random ``chrome-extension://`` origins, so we
    # allow all origins but only for the localhost listener.
    allow_origins: tuple[str, ...] = ("*",)


SETTINGS = Settings()
