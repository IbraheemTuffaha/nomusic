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

    # Operator-issued credentials protect every processing/media operation.
    # The file is deliberately outside the repository and is never read by
    # the browser extension's page-facing contexts.
    auth_file: Path = field(
        default_factory=lambda: Path(
            _env(
                "AUTH_FILE",
                str(Path.home() / ".config" / "nomusic" / "operator-keys.json"),
            )
        ).expanduser()
    )

    # One total process-level grace period for active jobs and HTTP responses.
    # Warmup alone is disposable; it never consumes this grace period.
    shutdown_grace_seconds: float = field(
        default_factory=lambda: _env_float("SHUTDOWN_GRACE_SECONDS", 60.0)
    )

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

    # 10 s chunks: faster first-chunk + seek recovery while staying above
    # demucs's internal segment size (~7.8 s for htdemucs) so each chunk
    # only needs one separator pass. Larger chunks reduce per-chunk fixed
    # overhead at the cost of slower startup; smaller chunks invert that
    # trade. ``chunk_overlap_seconds`` is the total overlap between adjacent
    # chunks — kept small (5% of chunk) because the model + the 10 ms
    # anti-click fade in the writer handle boundary quality.
    chunk_seconds: float = field(
        default_factory=lambda: _env_float("CHUNK_SECONDS", 10.0)
    )
    chunk_overlap_seconds: float = field(
        default_factory=lambda: _env_float("CHUNK_OVERLAP_SECONDS", 0.5)
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

    # Once a job is fully processed (every chunk on disk), the original
    # compressed audio file we pulled from yt-dlp is dead weight for normal
    # re-watches — they read straight from the chunks. We delete
    # it by default to save space. Set to True if you frequently re-run with
    # different keep_stems on the same URL, since each variant requires the
    # source to be re-downloaded.
    keep_source_after_complete: bool = field(
        default_factory=lambda: _env_bool("KEEP_SOURCE_AFTER_COMPLETE", False)
    )

    # How long a worker keeps running after its last client interest and status
    # subscriber drop. Explicit client leases are the normal path; the SSE
    # subscriber clock remains as a compatibility fallback for older local
    # extensions. ``0`` disables idle-abandon (workers always run to completion
    # regardless of who's watching).
    idle_timeout_seconds: float = field(
        default_factory=lambda: _env_float("IDLE_TIMEOUT_SECONDS", 10.0)
    )
    # Gap between SSE keep-alive comments on an otherwise-quiet stream. Keeps
    # proxies and the browser from treating a long processing pause (e.g. a
    # slow probe + download with no chunk events) as a dead connection.
    sse_keepalive_seconds: float = field(
        default_factory=lambda: _env_float("SSE_KEEPALIVE_SECONDS", 15.0)
    )
    # Explicit processing interest is renewed by the extension heartbeat. A
    # paused tab stops renewing and retains the worker for at most this lease;
    # a later play re-submits the same cache key and resumes completed chunks.
    # The server clamps caller-requested TTLs to this bound.
    client_lease_seconds: float = field(
        default_factory=lambda: _env_float("CLIENT_LEASE_SECONDS", 30.0)
    )
    client_heartbeat_seconds: float = field(
        default_factory=lambda: _env_float("CLIENT_HEARTBEAT_SECONDS", 10.0)
    )
    # Maintenance expiry is separate from the worker idle check so abandoned
    # client entries do not accumulate in memory. ``0`` is useful for tests.
    interest_sweep_interval_seconds: float = field(
        default_factory=lambda: _env_float("INTEREST_SWEEP_INTERVAL_SECONDS", 5.0)
    )
    # SSE queues retain only the most recent bounded set of snapshots. A slow
    # subscriber cannot grow process memory without limit.
    sse_queue_size: int = field(
        default_factory=lambda: _env_int("SSE_QUEUE_SIZE", 64)
    )
    # Interval for the in-memory GC pass that drops JobStatus entries whose
    # disk cache has already been swept away. Runs on its own daemon thread
    # alongside the disk TTL sweep, keying in-memory lifetime to the on-disk
    # TTL so server memory never outlives the files it describes. ``0``
    # disables it.
    memory_gc_interval_seconds: float = field(
        default_factory=lambda: _env_float("MEMORY_GC_INTERVAL_SECONDS", 3600.0)
    )
    # How many chunks to separate in one batched GPU inference. A single chunk
    # under-utilizes the GPU (batch=1 leaves ~25% of the cores idle on an
    # M-series Pro); batching ~2 fills them for ~+25% throughput at identical
    # output. The sweet spot is chip-dependent — bigger GPUs (Max/Ultra) want a
    # higher value — so it's tunable. ``1`` disables batching. Batches form
    # opportunistically from whatever's already decoded, so a chunk never waits
    # to be batched.
    gpu_batch: int = field(default_factory=lambda: _env_int("GPU_BATCH", 2))
    # Start separating early chunks from the partially-downloaded source
    # instead of waiting for the whole download, so playback begins sooner.
    # Each chunk is sliced only once enough of the timeline is on disk; if the
    # partial container isn't decodable (some sites/formats) it transparently
    # falls back to waiting for the full file, so the worst case is just the
    # old download-once behavior. On by default; set NOMUSIC_PROGRESSIVE=0 to
    # force download-once.
    progressive_download: bool = field(
        default_factory=lambda: _env_bool("PROGRESSIVE", True)
    )

    # The model process accepts one execution at a time.  Registry admission
    # keeps this many additional jobs queued so a burst cannot create an
    # unbounded set of native downloads or waiting threads.
    max_queued_jobs: int = field(
        default_factory=lambda: _env_int("MAX_QUEUED_JOBS", 2)
    )
    # A native inference/download run is allowed this long from admission to
    # terminal publication.  The supervisor uses a monotonic deadline and
    # terminates a child that ignores cooperative cancellation.
    execution_timeout_seconds: float = field(
        default_factory=lambda: _env_float("EXECUTION_TIMEOUT_SECONDS", 1800.0)
    )
    worker_cancel_grace_seconds: float = field(
        default_factory=lambda: _env_float("WORKER_CANCEL_GRACE_SECONDS", 5.0)
    )
    worker_warmup_timeout_seconds: float = field(
        default_factory=lambda: _env_float("WORKER_WARMUP_TIMEOUT_SECONDS", 300.0)
    )
    # The supervised process is the production path.  Keeping an explicit
    # switch makes emergency local rollback possible without changing routes.
    supervised_worker: bool = field(
        default_factory=lambda: _env_bool("SUPERVISED_WORKER", True)
    )

    # Source/output policy. These are deliberately finite local-use defaults;
    # operators can tighten them for a smaller disk or memory budget.
    max_duration_seconds: float = field(
        default_factory=lambda: _env_float("MAX_DURATION_SECONDS", 7200.0)
    )
    max_source_bytes: int = field(
        default_factory=lambda: _env_int("MAX_SOURCE_BYTES", 512 * 1024 * 1024)
    )
    max_video_bytes: int = field(
        default_factory=lambda: _env_int("MAX_VIDEO_BYTES", 2 * 1024 * 1024 * 1024)
    )
    max_video_height: int = field(
        default_factory=lambda: _env_int("MAX_VIDEO_HEIGHT", 1080)
    )
    max_decode_bytes: int = field(
        default_factory=lambda: _env_int("MAX_DECODE_BYTES", 64 * 1024 * 1024)
    )
    max_chunk_bytes: int = field(
        default_factory=lambda: _env_int("MAX_CHUNK_BYTES", 16 * 1024 * 1024)
    )
    max_inference_batch: int = field(
        default_factory=lambda: _env_int("MAX_INFERENCE_BATCH", 2)
    )
    max_prefetch_chunks: int = field(
        default_factory=lambda: _env_int("MAX_PREFETCH_CHUNKS", 2)
    )
    final_chunk_tolerance_seconds: float = field(
        default_factory=lambda: _env_float("FINAL_CHUNK_TOLERANCE_SECONDS", 1.0)
    )
    max_cache_bytes: int = field(
        default_factory=lambda: _env_int("MAX_CACHE_BYTES", 4 * 1024 * 1024 * 1024)
    )
    min_free_bytes: int = field(
        default_factory=lambda: _env_int("MIN_FREE_BYTES", 256 * 1024 * 1024)
    )
    max_export_bytes: int = field(
        default_factory=lambda: _env_int("MAX_EXPORT_BYTES", 2 * 1024 * 1024 * 1024)
    )
    # Export preparation is deliberately separate from the HTTP download. A
    # small bounded queue keeps duplicate/slow builds from creating an
    # unbounded set of ffmpeg processes or retained artifacts.
    max_export_jobs: int = field(
        default_factory=lambda: _env_int("MAX_EXPORT_JOBS", 2)
    )
    export_ttl_seconds: float = field(
        default_factory=lambda: _env_float("EXPORT_TTL_SECONDS", 86400.0)
    )
    export_sweep_interval_seconds: float = field(
        default_factory=lambda: _env_float("EXPORT_SWEEP_INTERVAL_SECONDS", 300.0)
    )
    max_export_downloads: int = field(
        default_factory=lambda: _env_int("MAX_EXPORT_DOWNLOADS", 4)
    )


SETTINGS = Settings()
