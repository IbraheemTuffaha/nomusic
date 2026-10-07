"""Process-supervisor and bounded-admission regressions for M3-T2."""

from __future__ import annotations

from pathlib import Path
import multiprocessing as mp
import threading
import time
from types import SimpleNamespace

import pytest

from nomusic.jobs import JobQueueFull, JobRegistry
from nomusic.pipeline.processor import RunHooks
from nomusic.worker import (
    SupervisedModelWorker,
    WorkerAbandoned,
    _ChildCancelled,
    _ChildChunkProvider,
)


def _stuck_worker(_settings, _commands, _events):
    """A native-call stand-in that ignores every cooperative command."""
    while True:
        time.sleep(1)


def test_child_provider_honors_cancel_and_priority():
    commands = mp.Queue()
    provider = _ChildChunkProvider(commands)
    provider.configure(5, [0])
    assert provider.next() == 1
    commands.put(("prioritize", 4))
    time.sleep(0.05)  # multiprocessing.Queue uses a feeder thread
    assert provider.next() == 4
    commands.put(("cancel",))
    time.sleep(0.05)
    with pytest.raises(_ChildCancelled):
        provider.next()


def test_supervisor_terminates_stuck_child_and_restarts(tmp_path):
    settings = SimpleNamespace(
        engine_name="fixture",
        cache_dir=Path(tmp_path),
        chunk_seconds=10.0,
        chunk_overlap_seconds=0.5,
        keep_source_after_complete=False,
        progressive_download=False,
    )
    worker = SupervisedModelWorker(
        settings,
        execution_timeout_seconds=30,
        cancel_grace_seconds=0.1,
        target=_stuck_worker,
    )
    started = threading.Event()
    try:
        worker.start()
        assert worker.alive

        def abort():
            started.set()
            raise WorkerAbandoned("test cancel")

        with pytest.raises(WorkerAbandoned, match="test cancel"):
            worker.run(
                "key",
                "https://example.test/video",
                model="fixture",
                keep_stems=["vocals"],
                hooks=RunHooks(),
                abort_check=abort,
            )
        assert started.is_set()
        assert not worker.alive

        # A subsequent admission gets a new process instead of inheriting the
        # stuck native state.
        worker.start()
        assert worker.alive
    finally:
        worker.shutdown()
    assert not worker.alive


def test_registry_rejects_excess_queued_work(monkeypatch):
    class Cache:
        def key(self, url, *_args, **_kwargs):
            return url

        def load_meta(self, _key):
            return None

    class Processor:
        chunk_seconds = 10.0
        chunk_overlap_seconds = 0.5

        def run(self, *_args, **_kwargs):
            time.sleep(1)

    registry = JobRegistry(Processor(), Cache(), max_queued_jobs=1)
    try:
        registry.submit("one", model="fake", keep_stems=["vocals"])
        registry.submit("two", model="fake", keep_stems=["vocals"])
        with pytest.raises(JobQueueFull):
            registry.submit("three", model="fake", keep_stems=["vocals"])
    finally:
        registry.shutdown()
