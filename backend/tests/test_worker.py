"""Process-supervisor and bounded-admission regressions for M3-T2."""

from __future__ import annotations

from pathlib import Path
import multiprocessing as mp
import os
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


def _fixture_worker(settings, commands, events):
    """Run the real child loop with a tiny importable local acquisition/engine."""
    import numpy as np
    import soundfile as sf

    from nomusic.engines.base import EngineCapabilities, SeparationResult
    from nomusic.pipeline import processor as processor_module
    from nomusic.pipeline.downloader import VideoMetadata
    from nomusic import worker as worker_module

    class FixtureEngine:
        def warmup(self):
            return None

        def prepare(self, path, *, model=None):
            return sf.read(str(path), always_2d=True, dtype="float32")

        def infer_batch(self, prepared):
            results = []
            for audio, sample_rate in prepared:
                zeros = np.zeros_like(audio)
                results.append(SeparationResult(
                    stems={"vocals": audio, "drums": zeros, "bass": zeros, "other": zeros},
                    sample_rate=sample_rate,
                    duration_seconds=len(audio) / sample_rate,
                ))
            return results

        def capabilities(self):
            return EngineCapabilities("fixture", "cpu", ("fixture",), "fixture")

    class FixtureFetcher:
        def __init__(self, url, out_dir):
            self.url = url
            self.out_dir = out_dir
            self.limits = None

        def extract(self):
            return VideoMetadata("fixture", "fixture", 1.0, "fixture", self.url)

        def download(self, progress_hook=None):
            self.out_dir.mkdir(parents=True, exist_ok=True)
            path = self.out_dir / "source.wav"
            sf.write(str(path), np.zeros((44100, 2), dtype=np.float32), 44100)
            if progress_hook:
                progress_hook({"status": "finished", "downloaded_bytes": path.stat().st_size,
                               "total_bytes": path.stat().st_size})
            return path

        def close(self):
            return None

    def slice_source(source, out_path, *, start, end, pass_fds=()):
        audio, sample_rate = sf.read(str(source), always_2d=True, dtype="float32")
        sf.write(str(out_path), audio[int(start * sample_rate):int(end * sample_rate)], sample_rate)
        return out_path

    worker_module.get_engine = lambda _name: FixtureEngine()
    processor_module.SourceFetcher = FixtureFetcher
    processor_module.slice_source = slice_source
    worker_module._worker_main(settings, commands, events)


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


def test_supervisor_runs_real_child_pipeline_and_can_run_again(tmp_path):
    settings = SimpleNamespace(
        engine_name="fixture",
        cache_dir=Path(tmp_path),
        chunk_seconds=1.0,
        chunk_overlap_seconds=0.0,
        keep_source_after_complete=False,
        progressive_download=False,
    )
    worker = SupervisedModelWorker(
        settings, execution_timeout_seconds=30, cancel_grace_seconds=1,
        target=_fixture_worker,
    )
    previous_pythonpath = os.environ.get("PYTHONPATH")
    test_root = str(Path(__file__).resolve().parents[1])
    os.environ["PYTHONPATH"] = (
        test_root if not previous_pythonpath
        else test_root + os.pathsep + previous_pythonpath
    )
    try:
        worker.start()
        result_key = None
        for _ in range(2):
            key = worker.run(
                "fixture-key",
                "fixture://video",
                model="fixture",
                keep_stems=["vocals"],
                hooks=RunHooks(),
            )
            assert key
            if result_key is None:
                result_key = key
            else:
                assert key == result_key
        assert result_key is not None
        assert (Path(tmp_path) / result_key / "chunk_000.opus").exists()
    finally:
        worker.shutdown()
        if previous_pythonpath is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = previous_pythonpath


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
