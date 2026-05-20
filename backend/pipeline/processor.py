"""Chunked separation + crossfade.

The processor turns a video URL into a sequence of clean, music-free WAV chunks.
It coordinates the downloader, the engine, and the cache; it does not know
about HTTP, asyncio, or the extension.

Pipeline per chunk:

1. Download the relevant time range as a WAV (downloader).
2. Run source separation (engine).
3. Mix the kept stems (e.g. ``vocals + other``) into one chunk WAV.
4. Apply a half-window crossfade so adjacent chunks blend smoothly.
5. Write ``chunk_NNN.wav`` into the cache.

The full WAV (``full.wav``) is concatenated lazily, once all chunks exist.
"""

from __future__ import annotations

import logging
import math
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

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

log = logging.getLogger(__name__)

# (meta, phase) where phase is one of: 'downloading', 'separating', 'mixing',
# 'chunk_complete'. Phase 'chunk_complete' is the only one that guarantees
# meta.chunks_ready has been updated; the others are best-effort UI hints.
ProgressCb = Callable[[CacheMeta, str], None]


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


class Processor:
    def __init__(
        self,
        engine: Engine,
        cache: JobCache,
        *,
        chunk_seconds: float,
        chunk_overlap_seconds: float,
    ) -> None:
        self.engine = engine
        self.cache = cache
        self.chunk_seconds = chunk_seconds
        self.chunk_overlap_seconds = chunk_overlap_seconds

    # -- planning ------------------------------------------------------------

    def prepare(
        self,
        url: str,
        *,
        model: str,
        keep_stems: list[str],
    ) -> tuple[str, CacheMeta, VideoMetadata, list[ChunkPlan]]:
        """Probe the video, build/refresh the cache meta, and plan the chunks."""
        info = probe(url)
        key = self.cache.key(url, model, keep_stems)
        plans = plan_chunks(
            info.duration_seconds,
            self.chunk_seconds,
            self.chunk_overlap_seconds,
        )

        existing = self.cache.load_meta(key)
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
        on_progress: ProgressCb | None = None,
    ) -> str:
        """Run the full chunked pipeline. Returns the cache key.

        Safe to call on an already-cached job: missing chunks are filled in,
        and a fully-cached job returns immediately.
        """
        key, meta, _info, plans = self.prepare(
            url, model=model, keep_stems=keep_stems
        )

        if meta.complete:
            log.info("Cache hit for %s (%d chunks)", url, meta.total_chunks)
            return key

        # Download the full source once. Each chunk is sliced from this file
        # so cuts are sample-accurate (yt-dlp's per-range download cuts at the
        # nearest preceding keyframe, which drifts by 5-10 s on AAC/Opus).
        source_path = download_source(url, self.cache.source_dir(url))

        for plan in plans:
            if plan.index in meta.chunks_ready and self.cache.chunk_path(
                key, plan.index
            ).exists():
                continue
            self._process_one(
                source_path,
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

        # Concatenate when every chunk is on disk.
        all_present = all(
            self.cache.chunk_path(key, p.index).exists() for p in plans
        )
        if all_present:
            self._concatenate_full(key, plans)
            self.cache.mark_complete(key)

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
            emit("downloading")  # phase name kept for UI compatibility
            slice_source(source_path, raw, start=plan.start, end=plan.end)
            emit("separating")
            result = self.engine.separate(raw, tmp / "stems", model=model)
            emit("mixing")
            mixed = self._mix_stems(result.stems, keep_stems)
            self._write_chunk(mixed, result.sample_rate, key, plan)

    @staticmethod
    def _mix_stems(
        stems: dict[str, Path],
        keep: list[str],
    ) -> np.ndarray:
        """Sum the kept stems and return a (samples, channels) float32 array."""
        missing = [s for s in keep if s not in stems]
        if missing:
            raise RuntimeError(f"engine did not return stems: {missing}")

        mix: np.ndarray | None = None
        for name in keep:
            audio, _ = sf.read(str(stems[name]), always_2d=True, dtype="float32")
            mix = audio if mix is None else mix + audio
        assert mix is not None
        # Soft-clip to avoid hard ceiling: tanh keeps the loud-passage feel
        # without introducing audible square-wave clipping.
        peak = float(np.abs(mix).max() or 1.0)
        if peak > 1.0:
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
        # Atomic write so a crash mid-write doesn't poison the cache. ``format``
        # is explicit because the temp filename doesn't end in ``.wav``.
        tmp_path = out.with_suffix(".part")
        sf.write(
            str(tmp_path), trimmed, sample_rate, subtype="PCM_16", format="WAV"
        )
        tmp_path.replace(out)

    def _concatenate_full(self, key: str, plans: list[ChunkPlan]) -> None:
        full_path = self.cache.full_path(key)
        tmp_path = full_path.with_suffix(".part")
        # Concatenate using soundfile streaming so we don't load everything at
        # once. Chunks already have crossfades baked in, but here we just butt
        # them together — the fade-ins/outs handle the seam.
        first_chunk = sf.SoundFile(str(self.cache.chunk_path(key, plans[0].index)))
        sample_rate = first_chunk.samplerate
        channels = first_chunk.channels
        first_chunk.close()

        with sf.SoundFile(
            str(tmp_path),
            mode="w",
            samplerate=sample_rate,
            channels=channels,
            subtype="PCM_16",
            format="WAV",
        ) as out_f:
            for plan in plans:
                with sf.SoundFile(str(self.cache.chunk_path(key, plan.index))) as in_f:
                    block_size = sample_rate * 4
                    while True:
                        block = in_f.read(block_size, dtype="float32")
                        if not len(block):
                            break
                        out_f.write(block)
        tmp_path.replace(full_path)


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
