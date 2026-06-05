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
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# WAV is large but lossless and trivial to load with soundfile. We're operating
# on short ranges (~30 s) so the size is fine, and any other codec would force
# us to round-trip through ffmpeg twice (download -> re-encode -> decode again).
_TARGET_SAMPLE_RATE = 44100
_TARGET_CHANNELS = 2


def _common_opts() -> dict[str, Any]:
    """Options shared by ``probe`` and ``download_range``.

    YouTube requires a JavaScript runtime + EJS challenge solver scripts for
    most videos (without them, extraction fails with the misleading "This
    video is not available" error). We auto-detect ``node`` / ``deno`` / ``bun``
    and pin to the first one found; ``NOMUSIC_JS_RUNTIME=/path/to/bin``
    overrides. If nothing is available we still try the request — many
    short-form videos work without it.
    """
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # Pull the EJS challenge-solver scripts that yt-dlp uses to defeat
        # YouTube's player JS. Hosted by the yt-dlp project.
        "remote_components": ["ejs:github"],
    }

    runtime_override = os.environ.get("NOMUSIC_JS_RUNTIME")
    if runtime_override:
        name = Path(runtime_override).name
        opts["js_runtimes"] = {name: {"path": runtime_override}}
        return opts

    for name in ("deno", "node", "bun"):
        path = shutil.which(name)
        if path:
            opts["js_runtimes"] = {name: {"path": path}}
            break
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
        mult, raw = 1024, raw[:-1]
    elif raw.endswith("M"):
        mult, raw = 1024 * 1024, raw[:-1]
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


def probe(url: str) -> VideoMetadata:
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

    duration = info.get("duration")
    if duration is None:
        raise RuntimeError(
            f"yt-dlp could not determine duration for {url}; "
            "live streams and unbounded media are not supported yet."
        )

    return VideoMetadata(
        id=str(info.get("id", "unknown")),
        title=str(info.get("title", "untitled")),
        duration_seconds=float(duration),
        extractor=str(info.get("extractor", "unknown")),
        webpage_url=str(info.get("webpage_url", url)),
    )


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
    progress_hook=None,
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
        log.info("Using cached source audio: %s", existing.name)
        if progress_hook:
            # Synthesize a "finished" event so callers driving a UI bar can
            # jump to 100% on a cache hit without special-casing.
            try:
                progress_hook(
                    {
                        "status": "finished",
                        "downloaded_bytes": existing.stat().st_size,
                        "total_bytes": existing.stat().st_size,
                    }
                )
            except Exception:  # never let a UI hook break the pipeline
                pass
        return existing

    from yt_dlp import YoutubeDL

    opts = _source_download_opts(out_dir)
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    log.info("Downloading source audio for %s -> %s", url, out_dir)
    with YoutubeDL(opts) as ydl:
        ydl.download([url])

    final = _find_source(out_dir)
    if final is None:
        raise RuntimeError(
            f"yt-dlp didn't produce a source file in {out_dir}; "
            "supported extensions: " + ", ".join(_SOURCE_EXTS)
        )
    return final


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
    progress_hook=None,
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
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = _find_video(out_dir)
    if existing is not None:
        log.info("Using cached video: %s", existing.name)
        if progress_hook:
            try:
                size = existing.stat().st_size
                progress_hook(
                    {"status": "finished", "downloaded_bytes": size, "total_bytes": size}
                )
            except Exception:  # never let a UI hook break the download
                pass
        return existing

    from yt_dlp import YoutubeDL

    opts: dict[str, Any] = {
        **_common_opts(),
        "format": _video_format(max_height),
        "format_sort": _VIDEO_FORMAT_SORT,
        "outtmpl": str(out_dir / f"{_VIDEO_STEM}.%(ext)s"),
        "overwrites": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
    }
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
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
    return final


def _find_video(out_dir: Path) -> Path | None:
    for ext in _VIDEO_EXTS:
        p = out_dir / f"{_VIDEO_STEM}.{ext}"
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def _source_download_opts(out_dir: Path) -> dict[str, Any]:
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
    """

    def __init__(self, url: str, out_dir: Path) -> None:
        self.url = url
        self.out_dir = out_dir
        self._ydl = None
        self._info: dict | None = None
        self._cached: Path | None = None

    def extract(self) -> VideoMetadata:
        from yt_dlp import YoutubeDL

        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._ydl = YoutubeDL(_source_download_opts(self.out_dir))
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
        # If the source is already on disk, the download step short-circuits.
        self._cached = _find_source(self.out_dir)
        return VideoMetadata(
            id=str(info.get("id", "unknown")),
            title=str(info.get("title", "untitled")),
            duration_seconds=float(duration),
            extractor=str(info.get("extractor", "unknown")),
            webpage_url=str(info.get("webpage_url", self.url)),
        )

    def download(self, progress_hook=None) -> Path:
        if self._cached is not None:
            log.info("Using cached source audio: %s", self._cached.name)
            if progress_hook:
                try:
                    size = self._cached.stat().st_size
                    progress_hook(
                        {"status": "finished", "downloaded_bytes": size, "total_bytes": size}
                    )
                except Exception:
                    pass
            self._close()
            return self._cached

        if self._ydl is None or self._info is None:
            # extract() wasn't called (shouldn't happen) — just do a clean run.
            return download_source(self.url, self.out_dir, progress_hook=progress_hook)

        log.info("Downloading source audio for %s -> %s", self.url, self.out_dir)
        t0 = time.monotonic()
        try:
            if progress_hook:
                self._ydl.add_progress_hook(progress_hook)
            # Same-session download of the already-extracted result: this is
            # exactly what extract_info(download=True) does internally, split
            # in two — no second extraction, no cross-session 403.
            self._ydl.process_ie_result(self._info, download=True)
            log.info(
                "SourceFetcher: same-session download OK (no re-extract), %.1fs",
                time.monotonic() - t0,
            )
        except Exception:
            log.warning(
                "SourceFetcher: same-session download failed; retrying clean",
                exc_info=True,
            )
            self._close()
            return download_source(self.url, self.out_dir, progress_hook=progress_hook)
        self._close()

        final = _find_source(self.out_dir)
        if final is None:
            raise RuntimeError(
                f"yt-dlp didn't produce a source file in {self.out_dir}; "
                "supported extensions: " + ", ".join(_SOURCE_EXTS)
            )
        return final

    def _close(self) -> None:
        if self._ydl is not None:
            try:
                self._ydl.close()
            except Exception:
                pass
            self._ydl = None


def slice_source(
    source: Path,
    out_path: Path,
    *,
    start: float,
    end: float,
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
    tmp_path = out_path.with_suffix(".part")

    import subprocess

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
    # server's inherited stderr and logging a bare "return code 1".
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip() or "(no stderr)"
        raise RuntimeError(f"ffmpeg slice failed (exit {proc.returncode}): {detail}")
    tmp_path.replace(out_path)
    return out_path


def _find_source(out_dir: Path) -> Path | None:
    for ext in _SOURCE_EXTS:
        p = out_dir / f"{_SOURCE_STEM}.{ext}"
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


# Back-compat shim. Old code (or external callers) may still import
# download_range; we redirect them through download_source + slice_source so
# the keyframe-drift bug can't sneak back in.
def download_range(
    url: str,
    out_path: Path,
    *,
    start: float,
    end: float,
) -> Path:
    source_dir = out_path.parent / "_source"
    source = download_source(url, source_dir)
    return slice_source(source, out_path, start=start, end=end)
