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
import subprocess
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
import soundfile as sf

from config import SETTINGS
from engines.base import Engine

from .cache import CacheMeta, JobCache
from .downloader import (
    SourceFetcher,
    VideoMetadata,
    download_source,
    slice_source,
)

# Opus only supports 8/12/16/24/48 kHz; demucs hands us 44.1 kHz. ffmpeg
# resamples internally during encode at this bitrate it's transparent for
# vocals and the file size drops ~15x vs PCM_16 WAV.
_OPUS_BITRATE = "96k"

# Hard ceiling for a single chunk's Opus encode. A chunk is ~10 s of audio, so a
# transparent VBR encode finishes in well under a second; this bound only trips
# if ffmpeg wedges, in which case we'd rather fail the chunk than hang the
# write thread forever.
_OPUS_ENCODE_TIMEOUT_SECONDS = 120.0

log = logging.getLogger(__name__)

# (meta, phase) where phase is one of: 'separating', 'chunk_complete'.
# Phase 'chunk_complete' is the only one that guarantees meta.chunks_ready has
# been updated; 'separating' is a best-effort UI hint emitted when a batch
# enters the GPU stage.
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


@dataclass
class _ChunkWork:
    """One chunk in flight through the decode → infer → write pipeline. The
    producer fills ``prepared`` + slice/decode times; the GPU stage fills
    ``result`` + infer/gpu times; the consumer reads it all to write + log."""

    plan: ChunkPlan
    prepared: Any
    t_slice: float
    t_decode: float
    result: Any = None
    t_infer: float = 0.0
    gpu: float = 0.0


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

    def __init__(
        self, url: str, out_dir: Path, duration: float, ui_hook, fetcher=None
    ) -> None:
        self._url = url
        self._out_dir = out_dir
        self._duration = duration
        self._ui_hook = ui_hook
        # When set, the worker already extracted metadata in this session; the
        # background download reuses it (no second extraction). None on resume.
        self._fetcher = fetcher
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
            log.debug("progressive UI hook raised", exc_info=True)
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
            if self._fetcher is not None:
                final = self._fetcher.download(progress_hook=self._hook)
            else:
                final = download_source(
                    self._url, self._out_dir, progress_hook=self._hook
                )
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

    def source_for(self, plan, overlap: float, abort_check=None, on_wait=None) -> Path:
        """Block until ``plan``'s download window is on disk, then return the
        file to slice (the partial mid-download, or the final file).

        While blocked (the chunk's bytes aren't downloaded yet — e.g. the user
        seeked past the downloaded point), ``on_wait`` is called with the
        current download fraction so the UI can show "Fetching" instead of a
        frozen "Removing". Not called when the bytes are already present.
        """
        needed = plan.end + overlap + _PROGRESSIVE_MARGIN_SECONDS
        waits = 0
        while not self._done.is_set() and self.available_seconds() < needed:
            self.raise_if_error()
            if abort_check:
                abort_check()
            waits += 1
            # Only flip the UI to "Fetching" once we've actually been blocked a
            # moment (~0.3s), so a chunk whose bytes are basically already here
            # doesn't cause a one-frame flash.
            if on_wait and waits >= 2:
                on_wait(
                    self.available_seconds() / self._duration
                    if self._duration
                    else None
                )
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
    ) -> tuple[str, CacheMeta, VideoMetadata, list[ChunkPlan], Optional[SourceFetcher]]:
        """Probe the video, build/refresh the cache meta, and plan the chunks.

        Returns ``(key, meta, info, plans, fetcher)``. ``fetcher`` is a
        :class:`SourceFetcher` holding an open yt-dlp session that already
        extracted ``info`` — the caller downloads from it (same session, no
        second extraction). It's ``None`` on the resume fast-path, where the
        duration comes from the cached meta and no extraction happened.
        """
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
                return key, existing, info, plans, None

        # First run: extract metadata in a session we'll also download from, so
        # the JS-challenge extraction is paid once, not once here + again at
        # download time.
        fetcher = SourceFetcher(url, self.cache.source_dir(url))
        info = fetcher.extract()
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
        return key, meta, info, plans, fetcher

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
        on_wait_for_download: Callable[[float | None], None] | None = None,
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
        key, meta, info, plans, fetcher = self.prepare(
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
                url, source_dir, info.duration_seconds, _yt_hook, fetcher=fetcher
            )
            dl.start()
            source_for = lambda plan: dl.source_for(
                plan, self.chunk_overlap_seconds, abort_check, on_wait_for_download
            )
        elif fetcher is not None:
            # First run: download from the session that already extracted info.
            full = fetcher.download(progress_hook=_yt_hook)
            source_for = lambda plan: full
        else:
            # Resume: download the full source up front (cached source returns
            # immediately); every chunk slices from it.
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

        # ---- Pipelined stages so the GPU runs back-to-back -------------------
        # decode (producer thread)  →  infer (GPU, THIS thread, batched)  →
        # mix+write (consumer thread). Chunks are decoded ahead and writes drain
        # behind, so the per-chunk CPU/IO (~25% of wall, serial before) hides
        # inside the GPU window; the GPU itself separates several chunks per call
        # to fill its cores. Only this thread calls the engine (single GPU
        # stream) and only the write pool touches cache meta (single writer).
        loop_start = time.perf_counter()
        total_gpu = 0.0
        chunks_processed = 0
        logged_first = False

        def _log_duty() -> None:
            if not chunks_processed:
                return
            loop_wall = time.perf_counter() - loop_start
            duty = 100.0 * total_gpu / loop_wall if loop_wall > 0 else 0.0
            # GPU-busy vs the whole processing loop — the headline "how fully are
            # we using the GPU" number. Logged on completion AND on abandon.
            log.info(
                "GPU duty cycle: %.0f%% — %.1fs GPU / %.1fs wall across %d "
                "chunks (%.1fs idle around/between chunks)",
                duty, total_gpu, loop_wall, chunks_processed,
                max(0.0, loop_wall - total_gpu),
            )

        def _next_decoded() -> _ChunkWork | None:
            """Pick the next pending chunk and decode it (CPU/IO half). Returns a
            ``_ChunkWork``, or ``None`` when the queue is exhausted. Raises
            ``WorkerAbandoned`` (from the provider) to abort the run. Runs on the
            single decode thread, so its skip-check reads are race-free."""
            nonlocal logged_first
            while True:
                idx = next_chunk_provider()  # may raise WorkerAbandoned
                if idx is None:
                    return None
                plan = plans_by_index.get(idx)
                if plan is None:
                    log.warning("provider returned unknown chunk index %d", idx)
                    continue
                # Race-safety: another caller may have finished this chunk
                # between picks. Skip if so.
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
                    log.debug(
                        "progressive: first chunk %d released with download %s "
                        "(%.0f/%.0fs on disk)",
                        plan.index,
                        "still running" if not dl.is_done() else "already complete",
                        dl.available_seconds(),
                        info.duration_seconds,
                    )
                return self._decode_chunk(source_path, key, plan, model=model, dl=dl)

        # Batch up to BATCH chunks per GPU call: a single chunk leaves the GPU
        # cores partly idle, so we separate a few at once for higher throughput
        # at identical per-chunk output. Decode stays on one thread (so the
        # provider's skip-check is race-free) but we keep BATCH decodes queued so
        # a full batch is usually ready when the GPU frees up.
        batch_size = max(1, SETTINGS.gpu_batch)
        decode_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nm-decode")
        write_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nm-write")
        write_futures: list = []
        inflight: deque = deque()
        stream_done = False

        def _refill() -> None:
            while not stream_done and len(inflight) < batch_size:
                inflight.append(decode_pool.submit(_next_decoded))

        try:
            _refill()
            while inflight:
                # Block for the first ready chunk, then add only chunks that are
                # ALREADY decoded — never wait to *fill* a batch. So at startup
                # or right after a seek (one chunk ready) we run a batch of 1 at
                # minimal latency, while in steady state (decode running ahead of
                # the GPU) the batch fills to ``batch_size``.
                batch_works: list[_ChunkWork] = []
                work = inflight.popleft().result()  # may raise WorkerAbandoned
                if work is None:
                    stream_done = True
                else:
                    batch_works.append(work)
                    _refill()
                    while (
                        inflight
                        and len(batch_works) < batch_size
                        and inflight[0].done()
                    ):
                        nxt = inflight.popleft().result()
                        if nxt is None:
                            stream_done = True
                            break
                        batch_works.append(nxt)
                        _refill()
                if not batch_works:
                    break
                # GPU stage — this thread only.
                if on_progress:
                    m = self.cache.load_meta(key)
                    if m:
                        on_progress(m, "separating")
                ti = time.perf_counter()
                results = self.engine.infer_batch([w.prepared for w in batch_works])
                dt = time.perf_counter() - ti
                for work, result in zip(batch_works, results):
                    work.result = result
                    work.prepared = None  # free the decoded input tensor promptly
                    work.t_infer = dt / len(batch_works)
                    work.gpu = result.gpu_seconds or 0.0
                    total_gpu += work.gpu
                    chunks_processed += 1
                    # Hand mix+write+record to the consumer; overlaps the next GPU.
                    write_futures.append(
                        write_pool.submit(
                            self._finish_chunk, work, key, keep_stems, on_progress
                        )
                    )
                # Bound the write backlog and surface any consumer error early.
                while len(write_futures) > 2 * batch_size:
                    write_futures.pop(0).result()
            for f in write_futures:
                f.result()
        finally:
            decode_pool.shutdown(wait=True)
            write_pool.shutdown(wait=True)
            _log_duty()

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

    def _decode_chunk(
        self,
        source_path: Path,
        key: str,
        plan: ChunkPlan,
        *,
        model: str,
        dl: Any | None,
    ) -> _ChunkWork:
        """Producer stage: slice + decode one chunk into a model-ready input.

        Returns a ``_ChunkWork`` carrying the in-memory ``prepared`` input (no
        files held across stages) and the slice/decode timings. In progressive
        mode, a slice that fails on the partial container (e.g. a not-yet-
        streamable mp4) degrades to waiting for the full download, then retries.
        """

        def _do(src: Path) -> _ChunkWork:
            with tempfile.TemporaryDirectory(prefix="nomusic-") as tmp_str:
                raw = Path(tmp_str) / f"raw_{plan.index:03d}.wav"
                t0 = time.perf_counter()
                slice_source(src, raw, start=plan.start, end=plan.end)
                t_slice = time.perf_counter() - t0
                t1 = time.perf_counter()
                prepared = self.engine.prepare(raw, model=model)
                t_decode = time.perf_counter() - t1
            return _ChunkWork(
                plan=plan, prepared=prepared, t_slice=t_slice, t_decode=t_decode
            )

        try:
            return _do(source_path)
        except Exception:
            if dl is None:
                raise
            log.warning(
                "progressive: chunk %d slice failed; waiting for full download",
                plan.index,
                exc_info=True,
            )
            return _do(dl.wait_complete())

    def _finish_chunk(
        self,
        work: _ChunkWork,
        key: str,
        keep_stems: list[str],
        on_progress: ProgressCb | None,
    ) -> None:
        """Consumer stage: mix the kept stems, encode + write the chunk, record
        it, and emit progress. Runs on the single write thread, so it's the only
        writer of the cache meta (no lock needed)."""
        plan = work.plan
        t2 = time.perf_counter()
        mixed = self._mix_stems(work.result.stems, keep_stems)
        self._write_chunk(mixed, work.result.sample_rate, key, plan)
        t_mix_write = time.perf_counter() - t2

        self.cache.record_chunk(key, plan.index)
        if on_progress:
            refreshed = self.cache.load_meta(key)
            if refreshed:
                on_progress(refreshed, "chunk_complete")

        # Stage breakdown for one chunk. ``work`` is the sum of stage times (the
        # serial-equivalent cost); the job-end "GPU duty cycle" line is the real
        # measure of how much of it overlapped.
        work_s = work.t_slice + work.t_decode + work.t_infer + t_mix_write
        chunk_dur = plan.play_end - plan.play_start
        ratio = chunk_dur / work_s if work_s > 0 else 0.0
        log.info(
            "chunk %d: %.2fs work (%.1fx realtime) — "
            "slice=%.2fs decode=%.2fs infer=%.2fs mix+write=%.2fs | gpu=%.2fs",
            plan.index, work_s, ratio, work.t_slice, work.t_decode,
            work.t_infer, t_mix_write, work.gpu,
        )

    @staticmethod
    def _mix_stems(
        stems: dict[str, np.ndarray],
        keep: list[str],
    ) -> np.ndarray:
        """Sum the kept stems and return a (samples, channels) float32 array.

        ``stems`` are the engine's in-memory ``(samples, channels)`` arrays.

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

        arrays = stems  # already in-memory; the full set is the loudness ref
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
        """Write ``chunk_NNN.opus`` covering exactly ``[play_start, play_end]``.

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
    try:
        subprocess.run(
            cmd,
            input=wav_buf.getvalue(),
            check=True,
            capture_output=True,
            timeout=_OPUS_ENCODE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"opus encode timed out after {_OPUS_ENCODE_TIMEOUT_SECONDS:.0f}s"
        ) from exc
