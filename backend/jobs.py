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
    DOWNLOADING = "downloading"  # only the first call to download_range; coarse
    PROCESSING = "processing"
    READY = "ready"
    ERROR = "error"


@dataclass
class JobStatus:
    job_id: str  # equals the cache key
    state: JobState
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
        # Cheap probe + plan up front so the client gets a job_id, total_chunks,
        # and duration immediately. ``prepare()`` is fast: a yt-dlp metadata
        # call, no media download.
        key, meta, info, _plans = self.processor.prepare(
            url, model=model, keep_stems=keep_stems
        )

        with self._lock:
            existing = self._jobs.get(key)
            if existing and existing.state in (
                JobState.QUEUED,
                JobState.DOWNLOADING,
                JobState.PROCESSING,
            ):
                return existing

            status = JobStatus(
                job_id=key,
                cache_key=key,
                state=(
                    JobState.READY
                    if meta.complete
                    else JobState.QUEUED
                ),
                chunks_ready=len(meta.chunks_ready),
                total_chunks=meta.total_chunks,
                duration_seconds=meta.duration_seconds,
                title=info.title,
            )
            self._jobs[key] = status

            if meta.complete:
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
        return JobStatus(
            job_id=job_id,
            cache_key=job_id,
            state=JobState.READY if meta.complete else JobState.QUEUED,
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
        self._update(key, state=JobState.DOWNLOADING)
        try:
            with self._gpu_lock:
                self.processor.run(
                    url,
                    model=model,
                    keep_stems=keep_stems,
                    on_progress=lambda meta: self._on_progress(key, meta),
                )
            meta = self.cache.load_meta(key)
            chunks_ready = len(meta.chunks_ready) if meta else 0
            self._update(
                key,
                state=JobState.READY,
                chunks_ready=chunks_ready,
            )
            log.info("Job %s ready", key)
        except Exception as exc:
            log.exception("Job %s failed", key)
            self._update(
                key,
                state=JobState.ERROR,
                error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}",
            )

    def _on_progress(self, key: str, meta: CacheMeta) -> None:
        self._update(
            key,
            state=JobState.PROCESSING,
            chunks_ready=len(meta.chunks_ready),
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
