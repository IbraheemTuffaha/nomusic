"""A first signal stops admission and processing before Uvicorn's drain hook."""

import asyncio
from dataclasses import replace
import signal
import threading
from types import SimpleNamespace

import pytest
import uvicorn

from nomusic.config import SETTINGS
from nomusic.jobs import RegistryClosed
from nomusic.pipeline import processor as pipeline
from nomusic.pipeline.downloader import VideoMetadata
from nomusic.routes.jobs import events
from nomusic.services import Services
from nomusic.serving import LifecycleServer


def test_handle_exit_immediately_closes_registry_gates_and_discards_prefetch(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    prefetched = threading.Event()
    inferred = []

    class Fetcher:
        def __init__(self, url, out_dir):
            self.url = url

        def extract(self):
            return VideoMetadata("fixture", "Fixture", 60, "fixture", self.url)

        def download(self, **kwargs):
            return tmp_path / "source.wav"

        def close(self):
            pass

    def infer_batch(prepared):
        inferred.append(prepared)
        entered.set()
        assert release.wait(5), "test did not release active inference"
        return [SimpleNamespace(gpu_seconds=0) for _ in prepared]

    def decode(source, key, plan, **kwargs):
        if plan.index == 1:
            prefetched.set()
        return pipeline._ChunkWork(plan, plan.index, 0, 0)

    settings = replace(
        SETTINGS, cache_dir=tmp_path / "cache", cache_ttl_days=0,
        memory_gc_interval_seconds=0, progressive_download=False,
    )
    monkeypatch.setattr(pipeline, "SourceFetcher", Fetcher)
    monkeypatch.setattr(pipeline, "SETTINGS", replace(pipeline.SETTINGS, gpu_batch=1))
    engine = SimpleNamespace(warmup=lambda: None, infer_batch=infer_batch)
    services = Services(settings, lambda _: engine)
    app = SimpleNamespace(state=SimpleNamespace(services=services))
    adapter = LifecycleServer(uvicorn.Config(app))
    adapter._application = app

    async def scenario():
        services.start(asyncio.get_running_loop())
        registry, cache = services.registry, services.cache
        app.state.registry = registry
        monkeypatch.setattr(registry.processor, "_decode_chunk", decode)

        def publish(work, key, stems, on_progress, publish_check=None):
            cache.chunk_path(key, work.plan.index).write_bytes(b"published audio")
            cache.record_chunk(key, work.plan.index)

        monkeypatch.setattr(registry.processor, "_finish_chunk", publish)
        submitted = registry.submit("https://example.test/signal", model="fixture", keep_stems=["vocals"])
        assert await asyncio.to_thread(entered.wait, 5)
        assert await asyncio.to_thread(prefetched.wait, 5)

        async def connected():
            return False

        request = SimpleNamespace(app=app, is_disconnected=connected)
        response = await events(submitted.job_id, request)
        stream = response.body_iterator
        await anext(stream)
        queue = registry._subscribers[submitted.job_id][0]
        await asyncio.sleep(0)
        while not queue.empty():
            queue.get_nowait()

        # Signals may interrupt code holding this non-reentrant lock. The
        # handler must only set its plain flag, without taking registry locks.
        with registry._lock:
            adapter.handle_exit(signal.SIGINT, None)
        assert services.stopping
        assert not registry._closed and not services._stop.is_set()
        with pytest.raises(RegistryClosed):
            registry.submit("https://example.test/later", model="fixture", keep_stems=["vocals"])
        with pytest.raises(RegistryClosed):
            registry.subscribe(submitted.job_id)
        with pytest.raises(RegistryClosed):
            registry.attach_loop(asyncio.get_running_loop())
        original_title = submitted.title
        registry._update(submitted.job_id, title="must not be published")
        assert submitted.title == original_title and queue.empty()
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

        workers = tuple(registry._worker_threads)
        release.set()
        await asyncio.gather(*(asyncio.to_thread(worker.join, 5) for worker in workers))
        assert all(not worker.is_alive() for worker in workers)
        assert inferred == [[0]]
        assert cache.load_meta(submitted.job_id).chunks_ready == [0]
        assert cache.chunk_path(submitted.job_id, 0).read_bytes() == b"published audio"
        assert not cache.chunk_path(submitted.job_id, 1).exists()
        await asyncio.to_thread(services.shutdown)

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        services.shutdown()
