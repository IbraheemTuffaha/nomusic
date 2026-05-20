"""In-process job registry and background worker.

The HTTP layer is intentionally thin: clients POST a job, then poll
``/status/{id}`` and pull ready chunks. All state lives here so server.py stays
small and the worker stays testable without spinning up uvicorn.

Concurrency model: one background ``threading.Thread`` per job. The MLX engine
holds the GPU exclusively (Apple unified memory; demucs serializes anyway), so
we serialize *runs* across jobs with a global lock; multiple jobs queue up
rather than fighting for the GPU.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional

from pipeline.cache import CacheMeta, JobCache
from pipeline.processor import Processor

log = logging.getLogger(__name__)


class JobState(str, Enum):
    QUEUED = "queued"
    PROBING = "probing"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    READY = "ready"
    ERROR = "error"


_PHASE_LABELS: dict[str, str] = {
    "queued": "Queued",
    "probing": "Inspecting video",
    "downloading": "Downloading video",
    "processing": "Removing music",
    "ready": "Ready",
    "error": "Error",
}


@dataclass
class JobStatus:
    job_id: str  # equals the cache key
    state: JobState
    # phase mirrors state.value for the UI; kept separate so future phases
    # (probing, mixing, post-processing) can split out without renaming
    # existing states.
    phase: str = "queued"
    # phase_progress is 0..1 within the current phase. None = indeterminate
    # (rare; only when yt-dlp can't report total bytes).
    phase_progress: float | None = 0.0
    phase_label: str = "Queued"
    chunks_ready: int = 0
    total_chunks: int = 0
    duration_seconds: float = 0.0
    title: str = ""
    error: str = ""
    cache_key: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["state"] = self.state.value
        return d


class JobRegistry:
    def __init__(self, processor: Processor, cache: JobCache) -> None:
        self.processor = processor
        self.cache = cache
        self._jobs: dict[str, JobStatus] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        # One job runs at a time on the engine; further jobs block here.
        self._gpu_lock = threading.Lock()

    # -- public --------------------------------------------------------------

    def submit(
        self,
        url: str,
        *,
        model: str,
        keep_stems: list[str],
    ) -> JobStatus:
        # No probe here: the yt-dlp metadata call takes 3-6s on YouTube
        # because of the JS challenge, and blocking /process on it made the
        # button look stuck on "Starting". The cache key only needs (url,
        # model, stems), all of which we have, so we can return immediately
        # and let the worker thread do the slow probe.
        key = self.cache.key(
            url,
            model,
            list(keep_stems),
            chunk_seconds=self.processor.chunk_seconds,
            chunk_overlap_seconds=self.processor.chunk_overlap_seconds,
        )
        existing_meta = self.cache.load_meta(key)

        with self._lock:
            existing = self._jobs.get(key)
            if existing and existing.state in (
                JobState.QUEUED,
                JobState.PROBING,
                JobState.DOWNLOADING,
                JobState.PROCESSING,
            ):
                return existing

            if existing_meta and existing_meta.complete:
                initial_state = JobState.READY
                progress = 1.0
                # Replay is a "use" of the cache entry; renew its TTL by
                # bumping meta.json's mtime so the hourly sweep doesn't
                # reap a video the user is actively re-watching.
                self.cache.touch(key)
            else:
                initial_state = JobState.QUEUED
                progress = 0.0
            status = JobStatus(
                job_id=key,
                cache_key=key,
                state=initial_state,
                phase=initial_state.value,
                phase_progress=progress,
                phase_label=_PHASE_LABELS[initial_state.value],
                chunks_ready=len(existing_meta.chunks_ready) if existing_meta else 0,
                total_chunks=existing_meta.total_chunks if existing_meta else 0,
                duration_seconds=existing_meta.duration_seconds if existing_meta else 0.0,
                title=existing_meta.title if existing_meta else "",
            )
            self._jobs[key] = status

            if initial_state == JobState.READY:
                return status

            t = threading.Thread(
                target=self._run,
                args=(key, url, model, keep_stems),
                name=f"nomusic-job-{key[:6]}",
                daemon=True,
            )
            self._threads[key] = t
            t.start()
            return status

    def get(self, job_id: str) -> Optional[JobStatus]:
        with self._lock:
            status = self._jobs.get(job_id)
            if status is not None:
                return status
        # Job not in this process's memory but may be fully cached on disk —
        # surface it as READY so the client can stream chunks anyway.
        meta = self.cache.load_meta(job_id)
        if meta is None:
            return None
        state = JobState.READY if meta.complete else JobState.QUEUED
        return JobStatus(
            job_id=job_id,
            cache_key=job_id,
            state=state,
            phase=state.value,
            phase_progress=1.0 if meta.complete else 0.0,
            phase_label=_PHASE_LABELS[state.value],
            chunks_ready=len(meta.chunks_ready),
            total_chunks=meta.total_chunks,
            duration_seconds=meta.duration_seconds,
            title=meta.title,
        )

    # -- worker --------------------------------------------------------------

    def _run(
        self,
        key: str,
        url: str,
        model: str,
        keep_stems: list[str],
    ) -> None:
        # First user-visible phase. Indeterminate progress (None) because we
        # don't know how long the JS-challenge probe will take.
        self._enter_phase(key, JobState.PROBING, progress=None)
        try:
            with self._gpu_lock:
                self.processor.run(
                    url,
                    model=model,
                    keep_stems=keep_stems,
                    on_probed=lambda info, plans, meta: self._on_probed(
                        key, info, plans, meta
                    ),
                    on_progress=lambda meta, phase: self._on_separation_progress(
                        key, meta, phase
                    ),
                    on_download_progress=lambda p: self._on_download_progress(
                        key, p
                    ),
                )
            meta = self.cache.load_meta(key)
            chunks_ready = len(meta.chunks_ready) if meta else 0
            total = meta.total_chunks if meta else 0
            self._enter_phase(
                key,
                JobState.READY,
                progress=1.0,
                chunks_ready=chunks_ready,
                total_chunks=total,
            )
            log.info("Job %s ready", key)
        except Exception as exc:
            log.exception("Job %s failed", key)
            self._enter_phase(
                key,
                JobState.ERROR,
                progress=1.0,
                error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}",
            )

    def _enter_phase(
        self,
        key: str,
        state: JobState,
        *,
        progress: float | None,
        **extra,
    ) -> None:
        self._update(
            key,
            state=state,
            phase=state.value,
            phase_progress=progress,
            phase_label=_PHASE_LABELS.get(state.value, state.value),
            **extra,
        )

    def _on_probed(
        self,
        key: str,
        info,
        plans,
        meta: CacheMeta,
    ) -> None:
        # Probe done. Surface metadata so the popup / button can show real
        # duration + title; phase stays at PROBING until the first download
        # tick flips it to DOWNLOADING.
        self._update(
            key,
            title=info.title,
            duration_seconds=info.duration_seconds,
            total_chunks=meta.total_chunks,
            chunks_ready=len(meta.chunks_ready),
        )

    def _on_download_progress(self, key: str, fraction: float | None) -> None:
        # yt-dlp emits 1.0 on completion; we stay in DOWNLOADING until the
        # first separation chunk fires, so the UI doesn't briefly snap to
        # 0% on the phase boundary.
        self._update(key, state=JobState.DOWNLOADING, phase="downloading",
                     phase_label=_PHASE_LABELS["downloading"],
                     phase_progress=fraction)

    def _on_separation_progress(
        self,
        key: str,
        meta: CacheMeta,
        phase: str,
    ) -> None:
        done = len(meta.chunks_ready)
        total = max(1, meta.total_chunks)
        # Smooth the bar inside a chunk: 'separating' = chunk just started
        # (counts as half done), 'chunk_complete' = chunk fully done.
        per_chunk = 0.0
        if phase == "separating":
            per_chunk = 0.3
        elif phase == "mixing":
            per_chunk = 0.7
        progress = min(1.0, (done + per_chunk) / total)
        self._update(
            key,
            state=JobState.PROCESSING,
            phase="processing",
            phase_label=_PHASE_LABELS["processing"],
            phase_progress=progress,
            chunks_ready=done,
            total_chunks=meta.total_chunks,
        )

    def _update(self, key: str, **fields) -> None:
        with self._lock:
            status = self._jobs.get(key)
            if status is None:
                return
            for name, value in fields.items():
                setattr(status, name, value)
            status.updated_at = time.time()
