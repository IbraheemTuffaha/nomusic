"""Media-serving endpoints: per-chunk audio, the on-demand concatenated track,
and the muxed MP4 export (plus its in-flight progress map).

Split out of ``server.create_app`` so the handlers live at module scope. Each
reads the shared :class:`~pipeline.cache.JobCache` from ``request.app.state``;
the ffmpeg helpers and the export-progress map are module-local because only
these routes use them.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask

from config import SETTINGS
from netsec import validate_public_url
from pipeline import downloader
from pipeline.cache import CHUNK_MEDIA_TYPE
from pipeline.export import (
    MP4_COPYABLE_VCODECS,
    mp3_transcode_cmd,
    mux_video_cmd,
    snapshot_chunk_files,
    video_codec,
    video_duration,
)
from ratelimit import (
    audio_transcode_gate,
    rate_limit,
    video_export_gate,
    video_rl,
)
from security import ChunkIdx, JobId, require_edge

from . import JsonDict

log = logging.getLogger("nomusic.server")

router = APIRouter()


def _ffmpeg_fail_detail(raw: str) -> str:
    """Client-facing detail for an ffmpeg failure. Public mode hides ffmpeg's
    stderr (which can carry paths/versions); the full text is always logged."""
    return "media processing failed" if SETTINGS.public else f"ffmpeg failed: {raw}"


def _ffmpeg_timeout_detail() -> str:
    return (
        "media processing timed out"
        if SETTINGS.public
        else f"ffmpeg timed out after {_FFMPEG_TIMEOUT_SECONDS:.0f}s"
    )


def _snap_height(max_height: Optional[int]) -> Optional[int]:
    """Normalize a requested export height. ``None``/<=0 → best available. In
    public mode, snap to the nearest allowlisted height so ~4000 possible values
    collapse to ≤len(allowed) cache keys (F8); in dev, keep the old 144–4320
    clamp. video() and video_progress() must snap identically so their progress
    keys match."""
    if max_height is None or max_height <= 0:
        return None
    if SETTINGS.public:
        heights = SETTINGS.allowed_video_heights
        if heights and max_height not in heights:
            return min(heights, key=lambda h: abs(h - max_height))
        return max_height
    return max(144, min(4320, max_height))

# Block size (64 KiB) for streaming concatenated audio chunks to the client.
_STREAM_BLOCK_BYTES = 65536

# ffmpeg transcodes/muxes can run for a while on a long video; this ceiling only
# bounds a wedged subprocess so a corrupt input fails the request instead of
# hanging the worker. (ffprobe's shorter timeout lives with the probe helpers in
# pipeline/export.py.)
_FFMPEG_TIMEOUT_SECONDS = 3600.0


def _run_ffmpeg(cmd: list[str]) -> None:
    """Run an ffmpeg command, surfacing its stderr as a 500 on failure.

    Mirrors slice_source's error handling: capture stderr so a failure carries
    ffmpeg's actual message instead of a bare exit code.
    """
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=_FFMPEG_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired as exc:
        log.error("ffmpeg timed out after %.0fs", _FFMPEG_TIMEOUT_SECONDS)
        raise HTTPException(
            status_code=500, detail=_ffmpeg_timeout_detail()
        ) from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip() or "(no stderr)"
        log.error("ffmpeg failed (exit %d): %s", proc.returncode, detail)
        raise HTTPException(status_code=500, detail=_ffmpeg_fail_detail(detail))


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
        # Bound the whole run with a watchdog: the stdout loop below blocks until
        # ffmpeg closes stdout, which a *wedged* encode never does — so a plain
        # ``proc.wait(timeout=…)`` after the loop can't bound that hang because we
        # never reach it. The timer kills the process at the ceiling, which closes
        # stdout and ends the loop, so the request fails instead of pinning the
        # worker forever.
        timed_out = threading.Event()

        def _kill_on_timeout() -> None:
            timed_out.set()
            proc.kill()

        watchdog = threading.Timer(_FFMPEG_TIMEOUT_SECONDS, _kill_on_timeout)
        watchdog.daemon = True
        watchdog.start()
        try:
            for line in proc.stdout:
                line = line.strip()
                # ffmpeg reports out_time_us in microseconds (the older out_time_ms
                # key is also microseconds despite its name — we read out_time_us).
                if line.startswith("out_time_us=") and total_seconds > 0:
                    try:
                        done = int(line.split("=", 1)[1]) / 1e6 / total_seconds
                        on_pct(max(0.0, min(1.0, done)))
                    except ValueError:
                        # Progress is cosmetic; a malformed line just skips one tick.
                        log.debug("unparseable ffmpeg progress line: %r", line)
            proc.wait()
        finally:
            watchdog.cancel()
        if timed_out.is_set():
            log.error("ffmpeg timed out after %.0fs", _FFMPEG_TIMEOUT_SECONDS)
            raise HTTPException(status_code=500, detail=_ffmpeg_timeout_detail())
        if proc.returncode != 0:
            errf.seek(0)
            detail = errf.read().decode("utf-8", "replace").strip() or "(no stderr)"
            log.error("ffmpeg failed (exit %d): %s", proc.returncode, detail)
            raise HTTPException(status_code=500, detail=_ffmpeg_fail_detail(detail))


# --- MP4 export progress (polled by the extension while it prepares a video) --
class _ExportProgress:
    """Thread-safe map of in-flight MP4-export progress.

    Keyed by ``"<job_id>:<max_height or 0>"``; each value is
    ``{"phase": str, "percent": 0..100}``. Co-locating the dict with its lock
    (rather than leaving both at module scope) keeps the synchronization
    invariant in one place. Exports are rare and short-lived, so the map never
    grows unbounded.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_key: dict[str, JsonDict] = {}

    @staticmethod
    def key(job_id: str, max_height: Optional[int]) -> str:
        return f"{job_id}:{max_height or 0}"

    def set(self, key: str, phase: str, percent: float) -> None:
        with self._lock:
            self._by_key[key] = {"phase": phase, "percent": round(percent, 1)}

    def clear(self, key: str) -> None:
        with self._lock:
            self._by_key.pop(key, None)

    def get(self, key: str) -> JsonDict:
        """Snapshot for ``key``, or the ``idle`` sentinel when none is in flight."""
        with self._lock:
            return self._by_key.get(key, {"phase": "idle", "percent": 0})


_export_progress = _ExportProgress()


@router.get("/chunk/{job_id}/{chunk_idx}", dependencies=[Depends(require_edge)])
def chunk(job_id: JobId, chunk_idx: ChunkIdx, request: Request) -> FileResponse:
    cache = request.app.state.cache
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


@router.get("/audio/{job_id}", dependencies=[Depends(require_edge)])
def audio(job_id: JobId, request: Request, format: str = "opus") -> Response:
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
    cache = request.app.state.cache
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
            out = tmp_dir / "full.mp3"
            # Bound concurrent transcodes (F9) so they can't monopolize the
            # threadpool and starve status/chunk requests. No-op in dev.
            with audio_transcode_gate.slot():
                _run_ffmpeg(mp3_transcode_cmd(chunk_files, out))
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
                    block = f.read(_STREAM_BLOCK_BYTES)
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


@router.get(
    "/video/{job_id}",
    dependencies=[Depends(require_edge), Depends(rate_limit(video_rl))],
)
def video(job_id: JobId, request: Request, max_height: Optional[int] = None) -> Response:
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
    cache = request.app.state.cache
    # Best-available, or (public) snapped to an allowlisted height so the
    # per-height video cache can't be blown up into thousands of keys (F8).
    max_height = _snap_height(max_height)

    meta = cache.load_meta(job_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="unknown job_id")
    if not meta.complete:
        raise HTTPException(status_code=425, detail="full audio not ready")

    # Re-validate the STORED url on this delayed outbound fetch (F12/F27): the
    # submit-time SSRF gate may predate a policy tightening, and /video is the
    # one place a cached job triggers a fresh download.
    try:
        validate_public_url(meta.url)
    except ValueError:
        # ``UrlNotAllowed`` (policy reject) subclasses ValueError, but
        # ``validate_public_url`` also raises a bare ValueError when the host
        # can't be resolved (DNS failure); both map to a 404 here rather than
        # escaping to the generic 500 handler.
        raise HTTPException(status_code=404)

    chunk_files = snapshot_chunk_files(cache, job_id, meta.total_chunks)
    if not chunk_files:
        raise HTTPException(status_code=425, detail="full audio not ready")

    progress_key = _export_progress.key(job_id, max_height)
    # Bound concurrent heavy exports (F8/F9/F12): cap how many download+mux ops
    # run at once so they can't starve the threadpool. 503s when full; no-op in
    # dev. Held across the whole build, released before the file is streamed.
    with video_export_gate.slot():
        _export_progress.set(progress_key, "downloading", 0.0)
        tmp_dir: Optional[Path] = None
        try:
            # --- Phase 1: fetch the video stream (cached per url+resolution) ---
            def _dl_hook(d: dict[str, object]) -> None:
                if d.get("status") == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate")
                    got = d.get("downloaded_bytes")
                    if total and got is not None:
                        _export_progress.set(
                            progress_key, "downloading", 100.0 * got / total
                        )
                elif d.get("status") == "finished":
                    _export_progress.set(progress_key, "downloading", 100.0)

            try:
                video_path = downloader.download_video(
                    meta.url,
                    cache.video_dir(meta.url, max_height),
                    max_height=max_height,
                    progress_hook=_dl_hook,
                )
            except Exception as exc:
                # yt-dlp failures are the user's URL going stale / network
                # issues, not a server bug — surface them as a 502 with a generic
                # message (F20); the real cause is logged.
                log.warning("video download failed for %s", job_id, exc_info=True)
                raise HTTPException(status_code=502, detail="video download failed")

            # --- Phase 2: mux the stripped audio over the video ---
            _export_progress.set(progress_key, "encoding", 0.0)
            tmp_dir = Path(tempfile.mkdtemp(prefix="nomusic-mp4-"))
            out = tmp_dir / "full.mp4"
            total_seconds = video_duration(video_path)

            def _enc_pct(frac: float) -> None:
                _export_progress.set(progress_key, "encoding", 100.0 * frac)

            # Copy H.264/HEVC straight through (fast, lossless); re-encode
            # VP9/AV1 to H.264 so the MP4 plays in QuickTime/Safari too.
            reencode = video_codec(video_path) not in MP4_COPYABLE_VCODECS
            try:
                _run_ffmpeg_progress(
                    mux_video_cmd(video_path, chunk_files, out, reencode_video=reencode),
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
                _export_progress.set(progress_key, "encoding", 0.0)
                _run_ffmpeg_progress(
                    mux_video_cmd(video_path, chunk_files, out, reencode_video=True),
                    total_seconds, _enc_pct,
                )
        except BaseException:
            _export_progress.clear(progress_key)
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

    _export_progress.set(progress_key, "done", 100.0)

    def _cleanup() -> None:
        _export_progress.clear(progress_key)
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return FileResponse(
        str(out),
        media_type="video/mp4",
        headers={"Cache-Control": "no-store"},
        background=BackgroundTask(_cleanup),
    )


@router.get("/video/{job_id}/progress", dependencies=[Depends(require_edge)])
def video_progress(job_id: JobId, max_height: Optional[int] = None) -> JsonDict:
    """Current MP4-export progress for the extension's download menu.

    Returns ``{"phase": "downloading"|"encoding"|"done"|"idle", "percent":
    0..100}``. ``idle`` means no export is in flight for this (job,
    resolution) — the extension stops polling once its download fetch
    resolves, so a stale entry never lingers. The height is snapped exactly as
    in :func:`video` so the progress key matches."""
    return _export_progress.get(_export_progress.key(job_id, _snap_height(max_height)))
