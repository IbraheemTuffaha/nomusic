"""Audio acquisition via yt-dlp.

We never touch yt-dlp's CLI; we use it as a Python library so we can keep the
ergonomic of "give me a WAV at this path" stable across CLI changes upstream.

The downloader exposes two public surfaces:

* :func:`probe`  - lightweight metadata fetch (title, duration, id) without
  pulling the media. Used to plan chunks before processing.
* :func:`download_range` - download a single time range to a deterministic WAV.

Time-range downloads are the workhorse: the processor calls this once per chunk
and the resulting WAVs are fed straight to the engine.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# WAV is large but lossless and trivial to load with soundfile. We're operating
# on short ranges (~30 s) so the size is fine, and any other codec would force
# us to round-trip through ffmpeg twice (download -> re-encode -> decode again).
_TARGET_SAMPLE_RATE = 44100
_TARGET_CHANNELS = 2


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

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
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


def download_range(
    url: str,
    out_path: Path,
    *,
    start: float,
    end: float,
) -> Path:
    """Download ``[start, end)`` seconds of ``url`` as a 44.1 kHz stereo WAV.

    ``out_path`` is the final WAV path. Intermediate files are written next to
    it with a ``.part`` prefix and cleaned up.
    """
    if end <= start:
        raise ValueError(f"download_range: end ({end}) must be > start ({start})")

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # yt-dlp writes the output template; we strip the extension and let yt-dlp
    # add the right one for the downloaded stream. The postprocessor then
    # re-encodes to WAV.
    template_stem = out_path.with_suffix("")

    from yt_dlp import YoutubeDL

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": f"{template_stem}.%(ext)s",
        "download_ranges": _download_ranges(start, end),
        "force_keyframes_at_cuts": False,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "wav",
                "preferredquality": "0",
            },
        ],
        # WAV postprocessor uses these:
        "postprocessor_args": {
            "ffmpegextractaudio": [
                "-ar", str(_TARGET_SAMPLE_RATE),
                "-ac", str(_TARGET_CHANNELS),
            ],
        },
        "overwrites": True,
    }

    log.info("Downloading %.1fs-%.1fs of %s", start, end, url)
    with YoutubeDL(opts) as ydl:
        ydl.download([url])

    final_wav = template_stem.with_suffix(".wav")
    if not final_wav.exists():
        raise RuntimeError(
            f"yt-dlp postprocessor did not produce {final_wav}; "
            "check that ffmpeg is installed."
        )
    if final_wav != out_path:
        final_wav.replace(out_path)
    return out_path


def _download_ranges(start: float, end: float):
    """Build the ``download_ranges`` callable yt-dlp expects.

    yt-dlp expects a callable that receives ``info_dict`` and ``ydl`` and yields
    dicts with ``start_time`` / ``end_time`` keys. Wrapping closures avoids
    string-format issues with the CLI's ``--download-sections`` syntax.
    """

    def _ranges(_info_dict, _ydl):  # noqa: ANN001 - yt-dlp's signature
        yield {"start_time": start, "end_time": end}

    return _ranges
