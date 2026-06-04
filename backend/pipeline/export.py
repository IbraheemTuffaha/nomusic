"""Helpers for assembling a finished job's chunks into a downloadable file.

These back the ``/audio?format=mp3`` and ``/video`` download endpoints. They're
kept out of ``server.py`` so they can be unit-tested without importing the
FastAPI app (which loads the separation engine at import time).

We feed the per-chunk Opus files to ffmpeg via the **concat demuxer** (a list
file), not by byte-concatenating them into one stream. Raw byte concatenation
of chained Ogg streams works for the browser's Web Audio decoder, but each
chunk's Ogg timestamps restart at zero, so ffmpeg sees a "timestamp
discontinuity" at every boundary — which produces an MP3 you can't seek in and
an MP4 whose audio drifts out of sync. The concat demuxer rebases each file's
timestamps onto a single monotonic timeline, so the output seeks cleanly.
"""

from __future__ import annotations

from pathlib import Path


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


def write_concat_list(chunk_files: list[tuple[Path, int]], dest: Path) -> None:
    """Write an ffmpeg concat-demuxer list file referencing each chunk in order.

    Each line is ``file '<abs-path>'``; embedded single quotes are escaped the
    way the concat demuxer expects (``'`` -> ``'\\''``), so a cache path under a
    home dir with odd characters can't break the list.
    """
    lines = []
    for p, _ in chunk_files:
        safe = str(p).replace("'", "'\\''")
        lines.append(f"file '{safe}'")
    dest.write_text("\n".join(lines) + "\n")


_FFMPEG_BASE = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]


def mp3_transcode_cmd(list_path: Path, dest: Path) -> list[str]:
    """ffmpeg command to transcode the chunk list (concat demuxer) to MP3."""
    return [
        *_FFMPEG_BASE,
        "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-c:a", "libmp3lame", "-b:a", "192k",
        "-f", "mp3", str(dest),
    ]


def mux_video_cmd(
    video: Path,
    list_path: Path,
    dest: Path,
    *,
    reencode_video: bool = False,
) -> list[str]:
    """ffmpeg command to mux the stripped audio over ``video`` into an MP4.

    The audio (the chunk list, via the concat demuxer) is re-encoded to AAC,
    which the MP4 container needs (it can't carry Opus reliably). The video is
    stream-copied by default — fast and lossless — but VP9/AV1 sources (the only
    codecs YouTube serves above 1080p) play in an MP4 only in VLC/Chrome, not in
    QuickTime/Safari. Pass ``reencode_video=True`` for those to re-encode to
    H.264 so the file plays everywhere. ``yuv420p`` forces 8-bit 4:2:0 (some
    VP9/AV1 are 10-bit, which QuickTime won't decode); ``veryfast`` keeps a 4K
    re-encode tolerable. ``+faststart`` moves the moov atom to the front so
    players can start immediately.

    ``apad`` + ``-shortest`` pad/trim the audio to the video's length so both
    streams start and end together. The per-chunk Opus durations sum to slightly
    more than the video (encoder priming, ~6.5 ms/chunk), which otherwise leaves
    the audio running past the end of the video; this keeps them aligned.
    """
    if reencode_video:
        vcodec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                  "-pix_fmt", "yuv420p"]
    else:
        vcodec = ["-c:v", "copy"]
    return [
        *_FFMPEG_BASE,
        "-i", str(video),
        "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-map", "0:v:0", "-map", "1:a:0",
        *vcodec,
        "-filter:a", "apad", "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        str(dest),
    ]
