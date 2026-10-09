"""Helpers for assembling a finished job's chunks into a downloadable file.

These back the asynchronous ``/exports`` workflow. They're
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
import os
import re
import selectors
import signal
import subprocess
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

# ffprobe is a metadata read; a sub-second job in practice. The ceiling only
# trips if ffprobe wedges, in which case the caller degrades gracefully.
_FFPROBE_TIMEOUT_SECONDS = 60.0

# Codecs QuickTime/Safari can play inside an MP4 — these the mux stream-copies.
# Any other video codec (VP9/AV1, which YouTube uses above 1080p) is re-encoded
# to H.264 so the exported MP4 plays everywhere, not just in VLC/Chrome.
MP4_COPYABLE_VCODECS = frozenset({"h264", "hevc"})

_FFMPEG_TIMEOUT_SECONDS = 3600.0
_PART_SUFFIX = ".part"


def complete_manifest(meta) -> bool:
    """Return true only for a complete, gap-free, in-range chunk manifest."""
    try:
        total = int(meta.total_chunks)
        ready = sorted(int(index) for index in meta.chunks_ready)
    except (AttributeError, TypeError, ValueError):
        return False
    return bool(meta.complete and total > 0 and ready == list(range(total)))


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


def snapshot_chunk_files(
    cache, job_id: str, total_chunks: int, *, require_complete: bool = False
) -> list[tuple[Path, int]]:
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
    if require_complete and len(chunk_files) != total_chunks:
        return []
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


def opus_transcode_cmd(chunk_files: list[tuple[Path, int]], dest: Path) -> list[str]:
    """Splice chunks through the decoder and write one sample-accurate Ogg/Opus file."""
    n = len(chunk_files)
    return [
        *_FFMPEG_BASE,
        *_audio_inputs(chunk_files),
        "-filter_complex", _concat_chain(n) + "[aout]",
        "-map", "[aout]",
        "-c:a", "libopus", "-b:a", "96k",
        "-f", "ogg", str(dest),
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
        # The worker writes to a .mp4.part staging path. Keep the muxer
        # explicit because ffmpeg cannot infer it from that temporary suffix.
        "-f", "mp4",
        str(dest),
    ]


def _export_reservation(
    chunk_files: list[tuple[Path, int]], *, cap: int, extra_bytes: int = 0
) -> int:
    """Reserve a bounded estimate rather than the maximum for every export."""
    source_bytes = sum(size for _, size in chunk_files)
    estimate = max(1, source_bytes * 2 + max(0, extra_bytes))
    return min(cap, estimate)


def _run_export_ffmpeg(
    cmd: list[str], cancel_event=None, *, on_progress=None, total_seconds: float = 0.0
) -> None:
    """Run ffmpeg without pipe deadlocks and with cancellable progress."""
    report_progress = on_progress is not None and total_seconds > 0
    full = [cmd[0], "-progress", "pipe:1", "-nostats", *cmd[1:]] if report_progress else cmd
    stdout = subprocess.PIPE if report_progress else subprocess.DEVNULL
    stderr_file = tempfile.TemporaryFile()
    proc = subprocess.Popen(
        full,
        stdout=stdout,
        stderr=stderr_file,
        start_new_session=True,
    )

    def stop(force: bool = False) -> None:
        if proc.poll() is not None:
            return
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            try:
                (proc.kill if force else proc.terminate)()
            except ProcessLookupError:
                pass

    deadline = time.monotonic() + _FFMPEG_TIMEOUT_SECONDS
    selector = selectors.DefaultSelector()
    if proc.stdout is not None:
        os.set_blocking(proc.stdout.fileno(), False)
        selector.register(proc.stdout, selectors.EVENT_READ)
    output = b""
    try:
        while proc.poll() is None or selector.get_map():
            if cancel_event is not None and cancel_event.is_set():
                stop()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    stop(force=True)
                    proc.wait(timeout=2)
                raise RuntimeError("cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stop(force=True)
                proc.wait(timeout=2)
                proc.communicate()
                drained = True
                raise RuntimeError(
                    f"ffmpeg timed out after {_FFMPEG_TIMEOUT_SECONDS:.0f}s"
                )
            for key, _ in selector.select(timeout=min(0.2, remaining)):
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output += chunk
                if report_progress:
                    for line in output.splitlines(keepends=True):
                        if not line.endswith((b"\n", b"\r")):
                            continue
                        text = line.decode("utf-8", "replace").strip()
                        if text.startswith("out_time_us="):
                            try:
                                fraction = int(text.split("=", 1)[1]) / 1e6 / total_seconds
                                on_progress(max(0.0, min(1.0, fraction)))
                            except (ValueError, ZeroDivisionError):
                                log.debug("unparseable ffmpeg progress: %r", text)
                    output = output.split(b"\n")[-1]
            if proc.poll() is not None and not selector.get_map():
                break
        proc.wait(timeout=2)
    except BaseException:
        if proc.poll() is None:
            stop(force=True)
            proc.wait(timeout=2)
        selector.close()
        stderr_file.close()
        raise
    finally:
        selector.close()
        if proc.stdout is not None:
            proc.stdout.close()
    stderr_file.seek(0)
    stderr = stderr_file.read()
    stderr_file.close()
    if proc.returncode:
        detail = stderr.decode("utf-8", "replace").strip() or "(no stderr)"
        raise RuntimeError(f"ffmpeg failed: {detail[-2000:]}")


def _safe_filename(title: str, extension: str) -> str:
    value = re.sub(r"[/\\:*?\"<>|\x00-\x1f\x7f]", " ", title or "nomusic")
    value = value.strip(" .")
    value = re.sub(r"\s+", " ", value)[:120] or "nomusic"
    return f"{value}.{extension}"


def build_export(cache, settings, spec, destination: Path, on_progress, cancel_event):
    """Build one export in scratch, then publish one complete file."""
    from nomusic.exports import ExportArtifact, ExportBuildError
    from nomusic.pipeline import downloader
    from nomusic.pipeline.cache import StorageLimitExceeded
    from nomusic.pipeline.downloader import ResourceLimitExceeded

    meta = cache.load_meta(spec.job_id)
    if meta is None or not complete_manifest(meta):
        raise ExportBuildError("source job is not complete")
    lease = cache.job_lease(spec.job_id, shared=True)
    video_lease = None
    reservation = None
    with tempfile.TemporaryDirectory(dir=cache.scratch.path, prefix="export-") as work:
        work_dir = Path(work)
        try:
            chunk_files = snapshot_chunk_files(
                cache, spec.job_id, meta.total_chunks, require_complete=True
            )
            if not chunk_files:
                raise ExportBuildError("source chunks are unavailable")
            if cancel_event.is_set():
                raise ExportBuildError("cancelled")

            format_details = {
                "opus": ("opus", "audio/ogg", opus_transcode_cmd),
                "mp3": ("mp3", "audio/mpeg", mp3_transcode_cmd),
            }
            if spec.format in format_details:
                extension, media_type, command = format_details[spec.format]
                final = destination / _safe_filename(meta.title, extension)
                part = work_dir / (final.name + ".part")
                reservation = cache.reserve(
                    _export_reservation(chunk_files, cap=settings.max_export_bytes)
                )
                on_progress("encoding", 0.0)
                _run_export_ffmpeg(command(chunk_files, part), cancel_event)
                if cancel_event.is_set():
                    raise ExportBuildError("cancelled")
                size = part.stat().st_size
                if size <= 0 or size > settings.max_export_bytes:
                    raise ExportBuildError("export artifact exceeds the configured size limit")
                reservation.resize(size)
                os.replace(part, final)
                on_progress("encoding", 1.0)
                return ExportArtifact(final, final.name, media_type, size)

            if spec.format != "mp4":
                raise ExportBuildError(f"unsupported export format: {spec.format}")

            extension, media_type = "mp4", "video/mp4"
            final = destination / _safe_filename(meta.title, extension)
            part = work_dir / (final.name + ".part")
            video_dir = cache.video_dir(meta.url, spec.max_height)
            cached_video = any(video_dir.glob("video.*"))
            reservation = cache.reserve(
                settings.max_video_bytes if not cached_video else
                _export_reservation(chunk_files, cap=settings.max_export_bytes)
            )
            on_progress("downloading", 0.0)

            def download_hook(data: dict[str, object]) -> None:
                if cancel_event.is_set():
                    raise downloader.DownloadCancelled()
                _download_progress(data, on_progress)

            try:
                downloader.validate_public_url(meta.url)
                while True:
                    try:
                        video_lease = cache.video_lease(
                            meta.url, spec.max_height, blocking=False
                        )
                        break
                    except BlockingIOError:
                        if cancel_event.wait(0.1):
                            raise downloader.DownloadCancelled()
                video_path = downloader.download_video(
                    meta.url,
                    video_dir,
                    max_height=spec.max_height,
                    limits=downloader.limits_from_settings(settings),
                    progress_hook=download_hook,
                )
            except downloader.DownloadCancelled as exc:
                raise ExportBuildError("cancelled") from exc
            except (StorageLimitExceeded, ResourceLimitExceeded) as exc:
                raise ExportBuildError(str(exc)) from exc
            except Exception as exc:
                raise ExportBuildError(f"video download failed: {exc}") from exc
            if cancel_event.is_set():
                raise ExportBuildError("cancelled")
            if video_lease is not None:
                video_lease.close()
                while True:
                    try:
                        video_lease = cache.video_lease(
                            meta.url, spec.max_height, shared=True, blocking=False
                        )
                        break
                    except BlockingIOError:
                        if cancel_event.wait(0.1):
                            raise ExportBuildError("cancelled")
            reservation.resize(
                _export_reservation(chunk_files, cap=settings.max_export_bytes)
            )
            on_progress("encoding", 0.0)
            duration = video_duration(video_path)
            reencode = video_codec(video_path) not in MP4_COPYABLE_VCODECS

            def encode() -> None:
                _run_export_ffmpeg(
                    mux_video_cmd(
                        video_path, chunk_files, part, reencode_video=reencode
                    ),
                    cancel_event,
                    on_progress=lambda fraction: on_progress("encoding", fraction),
                    total_seconds=duration,
                )

            try:
                encode()
            except RuntimeError:
                if reencode or cancel_event.is_set():
                    raise
                reencode = True
                on_progress("encoding", 0.0)
                encode()
            if cancel_event.is_set():
                raise ExportBuildError("cancelled")
            size = part.stat().st_size
            if size <= 0 or size > settings.max_export_bytes:
                raise ExportBuildError("export artifact exceeds the configured size limit")
            reservation.resize(size)
            os.replace(part, final)
            on_progress("encoding", 1.0)
            return ExportArtifact(final, final.name, media_type, size)
        except StorageLimitExceeded as exc:
            raise ExportBuildError(str(exc)) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise ExportBuildError(str(exc)) from exc
        finally:
            if reservation is not None:
                reservation.release()
            if video_lease is not None:
                video_lease.close()
            lease.close()


def _download_progress(data: dict[str, object], on_progress) -> None:
    if data.get("status") == "downloading":
        total = data.get("total_bytes") or data.get("total_bytes_estimate")
        got = data.get("downloaded_bytes")
        if total and got is not None:
            on_progress("downloading", max(0.0, min(1.0, float(got) / float(total))))
    elif data.get("status") == "finished":
        on_progress("downloading", 1.0)
