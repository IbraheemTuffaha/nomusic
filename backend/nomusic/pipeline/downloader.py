"""Audio acquisition via yt-dlp + ffmpeg.

We never touch yt-dlp's CLI; we use it as a Python library so the
"give me a WAV at this path" contract stays stable across CLI changes
upstream.

Public surfaces:

* :func:`probe`           - lightweight metadata fetch (title, duration, id).
* :func:`download_source` - pulls bestaudio for the *whole* video to a single
  compressed file (m4a / webm / opus). Idempotent; reuses an existing file.
* :func:`slice_source`    - cuts a precise [start, end) range out of a source
  file into a 44.1 kHz stereo WAV using ffmpeg.

We deliberately do *not* expose a "download just this range" function. yt-dlp's
``download_ranges`` cuts at the nearest preceding keyframe in the compressed
source, which can shift the start by 5-10 s for AAC/Opus streams — fine for
video previews, fatal for sample-accurate audio sync. The download-once-and-
slice approach gives sample-accurate cuts and is faster overall because it
avoids the per-chunk yt-dlp / JS-challenge overhead.
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from nomusic.runtime import javascript_runtime

log = logging.getLogger(__name__)


class DownloadCancelled(Exception):
    """Raised to unwind a download that's being cancelled — from the yt-dlp
    progress hook (which aborts the running download) and from the progressive
    source's per-chunk wait. Distinct from a real download error so callers can
    tell an intentional abort from a failure: the retry-clean path in
    ``SourceFetcher.download`` re-raises it instead of restarting the download.
    """


class ResourceLimitExceeded(RuntimeError):
    """Input or output exceeds the configured finite local-use policy."""


@dataclass(frozen=True)
class ResourceLimits:
    max_duration_seconds: float = 7200.0
    max_source_bytes: int = 512 * 1024 * 1024
    max_video_bytes: int = 2 * 1024 * 1024 * 1024
    max_video_height: int = 1080
    max_decode_bytes: int = 64 * 1024 * 1024
    max_chunk_bytes: int = 16 * 1024 * 1024
    max_inference_batch: int = 2
    max_prefetch_chunks: int = 2
    final_chunk_tolerance_seconds: float = 1.0


def limits_from_settings(settings: Any) -> ResourceLimits:
    return ResourceLimits(
        max_duration_seconds=settings.max_duration_seconds,
        max_source_bytes=settings.max_source_bytes,
        max_video_bytes=settings.max_video_bytes,
        max_video_height=settings.max_video_height,
        max_decode_bytes=settings.max_decode_bytes,
        max_chunk_bytes=settings.max_chunk_bytes,
        max_inference_batch=settings.max_inference_batch,
        max_prefetch_chunks=settings.max_prefetch_chunks,
        final_chunk_tolerance_seconds=settings.final_chunk_tolerance_seconds,
    )


# yt-dlp progress-hook callback. yt-dlp invokes it with a status dict whose keys
# vary by phase — at minimum ``status`` ("downloading"/"finished"), plus
# ``downloaded_bytes``, ``total_bytes`` / ``total_bytes_estimate``, ``speed``,
# ``eta`` while downloading. We forward it verbatim; the values are
# heterogeneous, hence the loose value type.
ProgressHook = Callable[[dict[str, Any]], None]

# Hard ceiling for a single ffmpeg slice. A slice decodes at most one chunk's
# download window (tens of seconds of audio), so anything beyond this means
# ffmpeg has wedged on a corrupt/partial container rather than doing real work —
# kill it and let the caller surface the failure instead of hanging the worker.
_FFMPEG_SLICE_TIMEOUT_SECONDS = 300.0

# WAV is large but lossless and trivial to load with soundfile. We're operating
# on short ranges (~30 s) so the size is fine, and any other codec would force
# us to round-trip through ffmpeg twice (download -> re-encode -> decode again).
_TARGET_SAMPLE_RATE = 44100
_TARGET_CHANNELS = 2

# Byte-size multipliers for the K/M suffixes in NOMUSIC_DOWNLOAD_RATELIMIT.
_BYTES_PER_KB = 1024
_BYTES_PER_MB = _BYTES_PER_KB * _BYTES_PER_KB


def _emit_finished_progress(progress_hook: ProgressHook | None, size_bytes: int) -> None:
    """Synthesize a yt-dlp ``finished`` progress event for a cache hit, so a
    caller driving a UI bar jumps to 100% without special-casing the no-download
    path. Best-effort: a hook that raises must not break the (already-complete)
    fetch."""
    if not progress_hook:
        return
    try:
        progress_hook(
            {"status": "finished", "downloaded_bytes": size_bytes, "total_bytes": size_bytes}
        )
    except Exception:  # never let a UI hook break the pipeline
        log.debug("cache-hit progress hook raised", exc_info=True)


def _common_opts() -> dict[str, Any]:
    """Options shared by ``probe`` and the source/video download helpers.

    Use the packaged EJS challenge solver with a supported Node/Deno runtime.
    No solver code is downloaded from a moving remote release at job time.
    """
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "remote_components": [],
    }

    runtime = javascript_runtime()
    opts["js_runtimes"] = {runtime.name: {"path": runtime.path}}
    return opts


def _download_ratelimit() -> float | None:
    """Optional artificial download cap (bytes/sec) for testing slow links.

    ``NOMUSIC_DOWNLOAD_RATELIMIT`` accepts a raw byte/sec number or a
    ``K``/``M`` suffix (e.g. ``200K``, ``1.5M``). Unset/invalid → no cap. Maps
    straight to yt-dlp's ``ratelimit``."""
    raw = os.environ.get("NOMUSIC_DOWNLOAD_RATELIMIT")
    if not raw:
        return None
    raw = raw.strip().upper()
    mult = 1
    if raw.endswith("K"):
        mult, raw = _BYTES_PER_KB, raw[:-1]
    elif raw.endswith("M"):
        mult, raw = _BYTES_PER_MB, raw[:-1]
    try:
        return float(raw) * mult
    except ValueError:
        return None


@dataclass(frozen=True)
class VideoMetadata:
    id: str
    title: str
    duration_seconds: float
    extractor: str
    webpage_url: str
    width: int | None = None
    height: int | None = None
    filesize: int | None = None


def _metadata_from_info(info: dict[str, Any], url: str) -> VideoMetadata:
    duration = info.get("duration")
    if duration is None:
        raise RuntimeError(
            f"yt-dlp could not determine duration for {url}; "
            "live streams and unbounded media are not supported yet."
        )
    duration = float(duration)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"yt-dlp returned an invalid duration {duration!r} for {url}")
    def _positive_int(value: Any) -> int | None:
        try:
            value = int(value)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None
    return VideoMetadata(
        id=str(info.get("id", "unknown")),
        title=str(info.get("title", "untitled")),
        duration_seconds=duration,
        extractor=str(info.get("extractor", "unknown")),
        webpage_url=str(info.get("webpage_url", url)),
        width=_positive_int(info.get("width")),
        height=_positive_int(info.get("height")),
        filesize=_positive_int(info.get("filesize") or info.get("filesize_approx")),
    )


def validate_metadata(info: VideoMetadata, limits: ResourceLimits | None) -> VideoMetadata:
    if limits is None:
        return info
    if not math.isfinite(info.duration_seconds) or info.duration_seconds <= 0:
        raise ResourceLimitExceeded("source duration must be finite and greater than zero")
    if info.duration_seconds > limits.max_duration_seconds:
        raise ResourceLimitExceeded(
            f"source duration {info.duration_seconds:.1f}s exceeds the "
            f"{limits.max_duration_seconds:.1f}s limit"
        )
    if info.filesize is not None and info.filesize > limits.max_source_bytes:
        raise ResourceLimitExceeded(
            f"source estimate {info.filesize} bytes exceeds the "
            f"{limits.max_source_bytes} byte limit"
        )
    return info


def probe(url: str, *, limits: ResourceLimits | None = None) -> VideoMetadata:
    """Fetch metadata without downloading the media."""
    from yt_dlp import YoutubeDL  # imported lazily; yt-dlp is heavy

    opts = {**_common_opts(), "skip_download": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if info is None:
        raise RuntimeError(f"yt-dlp returned no metadata for {url}")
    # Playlists: take the first entry.
    if "entries" in info and info["entries"]:
        info = info["entries"][0]

    return validate_metadata(_metadata_from_info(info, url), limits)


_SOURCE_STEM = "source"
# Extensions yt-dlp may emit for bestaudio across the sites we support. Order
# doesn't matter — we glob, find one, use it.
_SOURCE_EXTS: tuple[str, ...] = (
    "m4a",
    "webm",
    "opus",
    "ogg",
    "mp3",
    "aac",
    "mp4",
    "wav",
)


def download_source(
    url: str,
    out_dir: Path,
    *,
    progress_hook: ProgressHook | None = None,
    limits: ResourceLimits | None = None,
) -> Path:
    """Download the entire bestaudio stream for ``url`` into ``out_dir``.

    Returns the path to the downloaded file. Idempotent: if a previously-
    downloaded source file is already present, it's returned as-is.

    ``progress_hook`` is forwarded to yt-dlp's progress hooks; see yt-dlp
    docs for the dict shape (``status``, ``downloaded_bytes``,
    ``total_bytes``, ``total_bytes_estimate``, ``speed``, ``eta``).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = _find_source(out_dir)
    if existing is not None:
        if limits is not None:
            try:
                _validate_file_size(existing, limits.max_source_bytes, "source")
            except ResourceLimitExceeded:
                _remove_media_outputs(out_dir, _SOURCE_STEM, _SOURCE_EXTS)
                raise
        log.info("Using cached source audio: %s", existing.name)
        _emit_finished_progress(progress_hook, existing.stat().st_size)
        return existing

    from yt_dlp import YoutubeDL

    opts = (
        _source_download_opts(out_dir)
        if limits is None
        else _source_download_opts(out_dir, limits=limits)
    )
    if progress_hook:
        opts["progress_hooks"] = [
            lambda event: _guard_download_progress(event, progress_hook, limits, "source")
        ]
    log.info("Downloading source audio for %s -> %s", url, out_dir)
    with YoutubeDL(opts) as ydl:
        ydl.download([url])

    final = _find_source(out_dir)
    if final is None:
        raise RuntimeError(
            f"yt-dlp didn't produce a source file in {out_dir}; "
            "supported extensions: " + ", ".join(_SOURCE_EXTS)
        )
    try:
        _validate_file_size(final, limits.max_source_bytes if limits else None, "source")
    except BaseException:
        _remove_media_outputs(out_dir, _SOURCE_STEM, _SOURCE_EXTS)
        raise
    return final


def _guard_download_progress(
    event: dict[str, Any],
    hook: ProgressHook,
    limits: ResourceLimits | None,
    kind: str,
) -> None:
    if limits is not None:
        limit = limits.max_source_bytes if kind == "source" else limits.max_video_bytes
        for field in ("downloaded_bytes", "total_bytes", "total_bytes_estimate"):
            observed = event.get(field)
            try:
                observed = int(observed) if observed is not None else None
            except (TypeError, ValueError):
                observed = None
            if observed is not None and observed > limit:
                raise ResourceLimitExceeded(
                    f"{kind} download exceeded the {limit} byte limit"
                )
    hook(event)


def _validate_file_size(path: Path, limit: int | None, kind: str) -> None:
    if not path.exists() or path.stat().st_size <= 0:
        raise RuntimeError(f"{kind} file is missing or empty: {path}")
    if limit is not None and path.stat().st_size > limit:
        raise ResourceLimitExceeded(
            f"{kind} file is {path.stat().st_size} bytes; limit is {limit} bytes"
        )


def _remove_media_outputs(out_dir: Path, stem: str, extensions: tuple[str, ...]) -> None:
    for ext in (*extensions, "part"):
        for path in out_dir.glob(f"{stem}*.{ext}"):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


_VIDEO_STEM = "video"
# Extensions yt-dlp may emit for a video stream across the sites we support.
_VIDEO_EXTS: tuple[str, ...] = ("mp4", "webm", "mkv", "mov", "flv")


def _video_format(max_height: int | None) -> str:
    """Build the yt-dlp format selector for the MP4 export.

    Pick the highest-resolution video-only stream up to ``max_height``,
    regardless of codec, then fall back to anything. We deliberately do NOT
    filter by codec here: on YouTube, H.264 (avc1) tops out at 1080p while
    1440p/4K exist only as VP9/AV1, so an ``avc1``-first selector would silently
    cap every request at 1080p. Codec preference (H.264 for a clean MP4
    stream-copy) is handled by ``_VIDEO_FORMAT_SORT`` *within* a resolution, so
    1080p still comes out as copyable H.264 while higher resolutions take the
    VP9/AV1 stream (which the mux copies, or re-encodes as a fallback).
    ``max_height`` caps the resolution (e.g. 1080); ``None`` takes the best.
    """
    # Capped: try the height-limited video-only stream, then any video-only
    # stream, then anything. Uncapped: the first two would be identical, so
    # collapse to a single bestvideo before the catch-all.
    if max_height:
        return f"bestvideo[height<={max_height}]/bestvideo/best"
    return "bestvideo/best"


# Sort priority: highest resolution first, then prefer H.264 among equal-res
# streams (so the MP4 export can stream-copy without re-encoding when possible).
# User format_sort fields take precedence over yt-dlp's defaults.
_VIDEO_FORMAT_SORT = ["res", "vcodec:h264"]


def download_video(
    url: str,
    out_dir: Path,
    *,
    max_height: int | None = None,
    progress_hook: ProgressHook | None = None,
    limits: ResourceLimits | None = None,
) -> Path:
    """Download the video stream for ``url`` into ``out_dir`` for the MP4 export.

    The normal pipeline fetches audio only, so this is a separate, on-demand
    pull. We grab a video-only stream when one exists (we replace the audio at
    mux time anyway) and fall back to a progressive video+audio stream.
    ``max_height`` caps the resolution (e.g. 1080); ``None`` takes the best
    available.

    Returns the path to the downloaded file. Idempotent: a previously-downloaded
    video file already present is returned as-is.
    """
    if limits is not None and max_height is not None and max_height > limits.max_video_height:
        raise ResourceLimitExceeded(
            f"requested video height {max_height} exceeds the "
            f"{limits.max_video_height}p limit"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = _find_video(out_dir)
    if existing is not None:
        try:
            _validate_file_size(
                existing, limits.max_video_bytes if limits else None, "video"
            )
            _validate_video_dimensions(existing, max_height, limits)
        except ResourceLimitExceeded:
            _remove_media_outputs(out_dir, _VIDEO_STEM, _VIDEO_EXTS)
            raise
        log.info("Using cached video: %s", existing.name)
        _emit_finished_progress(progress_hook, existing.stat().st_size)
        return existing

    from yt_dlp import YoutubeDL

    effective_height = max_height
    if limits is not None:
        effective_height = min(
            limits.max_video_height,
            max_height if max_height else limits.max_video_height,
        )
    opts: dict[str, Any] = {
        **_common_opts(),
        "format": _video_format(effective_height),
        "format_sort": _VIDEO_FORMAT_SORT,
        "outtmpl": str(out_dir / f"{_VIDEO_STEM}.%(ext)s"),
        "overwrites": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
    }
    if limits is not None:
        opts["max_filesize"] = limits.max_video_bytes
    if progress_hook:
        opts["progress_hooks"] = [
            lambda event: _guard_download_progress(event, progress_hook, limits, "video")
        ]
    ratelimit = _download_ratelimit()
    if ratelimit:
        opts["ratelimit"] = ratelimit
    log.info("Downloading video for %s -> %s", url, out_dir)
    with YoutubeDL(opts) as ydl:
        ydl.download([url])

    final = _find_video(out_dir)
    if final is None:
        raise RuntimeError(
            f"yt-dlp didn't produce a video file in {out_dir}; "
            "supported extensions: " + ", ".join(_VIDEO_EXTS)
        )
    try:
        _validate_file_size(final, limits.max_video_bytes if limits else None, "video")
        _validate_video_dimensions(final, max_height, limits)
    except BaseException:
        _remove_media_outputs(out_dir, _VIDEO_STEM, _VIDEO_EXTS)
        raise
    return final


def _validate_video_dimensions(
    path: Path, requested_height: int | None, limits: ResourceLimits | None
) -> None:
    if limits is None and requested_height is None:
        return
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        import json
        data = json.loads(probe.stdout or "{}")
        stream = (data.get("streams") or [None])[0] or {}
        height = int(stream.get("height"))
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError, IndexError):
        raise ResourceLimitExceeded("video dimensions could not be validated")
    cap = limits.max_video_height if limits is not None else requested_height
    if cap is not None and height > cap:
        raise ResourceLimitExceeded(f"video height {height}p exceeds the {cap}p limit")


def validate_source_file(
    path: Path, expected_duration: float, limits: ResourceLimits | None
) -> None:
    """Validate a completed audio source before it becomes reusable cache."""
    if limits is None:
        return
    _validate_file_size(path, limits.max_source_bytes, "source")
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        actual = float(probe.stdout.strip())
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise ResourceLimitExceeded("source duration could not be validated")
    if not math.isfinite(actual) or actual <= 0:
        raise ResourceLimitExceeded("source duration is invalid")
    if actual + limits.final_chunk_tolerance_seconds < expected_duration:
        raise ResourceLimitExceeded(
            f"downloaded source is {actual:.3f}s but metadata requires "
            f"{expected_duration:.3f}s"
        )


def _find_video(out_dir: Path) -> Path | None:
    for ext in _VIDEO_EXTS:
        p = out_dir / f"{_VIDEO_STEM}.{ext}"
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def _source_download_opts(
    out_dir: Path, *, limits: ResourceLimits | None = None
) -> dict[str, Any]:
    """yt-dlp options for pulling the source audio (shared by download_source
    and SourceFetcher), minus the per-call progress hook."""
    opts: dict[str, Any] = {
        **_common_opts(),
        # Cap at ~128 kbps audio: it's effectively transparent for source
        # separation (demucs works on the decoded waveform) and trims download
        # time on long videos / slow links. Falls back to the best available
        # audio, then to best overall, for sources without a 128k rendition.
        "format": "bestaudio[abr<=128]/bestaudio/best",
        "outtmpl": str(out_dir / f"{_SOURCE_STEM}.%(ext)s"),
        "overwrites": True,
        # Self-heal transient network hiccups instead of failing the whole job
        # on a single timeout. yt-dlp retries the request/fragments internally;
        # socket_timeout bounds a stalled read.
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
    }
    ratelimit = _download_ratelimit()
    if ratelimit:
        log.info("Throttling download to %.0f bytes/sec (test mode)", ratelimit)
        opts["ratelimit"] = ratelimit
    if limits is not None:
        # yt-dlp can reject a known oversized format before writing bytes. The
        # progress wrapper below remains necessary for unknown/approximate
        # sizes and for cached/fallback paths.
        opts["max_filesize"] = limits.max_source_bytes
    return opts


class SourceFetcher:
    """One yt-dlp session that extracts metadata once and then downloads from
    the *same* session.

    The naive optimization — extract in :func:`probe`, then reuse that info in a
    separate download session — fails on YouTube with HTTP 403, because the
    media URLs are bound to the extracting session (signature / po_token). Doing
    both in one session avoids the second JS-challenge extraction *and* the 403.

    Usage::

        f = SourceFetcher(url, out_dir)
        meta = f.extract()           # one extraction; metadata for planning
        path = f.download(hook)      # download from the same session

    ``download`` falls back to a clean :func:`download_source` if the same-
    session download raises, so the optimization can never fail a job outright.
    If prepared work is abandoned before downloading, call ``close`` to release
    the session. Extraction failures and all download exits close it themselves.
    """

    def __init__(
        self, url: str, out_dir: Path, *, limits: ResourceLimits | None = None
    ) -> None:
        self.url = url
        self.out_dir = out_dir
        self.limits = limits
        self._ydl = None
        self._info: dict | None = None
        self._cached: Path | None = None

    def extract(self) -> VideoMetadata:
        from yt_dlp import YoutubeDL

        # Reusing the fetcher starts a new session; do not lose ownership of a
        # previous one or retain metadata from a failed extraction.
        self.close()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        options = (
            _source_download_opts(self.out_dir)
            if self.limits is None
            else _source_download_opts(self.out_dir, limits=self.limits)
        )
        self._ydl = YoutubeDL(options)
        try:
            t0 = time.monotonic()
            info = self._ydl.extract_info(self.url, download=False)
            log.info("SourceFetcher: metadata extracted in %.1fs", time.monotonic() - t0)
            if info is None:
                raise RuntimeError(f"yt-dlp returned no metadata for {self.url}")
            if "entries" in info and info["entries"]:
                info = info["entries"][0]
            duration = info.get("duration")
            if duration is None:
                raise RuntimeError(
                    f"yt-dlp could not determine duration for {self.url}; "
                    "live streams and unbounded media are not supported yet."
                )
            self._info = info
            # If the source is already on disk, download short-circuits.
            self._cached = _find_source(self.out_dir)
            return validate_metadata(_metadata_from_info(info, self.url), self.limits)
        except BaseException:
            self.close()
            raise

    def download(self, progress_hook: ProgressHook | None = None) -> Path:
        try:
            if self._cached is not None:
                if self.limits is not None:
                    _validate_file_size(self._cached, self.limits.max_source_bytes, "source")
                log.info("Using cached source audio: %s", self._cached.name)
                _emit_finished_progress(progress_hook, self._cached.stat().st_size)
                return self._cached

            if self._ydl is None or self._info is None:
                # extract() wasn't called — just do a clean run.
                if self.limits is None:
                    return download_source(
                        self.url, self.out_dir, progress_hook=progress_hook
                    )
                return download_source(
                    self.url, self.out_dir,
                    progress_hook=progress_hook,
                    limits=self.limits,
                )

            log.info("Downloading source audio for %s -> %s", self.url, self.out_dir)
            t0 = time.monotonic()
            try:
                if progress_hook:
                    self._ydl.add_progress_hook(
                        lambda event: _guard_download_progress(
                            event, progress_hook, self.limits, "source"
                        )
                    )
                # Keep extraction and download in the same session to retain
                # the credentials associated with the extracted media URLs.
                self._ydl.process_ie_result(self._info, download=True)
                log.info(
                    "SourceFetcher: same-session download OK (no re-extract), %.1fs",
                    time.monotonic() - t0,
                )
            except DownloadCancelled:
                # Never restart a download the caller is deliberately stopping.
                raise
            except Exception:
                log.warning(
                    "SourceFetcher: same-session download failed; retrying clean",
                    exc_info=True,
                )
                self.close()
                if self.limits is None:
                    return download_source(
                        self.url, self.out_dir, progress_hook=progress_hook
                    )
                return download_source(
                    self.url, self.out_dir,
                    progress_hook=progress_hook,
                    limits=self.limits,
                )

            final = _find_source(self.out_dir)
            if final is None:
                raise RuntimeError(
                    f"yt-dlp didn't produce a source file in {self.out_dir}; "
                    "supported extensions: " + ", ".join(_SOURCE_EXTS)
                )
            if self.limits is not None:
                _validate_file_size(final, self.limits.max_source_bytes, "source")
            return final
        finally:
            self.close()

    def close(self) -> None:
        """Release the session after downloading or abandoning prepared work.

        Idempotent. A later ``extract`` starts a fresh reusable session. The
        caller must wait for any background download before calling this.
        """
        ydl, self._ydl = self._ydl, None
        self._info = None
        self._cached = None
        if ydl is not None:
            try:
                ydl.close()
            except Exception:  # closing is best-effort; a failure here is benign
                log.debug("SourceFetcher: ydl.close() raised", exc_info=True)


def slice_source(
    source: Path,
    out_path: Path,
    *,
    start: float,
    end: float,
    pass_fds: tuple[int, ...] = (),
    limits: ResourceLimits | None = None,
) -> Path:
    """Cut ``[start, end)`` seconds of ``source`` into a 44.1 kHz stereo WAV.

    Uses ffmpeg with output seek (``-ss`` after ``-i``) for sample-accurate
    cuts — slightly slower than input seek but the only way to avoid the
    keyframe-alignment drift we get from container-level partial decodes.
    """
    if end <= start:
        raise ValueError(f"slice_source: end ({end}) must be > start ({start})")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    duration = end - start
    if limits is not None:
        expected = int(math.ceil(duration * _TARGET_SAMPLE_RATE * _TARGET_CHANNELS * 4))
        if expected > limits.max_decode_bytes:
            raise ResourceLimitExceeded(
                f"decoded chunk requires about {expected} bytes; "
                f"limit is {limits.max_decode_bytes} bytes"
            )
    tmp_path = out_path.with_suffix(".part")

    cmd = [
        "ffmpeg",
        "-y",
        "-nostdin",
        "-loglevel",
        "error",
        # Output seek for accuracy: ffmpeg fully decodes from the start of the
        # source and discards samples until ``start``. For ranges deep inside a
        # 3h video this would be slow, so we combine fast input seek (-ss
        # before -i) with -accurate_seek so the demuxer lands at the right
        # packet, then re-seek precisely on the decoded output.
        "-accurate_seek",
        "-ss",
        f"{max(0.0, start - 0.5):.3f}",
        "-i",
        str(source),
        "-ss",
        f"{min(0.5, start):.3f}",
        "-t",
        f"{duration:.3f}",
        "-vn",
        "-ar",
        str(_TARGET_SAMPLE_RATE),
        "-ac",
        str(_TARGET_CHANNELS),
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        str(tmp_path),
    ]
    # Capture stderr so a failure (e.g. a not-yet-decodable partial progressive
    # download) carries ffmpeg's actual error instead of leaking it to the
    # server's inherited stderr and logging a bare "return code 1". The timeout
    # stops a wedged ffmpeg (corrupt container) from hanging the worker forever.
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=_FFMPEG_SLICE_TIMEOUT_SECONDS, pass_fds=pass_fds
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"ffmpeg slice timed out after {_FFMPEG_SLICE_TIMEOUT_SECONDS:.0f}s"
        ) from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip() or "(no stderr)"
        raise RuntimeError(f"ffmpeg slice failed (exit {proc.returncode}): {detail}")
    if limits is not None:
        _validate_file_size(tmp_path, limits.max_decode_bytes, "decoded chunk")
    tmp_path.replace(out_path)
    return out_path


def _find_source(out_dir: Path) -> Path | None:
    for ext in _SOURCE_EXTS:
        p = out_dir / f"{_SOURCE_STEM}.{ext}"
        if p.exists() and p.stat().st_size > 0:
            return p
    return None
