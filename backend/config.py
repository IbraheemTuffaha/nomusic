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


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(f"NOMUSIC_{key}")
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


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

    # Stems to keep in the final mix. Default = ``vocals`` only, because
    # demucs's ``other`` stem is a residual bucket that contains melodic
    # instruments (guitars, synths, strings, pianos) along with ambient
    # sounds — keeping it leaves most music audible. The popup lets the user
    # opt back into ``vocals + other`` if they're watching a sparse track
    # where music removal isn't critical and they want ambient SFX preserved.
    default_keep_stems: tuple[str, ...] = ("vocals",)

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

    # Cache entries (per-job dirs and per-source dirs) older than this are
    # swept on server startup and once an hour while running. ``0`` disables
    # the sweep entirely. Default keeps a week of recent work so a re-watch
    # later in the day is instant but a forgotten 3h video doesn't sit at
    # 2 GB on disk indefinitely.
    cache_ttl_days: float = field(
        default_factory=lambda: _env_float("CACHE_TTL_DAYS", 7.0)
    )
    cache_sweep_interval_seconds: float = field(
        default_factory=lambda: _env_float("CACHE_SWEEP_INTERVAL_SECONDS", 3600.0)
    )

    # Once a job is fully processed (every chunk on disk + full.wav written),
    # the original compressed audio file we pulled from yt-dlp is dead weight
    # for normal re-watches — they read straight from the chunks. We delete
    # it by default to save space. Set to True if you frequently re-run with
    # different keep_stems on the same URL, since each variant requires the
    # source to be re-downloaded.
    keep_source_after_complete: bool = field(
        default_factory=lambda: _env_bool("KEEP_SOURCE_AFTER_COMPLETE", False)
    )


SETTINGS = Settings()
