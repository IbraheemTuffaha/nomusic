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

    template = str(out_dir / f"{_SOURCE_STEM}.%(ext)s")
    opts: dict[str, Any] = {
        **_common_opts(),
        # Cap at ~128 kbps audio: it's effectively transparent for source
        # separation (demucs works on the decoded waveform) and trims download
        # time on long videos / slow links. Falls back to the best available
        # audio, then to best overall, for sources without a 128k rendition.
        "format": "bestaudio[abr<=128]/bestaudio/best",
        "outtmpl": template,
        "overwrites": True,
        # Self-heal transient network hiccups instead of failing the whole job
        # on a single timeout (the worker would otherwise drop to ERROR and the
        # user would have to re-click). yt-dlp retries the request/fragments
        # internally; socket_timeout bounds a stalled read.
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
    }
    ratelimit = _download_ratelimit()
    if ratelimit:
        log.info("Throttling download to %.0f bytes/sec (test mode)", ratelimit)
        opts["ratelimit"] = ratelimit
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
    subprocess.run(cmd, check=True)
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
