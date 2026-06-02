"""Chunked separation + Opus encode.

The processor turns a video URL into a sequence of clean, music-free OGG/Opus
chunks. It coordinates the downloader, the engine, and the cache; it does not
know about HTTP, asyncio, or the extension.

Pipeline per chunk:

1. Slice the source audio precisely (download_source -> slice_source).
2. Run source separation (engine).
3. Mix the kept stems into one float32 audio array.
4. Trim to the playable window + apply a 10 ms anti-click fade.
5. Pipe WAV bytes through ffmpeg to encode as OGG/Opus and atomically write
   ``chunk_NNN.opus`` into the cache.
"""

from __future__ import annotations

import io
import logging
import math
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

import numpy as np
import soundfile as sf

from engines.base import Engine

from .cache import CacheMeta, JobCache
from .downloader import (
    VideoMetadata,
    download_source,
    probe,
    slice_source,
)

# Opus only supports 8/12/16/24/48 kHz; demucs hands us 44.1 kHz. ffmpeg
# resamples internally during encode at this bitrate it's transparent for
# vocals and the file size drops ~15x vs PCM_16 WAV.
_OPUS_BITRATE = "96k"

log = logging.getLogger(__name__)

# (meta, phase) where phase is one of: 'separating', 'mixing', 'chunk_complete'.
# Phase 'chunk_complete' is the only one that guarantees meta.chunks_ready has
# been updated; the others are best-effort UI hints.
ProgressCb = Callable[[CacheMeta, str], None]

# Source-download progress, 0..1. ``None`` means total size unknown
# (rare with yt-dlp, but possible for some streaming containers); the UI
# should fall back to an indeterminate spinner in that case.
DownloadProgressCb = Callable[[float | None], None]

# (info, plans, meta) fired once the probe completes — gives jobs.py enough
# data to surface title / duration / total_chunks in /status.
ProbedCb = Callable[["VideoMetadata", list, CacheMeta], None]

# Returns the next chunk index to process, or ``None`` when the work queue
# is exhausted. When the caller supplies one, the processor lets it drive
# the chunk order — that's how jobs.py implements "process from the
# seeked-to position first, loop back to earlier chunks after". When None,
# the processor uses its own internal FIFO over the plan list.
NextChunkProvider = Callable[[], Optional[int]]


@dataclass
class ChunkPlan:
    """One row in the chunking schedule.

    ``start`` / ``end`` are the *download* window (includes overlap). ``play_start``
    / ``play_end`` are what the final, crossfaded chunk will contribute to the
    playback timeline (no overlap).
    """

    index: int
    start: float
    end: float
    play_start: float
    play_end: float

    @property
    def is_first(self) -> bool:
        return self.index == 0


def plan_chunks(
    duration: float,
    chunk_seconds: float,
    overlap_seconds: float,
) -> list[ChunkPlan]:
    if duration <= 0:
        return []
    if chunk_seconds <= overlap_seconds:
        raise ValueError(
            "chunk_seconds must exceed overlap_seconds (got "
            f"{chunk_seconds=} {overlap_seconds=})"
        )

    stride = chunk_seconds - overlap_seconds
    total = max(1, math.ceil((duration - overlap_seconds) / stride))
    plans: list[ChunkPlan] = []
    for i in range(total):
        play_start = i * stride
        play_end = min(duration, play_start + stride)
        dl_start = max(0.0, play_start - overlap_seconds / 2)
        dl_end = min(duration, play_end + overlap_seconds / 2)
        plans.append(
            ChunkPlan(
                index=i,
                start=dl_start,
                end=dl_end,
                play_start=play_start,
                play_end=play_end,
            )
        )
    return plans


# Progressive download tuning. The margin absorbs the bytes->seconds estimate
# being approximate (VBR), so we never slice a chunk whose tail hasn't landed.
_PROGRESSIVE_MARGIN_SECONDS = 3.0
_PROGRESSIVE_POLL_SECONDS = 0.15


class _ProgressiveSource:
    """Runs ``download_source`` on a background thread and tells the caller how
    much of the timeline is safely on disk, so chunk separation can start
    before the whole file has arrived.

    ``available_seconds`` is estimated from the download's byte fraction times
    the known duration — approximate for VBR, which is why ``source_for``
    applies a margin. If the partial container can't be decoded at all, the
    caller catches the slice error and falls back to ``wait_complete``.
    """

    def __init__(self, url: str, out_dir: Path, duration: float, ui_hook) -> None:
        self._url = url
        self._out_dir = out_dir
        self._duration = duration
        self._ui_hook = ui_hook
        self._lock = threading.Lock()
        self._available = 0.0
        self._tmpfile: Optional[str] = None
        self._final: Optional[Path] = None
        self._error: Optional[BaseException] = None
        self._done = threading.Event()
        self._logged_size = False
        self._thread = threading.Thread(
            target=self._run, name="nomusic-progressive-dl", daemon=True
        )

    def start(self) -> None:
        log.debug(
            "progressive: streaming download + separate (duration=%.0fs)",
            self._duration,
        )
        self._thread.start()

    def is_done(self) -> bool:
        return self._done.is_set()

    def _hook(self, d: dict) -> None:
        try:
            if self._ui_hook:
                self._ui_hook(d)
        except Exception:  # never let a UI hook break the download
            pass
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes", 0)
            if not self._logged_size:
                self._logged_size = True
                # The make-or-break signal for byte-gating: if yt-dlp can't
                # report a total size, available_seconds stays 0 and we can only
                # release chunks once the whole file lands (no early start).
                log.debug(
                    "progressive: download size %s",
                    "known" if total else "UNKNOWN — byte-gate disabled, "
                    "will wait for full file",
                )
            with self._lock:
                tmp = d.get("tmpfilename")
                if tmp:
                    self._tmpfile = tmp
                if total and self._duration:
                    self._available = (done / total) * self._duration
        elif status == "finished":
            with self._lock:
                self._available = self._duration

    def _run(self) -> None:
        try:
            final = download_source(self._url, self._out_dir, progress_hook=self._hook)
            with self._lock:
                self._final = final
                self._available = self._duration
        except BaseException as exc:  # surfaced to the worker via raise_if_error
            self._error = exc
        finally:
            self._done.set()

    def available_seconds(self) -> float:
        with self._lock:
            return self._available

    def raise_if_error(self) -> None:
        if self._error is not None:
            raise self._error

    def current_path(self) -> Optional[Path]:
        with self._lock:
            if self._final is not None:
                return self._final
            if self._tmpfile is not None:
                p = Path(self._tmpfile)
                if p.exists():
                    return p
        return None

    def wait_complete(self) -> Path:
        self._done.wait()
        self.raise_if_error()
        if self._final is None:
            raise RuntimeError("progressive download finished without a file")
        return self._final

    def source_for(self, plan, overlap: float, abort_check=None) -> Path:
        """Block until ``plan``'s download window is on disk, then return the
        file to slice (the partial mid-download, or the final file)."""
        needed = plan.end + overlap + _PROGRESSIVE_MARGIN_SECONDS
        while not self._done.is_set() and self.available_seconds() < needed:
            self.raise_if_error()
            if abort_check:
                abort_check()
            time.sleep(_PROGRESSIVE_POLL_SECONDS)
        self.raise_if_error()
        path = self.current_path()
        # No partial visible yet (rare timing gap) — wait for the full file.
        return path if path is not None else self.wait_complete()


class Processor:
    def __init__(
        self,
        engine: Engine,
        cache: JobCache,
        *,
        chunk_seconds: float,
        chunk_overlap_seconds: float,
        keep_source_after_complete: bool = False,
        progressive: bool = False,
    ) -> None:
        self.engine = engine
        self.cache = cache
        self.chunk_seconds = chunk_seconds
        self.chunk_overlap_seconds = chunk_overlap_seconds
        self.keep_source_after_complete = keep_source_after_complete
        self.progressive = progressive

    # -- planning ------------------------------------------------------------

    def prepare(
        self,
        url: str,
        *,
        model: str,
        keep_stems: list[str],
    ) -> tuple[str, CacheMeta, VideoMetadata, list[ChunkPlan]]:
        """Probe the video, build/refresh the cache meta, and plan the chunks."""
        key = self.cache.key(
            url,
            model,
            keep_stems,
            chunk_seconds=self.chunk_seconds,
            chunk_overlap_seconds=self.chunk_overlap_seconds,
        )
        existing = self.cache.load_meta(key)

        # Fast resume: a prior run already probed this exact (url, model, stems,
        # chunk plan) and persisted the duration. The yt-dlp probe costs 3-6s on
        # YouTube (JS challenge), so on a resume — e.g. after an idle-abandon or
        # a page refresh — we skip it entirely and rebuild VideoMetadata from
        # the cached meta. The URL is part of the cache key, so the cached
        # duration can't belong to a different video.
        if existing and existing.total_chunks > 0 and existing.duration_seconds > 0:
            plans = plan_chunks(
                existing.duration_seconds,
                self.chunk_seconds,
                self.chunk_overlap_seconds,
            )
            if existing.total_chunks == len(plans):
                info = VideoMetadata(
                    id="",
                    title=existing.title,
                    duration_seconds=existing.duration_seconds,
                    extractor=existing.extractor,
                    webpage_url=url,
                )
                self.cache.save_meta(key, existing)
                return key, existing, info, plans

        info = probe(url)
        plans = plan_chunks(
            info.duration_seconds,
            self.chunk_seconds,
            self.chunk_overlap_seconds,
        )

        # Reuse the existing meta only if it matches; otherwise rebuild.
        if existing and existing.total_chunks == len(plans):
            meta = existing
            meta.title = info.title
            meta.extractor = info.extractor
        else:
            meta = CacheMeta(
                url=url,
                model=model,
                keep_stems=sorted(keep_stems),
                duration_seconds=info.duration_seconds,
                chunk_seconds=self.chunk_seconds,
                chunk_overlap_seconds=self.chunk_overlap_seconds,
                total_chunks=len(plans),
                title=info.title,
                extractor=info.extractor,
            )
        self.cache.save_meta(key, meta)
        return key, meta, info, plans

    # -- execution -----------------------------------------------------------

    def run(
        self,
        url: str,
        *,
        model: str,
        keep_stems: list[str],
        on_probed: ProbedCb | None = None,
        on_progress: ProgressCb | None = None,
        on_download_progress: DownloadProgressCb | None = None,
        next_chunk_provider: NextChunkProvider | None = None,
        abort_check: Callable[[], None] | None = None,
    ) -> str:
        """Run the full chunked pipeline. Returns the cache key.

        Safe to call on an already-cached job: missing chunks are filled in,
        and a fully-cached job returns immediately.

        ``next_chunk_provider`` lets the caller drive chunk order (e.g.
        prioritize chunks near the user's seek position); when None the
        processor falls back to a simple FIFO over remaining chunks.

        ``abort_check``, when supplied, is called periodically during any wait
        (notably the progressive-download gate); it should raise to abort the
        run. The provider raising is the normal abort path between chunks, but
        a long download has no chunk boundary, so this gives one there too.
        """
        key, meta, info, plans = self.prepare(
            url, model=model, keep_stems=keep_stems
        )
        if on_probed:
            on_probed(info, plans, meta)

        if meta.complete:
            log.info("Cache hit for %s (%d chunks)", url, meta.total_chunks)
            if on_download_progress:
                on_download_progress(1.0)
            return key

        # Download the full source once. Each chunk is sliced from this file
        # so cuts are sample-accurate (yt-dlp's per-range download cuts at the
        # nearest preceding keyframe, which drifts by 5-10 s on AAC/Opus).
        def _yt_hook(d: dict) -> None:
            if not on_download_progress:
                return
            try:
                if d.get("status") == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate")
                    done = d.get("downloaded_bytes", 0)
                    on_download_progress(done / total if total else None)
                elif d.get("status") == "finished":
                    on_download_progress(1.0)
            except Exception:  # never let a UI hook break the pipeline
                log.debug("download progress hook raised", exc_info=True)

        source_dir = self.cache.source_dir(url)
        dl: _ProgressiveSource | None = None
        if self.progressive:
            # Download on a background thread; ``source_for`` blocks per chunk
            # until enough of the timeline is on disk to slice it.
            dl = _ProgressiveSource(
                url, source_dir, info.duration_seconds, _yt_hook
            )
            dl.start()
            source_for = lambda plan: dl.source_for(
                plan, self.chunk_overlap_seconds, abort_check
            )
        else:
            # Download the full source up front; every chunk slices from it.
            full = download_source(url, source_dir, progress_hook=_yt_hook)
            source_for = lambda plan: full

        plans_by_index = {p.index: p for p in plans}
        # Default provider: simple FIFO over remaining chunks. Used by the
        # CLI and tests; jobs.py supplies its own that supports reordering.
        if next_chunk_provider is None:
            fallback = deque(
                p.index for p in plans if p.index not in meta.chunks_ready
            )
            next_chunk_provider = lambda: fallback.popleft() if fallback else None

        logged_first = False
        while True:
            idx = next_chunk_provider()
            if idx is None:
                break
            plan = plans_by_index.get(idx)
            if plan is None:
                log.warning("provider returned unknown chunk index %d", idx)
                continue
            # Race-safety: another caller (or a prior reorder pop) may have
            # already completed this chunk between picks. Skip if so.
            current_meta = self.cache.load_meta(key)
            if (
                current_meta
                and plan.index in current_meta.chunks_ready
                and self.cache.chunk_path(key, plan.index).exists()
            ):
                continue
            source_path = source_for(plan)
            if dl is not None and not logged_first:
                logged_first = True
                # The headline progressive signal: if the download is still
                # running here, separation genuinely overlapped it (early
                # start). If it's already complete, we ran sequentially.
                log.debug(
                    "progressive: first chunk %d released with download %s "
                    "(%.0f/%.0fs on disk)",
                    plan.index,
                    "still running" if not dl.is_done() else "already complete",
                    dl.available_seconds(),
                    info.duration_seconds,
                )
            try:
                self._process_one(
                    source_path,
                    key,
                    plan,
                    model=model,
                    keep_stems=keep_stems,
                    on_progress=on_progress,
                )
            except Exception:
                # Non-progressive failures are real. In progressive mode a slice
                # can fail because the partial container isn't decodable yet
                # (e.g. a non-streamable mp4) — degrade: wait for the full
                # download and slice from it. Once it's complete, every later
                # source_for() returns the final file, so this self-heals.
                if dl is None:
                    raise
                log.warning(
                    "progressive: chunk %d slice failed; waiting for full download",
                    plan.index,
                    exc_info=True,
                )
                full = dl.wait_complete()
                self._process_one(
                    full,
                    key,
                    plan,
                    model=model,
                    keep_stems=keep_stems,
                    on_progress=on_progress,
                )
            self.cache.record_chunk(key, plan.index)
            if on_progress:
                refreshed = self.cache.load_meta(key)
                if refreshed:
                    on_progress(refreshed, "chunk_complete")

        # Mark complete when every chunk is on disk.
        all_present = all(
            self.cache.chunk_path(key, p.index).exists() for p in plans
        )
        if all_present:
            self.cache.mark_complete(key)
            if not self.keep_source_after_complete:
                # Source has served its purpose. Re-watches read straight from
                # the chunk Opus files; only a stems/model change would need
                # it back, and that re-downloads transparently.
                self.cache.drop_source(url)

        return key

    # -- internals -----------------------------------------------------------

    def _process_one(
        self,
        source_path: Path,
        key: str,
        plan: ChunkPlan,
        *,
        model: str,
        keep_stems: list[str],
        on_progress: ProgressCb | None = None,
    ) -> None:
        def emit(phase: str) -> None:
            if not on_progress:
                return
            meta = self.cache.load_meta(key)
            if meta:
                on_progress(meta, phase)

        with tempfile.TemporaryDirectory(prefix="nomusic-") as tmp_str:
            tmp = Path(tmp_str)
            raw = tmp / f"raw_{plan.index:03d}.wav"

            t0 = time.perf_counter()
            emit("downloading")  # phase name kept for UI compatibility
            slice_source(source_path, raw, start=plan.start, end=plan.end)
            t_slice = time.perf_counter() - t0

            emit("separating")
            t1 = time.perf_counter()
            result = self.engine.separate(raw, tmp / "stems", model=model)
            t_separate = time.perf_counter() - t1

            emit("mixing")
            t2 = time.perf_counter()
            mixed = self._mix_stems(result.stems, keep_stems)
            self._write_chunk(mixed, result.sample_rate, key, plan)
            t_mix_write = time.perf_counter() - t2

            # Per-chunk benchmark: realtime ratio is the headline metric
            # (how many seconds of audio we process per wall-clock second).
            # Headline number first, breakdown after, so a grep is enough
            # to scan a long log without parsing.
            wall = time.perf_counter() - t0
            chunk_dur = plan.play_end - plan.play_start
            ratio = chunk_dur / wall if wall > 0 else 0.0
            log.info(
                "chunk %d: %.2fs wall (%.1fx realtime) — "
                "slice=%.2fs separate=%.2fs mix+write=%.2fs",
                plan.index, wall, ratio, t_slice, t_separate, t_mix_write,
            )

    @staticmethod
    def _mix_stems(
        stems: dict[str, Path],
        keep: list[str],
    ) -> np.ndarray:
        """Sum the kept stems and return a (samples, channels) float32 array.

        Applies RMS-matched loudness compensation. The vocals stem alone
        typically sits 6-12 dB below the full mix's RMS energy, so playing
        it back at the same slider position sounds quieter than the original
        track. We compute the RMS of all four stems summed (the original
        mix) and the RMS of the kept stems, then apply a gain to close the
        gap — capped at 2x (+6 dB) so an instrumental section with near-
        silent vocals doesn't get pumped up to full volume.
        """
        missing = [s for s in keep if s not in stems]
        if missing:
            raise RuntimeError(f"engine did not return stems: {missing}")

        # Load every stem we got back so we can use the full-mix RMS as a
        # loudness reference. This costs one extra disk read + sum per
        # chunk; negligible compared with separation.
        arrays: dict[str, np.ndarray] = {}
        for name, path in stems.items():
            audio, _ = sf.read(str(path), always_2d=True, dtype="float32")
            arrays[name] = audio

        mix = sum(arrays[n] for n in keep)
        full_mix = sum(arrays.values())

        full_rms = float(np.sqrt(np.mean(np.square(full_mix))) + 1e-9)
        kept_rms = float(np.sqrt(np.mean(np.square(mix))) + 1e-9)
        # max(1, ...) so we never attenuate; min(2, ...) bounds the pump on
        # quiet-vocal chunks. The chunk-to-chunk gain spread is then ≤ 6 dB,
        # which is below the threshold where typical listeners notice level
        # shifts at scene boundaries.
        boost = min(2.0, max(1.0, full_rms / kept_rms))
        if boost > 1.0:
            mix = mix * boost

        # Soft-clip if the boost pushed past unity. tanh preserves loud-
        # passage character without audible square-wave clipping.
        peak = float(np.abs(mix).max() or 1.0)
        if peak > 0.99:
            mix = np.tanh(mix / peak) * 0.99
        return mix

    def _write_chunk(
        self,
        audio: np.ndarray,
        sample_rate: int,
        key: str,
        plan: ChunkPlan,
    ) -> None:
        """Write ``chunk_NNN.wav`` covering exactly ``[play_start, play_end]``.

        The download window includes ``half_overlap`` of pre-roll / post-roll
        on each side so the separator has clean context at boundaries — but
        the written chunk is trimmed to the precise playable region. The
        extension schedules chunks back-to-back at ``idx * stride`` on the
        video timeline; any pre-roll left in the file would shift each chunk
        by the overlap amount and stack into accumulating drift.

        A short anti-click fade (~10 ms) is applied to each end so the
        boundary between adjacent chunks doesn't produce an audible click.
        """
        n = audio.shape[0]
        head_trim = int((plan.play_start - plan.start) * sample_rate)
        tail_trim_end = head_trim + int(
            (plan.play_end - plan.play_start) * sample_rate
        )
        # Clamp inside the downloaded buffer in case the download came back
        # slightly short (ffmpeg seek rounding).
        head_trim = max(0, min(head_trim, n))
        tail_trim_end = max(head_trim, min(tail_trim_end, n))
        trimmed = audio[head_trim:tail_trim_end].copy()

        click_fade = min(
            int(0.010 * sample_rate),  # 10 ms
            trimmed.shape[0] // 2,
        )
        if click_fade > 1:
            head_ramp = np.linspace(0.0, 1.0, click_fade, dtype=np.float32)[:, None]
            tail_ramp = np.linspace(1.0, 0.0, click_fade, dtype=np.float32)[:, None]
            trimmed[:click_fade] *= head_ramp
            trimmed[-click_fade:] *= tail_ramp

        out = self.cache.chunk_path(key, plan.index)
        tmp_path = out.with_suffix(".part")
        _encode_opus(trimmed, sample_rate, tmp_path)
        tmp_path.replace(out)



def _encode_opus(audio: np.ndarray, sample_rate: int, out_path: Path) -> None:
    """Encode ``audio`` (samples, channels) to OGG/Opus at ``out_path``.

    Pipes a WAV through ffmpeg's stdin and writes the resulting OGG/Opus
    file. Going through ffmpeg lets us inherit its high-quality resampler
    (libswresample); libsndfile can't resample, and Opus is restricted to
    8/12/16/24/48 kHz, so we'd otherwise need a separate Python resampler.
    """
    wav_buf = io.BytesIO()
    sf.write(wav_buf, audio, sample_rate, subtype="PCM_16", format="WAV")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-nostdin",
        "-loglevel",
        "error",
        "-f",
        "wav",
        "-i",
        "pipe:0",
        "-c:a",
        "libopus",
        "-b:a",
        _OPUS_BITRATE,
        "-vbr",
        "on",
        "-application",
        "audio",
        "-f",
        "ogg",
        str(out_path),
    ]
    subprocess.run(cmd, input=wav_buf.getvalue(), check=True, capture_output=True)


def iter_ready_chunks(meta: CacheMeta) -> Iterator[int]:
    """Yield each ready chunk index in order. Convenience for the HTTP layer."""
    yield from sorted(set(meta.chunks_ready))


# Small helper used by tests / the API; placed here so it lives with the rest
# of the pipeline rather than leaking into the engine module.
def silence_wav(path: Path, *, seconds: float, sample_rate: int = 44100) -> None:
    samples = int(seconds * sample_rate)
    audio = np.zeros((samples, 2), dtype=np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), audio, sample_rate, subtype="PCM_16")


def copy_wav(src: Path, dst: Path) -> None:
    shutil.copyfile(src, dst)
