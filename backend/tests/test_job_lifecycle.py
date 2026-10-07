"""Ordinary shutdown owns actual workers until their cleanup has finished."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from nomusic.jobs import JobRegistry, RegistryClosed, _JobControl


@pytest.mark.parametrize("hint, expected", [(-10, 0), (2, 2), (99, 4)])
def test_prioritize_clamps_to_the_job_chunk_range(hint, expected):
    control = _JobControl(total_chunks=5, done=set())
    control.prioritize(hint)
    assert control.pending[0] == expected


class _Cache:
    def key(self, url, *_args, **_kwargs):
        return url

    def load_meta(self, _key):
        return None


class _GatedProcessor:
    chunk_seconds = 10.0
    chunk_overlap_seconds = 0.5

    def __init__(self):
        self.started = threading.Event()
        self.check_abort = threading.Event()
        self.unwinding = threading.Event()
        self.finish_cleanup = threading.Event()
        self.exited = threading.Event()
        self.calls = []

    def run(self, url, *, model, keep_stems, hooks):
        self.calls.append(url)
        self.started.set()
        try:
            assert self.check_abort.wait(5), "test did not allow abort check"
            hooks.abort_check()
            pytest.fail("worker ignored shutdown")
        finally:
            self.unwinding.set()
            assert self.finish_cleanup.wait(5), "test did not allow cleanup"
            self.exited.set()


def _submit(registry, key="first"):
    return registry.submit(key, model="fake", keep_stems=["vocals"])


@pytest.mark.parametrize("supersede", [False, True], ids=["queued", "superseded"])
def test_shutdown_joins_active_and_queued_physical_workers(supersede):
    processor = _GatedProcessor()
    registry = JobRegistry(processor, _Cache())
    try:
        original = _submit(registry)
        assert processor.started.wait(5)
        original_thread = registry._threads[original.job_id]
        if supersede:
            # /cache/clear drops logical state before the old execution ends.
            # Re-submitting its key replaces _threads[key] and clears the
            # per-key abandon mark, but must not lose ownership of that run.
            registry.abandon_all()
            replacement = _submit(registry)
            assert replacement is not original
            assert len(registry._threads) == 1
            assert "first" not in registry._abandoning
        else:
            _submit(registry, "queued")
        physical_workers = tuple(registry._worker_threads)
        assert len(physical_workers) == 2
        assert original_thread in physical_workers

        registry.begin_shutdown()
        with pytest.raises(RegistryClosed, match="shutting down"):
            _submit(registry, "later")
        processor.check_abort.set()
        assert processor.unwinding.wait(5)
        with ThreadPoolExecutor(max_workers=1) as pool:
            stopped = pool.submit(registry.shutdown)
            try:
                # Cancellation has been observed, but pipeline cleanup still
                # owns resources. shutdown must wait for that actual exit.
                with pytest.raises(TimeoutError):
                    stopped.result(timeout=0.05)
                assert original_thread.is_alive()
            finally:
                processor.finish_cleanup.set()
            stopped.result(timeout=5)

        assert processor.exited.is_set()
        assert processor.calls == ["first"]  # queued work never starts probing
        assert all(not worker.is_alive() for worker in physical_workers)
        assert registry._worker_threads == set()
        assert registry._threads == {}
        assert registry._loop is None
        registry.shutdown()  # repeated cleanup is safe
    finally:
        processor.check_abort.set()
        processor.finish_cleanup.set()
        registry.shutdown()


def test_shutdown_keeps_loop_attached_through_worker_callbacks():
    async def exercise():
        processor = _GatedProcessor()
        registry = JobRegistry(processor, _Cache())
        loop = asyncio.get_running_loop()
        registry.attach_loop(loop)
        try:
            status = _submit(registry)
            assert await asyncio.to_thread(processor.started.wait, 5)
            queue = registry.subscribe(status.job_id)
            registry.begin_shutdown()
            processor.check_abort.set()
            assert await asyncio.to_thread(processor.unwinding.wait, 5)
            stopping = asyncio.create_task(asyncio.to_thread(registry.shutdown))
            try:
                await asyncio.sleep(0)
                assert queue.empty()  # planned shutdown sends no fabricated failure
                assert not stopping.done()
                assert registry._loop is loop
                # A final pipeline callback can still safely schedule onto
                # the active loop while shutdown waits for owned work.
                await asyncio.to_thread(
                    registry._update, status.job_id, title="cleanup callback"
                )
                await asyncio.sleep(0)
                assert queue.empty()  # late callbacks cannot publish after stop
                with pytest.raises(RegistryClosed):
                    registry.subscribe(status.job_id)
            finally:
                processor.finish_cleanup.set()
                await asyncio.wait_for(stopping, timeout=5)
            assert registry._loop is None
            with pytest.raises(RegistryClosed):
                registry.attach_loop(loop)
        finally:
            processor.check_abort.set()
            processor.finish_cleanup.set()
            await asyncio.to_thread(registry.shutdown)

    asyncio.run(exercise())


def test_submit_racing_shutdown_does_not_admit_after_cache_lookup():
    lookup_started = threading.Event()
    finish_lookup = threading.Event()

    class BlockingCache(_Cache):
        def load_meta(self, key):
            lookup_started.set()
            assert finish_lookup.wait(5)
            return None

    processor = _GatedProcessor()
    registry = JobRegistry(processor, BlockingCache())
    with ThreadPoolExecutor(max_workers=1) as pool:
        admission = pool.submit(_submit, registry)
        try:
            assert lookup_started.wait(5)
            registry.begin_shutdown()
        finally:
            finish_lookup.set()
        with pytest.raises(RegistryClosed):
            admission.result(timeout=5)
    registry.shutdown()
    assert processor.calls == []
    assert registry._threads == {}


def test_shutdown_closes_subscriptions_without_an_in_memory_job():
    async def exercise():
        registry = JobRegistry(_GatedProcessor(), _Cache())
        registry.attach_loop(asyncio.get_running_loop())
        queue = registry.subscribe("partial-disk-cache")
        registry.begin_shutdown()
        registry.begin_shutdown()  # terminal notification stays idempotent
        try:
            await asyncio.sleep(0)
            assert queue.empty()
        finally:
            await asyncio.to_thread(registry.shutdown)

    asyncio.run(exercise())


def test_failed_thread_start_does_not_leave_an_unjoinable_worker(monkeypatch):
    registry = JobRegistry(_GatedProcessor(), _Cache())

    def fail_start(_thread):
        raise RuntimeError("cannot start thread")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    with pytest.raises(RuntimeError, match="cannot start thread"):
        _submit(registry)
    registry.shutdown()
    assert registry._worker_threads == set()
    assert registry._jobs == {}
