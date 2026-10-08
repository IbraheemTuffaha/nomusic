"""A small, supervised process that owns model execution.

The HTTP process owns admission, status and cache policy.  This module owns the
native model boundary: one spawned child loads the selected engine once and
executes one pipeline at a time.  Commands and observations cross a narrow
pickle queue so a wedged torch/ffmpeg call can be terminated and the next job
can start in a fresh process.

The worker intentionally uses ``spawn`` on every platform.  Forking a process
after importing torch or creating an MPS/CUDA context can duplicate allocator
state and deadlock in native code.  A worker is therefore disposable, while
the parent supervisor remains the authority for admission and generation
ownership.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass
import logging
import multiprocessing as mp
import os
from pathlib import Path
import queue
import signal
import threading
import time
from typing import Any, Callable

from nomusic.engines import get_engine
from nomusic.pipeline.cache import JobCache
from nomusic.pipeline.processor import Processor, RunHooks

log = logging.getLogger(__name__)


class WorkerAbandoned(RuntimeError):
    """The parent cancelled or expired the current worker run."""


class WorkerCrashed(RuntimeError):
    """The supervised child exited without reporting a terminal result."""


@dataclass(frozen=True)
class WorkerSettings:
    """Serializable settings needed by the child process."""

    engine_name: str
    cache_dir: str
    chunk_seconds: float
    chunk_overlap_seconds: float
    keep_source_after_complete: bool
    progressive_download: bool


def settings_for_worker(settings: Any) -> WorkerSettings:
    return WorkerSettings(
        engine_name=settings.engine_name,
        cache_dir=str(settings.cache_dir),
        chunk_seconds=settings.chunk_seconds,
        chunk_overlap_seconds=settings.chunk_overlap_seconds,
        keep_source_after_complete=settings.keep_source_after_complete,
        progressive_download=settings.progressive_download,
    )


class _ChildCancelled(Exception):
    pass


class _ChildChunkProvider:
    """Chunk ordering and cancellation state living beside the pipeline."""

    def __init__(self, commands: Any, run_id: int | None = None) -> None:
        self._commands = commands
        self._run_id = run_id
        self._cancelled = False
        self._configured = threading.Event()
        self._lock = threading.Lock()
        self._pending: collections.deque[int] = collections.deque()
        self._pending_priority: int | None = None
        self._total = 0

    def configure(self, total: int, done: list[int]) -> None:
        with self._lock:
            self._total = total
            ready = set(done)
            self._pending = collections.deque(i for i in range(total) if i not in ready)
            if self._pending_priority is not None:
                self._prioritize_locked(self._pending_priority)
                self._pending_priority = None
            self._configured.set()

    def poll(self) -> None:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            if not command:
                continue
            name = command[0]
            if name == "cancel" and (
                (self._run_id is None and len(command) == 1)
                or (self._run_id is not None and len(command) == 2 and command[1] == self._run_id)
            ):
                self._cancelled = True
            elif name == "prioritize" and (
                (self._run_id is None and len(command) > 1)
                or (self._run_id is not None and len(command) > 2 and command[1] == self._run_id)
            ):
                self.prioritize(int(command[-1]))

    def abort(self) -> None:
        self.poll()
        if self._cancelled:
            raise _ChildCancelled()

    def next(self) -> int | None:
        self.abort()
        if not self._configured.wait(0.1):
            self.abort()
        with self._lock:
            return self._pending.popleft() if self._pending else None

    def prioritize(self, from_chunk: int) -> None:
        with self._lock:
            if self._total <= 0:
                self._pending_priority = from_chunk
                return
            self._prioritize_locked(from_chunk)

    def _prioritize_locked(self, from_chunk: int) -> None:
        if not self._pending or self._total <= 0:
            return
        from_chunk = max(0, min(from_chunk, self._total - 1))
        pending = set(self._pending)
        self._pending = collections.deque(
            sorted(i for i in pending if i >= from_chunk)
            + sorted(i for i in pending if i < from_chunk)
        )


def _send(events: Any, event: dict[str, Any]) -> None:
    try:
        events.put(event)
    except (BrokenPipeError, EOFError, OSError):
        # The parent is already terminating the worker. There is no useful
        # recovery in the child and, most importantly, no reason to turn a
        # shutdown into a second traceback.
        pass


def _worker_main(settings: WorkerSettings, commands: Any, events: Any) -> None:
    """Process entry point. Keep imports and native initialization in the child."""
    logging.basicConfig(
        level=(logging.DEBUG if os.environ.get("NOMUSIC_DEBUG", "").strip().lower()
               in {"1", "true", "yes", "on"} else logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if os.name == "posix":
        try:
            os.setsid()
        except OSError:
            log.debug("worker could not create its own process group", exc_info=True)

    parent = mp.parent_process()
    if parent is not None:
        def stop_when_parent_exits() -> None:
            parent.join()
            try:
                if os.name == "posix":
                    os.killpg(os.getpid(), signal.SIGTERM)
                else:
                    os._exit(1)
            except (OSError, SystemExit):
                pass

        threading.Thread(
            target=stop_when_parent_exits,
            name="nomusic-worker-parent-watch",
            daemon=True,
        ).start()

    cache: JobCache | None = None
    try:
        engine = get_engine(settings.engine_name)
        cache = JobCache(Path(settings.cache_dir))
        processor = Processor(
            engine=engine,
            cache=cache,
            chunk_seconds=settings.chunk_seconds,
            chunk_overlap_seconds=settings.chunk_overlap_seconds,
            keep_source_after_complete=settings.keep_source_after_complete,
            progressive=settings.progressive_download,
        )
        _send(events, {"type": "started", "pid": os.getpid()})
        while True:
            command = commands.get()
            if not command:
                continue
            name = command[0]
            if name == "shutdown":
                return
            if name == "warmup":
                warmup_id = command[1] if len(command) > 1 else None
                try:
                    engine.warmup()
                    _send(events, {"type": "warmup", "warmup_id": warmup_id, "ok": True})
                except BaseException as exc:
                    _send(events, {
                        "type": "warmup", "warmup_id": warmup_id, "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                continue
            if name != "run" or len(command) != 6:
                _send(events, {
                    "type": "protocol_error",
                    "run_id": command[1] if len(command) > 1 else None,
                    "error": repr(command),
                })
                continue

            _, run_id, url, model, keep_stems, deadline = command
            provider = _ChildChunkProvider(commands, run_id)
            _send(events, {"type": "run_started", "run_id": run_id, "pid": os.getpid()})

            def send_run_event(kind: str, **fields: Any) -> None:
                _send(events, {"type": kind, "run_id": run_id, **fields})

            def abort() -> None:
                provider.abort()
                if deadline is not None and time.monotonic() >= deadline:
                    provider._cancelled = True
                    raise _ChildCancelled()

            def on_probed(info, plans, meta) -> None:
                provider.configure(meta.total_chunks, list(meta.chunks_ready))
                send_run_event("probed", info=info, plans=plans, meta=meta)

            def on_progress(meta, phase) -> None:
                send_run_event("progress", meta=meta, phase=phase)

            def on_download(fraction) -> None:
                send_run_event("download", fraction=fraction)

            def on_wait(fraction) -> None:
                send_run_event("wait_download", fraction=fraction)

            try:
                result = processor.run(
                    url,
                    model=model,
                    keep_stems=list(keep_stems),
                    hooks=RunHooks(
                        on_probed=on_probed,
                        on_progress=on_progress,
                        on_download_progress=on_download,
                        next_chunk_provider=provider.next,
                        abort_check=abort,
                        publish_check=abort,
                        on_wait_for_download=on_wait,
                    ),
                )
                abort()
                send_run_event("completed", key=result)
            except _ChildCancelled:
                send_run_event("cancelled")
            except BaseException as exc:
                send_run_event("error", error=f"{type(exc).__name__}: {exc}")
    except BaseException as exc:
        _send(events, {"type": "fatal", "error": f"{type(exc).__name__}: {exc}"})
    finally:
        if cache is not None:
            cache.close()


class SupervisedModelWorker:
    """Parent-side supervisor for one persistent model process.

    ``run`` is called from a registry worker thread. The process remains alive
    between runs so model weights are retained, but a cancellation that does
    not reach a cooperative boundary is terminated as a process group. The
    next run starts a clean child and never waits on stale native state.
    """

    def __init__(
        self,
        settings: Any,
        *,
        execution_timeout_seconds: float = 1800.0,
        cancel_grace_seconds: float = 5.0,
        target: Callable[..., None] = _worker_main,
        context: mp.context.BaseContext | None = None,
        warmup_timeout_seconds: float = 300.0,
    ) -> None:
        self.settings = settings_for_worker(settings)
        self.execution_timeout_seconds = execution_timeout_seconds
        self.cancel_grace_seconds = cancel_grace_seconds
        self.warmup_timeout_seconds = warmup_timeout_seconds
        self._operation_lock = threading.RLock()
        self._ctx = context or mp.get_context("spawn")
        self._target = target
        self._lock = threading.RLock()
        self._commands: Any | None = None
        self._events: Any | None = None
        self._process: mp.Process | None = None
        self._active_key: str | None = None
        self._active_run_id: int | None = None
        self._next_run_id = 0
        self._cancelled: set[str] = set()

    @property
    def process(self) -> mp.Process | None:
        with self._lock:
            return self._process

    @property
    def alive(self) -> bool:
        process = self.process
        return process is not None and process.is_alive()

    def start(self) -> None:
        with self._lock:
            if self.alive:
                return
            self._commands = self._ctx.Queue()
            self._events = self._ctx.Queue()
            process = self._ctx.Process(
                target=self._target,
                args=(self.settings, self._commands, self._events),
                name="nomusic-model-worker",
                daemon=False,
            )
            process.start()
            self._process = process

    def _ensure_started(self) -> None:
        if not self.alive:
            self._discard_dead()
            self.start()

    def _discard_dead(self) -> None:
        with self._lock:
            process = self._process
            if process is not None and not process.is_alive():
                process.join(timeout=0)
                self._process = None
                self._commands = None
                self._events = None

    def _terminate(self, *, force: bool = False) -> None:
        with self._lock:
            process = self._process
            if process is None:
                return
            if process.is_alive():
                if not force:
                    try:
                        process.terminate()
                    except (OSError, AttributeError):
                        pass
                    process.join(timeout=self.cancel_grace_seconds)
                if process.is_alive():
                    if os.name == "posix":
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except OSError:
                            pass
                    try:
                        process.kill()
                    except (OSError, AttributeError):
                        pass
                    process.join(timeout=5)
            else:
                process.join(timeout=0)
            self._process = None
            self._commands = None
            self._events = None

    def warmup(self, timeout: float | None = None) -> None:
        with self._operation_lock:
            self._ensure_started()
            assert self._commands is not None and self._events is not None
            warmup_id = self._next_run_id + 1
            self._commands.put(("warmup", warmup_id))
            deadline = time.monotonic() + (
                self.warmup_timeout_seconds if timeout is None else timeout
            )
            while time.monotonic() < deadline:
                try:
                    event = self._events.get(timeout=min(0.2, deadline - time.monotonic()))
                except queue.Empty:
                    if not self.alive:
                        error = "worker exited during warmup"
                        self._discard_dead()
                        raise WorkerCrashed(error)
                    continue
                if event.get("type") == "warmup" and event.get("warmup_id") == warmup_id:
                    if event.get("ok"):
                        return
                    raise WorkerCrashed(event.get("error", "worker warmup failed"))
                if event.get("type") == "fatal":
                    raise WorkerCrashed(event.get("error", "worker failed"))
            self._terminate(force=True)
            raise WorkerCrashed("worker warmup deadline expired")

    def cancel(self, key: str) -> None:
        with self._lock:
            self._cancelled.add(key)
            if self._active_key == key and self._commands is not None:
                try:
                    self._commands.put_nowait(("cancel", self._active_run_id))
                except (queue.Full, OSError):
                    pass

    def prioritize(self, key: str, from_chunk: int) -> None:
        with self._lock:
            if self._active_key == key and self._commands is not None:
                try:
                    self._commands.put_nowait(("prioritize", self._active_run_id, int(from_chunk)))
                except (queue.Full, OSError):
                    log.debug("worker prioritize queue unavailable", exc_info=True)

    def run(
        self,
        key: str,
        url: str,
        *,
        model: str,
        keep_stems: list[str],
        hooks: RunHooks,
        abort_check: Callable[[], None] | None = None,
        publish_check: Callable[[], None] | None = None,
    ) -> str:
        with self._operation_lock:
            self._ensure_started()
            assert self._commands is not None and self._events is not None
            with self._lock:
                if self._active_key is not None:
                    raise WorkerCrashed("model worker already has an active execution")
                self._next_run_id += 1
                run_id = self._next_run_id
                self._active_key = key
            self._active_run_id = run_id
            self._cancelled.discard(key)
            deadline = time.monotonic() + self.execution_timeout_seconds
            self._commands.put(("run", run_id, url, model, list(keep_stems), deadline))
            try:
                cancel_sent = False
                cancel_at = 0.0
                while True:
                    now = time.monotonic()
                    if abort_check is not None:
                        try:
                            abort_check()
                        except BaseException:
                            if not cancel_sent:
                                self.cancel(key)
                                cancel_sent = True
                                cancel_at = now
                            elif now - cancel_at >= self.cancel_grace_seconds:
                                self._terminate(force=True)
                                raise
                    if now >= deadline and not cancel_sent:
                        self.cancel(key)
                        cancel_sent = True
                        cancel_at = now
                    if cancel_sent and now - cancel_at >= self.cancel_grace_seconds:
                        self._terminate(force=True)
                        raise WorkerAbandoned(f"execution deadline/cancel exceeded for {key}")
                    try:
                        event = self._events.get(timeout=0.1)
                    except queue.Empty:
                        if not self.alive:
                            self._discard_dead()
                            raise WorkerCrashed("model worker exited during execution")
                        continue
                    if event.get("run_id") != run_id:
                        continue
                    kind = event.get("type")
                    if kind == "probed" and hooks.on_probed:
                        hooks.on_probed(event["info"], event["plans"], event["meta"])
                    elif kind == "progress" and hooks.on_progress:
                        hooks.on_progress(event["meta"], event["phase"])
                    elif kind == "download" and hooks.on_download_progress:
                        hooks.on_download_progress(event["fraction"])
                    elif kind == "wait_download" and hooks.on_wait_for_download:
                        hooks.on_wait_for_download(event["fraction"])
                    elif kind == "completed":
                        if cancel_sent:
                            raise WorkerAbandoned(f"cancelled execution completed: {key}")
                        if publish_check:
                            publish_check()
                        return event["key"]
                    elif kind == "cancelled":
                        raise WorkerAbandoned(f"execution cancelled: {key}")
                    elif kind == "error":
                        raise RuntimeError(event.get("error", "worker execution failed"))
                    elif kind in {"fatal", "protocol_error"}:
                        raise WorkerCrashed(event.get("error", "worker protocol failure"))
            finally:
                with self._lock:
                    if self._active_run_id == run_id:
                        self._active_key = None
                        self._active_run_id = None

    def force_shutdown(self) -> None:
        """Terminate the child immediately before a process-level exit."""
        self._terminate(force=True)

    def shutdown(self) -> None:
        with self._lock:
            commands = self._commands
            process = self._process
            if process is None:
                return
            if commands is not None and process.is_alive():
                try:
                    commands.put_nowait(("shutdown",))
                except (OSError, queue.Full):
                    pass
        process.join(timeout=self.cancel_grace_seconds)
        if process.is_alive():
            self._terminate(force=True)
        else:
            self._discard_dead()
