"""Export registry state, persistence, admission, and HTTP serving tests."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from dataclasses import replace

from nomusic.exports import (
    ExportArtifact,
    ExportDownloadsFull,
    ExportQueueFull,
    ExportRegistry,
    ExportState,
)
from nomusic.jobs import JobState
from nomusic.pipeline.cache import CacheMeta, JobCache


@pytest.fixture
def client(monkeypatch, tmp_path):
    from nomusic import server
    from nomusic.engines.base import Engine, EngineCapabilities, SeparationResult

    class CapsOnlyEngine(Engine):
        def capabilities(self):
            return EngineCapabilities("fake", "cpu", ("fake",), "fake")

        def prepare(self, audio_path, *, model=None):
            raise NotImplementedError

        def infer_batch(self, prepared):
            raise NotImplementedError

    monkeypatch.setattr("nomusic.services.check_runtime", lambda: {})
    monkeypatch.setattr(server, "get_engine", lambda name: CapsOnlyEngine())
    monkeypatch.setattr(server, "SETTINGS", replace(server.SETTINGS, cache_dir=tmp_path))
    app = server.create_app()
    with TestClient(app) as test_client:
        yield test_client


@dataclass
class FakeJob:
    state: JobState


class FakeJobs:
    def __init__(self, state=JobState.READY):
        self.job = FakeJob(state)

    def get(self, job_id):
        return self.job if job_id in ("job", "job-2") else None


def _builder(spec, directory, progress, cancel):
    path = directory / f"built.{spec.format}"
    path.write_bytes(b"prepared")
    progress("encoding", 1.0)
    media = {"opus": "audio/ogg", "mp3": "audio/mpeg", "mp4": "video/mp4"}[spec.format]
    return ExportArtifact(path, path.name, media)


def _wait(registry, export_id, expected=ExportState.READY):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = registry.get(export_id)
        if status is not None and status.state is expected:
            return status
        time.sleep(0.01)
    raise AssertionError(f"export {export_id} did not reach {expected}")


def test_registry_deduplicates_and_recovers_ready_manifest(tmp_path):
    cache = JobCache(tmp_path / "cache")
    jobs = FakeJobs()
    registry = ExportRegistry(
        cache, jobs, _builder, max_jobs=1, ttl_seconds=60, wait_timeout_seconds=2
    )
    first = registry.submit("job", "mp3")
    ready = _wait(registry, first.export_id)
    duplicate = registry.submit("job", "mp3")
    assert duplicate.export_id == first.export_id
    assert ready.size_bytes == len(b"prepared")
    registry.shutdown()

    recovered = ExportRegistry(
        cache, jobs, _builder, max_jobs=1, ttl_seconds=60, wait_timeout_seconds=2
    )
    assert recovered.get(first.export_id).state is ExportState.READY
    assert recovered.submit("job", "mp3").export_id == first.export_id
    recovered.shutdown()


def test_registry_bounds_active_builds_and_cancels_waiter(tmp_path):
    cache = JobCache(tmp_path / "cache")
    jobs = FakeJobs(JobState.PROCESSING)
    registry = ExportRegistry(
        cache, jobs, _builder, max_jobs=1, ttl_seconds=60, wait_timeout_seconds=10
    )
    first = registry.submit("job", "opus")
    with pytest.raises(ExportQueueFull):
        registry.submit("job-2", "opus")
    cancelled = registry.cancel(first.export_id)
    assert cancelled.state is ExportState.CANCELLED
    registry.shutdown()


def test_registry_expires_ready_artifact(tmp_path):
    cache = JobCache(tmp_path / "cache")
    jobs = FakeJobs()
    now = [100.0]
    registry = ExportRegistry(
        cache,
        jobs,
        _builder,
        max_jobs=1,
        ttl_seconds=10,
        wait_timeout_seconds=2,
        clock=lambda: now[0],
    )
    status = registry.submit("job", "opus")
    _wait(registry, status.export_id)
    assert registry.cleanup(now=111) == 1
    assert registry.get(status.export_id).state is ExportState.EXPIRED
    assert not list(cache.export_dir(status.export_id).glob("built.*"))
    registry.shutdown()


def test_download_reader_slots_are_bounded(tmp_path):
    cache = JobCache(tmp_path / "cache")
    registry = ExportRegistry(
        cache, FakeJobs(), _builder, max_jobs=1, max_downloads=1,
        ttl_seconds=60, wait_timeout_seconds=2,
    )
    status = registry.submit("job", "opus")
    _wait(registry, status.export_id)
    first = registry.open_download(status.export_id)
    with pytest.raises(ExportDownloadsFull):
        registry.open_download(status.export_id)
    first.close()
    second = registry.open_download(status.export_id)
    assert second is not None
    second.close()
    registry.shutdown()


def test_artifact_size_limit_fails_before_publication(tmp_path):
    cache = JobCache(tmp_path / "cache")

    def oversized(spec, directory, progress, cancel):
        path = directory / "too-large"
        path.write_bytes(b"123456")
        return ExportArtifact(path, path.name, "application/octet-stream")

    registry = ExportRegistry(
        cache, FakeJobs(), oversized, max_jobs=1, max_artifact_bytes=5,
        ttl_seconds=60, wait_timeout_seconds=2,
    )
    status = registry.submit("job", "opus")
    failed = _wait(registry, status.export_id, ExportState.FAILED)
    assert "exceeds" in failed.error
    registry.shutdown()


def test_source_cache_sweep_does_not_expire_export_ttl(tmp_path):
    cache = JobCache(tmp_path / "cache")
    directory = cache.export_dir("a" * 32)
    artifact = directory / "ready.opus"
    artifact.write_bytes(b"artifact")
    old = time.time() - 1000
    artifact.touch()
    import os
    os.utime(artifact, (old, old))
    assert cache.sweep_older_than(1) == (0, 0)
    assert artifact.exists()


def test_http_export_api_serves_prepared_opus(client):
    from nomusic.config import SETTINGS

    cache = client.app.state.cache
    job_id = cache.key(
        "http://example.com/export",
        "fake",
        ["vocals"],
        chunk_seconds=SETTINGS.chunk_seconds,
        chunk_overlap_seconds=SETTINGS.chunk_overlap_seconds,
    )
    cache.save_meta(
        job_id,
        CacheMeta(
            url="http://example.com/export",
            model="fake",
            keep_stems=["vocals"],
            duration_seconds=1,
            chunk_seconds=1,
            chunk_overlap_seconds=0,
            total_chunks=2,
            title="Test export",
            chunks_ready=[0, 1],
            complete=True,
        ),
    )
    cache.chunk_path(job_id, 0).write_bytes(b"one")
    cache.chunk_path(job_id, 1).write_bytes(b"two")
    # Keep this route test independent of the local ffmpeg encoder matrix; the
    # format-specific pipeline tests cover real Ogg/Opus/MP3/MP4 output.
    client.app.state.exports.builder = _builder

    response = client.post("/exports", json={"job_id": job_id, "format": "opus"})
    assert response.status_code in (200, 202)
    export_id = response.json()["export_id"]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = client.get(f"/exports/{export_id}").json()
        if status["state"] == "ready":
            break
        time.sleep(0.01)
    assert status["state"] == "ready"
    downloaded = client.get(f"/exports/{export_id}/download")
    assert downloaded.status_code == 200
    assert downloaded.content == b"prepared"
    assert downloaded.headers["content-type"].startswith("audio/ogg")
