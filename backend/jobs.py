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

import asyncio
import collections
import logging
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Callable, Optional

from config import SETTINGS
from pipeline.cache import CacheMeta, JobCache
from pipeline.processor import Processor

log = logging.getLogger(__name__)


class WorkerAbandoned(Exception):
    """Raised from the chunk provider when a job has had no SSE subscriber for
    longer than ``idle_timeout_seconds``. It propagates up through
    ``Processor.run`` and out of ``with self._gpu_lock:``, so the GPU lock is
    released naturally on the way out and the worker thread exits. The client
    re-spawns the job from disk-cached progress on its next click."""


class JobState(str, Enum):
    QUEUED = "queued"
    PROBING = "probing"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    READY = "ready"
    ERROR = "error"


# Button labels. Kept to a single word each and device-neutral: the audio is
# fetched to the *server*, not the browser, and it's audio not video, so we
# avoid "Downloading video". "Fetching" covers the yt-dlp pull without implying
# where the bytes land; "Preparing" covers the metadata probe + planning;
# "Removing" is the separation phase (music removal).
_PHASE_LABELS: dict[str, str] = {
    "queued": "Queued",
    "probing": "Preparing",
    "downloading": "Fetching",
    "processing": "Removing",
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
    # Sorted list of completed chunk indices. With seek-driven reordering
    # these are no longer contiguous from 0, so the client needs the actual
    # set, not just the count, to know which /chunk/{idx} URLs to fetch.
    ready_chunks: list[int] = field(default_factory=list)
    total_chunks: int = 0
    duration_seconds: float = 0.0
    title: str = ""
    error: str = ""
    cache_key: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, object]:
        d = asdict(self)
        d["state"] = self.state.value
        return d


class _JobControl:
    """Per-job runtime control surface.

    Owns the pending-chunk deque that the worker pops from and the HTTP
    layer mutates via ``JobRegistry.prioritize``. The deque holds chunk
    indices in the order they should be processed; under its own lock so
    a /prioritize call can reorder it while the worker is mid-chunk.
    """

    def __init__(self, total_chunks: int, done: set[int]) -> None:
        self.lock = threading.Lock()
        self.pending: collections.deque[int] = collections.deque(
            i for i in range(total_chunks) if i not in done
        )

    def next_chunk(self) -> Optional[int]:
        with self.lock:
            return self.pending.popleft() if self.pending else None

    def prioritize(self, from_chunk: int) -> None:
        """Rotate to [from_chunk, from_chunk+1, …, N-1, 0, 1, …, from_chunk-1],
        keeping only entries currently still pending. No-op if the queue is
        empty.

        We sort both halves explicitly: after even one reorder the deque is
        no longer in ascending order, so a position-preserving filter would
        leave whichever chunks happened to be near the front of the prior
        ordering ahead of the new seek target. Sorting guarantees the next
        pop is always ``from_chunk`` (or the next still-pending index after
        it) regardless of how scrambled the prior order was.
        """
        with self.lock:
            if not self.pending:
                return
            pending = set(self.pending)
            front = sorted(i for i in pending if i >= from_chunk)
            back = sorted(i for i in pending if i < from_chunk)
            self.pending = collections.deque(front + back)


class JobRegistry:
    def __init__(self, processor: Processor, cache: JobCache) -> None:
        self.processor = processor
        self.cache = cache
        self._jobs: dict[str, JobStatus] = {}
        self._threads: dict[str, threading.Thread] = {}
        # Per-job control for chunk ordering. Built lazily inside
        # ``_on_probed`` (once we know total_chunks) and discarded when the
        # worker finishes.
        self._controls: dict[str, _JobControl] = {}
        # If /prioritize arrives before the control exists (job is still
        # probing/downloading), stash the hint here and apply on creation.
        self._pending_priority: dict[str, int] = {}
        # SSE bookkeeping. ``_subscribers`` maps a job key to the live
        # asyncio.Queues feeding each open /events stream; the worker thread
        # pushes status snapshots onto them via the event loop.
        # ``_last_disconnect_at`` records when a job's last subscriber dropped
        # off, which the idle-abandon timer reads. ``_loop`` is the running
        # asyncio loop, captured at startup so a worker thread can hand work
        # back to it safely.
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._last_disconnect_at: dict[str, float] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        # Keys whose worker should stop at its next chunk boundary. Set either
        # by the idle-abandon decision (atomically, under _lock, so a racing
        # submit() can't reuse a job that's already given up) or by
        # ``abandon_all`` (cache clear). The provider checks it first thing.
        self._abandoning: set[str] = set()
        self._lock = threading.Lock()
        # One job runs at a time on the engine; further jobs block here.
        self._gpu_lock = threading.Lock()

    # -- SSE subscription ----------------------------------------------------

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Capture the server's event loop so worker threads can schedule
        queue writes onto it. Called once from the FastAPI lifespan startup."""
        self._loop = loop

    def subscribe(self, key: str) -> asyncio.Queue:
        """Register an /events stream for ``key`` and return its queue.

        Clearing ``_last_disconnect_at`` here means a returning viewer resets
        the idle clock the instant their stream opens."""
        q: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subscribers.setdefault(key, []).append(q)
            self._last_disconnect_at.pop(key, None)
        return q

    def unsubscribe(self, key: str, q: asyncio.Queue) -> None:
        """Drop one stream's queue. When the last subscriber for a still-live
        job leaves, stamp the disconnect time so the idle timer can start
        counting. We only stamp when the job is still in ``_jobs`` — there's
        no point timing out a job that already finished."""
        with self._lock:
            subs = self._subscribers.get(key)
            if not subs:
                return
            if q in subs:
                subs.remove(q)
            if not subs and key in self._jobs:
                self._last_disconnect_at[key] = time.time()

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
            if (
                existing
                and existing.state
                in (
                    JobState.QUEUED,
                    JobState.PROBING,
                    JobState.DOWNLOADING,
                    JobState.PROCESSING,
                )
                and key not in self._abandoning
            ):
                # A returning/duplicate client. Restart the idle clock so the
                # worker doesn't abandon the job in the window before this
                # client's /events stream (re)connects, then hand back the
                # live job. Skipped when the job is mid-abandon (``_abandoning``)
                # so a /process landing during the unwind respawns a fresh
                # worker instead of adopting one that's about to vanish.
                self._last_disconnect_at[key] = time.time()
                return existing

            # New job, or one superseding an abandoning predecessor: clear any
            # stale abandon mark so the fresh worker isn't killed on its first
            # chunk boundary.
            self._abandoning.discard(key)

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
                ready_chunks=sorted(existing_meta.chunks_ready) if existing_meta else [],
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
            ready_chunks=sorted(meta.chunks_ready),
            total_chunks=meta.total_chunks,
            duration_seconds=meta.duration_seconds,
            title=meta.title,
        )

    def memory_gc(self) -> int:
        """Drop in-memory JobStatus entries whose disk cache is already gone.

        Completed and errored jobs are intentionally kept in ``_jobs`` after
        their worker exits so ``/status`` keeps reporting the final state.
        This bounds that retention: once the disk TTL sweep has reaped a job's
        cache directory (or it never had one), the in-memory entry is dead
        weight, so we reclaim it. Jobs with a live worker thread are never
        touched. Returns the number of entries dropped.
        """
        with self._lock:
            candidates = [k for k in self._jobs if k not in self._threads]
        dropped = 0
        for key in candidates:
            # Disk check is I/O; do it outside the lock.
            if self.cache.load_meta(key) is not None:
                continue
            with self._lock:
                if key in self._threads:
                    continue  # a fresh submit spawned a worker since we looked
                if self._jobs.pop(key, None) is not None:
                    self._subscribers.pop(key, None)
                    self._last_disconnect_at.pop(key, None)
                    dropped += 1
        return dropped

    def abandon_all(self) -> None:
        """Signal every running worker to abandon at its next chunk boundary
        and drop all in-memory job state. Used by /cache/clear so wiping the
        cache out from under live workers doesn't leave them mid-pipeline
        writing into directories that no longer exist — instead each worker
        unwinds cleanly out of the GPU lock via ``WorkerAbandoned`` and runs
        its own ``finally`` cleanup.
        """
        with self._lock:
            for key in self._jobs:
                self._abandoning.add(key)
            self._jobs.clear()
            self._subscribers.clear()
            self._last_disconnect_at.clear()

    # -- worker --------------------------------------------------------------

    def _run(
        self,
        key: str,
        url: str,
        model: str,
        keep_stems: list[str],
    ) -> None:
        # Capture our own identity so the finally cleanup only retracts state
        # we still own. A submit() racing our abandon-unwind may have already
        # replaced _jobs[key]/_threads[key] with a fresh worker; we must not
        # clobber that one's bookkeeping.
        my_thread = threading.current_thread()
        with self._lock:
            my_status = self._jobs.get(key)
        abandoned = False
        try:
            try:
                with self._gpu_lock:
                    # Stay in QUEUED while waiting for the GPU lock. Only flip
                    # to PROBING once we actually own it, so a second
                    # concurrent job honestly reports "Queued" instead of
                    # falsely showing "Inspecting video" while really blocked.
                    self._enter_phase(key, JobState.PROBING, progress=None)
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
                        next_chunk_provider=self._make_chunk_provider(key),
                        abort_check=lambda: self._raise_if_abandoned(
                            key, SETTINGS.idle_timeout_seconds
                        ),
                        on_wait_for_download=lambda frac: self._on_wait_for_download(
                            key, frac
                        ),
                    )
                meta = self.cache.load_meta(key)
                chunks_ready = len(meta.chunks_ready) if meta else 0
                ready_chunks = sorted(meta.chunks_ready) if meta else []
                total = meta.total_chunks if meta else 0
                self._enter_phase(
                    key,
                    JobState.READY,
                    progress=1.0,
                    chunks_ready=chunks_ready,
                    ready_chunks=ready_chunks,
                    total_chunks=total,
                )
                log.info("Job %s ready", key)
            except WorkerAbandoned:
                # Listed before the generic handler so an idle-abandon isn't
                # mistaken for a failure. The GPU lock has already released via
                # the with-block unwind; just flag it for finally to clean up.
                abandoned = True
                # ``%gs`` renders 30.0 as "30s" (not "30.0s") so the value
                # matches the grep documented in the verification steps.
                log.info(
                    "Job %s abandoned: idle > %gs",
                    key,
                    SETTINGS.idle_timeout_seconds,
                )
            except Exception as exc:
                log.exception("Job %s failed", key)
                self._enter_phase(
                    key,
                    JobState.ERROR,
                    progress=1.0,
                    error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}",
                )
        finally:
            # The control + any orphaned priority hint are only meaningful
            # while a worker is consuming chunks; drop them unconditionally.
            with self._lock:
                self._controls.pop(key, None)
                self._pending_priority.pop(key, None)
                # Only retract the shared bookkeeping if we're still the
                # registered worker for this key. If a submit() raced our
                # unwind and installed a fresh worker, _threads[key] now points
                # at that thread, not us — leave its _threads/_subscribers/
                # _jobs entries alone. Dropping our own handle marks the job as
                # having no live worker, which is what ``memory_gc`` keys off.
                if self._threads.get(key) is my_thread:
                    self._threads.pop(key, None)
                    self._subscribers.pop(key, None)
                    self._last_disconnect_at.pop(key, None)
                    self._abandoning.discard(key)
                    # The terminal _update(READY/ERROR) above already enqueued
                    # its snapshot to every open stream, so dropping
                    # _subscribers here loses nothing; each stream still drains
                    # and closes itself. A stream that opens after this sees the
                    # terminal state from disk and short-circuits.
                    if abandoned and self._jobs.get(key) is my_status:
                        # Drop the JobStatus so a later /process spawns a fresh
                        # worker (resuming from disk-cached chunks) rather than
                        # finding a stale entry. Completed/errored jobs are kept
                        # for /status until memory_gc reclaims them.
                        self._jobs.pop(key, None)

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
            ready_chunks=sorted(meta.chunks_ready),
        )
        # Now that we know total_chunks, build the per-job control. If a
        # /prioritize call beat us here, apply the stashed hint now.
        with self._lock:
            if key not in self._controls:
                self._controls[key] = _JobControl(
                    total_chunks=meta.total_chunks,
                    done=set(meta.chunks_ready),
                )
            hint = self._pending_priority.pop(key, None)
        if hint is not None:
            self._controls[key].prioritize(hint)

    def _on_download_progress(self, key: str, fraction: float | None) -> None:
        # In progressive mode the download runs concurrently with separation,
        # so download ticks ("Fetching", download %) and separation ticks
        # ("Removing", chunk %) interleave. Once separation has started, keep
        # the UI on "Removing" — flipping back to "Fetching" every download
        # tick just flickers the label and bounces the percentage between two
        # different scales. The download still proceeds in the background.
        with self._lock:
            status = self._jobs.get(key)
            if status is not None and status.state in (
                JobState.PROCESSING,
                JobState.READY,
                JobState.ERROR,
            ):
                return
        self._update(key, state=JobState.DOWNLOADING, phase="downloading",
                     phase_label=_PHASE_LABELS["downloading"],
                     phase_progress=fraction)

    def _on_wait_for_download(self, key: str, fraction: float | None) -> None:
        # Separation is blocked waiting for the download to reach this chunk —
        # the user seeked past the downloaded point, or the download is slower
        # than separation. This is the one case where flipping back to
        # "Fetching" is right: playback is genuinely gated on the download, not
        # on separation. Unlike _on_download_progress this isn't suppressed
        # during PROCESSING, because here the download IS the bottleneck.
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
            ready_chunks=sorted(meta.chunks_ready),
            total_chunks=meta.total_chunks,
        )

    # -- prioritization ------------------------------------------------------

    def _make_chunk_provider(self, key: str) -> Callable[[], Optional[int]]:
        """Build the callable that ``Processor.run`` calls between chunks.

        Returns the next chunk index, ``None`` when the queue is exhausted, or
        raises ``WorkerAbandoned`` when nobody has been streaming status for
        longer than ``idle_timeout_seconds``. The idle check runs here — at the
        chunk boundary — so abandoning costs at most one in-flight chunk and
        unwinds cleanly out of the GPU lock.
        """
        idle_timeout = SETTINGS.idle_timeout_seconds

        def provider() -> Optional[int]:
            self._raise_if_abandoned(key, idle_timeout)
            with self._lock:
                control = self._controls.get(key)
            if control is None:
                return None
            return control.next_chunk()

        return provider

    def _raise_if_abandoned(self, key: str, idle_timeout: float) -> None:
        """Raise ``WorkerAbandoned`` if the job has been flagged (idle decision
        on a prior call, or a cache clear) or has now gone idle. The whole
        check + flag-set happens under the lock so it can't interleave with a
        submit() deciding whether to adopt the job. Shared by the chunk
        provider and the progressive-download abort hook so a pause that lands
        mid-download still releases the GPU promptly."""
        with self._lock:
            if key in self._abandoning:
                raise WorkerAbandoned
            if idle_timeout <= 0:
                return
            if self._subscribers.get(key):
                return
            # No subscriber: count from the last disconnect, or from job
            # creation if nobody ever connected (covers a /process whose client
            # never opened /events). Mark _abandoning before releasing the lock
            # so a concurrent submit() sees a job that has committed to dying
            # and respawns instead of adopting it.
            last_disc = self._last_disconnect_at.get(key)
            status = self._jobs.get(key)
            created_at = status.created_at if status else time.time()
            ref = last_disc if last_disc is not None else created_at
            if time.time() - ref >= idle_timeout:
                self._abandoning.add(key)
                raise WorkerAbandoned

    def prioritize(self, key: str, from_chunk: int) -> bool:
        """Rotate the job's pending-chunk order so ``from_chunk`` is next.

        Returns False if the job is unknown or already done. If the control
        hasn't been built yet (we're still probing/downloading), the hint
        is stashed and applied as soon as the control comes into being.
        """
        with self._lock:
            if key not in self._jobs:
                log.info("prioritize: unknown job key=%s from_chunk=%d", key, from_chunk)
                return False
            control = self._controls.get(key)
            if control is None:
                self._pending_priority[key] = from_chunk
                log.info(
                    "prioritize: stashed (control not yet built) key=%s from_chunk=%d",
                    key, from_chunk,
                )
                return True
        before_size = len(control.pending)
        before_head = list(control.pending)[:5]
        control.prioritize(from_chunk)
        after_head = list(control.pending)[:5]
        log.info(
            "prioritize: key=%s from_chunk=%d pending=%d head_before=%s head_after=%s",
            key, from_chunk, before_size, before_head, after_head,
        )
        return True

    # -- internals -----------------------------------------------------------

    def _update(self, key: str, **fields) -> None:
        snapshot: dict | None = None
        subs: list[asyncio.Queue] = []
        with self._lock:
            status = self._jobs.get(key)
            if status is None:
                return
            for name, value in fields.items():
                setattr(status, name, value)
            status.updated_at = time.time()
            # Snapshot inside the lock and hand the immutable dict — not the
            # live JobStatus — to subscribers, so the worker can keep mutating
            # status while an SSE coroutine serializes a past state.
            subs = list(self._subscribers.get(key, []))
            if subs:
                snapshot = status.to_dict()
        # call_soon_threadsafe hops from this worker thread to the event loop,
        # which is the only safe way to touch an asyncio.Queue from off-loop.
        # Guard against a loop that's closing during shutdown/teardown: a
        # daemon worker mid-_update would otherwise raise RuntimeError("Event
        # loop is closed") into the separation callbacks and mislabel the job
        # ERROR. Dropping the snapshot is correct — the process is going away.
        loop = self._loop
        if snapshot is not None and loop is not None and not loop.is_closed():
            for q in subs:
                try:
                    loop.call_soon_threadsafe(q.put_nowait, snapshot)
                except RuntimeError:
                    break
