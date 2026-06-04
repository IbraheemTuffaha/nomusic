"""FastAPI entrypoint for the nomusic backend.

Run directly with ``python backend/server.py`` (no uvicorn CLI needed).

Endpoints:
  GET  /healthz
  GET  /capabilities
  POST /process              {url, model?, keep_stems?} -> {job_id, ...}
  GET  /status/{job_id}      -> JobStatus
  GET  /events/{job_id}      -> text/event-stream (SSE status updates)
  GET  /chunk/{job_id}/{idx} -> audio/wav (404 if not yet ready)
  GET  /audio/{job_id}       -> audio/ogg (concatenated track; ?format=mp3 transcodes)
  GET  /video/{job_id}       -> video/mp4 (original video, stripped audio muxed in)
  GET  /video/{job_id}/progress -> {phase, percent} for the export in flight
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
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
from starlette.background import BackgroundTask  # noqa: E402

from config import SETTINGS  # noqa: E402
from engines import get_engine  # noqa: E402
from engines.base import DEMUCS_STEMS  # noqa: E402
from jobs import JobRegistry  # noqa: E402
from pipeline import downloader  # noqa: E402
from pipeline.cache import CHUNK_MEDIA_TYPE, JobCache  # noqa: E402
from pipeline.export import (  # noqa: E402
    mp3_transcode_cmd,
    mux_video_cmd,
    snapshot_chunk_files,
    write_concat_list,
)
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


def _run_ffmpeg(cmd: list[str]) -> None:
    """Run an ffmpeg command, surfacing its stderr as a 500 on failure.

    Mirrors slice_source's error handling: capture stderr so a failure carries
    ffmpeg's actual message instead of a bare exit code.
    """
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip() or "(no stderr)"
        log.error("ffmpeg failed (exit %d): %s", proc.returncode, detail)
        raise HTTPException(status_code=500, detail=f"ffmpeg failed: {detail}")


# Codecs QuickTime/Safari can play inside an MP4 — these we stream-copy. Any
# other video codec (VP9/AV1, which YouTube uses above 1080p) is re-encoded to
# H.264 so the exported MP4 plays everywhere, not just in VLC/Chrome.
_MP4_COPYABLE_VCODECS = frozenset({"h264", "hevc"})


def _video_codec(path: Path) -> str:
    """Return the first video stream's codec name via ffprobe ("" on failure)."""
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _video_duration(path: Path) -> float:
    """Best-effort duration (seconds) of ``path``'s video stream, 0.0 on failure.

    Used as the denominator for the ffmpeg encode-progress percentage. Falls
    back to the container duration when the stream doesn't advertise one."""
    for args in (
        ["-select_streams", "v:0", "-show_entries", "stream=duration"],
        ["-show_entries", "format=duration"],
    ):
        out = subprocess.run(
            ["ffprobe", "-v", "error", *args, "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True,
        ).stdout.strip()
        try:
            if out and out != "N/A":
                return float(out)
        except ValueError:
            pass
    return 0.0


def _run_ffmpeg_progress(cmd: list[str], total_seconds: float, on_pct) -> None:
    """Run ffmpeg, streaming completion fraction to ``on_pct`` as it encodes.

    ``cmd`` must start with ``ffmpeg``; we inject ``-progress pipe:1`` so ffmpeg
    writes machine-readable progress to stdout (``out_time_us`` lines), which we
    parse into a 0..1 fraction. Errors are surfaced the same way as
    :func:`_run_ffmpeg`. ``total_seconds`` of 0 disables the percentage (the
    callback simply isn't driven)."""
    full = [cmd[0], "-progress", "pipe:1", "-nostats", *cmd[1:]]
    # stderr -> a temp file, not a PIPE: this loop only drains stdout, so a
    # PIPE'd stderr that fills the ~64KB OS buffer (a verbose re-encode failure
    # is the realistic trigger) would block ffmpeg's stderr write while we block
    # reading stdout — a classic pipe deadlock. A file never blocks the writer.
    with tempfile.TemporaryFile() as errf:
        proc = subprocess.Popen(
            full, stdout=subprocess.PIPE, stderr=errf, text=True
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            # ffmpeg reports out_time_us in microseconds (the older out_time_ms
            # key is also microseconds despite its name — we read out_time_us).
            if line.startswith("out_time_us=") and total_seconds > 0:
                try:
                    done = int(line.split("=", 1)[1]) / 1e6 / total_seconds
                    on_pct(max(0.0, min(1.0, done)))
                except ValueError:
                    pass
        proc.wait()
        if proc.returncode != 0:
            errf.seek(0)
            detail = errf.read().decode("utf-8", "replace").strip() or "(no stderr)"
            log.error("ffmpeg failed (exit %d): %s", proc.returncode, detail)
            raise HTTPException(status_code=500, detail=f"ffmpeg failed: {detail}")


# --- MP4 export progress (polled by the extension while it prepares a video) --
# Keyed by "<job_id>:<max_height or 0>". Each value is {"phase": str,
# "percent": 0..100}. A plain dict guarded by a lock — exports are rare and
# short-lived, so this never grows unbounded.
_export_progress: dict[str, dict] = {}
_export_progress_lock = threading.Lock()


def _export_key(job_id: str, max_height: Optional[int]) -> str:
    return f"{job_id}:{max_height or 0}"


def _set_export_progress(key: str, phase: str, percent: float) -> None:
    with _export_progress_lock:
        _export_progress[key] = {"phase": phase, "percent": round(percent, 1)}


def _clear_export_progress(key: str) -> None:
    with _export_progress_lock:
        _export_progress.pop(key, None)


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
    def audio(job_id: str, format: str = "opus"):
        """On-demand concatenation of every chunk into a single track.

        We no longer keep a precomputed full file on disk (cut storage in
        half), so this endpoint stitches the per-chunk OGG/Opus files
        together. OGG containers concatenate cleanly: writing one file's bytes
        after another produces a valid combined stream that Web Audio, VLC,
        and ffplay all decode as one track.

        ``format``:
          * ``opus`` (default) — stream the concatenated OGG/Opus bytes as-is.
          * ``mp3`` — transcode the concatenation to MP3 (for the download
            button) and serve it as a temp file, cleaned up after the response.
        """
        if format not in ("opus", "mp3"):
            raise HTTPException(status_code=400, detail="format must be opus or mp3")

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
        chunk_files = snapshot_chunk_files(cache, job_id, meta.total_chunks)

        if format == "mp3":
            if not chunk_files:
                # Mirror /video: an empty snapshot (a concurrent sweep/clear
                # emptied the dir after the meta.complete check) is a transient
                # not-ready, not an opaque "ffmpeg failed" 500 from an empty
                # concat list. The opus branch below degrades to an empty
                # stream on its own, so this guard is mp3-only.
                raise HTTPException(status_code=425, detail="full audio not ready")
            # Transcode up front into a temp dir, then hand the file to
            # FileResponse and delete the dir once the response is sent. A
            # single up-front transcode (rather than a streaming pipe) keeps
            # this simple and is fine for a local single-user backend.
            tmp_dir = Path(tempfile.mkdtemp(prefix="nomusic-mp3-"))
            try:
                list_path = tmp_dir / "chunks.txt"
                write_concat_list(chunk_files, list_path)
                out = tmp_dir / "full.mp3"
                _run_ffmpeg(mp3_transcode_cmd(list_path, out))
            except BaseException:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                raise
            return FileResponse(
                str(out),
                media_type="audio/mpeg",
                headers={"Cache-Control": "no-store"},
                background=BackgroundTask(shutil.rmtree, tmp_dir, ignore_errors=True),
            )

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

    @app.get("/video/{job_id}")
    def video(job_id: str, max_height: Optional[int] = None):
        """Original video with its audio replaced by the music-stripped track.

        The pipeline never downloads video, so we pull the video stream on
        demand (cached per-(url, resolution) under ``videos/`` so repeat
        exports are fast), concatenate the stripped chunks, and mux them
        together with a stream-copy of the video (only the audio is re-encoded,
        to AAC for the MP4 container). The muxed file is built per request and
        deleted once the response is sent.

        ``max_height`` caps the download resolution (e.g. 1080); omit it or pass
        0 for the best available. The extension drives this from the download
        menu, and polls ``/video/{job_id}/progress`` to show the live percent.
        """
        # 0/negative → best available. Clamp to a sane range so the value can't
        # come back as a pathological format selector.
        if max_height is not None and max_height <= 0:
            max_height = None
        if max_height is not None:
            max_height = max(144, min(4320, max_height))

        meta = cache.load_meta(job_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="unknown job_id")
        if not meta.complete:
            raise HTTPException(status_code=425, detail="full audio not ready")

        chunk_files = snapshot_chunk_files(cache, job_id, meta.total_chunks)
        if not chunk_files:
            raise HTTPException(status_code=425, detail="full audio not ready")

        progress_key = _export_key(job_id, max_height)
        _set_export_progress(progress_key, "downloading", 0.0)
        tmp_dir: Optional[Path] = None
        try:
            # --- Phase 1: fetch the video stream (cached per url+resolution) ---
            def _dl_hook(d: dict) -> None:
                if d.get("status") == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate")
                    got = d.get("downloaded_bytes")
                    if total and got is not None:
                        _set_export_progress(progress_key, "downloading", 100.0 * got / total)
                elif d.get("status") == "finished":
                    _set_export_progress(progress_key, "downloading", 100.0)

            try:
                video_path = downloader.download_video(
                    meta.url,
                    cache.video_dir(meta.url, max_height),
                    max_height=max_height,
                    progress_hook=_dl_hook,
                )
            except Exception as exc:
                # yt-dlp failures are the user's URL going stale / network
                # issues, not a server bug — surface them as a 502.
                log.warning("video download failed for %s", job_id, exc_info=True)
                raise HTTPException(status_code=502, detail=f"video download failed: {exc}")

            # --- Phase 2: mux the stripped audio over the video ---
            _set_export_progress(progress_key, "encoding", 0.0)
            tmp_dir = Path(tempfile.mkdtemp(prefix="nomusic-mp4-"))
            list_path = tmp_dir / "chunks.txt"
            write_concat_list(chunk_files, list_path)
            out = tmp_dir / "full.mp4"
            total_seconds = _video_duration(video_path)

            def _enc_pct(frac: float) -> None:
                _set_export_progress(progress_key, "encoding", 100.0 * frac)

            # Copy H.264/HEVC straight through (fast, lossless); re-encode
            # VP9/AV1 to H.264 so the MP4 plays in QuickTime/Safari too.
            reencode = _video_codec(video_path) not in _MP4_COPYABLE_VCODECS
            try:
                _run_ffmpeg_progress(
                    mux_video_cmd(video_path, list_path, out, reencode_video=reencode),
                    total_seconds, _enc_pct,
                )
            except HTTPException:
                if reencode:
                    raise  # already re-encoding; nothing left to fall back to
                # A copy we expected to work didn't — re-encode as a fallback.
                log.warning(
                    "video mux (copy) failed for %s; retrying with H.264 re-encode",
                    job_id,
                )
                # The retry's progress restarts at 0; reset the published
                # percent so the poller doesn't see it jump backward mid-export.
                _set_export_progress(progress_key, "encoding", 0.0)
                _run_ffmpeg_progress(
                    mux_video_cmd(video_path, list_path, out, reencode_video=True),
                    total_seconds, _enc_pct,
                )
        except BaseException:
            _clear_export_progress(progress_key)
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

        _set_export_progress(progress_key, "done", 100.0)

        def _cleanup() -> None:
            _clear_export_progress(progress_key)
            shutil.rmtree(tmp_dir, ignore_errors=True)

        return FileResponse(
            str(out),
            media_type="video/mp4",
            headers={"Cache-Control": "no-store"},
            background=BackgroundTask(_cleanup),
        )

    @app.get("/video/{job_id}/progress")
    def video_progress(job_id: str, max_height: Optional[int] = None) -> dict:
        """Current MP4-export progress for the extension's download menu.

        Returns ``{"phase": "downloading"|"encoding"|"done"|"idle", "percent":
        0..100}``. ``idle`` means no export is in flight for this (job,
        resolution) — the extension stops polling once its download fetch
        resolves, so a stale entry never lingers."""
        if max_height is not None and max_height <= 0:
            max_height = None
        if max_height is not None:
            max_height = max(144, min(4320, max_height))
        with _export_progress_lock:
            return _export_progress.get(
                _export_key(job_id, max_height), {"phase": "idle", "percent": 0}
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
