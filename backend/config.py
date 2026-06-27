"""Runtime configuration.

Values can be overridden by environment variables prefixed ``NOMUSIC_`` for
local debugging; the extension never sets these. Keep this module dependency-
free so it can be imported by tests and tools.
"""

from __future__ import annotations

import os
import re
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


def _env_tuple(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Comma-separated list override, e.g. NOMUSIC_ALLOWED_URL_HOSTS=youtube.com,vimeo.com.
    Unset → the default tuple; empty string → an empty tuple."""
    raw = os.environ.get(f"NOMUSIC_{key}")
    if raw is None:
        return default
    return tuple(s.strip().lower() for s in raw.split(",") if s.strip())


def _env_int_tuple(key: str, default: tuple[int, ...]) -> tuple[int, ...]:
    raw = os.environ.get(f"NOMUSIC_{key}")
    if raw is None:
        return default
    return tuple(int(s.strip()) for s in raw.split(",") if s.strip())


@dataclass(frozen=True)
class Settings:
    host: str = field(default_factory=lambda: _env("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("PORT", 8723))

    engine_name: str = field(default_factory=lambda: _env("ENGINE", "mlx"))

    # Resolved to an absolute path at startup: cheap argv hygiene so a relative
    # NOMUSIC_CACHE_DIR can't make cache paths depend on the worker's cwd.
    cache_dir: Path = field(
        default_factory=lambda: Path(
            _env("CACHE_DIR", str(Path.home() / ".cache" / "nomusic"))
        ).resolve()
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

    # How long a worker keeps running after its last status subscriber drops.
    # The extension closes its /events stream when you close the tab OR pause
    # the video, so "no subscriber for this long" means "not actively
    # watching". When it elapses, the worker abandons the job between chunks —
    # releasing the GPU lock — so an idle server stops burning the GPU. Resume
    # is cheap (re-spawn from disk-cached progress, no re-probe), so we keep
    # this short. ``0`` disables idle-abandon (workers always run to completion
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

    # ---- Public-deployment hardening ------------------------------------
    # Master toggle. With NOMUSIC_PUBLIC unset (the default — local/dev use),
    # every gate below is a no-op and the server behaves exactly as the
    # localhost-only tool did. Set NOMUSIC_PUBLIC=1 on the internet-facing
    # (Cloudflare-tunnelled) deployment to activate auth/rate-limit/quota/CORS
    # hardening. See docs/remote-deployment/.
    public: bool = field(default_factory=lambda: _env_bool("PUBLIC", False))

    # Secret the owner holds for destructive/admin endpoints (/cache, /cache/clear).
    # Empty in public mode ⇒ those endpoints fail closed (404). Never shipped in
    # the extension.
    admin_token: str = field(default_factory=lambda: _env("ADMIN_TOKEN", ""))
    # Optional shared secret injected by the Cloudflare tunnel (header
    # X-Nomusic-Tunnel). When set in public mode, a request that didn't traverse
    # the tunnel (e.g. a LAN-direct hit to :8723) is rejected.
    tunnel_secret: str = field(default_factory=lambda: _env("TUNNEL_SECRET", ""))
    # The published extension's chrome-extension://<id> origin, allowed through
    # CORS in public mode (the popup/service-worker fetch with this Origin).
    extension_origin: str = field(default_factory=lambda: _env("EXTENSION_ORIGIN", ""))

    # Concurrency / admission caps (public mode).
    max_inflight_jobs: int = field(
        default_factory=lambda: _env_int("MAX_INFLIGHT_JOBS", 3)
    )
    max_jobs_per_ip: int = field(
        default_factory=lambda: _env_int("MAX_JOBS_PER_IP", 2)
    )
    max_video_exports: int = field(
        default_factory=lambda: _env_int("MAX_VIDEO_EXPORTS", 1)
    )
    max_audio_transcodes: int = field(
        default_factory=lambda: _env_int("MAX_AUDIO_TRANSCODES", 2)
    )
    max_sse_per_job: int = field(default_factory=lambda: _env_int("MAX_SSE_PER_JOB", 4))
    max_sse_per_ip: int = field(default_factory=lambda: _env_int("MAX_SSE_PER_IP", 20))
    max_sse_global: int = field(default_factory=lambda: _env_int("MAX_SSE_GLOBAL", 200))
    sse_max_lifetime_seconds: float = field(
        default_factory=lambda: _env_float("SSE_MAX_LIFETIME_SECONDS", 1800.0)
    )

    # Per-IP request-rate caps (requests/minute, public mode).
    rate_process_per_min: int = field(
        default_factory=lambda: _env_int("RATE_PROCESS_PER_MIN", 6)
    )
    rate_video_per_min: int = field(
        default_factory=lambda: _env_int("RATE_VIDEO_PER_MIN", 4)
    )
    rate_default_per_min: int = field(
        default_factory=lambda: _env_int("RATE_DEFAULT_PER_MIN", 120)
    )

    # Hard deadlines (seconds) so one slow/hanging source can't pin resources.
    job_deadline_seconds: float = field(
        default_factory=lambda: _env_float("JOB_DEADLINE_SECONDS", 1800.0)
    )
    download_deadline_seconds: float = field(
        default_factory=lambda: _env_float("DOWNLOAD_DEADLINE_SECONDS", 900.0)
    )

    # Media size / duration ceilings (public mode).
    max_duration_seconds: float = field(
        default_factory=lambda: _env_float("MAX_DURATION_SECONDS", 5400.0)
    )
    max_source_filesize: int = field(
        default_factory=lambda: _env_int("MAX_SOURCE_BYTES", 600_000_000)
    )
    max_video_filesize: int = field(
        default_factory=lambda: _env_int("MAX_VIDEO_BYTES", 4_000_000_000)
    )

    # Disk-fill defense: keep the cache under this size (LRU evict) and never let
    # free space drop below the floor before admitting a new job.
    cache_max_bytes: int = field(
        default_factory=lambda: _env_int("CACHE_MAX_BYTES", 40_000_000_000)
    )
    free_disk_floor_bytes: int = field(
        default_factory=lambda: _env_int("FREE_DISK_FLOOR_BYTES", 5_000_000_000)
    )

    # Request-shape caps (public mode).
    max_request_bytes: int = field(
        default_factory=lambda: _env_int("MAX_REQUEST_BYTES", 8192)
    )
    max_url_length: int = field(
        default_factory=lambda: _env_int("MAX_URL_LENGTH", 2048)
    )
    max_keep_stems: int = field(default_factory=lambda: _env_int("MAX_KEEP_STEMS", 8))
    allowed_video_heights: tuple[int, ...] = field(
        default_factory=lambda: _env_int_tuple(
            "ALLOWED_VIDEO_HEIGHTS", (360, 480, 720, 1080)
        )
    )

    # Positive host + extractor allowlist for /process URLs (public mode), so the
    # box can't be turned into an open download proxy. YouTube + Facebook (§0).
    allowed_url_hosts: tuple[str, ...] = field(
        default_factory=lambda: _env_tuple(
            "ALLOWED_URL_HOSTS",
            (
                "youtube.com",
                "youtu.be",
                "m.youtube.com",
                "music.youtube.com",
                "www.facebook.com",
                "facebook.com",
                "m.facebook.com",
                "fb.watch",
            ),
        )
    )
    allowed_extractors: tuple[str, ...] = field(
        default_factory=lambda: _env_tuple(
            "ALLOWED_EXTRACTORS", ("youtube", "youtube:tab", "facebook")
        )
    )

    # Base domains the curated content scripts run on, used to build the CORS
    # origin allowlist (the page Origin of an extension content-script fetch is
    # e.g. https://www.youtube.com). Kept separate from allowed_url_hosts (which
    # is the yt-dlp download allowlist) because they answer different questions.
    allowed_origin_hosts: tuple[str, ...] = field(
        default_factory=lambda: _env_tuple(
            "ALLOWED_ORIGIN_HOSTS",
            ("youtube.com", "youtube-nocookie.com", "youtu.be", "facebook.com", "fb.watch"),
        )
    )

    @property
    def cors_origins(self) -> list[str]:
        """Static origins for CORSMiddleware. Non-public: '*' (localhost dev).
        Public: just the extension origin (curated sites are matched by
        :attr:`cors_origin_regex`, since their subdomains vary)."""
        if not self.public:
            return list(self.allow_origins)
        return [self.extension_origin] if self.extension_origin else []

    @property
    def cors_origin_regex(self) -> str | None:
        """Regex matching the curated sites' page origins (any subdomain over
        https) in public mode; ``None`` in dev so the static list applies."""
        if not self.public or not self.allowed_origin_hosts:
            return None
        hosts = "|".join(re.escape(h) for h in self.allowed_origin_hosts)
        return rf"^https://([a-z0-9-]+\.)*({hosts})$"

    def is_origin_allowed(self, origin: str) -> bool:
        """True if ``origin`` is the extension origin or a curated-site origin.
        Used by the server-side Origin gate (CORS can't stop side effects)."""
        if self.extension_origin and origin == self.extension_origin:
            return True
        rx = self.cors_origin_regex
        return bool(rx and re.match(rx, origin))


SETTINGS = Settings()
