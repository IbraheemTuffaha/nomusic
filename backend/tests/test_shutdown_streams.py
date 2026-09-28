"""Shutdown cancellation is transport closure, never a fabricated job failure."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from nomusic.jobs import JobRegistry, JobState, JobStatus, WorkerAbandoned
from nomusic.pipeline import processor as pipeline
from nomusic.pipeline.cache import JobCache
from nomusic.pipeline.downloader import VideoMetadata
from nomusic.routes.jobs import events


def request_for(registry):
    async def connected():
        await asyncio.sleep(0)
        return False
    services = SimpleNamespace(stopping=False)
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(registry=registry, services=services)),
        is_disconnected=connected,
    )


@pytest.mark.parametrize("stage", ["lookup", "download", "inference"])
@pytest.mark.parametrize("exception", [RuntimeError, WorkerAbandoned])
@pytest.mark.parametrize("shutdown", [False, True])
def test_pipeline_failure_during_shutdown_never_reaches_stream(
    tmp_path, monkeypatch, stage, exception, shutdown,
):
    entered, release = threading.Event(), threading.Event()

    def checkpoint(name):
        if name == stage:
            entered.set()
            assert release.wait(5), "test did not release failing pipeline step"
            raise exception(f"{stage} interrupted")

    class Fetcher:
        def __init__(self, url, out_dir):
            self.url = url
        def extract(self):
            checkpoint("lookup")
            return VideoMetadata("fixture", "Fixture", 1, "fixture", self.url)
        def download(self, **kwargs):
            checkpoint("download")
            return tmp_path / "source.wav"
        def close(self):
            pass

    def inference(prepared):
        checkpoint("inference")
        raise AssertionError("must reach selected failure checkpoint")

    monkeypatch.setattr(pipeline, "SourceFetcher", Fetcher)
    cache = JobCache(tmp_path / "cache")
    processor = pipeline.Processor(
        engine=SimpleNamespace(infer_batch=inference), cache=cache,
        chunk_seconds=10, chunk_overlap_seconds=0.5, progressive=False,
    )
    monkeypatch.setattr(processor, "_decode_chunk", lambda source, key, plan, **kwargs:
                        pipeline._ChunkWork(plan, object(), 0, 0))
    registry = JobRegistry(processor, cache)
    request = request_for(registry)

    async def scenario():
        registry.attach_loop(asyncio.get_running_loop())
        submitted = registry.submit("https://example.test/fixture", model="fixture",
                                    keep_stems=["vocals"])
        assert await asyncio.to_thread(entered.wait, 5)
        response = await events(submitted.job_id, request)
        stream = response.body_iterator
        first = await anext(stream)
        assert '"error"' not in first or '"error": ""' in first
        if shutdown:
            request.app.state.services.stopping = True
            registry.begin_shutdown()
        release.set()
        workers = tuple(registry._worker_threads)
        await asyncio.gather(*(asyncio.to_thread(t.join, 5) for t in workers))
        assert all(not t.is_alive() for t in workers)
        await asyncio.sleep(0)  # deliver every queued callback
        if shutdown:
            assert submitted.state is not JobState.ERROR
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(anext(stream), 2)
        elif exception is RuntimeError:
            received = []
            async for item in stream:
                received.append(item)
            assert any('"state": "error"' in item and f"{stage} interrupted" in item
                       for item in received)
        else:
            assert submitted.state is not JobState.ERROR
            await stream.aclose()
        await asyncio.to_thread(registry.shutdown)

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        registry.shutdown()


@pytest.mark.parametrize("state", [JobState.READY, JobState.ERROR])
@pytest.mark.parametrize("phase", ["one_shot", "initial", "waiting"])
def test_stop_suppresses_terminal_snapshot_before_each_yield(tmp_path, state, phase):
    registry = JobRegistry(object(), JobCache(tmp_path))
    registry._jobs["fixture"] = JobStatus(
        job_id="fixture", state=state if phase == "one_shot" else JobState.PROCESSING,
    )
    request = request_for(registry)

    async def scenario():
        registry.attach_loop(asyncio.get_running_loop())
        response = await events("fixture", request)
        stream = response.body_iterator
        if phase == "waiting":
            assert '"processing"' in await anext(stream)
            queue = registry._subscribers["fixture"][0]
            pending = asyncio.create_task(anext(stream))
            # Wait until the generator has passed its loop-top stop check.
            while not queue._getters:
                await asyncio.sleep(0)
            queue.put_nowait({"state": state.value, "error": "late result"})
        request.app.state.services.stopping = True
        registry.begin_shutdown()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(pending if phase == "waiting" else anext(stream), 2)
        assert registry._jobs["fixture"].state is (
            state if phase == "one_shot" else JobState.PROCESSING
        )
        await asyncio.to_thread(registry.shutdown)

    asyncio.run(scenario())
