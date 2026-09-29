"""One lifespan's services and background work.

Construction is inert. Shutdown signals cooperative work and joins it before
releasing services. The CLI may discard daemon model preload at process exit;
its adapter bounds the total drain and provides an emergency process exit.
Embedded lifespans still own all their threads through normal cleanup.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable

from nomusic.config import Settings
from nomusic.engines.base import Engine
from nomusic.jobs import JobRegistry
from nomusic.pipeline.cache import JobCache
from nomusic.pipeline.processor import Processor

log = logging.getLogger(__name__)


class Services:
    def __init__(self, settings: Settings, engine_factory: Callable[[str], Engine]) -> None:
        self.settings = settings
        self._engine_factory = engine_factory
        self.engine: Engine | None = None
        self.cache: JobCache | None = None
        self.registry: JobRegistry | None = None
        self._stop = threading.Event()
        self.shutdown_requested = False
        self._threads: list[threading.Thread] = []
        # The CLI owns process termination, so a daemon model preload need not
        # delay an otherwise drained server. Embedded lifespans still join it.
        self.wait_for_warmup = True
        self.discarded_preload: threading.Thread | None = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called once inside lifespan, with shutdown guaranteed even on failure."""
        if self.engine is not None or self._stop.is_set():
            raise RuntimeError("Services instances cannot be restarted")
        settings = self.settings
        self.engine = self._engine_factory(settings.engine_name)
        self.cache = JobCache(settings.cache_dir)
        processor = Processor(
            engine=self.engine,
            cache=self.cache,
            chunk_seconds=settings.chunk_seconds,
            chunk_overlap_seconds=settings.chunk_overlap_seconds,
            keep_source_after_complete=settings.keep_source_after_complete,
            progressive=settings.progressive_download,
        )
        self.registry = JobRegistry(processor=processor, cache=self.cache)
        self.registry.attach_loop(loop)

        if settings.cache_ttl_days > 0 and settings.cache_sweep_interval_seconds > 0:
            self._spawn(
                "nomusic-cache-ttl",
                lambda: self._repeat(
                    self._sweep_cache, settings.cache_sweep_interval_seconds, immediate=True,
                ),
            )
        if settings.memory_gc_interval_seconds > 0:
            self._spawn(
                "nomusic-memory-gc",
                lambda: self._repeat(self._collect_jobs, settings.memory_gc_interval_seconds),
            )
        self._spawn("nomusic-engine-warmup", self._warmup)

    def _spawn(self, name: str, target: Callable[[], None]) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _repeat(self, action: Callable[[], None], interval: float, *, immediate=False) -> None:
        # Event.wait wakes immediately on stop, even with hour-long intervals.
        if not immediate and self._stop.wait(interval):
            return
        while not self._stop.is_set():
            try:
                action()
            except Exception:
                log.exception("Maintenance pass failed; will retry")
            if self._stop.wait(interval):
                return

    def _sweep_cache(self) -> None:
        assert self.cache is not None
        removed, freed = self.cache.sweep_older_than(self.settings.cache_ttl_days * 86400)
        if removed:
            log.info("TTL sweep: removed %d entries, freed %d bytes", removed, freed)

    def _collect_jobs(self) -> None:
        assert self.registry is not None
        dropped = self.registry.memory_gc()
        if dropped:
            log.info("Memory GC dropped %d stale in-memory job(s)", dropped)

    def _warmup(self) -> None:
        engine = self.engine
        if self._stop.is_set() or engine is None:
            return
        try:
            engine.warmup()
            log.info("Engine warmup complete")
        except Exception:
            # Preserve lazy retry on first processing request. Readiness and
            # initialization diagnostics are separate from thread ownership.
            log.exception("Engine warmup failed; will load lazily on first job")

    @property
    def stopping(self) -> bool:
        return self.shutdown_requested or self._stop.is_set()

    def begin_shutdown(self) -> None:
        """Stop admission/maintenance and close status streams before HTTP drain."""
        self.shutdown_requested = True
        self._stop.set()
        if self.registry is not None:
            self.registry.begin_shutdown()

    def shutdown(self) -> None:
        """Join from outside the event loop so final queue callbacks can run."""
        self.begin_shutdown()
        if self.registry is not None:
            self.registry.shutdown()
        for thread in self._threads:
            if thread.name == "nomusic-engine-warmup" and not self.wait_for_warmup:
                if thread.is_alive():
                    self.discarded_preload = thread
                continue
            thread.join()
        self._threads.clear()
        if self.cache is not None:
            self.cache.close()
        self.registry = None
        self.cache = None
        self.engine = None
        log.info("Service shutdown complete")
