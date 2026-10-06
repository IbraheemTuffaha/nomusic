"""Helpers for assembling a finished job's chunks into a downloadable file.

These back the ``/audio?format=mp3`` and ``/video`` download endpoints. They're
kept out of ``server.py`` so they can be unit-tested without importing the
FastAPI app (which loads the separation engine at import time).

We join the per-chunk Opus files with ffmpeg's **concat filter** — one ``-i``
input per chunk, spliced in the filtergraph — not the concat *demuxer* (a list
file) and not raw byte concatenation. The distinction matters for A/V sync:

Each chunk is an independently-encoded Ogg/Opus file. Opus carries a fixed
encoder pre-skip (~6.5 ms of priming) and pads its final packet to a 20 ms
frame boundary; a file's header/granule positions let a decoder discard both so
a *single* file round-trips to its true length. The concat demuxer joins at the
packet/timestamp level, so those per-file priming/padding samples are NOT
trimmed at each boundary — every chunk lands a few ms long and the error
*accumulates*, dragging the audio progressively behind the video (negligible at
the start, a second or more by the end of a long video). Live playback hides
this because the extension schedules every chunk at an absolute position derived
from the video clock, re-syncing on each one; the export has no such anchor.

The concat *filter* decodes each input independently first, so each file's
pre-skip/end-padding is applied per-file and the splice is sample-accurate — no
per-boundary drift. The cost is one ffmpeg input per chunk (fine for the chunk
counts real videos produce).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# ffprobe is a metadata read; a sub-second job in practice. The ceiling only
# trips if ffprobe wedges, in which case the caller degrades gracefully.
_FFPROBE_TIMEOUT_SECONDS = 60.0

# Codecs QuickTime/Safari can play inside an MP4 — these the mux stream-copies.
# Any other video codec (VP9/AV1, which YouTube uses above 1080p) is re-encoded
# to H.264 so the exported MP4 plays everywhere, not just in VLC/Chrome.
MP4_COPYABLE_VCODECS = frozenset({"h264", "hevc"})


def video_codec(path: Path) -> str:
    """Return the first video stream's codec name via ffprobe ("" on failure)."""
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=_FFPROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        log.warning("ffprobe (codec) timed out for %s", path)
        return ""
    return proc.stdout.strip()


def video_duration(path: Path) -> float:
    """Best-effort duration (seconds) of ``path``'s video stream, 0.0 on failure.

    Used as the denominator for the ffmpeg encode-progress percentage. Falls
    back to the container duration when the stream doesn't advertise one."""
    for args in (
        ["-select_streams", "v:0", "-show_entries", "stream=duration"],
        ["-show_entries", "format=duration"],
    ):
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", *args, "-of", "default=nw=1:nk=1", str(path)],
                capture_output=True, text=True, timeout=_FFPROBE_TIMEOUT_SECONDS,
            ).stdout.strip()
        except subprocess.TimeoutExpired:
            log.warning("ffprobe (duration) timed out for %s", path)
            continue
        try:
            if out and out != "N/A":
                return float(out)
        except ValueError:
            # Non-numeric output: try the next probe form, then fall back to 0.0.
            log.debug("ffprobe returned non-numeric duration %r for %s", out, path)
    return 0.0


def snapshot_chunk_files(cache, job_id: str, total_chunks: int) -> list[tuple[Path, int]]:
    """Snapshot the contiguous run of on-disk chunk files for ``job_id`` ONCE.

    Returns ``(path, size)`` pairs for the contiguous prefix that exists,
    stopping at the first missing index. Taking sizes in the same pass that
    collects the paths means a concurrent cache sweep can't make a caller
    advertise bytes that aren't there (which clients read as a truncated or
    hung response).
    """
    chunk_files: list[tuple[Path, int]] = []
    for idx in range(total_chunks):
        p = cache.chunk_path(job_id, idx)
        try:
            size = p.stat().st_size
        except FileNotFoundError:
            break
        chunk_files.append((p, size))
    return chunk_files


_FFMPEG_BASE = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]


def _audio_inputs(chunk_files: list[tuple[Path, int]]) -> list[str]:
    """``-i <chunk>`` args, one per chunk, in order."""
    args: list[str] = []
    for p, _ in chunk_files:
        args += ["-i", str(p)]
    return args


def _concat_chain(n: int, *, first_input: int = 0) -> str:
    """An *unlabeled* filtergraph chain that decodes ``n`` audio inputs and
    splices them, in order, into one stream.

    The caller terminates the chain — ``+ "[aout]"`` to label it, or
    ``+ ",apad[aud]"`` to chain another filter on. ``first_input`` is the ffmpeg
    input index of the first chunk (0 for the audio-only MP3 path; 1 for the MP4
    path, where input 0 is the video). Each input is decoded on its own, so
    per-file Opus pre-skip/padding is discarded before the join — the splice is
    sample-accurate. ``concat=n=1`` is a no-op passthrough, so a single-chunk
    job works too.
    """
    labels = "".join(f"[{first_input + i}:a]" for i in range(n))
    return f"{labels}concat=n={n}:v=0:a=1"


def mp3_transcode_cmd(chunk_files: list[tuple[Path, int]], dest: Path) -> list[str]:
    """ffmpeg command to splice the chunks (concat filter) and encode to MP3."""
    n = len(chunk_files)
    return [
        *_FFMPEG_BASE,
        *_audio_inputs(chunk_files),
        "-filter_complex", _concat_chain(n) + "[aout]",
        "-map", "[aout]",
        "-c:a", "libmp3lame", "-b:a", "192k",
        "-f", "mp3", str(dest),
    ]


def mux_video_cmd(
    video: Path,
    chunk_files: list[tuple[Path, int]],
    dest: Path,
    *,
    reencode_video: bool = False,
) -> list[str]:
    """ffmpeg command to mux the stripped audio over ``video`` into an MP4.

    The audio (the chunks, spliced sample-accurately via the concat filter) is
    re-encoded to AAC, which the MP4 container needs (it can't carry Opus
    reliably). The video is stream-copied by default — fast and lossless — but
    VP9/AV1 sources (the only codecs YouTube serves above 1080p) play in an MP4
    only in VLC/Chrome, not in QuickTime/Safari. Pass ``reencode_video=True`` for
    those to re-encode to H.264 so the file plays everywhere. ``yuv420p`` forces
    8-bit 4:2:0 (some VP9/AV1 are 10-bit, which QuickTime won't decode);
    ``veryfast`` keeps a 4K re-encode tolerable. ``+faststart`` moves the moov
    atom to the front so players can start immediately.

    The concat filter makes the spliced audio sample-accurate, so it no longer
    drifts against the video. ``apad`` + ``-shortest`` then only equalize the
    *total* length: the stripped track's duration can differ from the video's by
    a fraction of a second (the source's audio and video streams need not be
    exactly equal), so we pad the audio tail with silence (``apad``) and trim the
    output to the video (``-shortest``). That keeps the full video and a
    matching-length audio stream without truncating either's content.
    """
    n = len(chunk_files)
    if reencode_video:
        vcodec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                  "-pix_fmt", "yuv420p"]
    else:
        vcodec = ["-c:v", "copy"]
    return [
        *_FFMPEG_BASE,
        "-i", str(video),
        *_audio_inputs(chunk_files),  # chunks are inputs 1..n
        "-filter_complex", _concat_chain(n, first_input=1) + ",apad[aud]",
        "-map", "0:v:0", "-map", "[aud]",
        *vcodec,
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        str(dest),
    ]
