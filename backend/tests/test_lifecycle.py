"""Service ownership checks; fakes isolate lifecycle from model/network timing."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from nomusic import server
from nomusic.services import Services
from nomusic.jobs import JobState, JobStatus
from nomusic.routes.jobs import events
from starlette.requests import Request


class WarmupEngine:
    def __init__(self):
        self.warmed = threading.Event()

    def warmup(self):
        self.warmed.set()


@pytest.fixture
def settings(monkeypatch, tmp_path):
    monkeypatch.setattr("nomusic.services.check_runtime", lambda: {})
    configured = replace(
        server.SETTINGS, cache_dir=tmp_path / "cache",
        cache_ttl_days=1, cache_sweep_interval_seconds=3600,
        memory_gc_interval_seconds=3600,
    )
    monkeypatch.setattr(server, "SETTINGS", configured)
    return configured


def test_import_and_app_construction_start_no_services(tmp_path):
    # A fresh process avoids hiding import effects behind Python's module cache.
    script = r'''
import logging, pathlib, resource, subprocess, sys, threading
before_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
before_handlers = list(logging.getLogger().handlers)
def forbidden(*args, **kwargs):
    raise AssertionError("import started external work")
threading.Thread.start = forbidden
subprocess.Popen = forbidden
from nomusic import server
for app in (server.app, server.create_app()):
    assert not hasattr(app.state, "services")
    assert not hasattr(app.state, "engine")
assert "torch" not in sys.modules
assert not server.SETTINGS.cache_dir.exists()
assert resource.getrlimit(resource.RLIMIT_NOFILE) == before_limit
assert logging.getLogger().handlers == before_handlers
'''
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=20,
        env={**os.environ, "NOMUSIC_CACHE_DIR": str(tmp_path / "never-created"),
             "NOMUSIC_DEVICE": "invalid-if-selected"},
    )
    assert result.returncode == 0, result.stderr


def test_repeated_lifespan_joins_threads_and_replaces_services(monkeypatch, settings):
    engines = []

    def make_engine(name):
        engine = WarmupEngine()
        engines.append(engine)
        return engine

    monkeypatch.setattr(server, "get_engine", make_engine)
    app = server.create_app()
    previous = None
    for _ in range(2):
        with TestClient(app) as client:
            owner = app.state.services
            assert owner is not previous
            assert engines[-1].warmed.wait(2)
            threads = list(owner._threads)
            assert {t.name for t in threads} == {
                "nomusic-cache-ttl", "nomusic-memory-gc", "nomusic-export-gc",
                "nomusic-engine-warmup",
            }
            assert client.get("/healthz").status_code == 200
            previous = owner
        assert all(not t.is_alive() for t in threads)
        assert (
            owner.engine is None and owner.registry is None
            and owner.exports is None and owner.cache is None
        )
        assert not hasattr(app.state, "services")
        assert settings.cache_dir.exists()  # shutdown retains the disk cache
    assert len(engines) == 2


def test_shutdown_waits_for_warmup_without_blocking_event_loop(monkeypatch, settings):
    entered, release = threading.Event(), threading.Event()

    class SlowEngine:
        def warmup(self):
            entered.set()
            assert release.wait(5), "test failed to release model load"

    monkeypatch.setattr(server, "get_engine", lambda name: SlowEngine())
    app = server.create_app()

    async def scenario():
        context = app.router.lifespan_context(app)
        await context.__aenter__()
        owner = app.state.services
        assert await asyncio.to_thread(entered.wait, 2)
        closing = asyncio.create_task(context.__aexit__(None, None, None))
        try:
            # This loop must remain free to release the blocking model load.
            assert await asyncio.to_thread(owner._stop.wait, 2)
            assert not closing.done()
            asyncio.get_running_loop().call_soon(release.set)
            await asyncio.wait_for(closing, 3)
        finally:
            release.set()
            await closing
        assert owner.engine is None

    asyncio.run(scenario())


def test_partial_startup_failure_joins_already_started_work(monkeypatch, settings):
    owners = []
    threads = []
    original_spawn = Services._spawn

    def fail_after_maintenance(self, name, target):
        if name == "nomusic-engine-warmup":
            owners.append(self)
            threads.extend(self._threads)
            raise RuntimeError("could not start warmup thread")
        return original_spawn(self, name, target)

    monkeypatch.setattr(server, "get_engine", lambda name: WarmupEngine())
    monkeypatch.setattr(Services, "_spawn", fail_after_maintenance)
    app = server.create_app()
    with pytest.raises(RuntimeError, match="could not start warmup"):
        with TestClient(app):
            pytest.fail("startup should fail")
    assert threads and all(not t.is_alive() for t in threads)
    assert owners[0].registry is None
    assert not hasattr(app.state, "services")


def test_failed_warmup_is_joined_and_reports_restart_remedy(monkeypatch, settings, caplog):
    class FailedEngine:
        def warmup(self):
            raise ValueError("test model load failure")

    monkeypatch.setattr(server, "get_engine", lambda name: FailedEngine())
    app = server.create_app()
    with TestClient(app) as client:
        threads = list(app.state.services._threads)
        assert client.get("/healthz").status_code == 200
        # Initialization now checks runtime/storage before model loading. Wait
        # for its outcome instead of racing shutdown against the warmup call.
        for thread in threads:
            if thread.name == "nomusic-engine-warmup":
                thread.join(timeout=3)
                assert not thread.is_alive()
    assert all(not t.is_alive() for t in threads)
    assert "Startup model check failed; fix the cause and restart" in caplog.text


def test_engine_initialization_failure_cleans_app_state(monkeypatch, settings):
    def fail(name):
        raise ValueError("unsupported device")

    monkeypatch.setattr(server, "get_engine", fail)
    app = server.create_app()
    with pytest.raises(ValueError, match="unsupported device"):
        with TestClient(app):
            pytest.fail("startup should fail")
    assert not hasattr(app.state, "services")
    assert not settings.cache_dir.exists()


def test_orphaned_status_stream_observes_service_shutdown(monkeypatch, settings):
    monkeypatch.setattr(server, "get_engine", lambda name: WarmupEngine())
    app = server.create_app()

    async def scenario():
        async with app.router.lifespan_context(app):
            registry = app.state.registry
            registry._jobs["job"] = JobStatus(job_id="job", state=JobState.PROCESSING)
            response = await events("job", Request({"type": "http", "app": app}))
            stream = response.body_iterator
            assert '"processing"' in await anext(stream)
            # Simulate the registry cleanup race: the response still holds its
            # queue, but shutdown can no longer notify it through the mapping.
            registry._subscribers.clear()
            app.state.services.begin_shutdown()
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(anext(stream), 2)

    asyncio.run(scenario())
