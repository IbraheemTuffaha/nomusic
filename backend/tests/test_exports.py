"""Export registry state, persistence, admission, and HTTP serving tests."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from dataclasses import replace

from nomusic.exports import (
    DownloadGrantStore,
    DownloadGrantsFull,
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
    from nomusic.auth import AuthStore
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
    app = server.create_app(auth_store=AuthStore(tmp_path / "keys.json", required=False))
    with TestClient(app) as test_client:
        yield test_client


@dataclass
class FakeJob:
    state: JobState


class FakeJobs:
    def __init__(self, state=JobState.READY):
        self.job = FakeJob(state)

    def get(self, job_id):
        return self.job if job_id in ("a" * 16, "b" * 16) else None


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
        cache, jobs, _builder, max_jobs=1, ttl_seconds=60
    )
    first = registry.submit("a" * 16, "mp3")
    ready = _wait(registry, first.export_id)
    duplicate = registry.submit("a" * 16, "mp3")
    assert duplicate.export_id == first.export_id
    assert ready.size_bytes == len(b"prepared")
    registry.shutdown()

    recovered = ExportRegistry(cache, jobs, _builder, max_jobs=1, ttl_seconds=60)
    assert recovered.get(first.export_id).state is ExportState.READY
    assert recovered.submit("a" * 16, "mp3").export_id == first.export_id
    recovered.shutdown()


def test_registry_bounds_active_builds_and_cancels_waiter(tmp_path):
    cache = JobCache(tmp_path / "cache")
    jobs = FakeJobs()
    registry = ExportRegistry(
        cache, jobs, _builder, max_jobs=1, ttl_seconds=60
    )
    first = registry.submit("a" * 16, "opus")
    with pytest.raises(ExportQueueFull):
        registry.submit("b" * 16, "opus")
    cancelled = registry.cancel(first.export_id)
    assert cancelled.state is ExportState.CANCELLED
    registry.shutdown()


def test_registry_rejects_source_that_is_not_ready(tmp_path):
    cache = JobCache(tmp_path / "cache")
    registry = ExportRegistry(
        cache, FakeJobs(JobState.PROCESSING), _builder, max_jobs=1,
        ttl_seconds=60,
    )
    with pytest.raises(RuntimeError, match="not ready"):
        registry.submit("a" * 16, "mp3")
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
        clock=lambda: now[0],
    )
    status = registry.submit("a" * 16, "opus")
    _wait(registry, status.export_id)
    assert registry.cleanup(now=111) == 1
    assert registry.get(status.export_id).state is ExportState.EXPIRED
    assert not list(cache.export_dir(status.export_id).glob("built.*"))
    registry.shutdown()


def test_cancel_keeps_admission_until_builder_exits(tmp_path):
    cache = JobCache(tmp_path / "cache")
    entered, release = threading.Event(), threading.Event()

    def ignores_cancel(spec, directory, progress, cancel):
        entered.set()
        assert release.wait(2)
        return _builder(spec, directory, progress, cancel)

    registry = ExportRegistry(cache, FakeJobs(), ignores_cancel, max_jobs=1, ttl_seconds=1)
    first = registry.submit("a" * 16, "mp3")
    assert entered.wait(1)
    registry.cancel(first.export_id)
    with pytest.raises(ExportQueueFull):
        registry.submit("b" * 16, "mp3")
    release.set()
    registry.shutdown()
    assert not registry.has_active_workers


def test_cancel_requires_the_last_client_owner(tmp_path):
    cache = JobCache(tmp_path / "cache")
    entered, release = threading.Event(), threading.Event()

    def waits_for_release(spec, directory, progress, cancel):
        entered.set()
        assert release.wait(2)
        return _builder(spec, directory, progress, cancel)

    registry = ExportRegistry(
        cache, FakeJobs(), waits_for_release, max_jobs=1, ttl_seconds=60,
    )
    first = registry.submit("a" * 16, "mp3", client_id="tab-a")
    assert entered.wait(1)
    duplicate = registry.submit("a" * 16, "mp3", client_id="tab-b")
    assert duplicate.export_id == first.export_id

    assert registry.cancel(first.export_id, "unknown-tab").state is ExportState.BUILDING
    assert registry.cancel(first.export_id, "tab-a").state is ExportState.BUILDING
    assert registry.cancel(first.export_id, "tab-b").state is ExportState.CANCELLED

    release.set()
    registry.shutdown()
    assert not registry.has_active_workers


def test_cleanup_does_not_remove_a_replacement_dedupe_entry(tmp_path):
    cache = JobCache(tmp_path / "cache")
    now = [100.0]
    registry = ExportRegistry(cache, FakeJobs(), _builder, ttl_seconds=10, clock=lambda: now[0])
    first = registry.submit("a" * 16, "mp3")
    _wait(registry, first.export_id)
    assert registry.cleanup(now=111) == 1
    now[0] = 111
    replacement = registry.submit("a" * 16, "mp3")
    _wait(registry, replacement.export_id)
    now[0] = 112
    registry.cleanup()
    assert registry.submit("a" * 16, "mp3").export_id == replacement.export_id
    registry.shutdown()


def test_download_reader_slots_are_bounded(tmp_path):
    cache = JobCache(tmp_path / "cache")
    registry = ExportRegistry(
        cache, FakeJobs(), _builder, max_jobs=1, max_downloads=1,
        ttl_seconds=60,
    )
    status = registry.submit("a" * 16, "opus")
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
        ttl_seconds=60,
    )
    status = registry.submit("a" * 16, "opus")
    failed = _wait(registry, status.export_id, ExportState.FAILED)
    assert "exceeds" in failed.error
    assert not (cache.export_path(status.export_id) / "too-large").exists()
    registry.shutdown()


def test_malformed_manifest_is_quarantined(tmp_path):
    cache = JobCache(tmp_path / "cache")
    directory = cache.ensure_export_dir("a" * 32)
    (directory / ".export.json").write_text("{}")
    registry = ExportRegistry(cache, FakeJobs(), _builder)
    assert registry.get("a" * 32) is None
    assert not directory.exists()


def test_malformed_export_directory_name_is_quarantined(tmp_path):
    cache = JobCache(tmp_path / "cache")
    directory = cache.root / "exports" / "copied-from-old-cache"
    directory.mkdir(parents=True)
    (directory / ".export.json").write_text("{}")
    registry = ExportRegistry(cache, FakeJobs(), _builder)
    assert not directory.exists()
    registry.shutdown()


def test_failed_manifest_write_rolls_back_admission(tmp_path, monkeypatch):
    cache = JobCache(tmp_path / "cache")
    registry = ExportRegistry(cache, FakeJobs(), _builder, max_jobs=1)

    def fail(_status):
        raise OSError("disk full")

    monkeypatch.setattr(registry, "_write_manifest", fail)
    with pytest.raises(OSError, match="disk full"):
        registry.submit("a" * 16, "mp3")
    assert not registry._statuses
    assert not registry._threads
    assert not list(cache.export_entries())
    registry.shutdown()


def test_source_cache_sweep_does_not_expire_export_ttl(tmp_path):
    cache = JobCache(tmp_path / "cache")
    directory = cache.ensure_export_dir("a" * 32)
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

    # Range errors and missing-artifact paths must release the reader slot even
    # though Starlette returns before its normal background callback.
    client.app.state.exports._download_slots = threading.BoundedSemaphore(1)
    bad_range = client.get(
        f"/exports/{export_id}/download", headers={"Range": "bytes=999-1000"}
    )
    assert bad_range.status_code == 416
    good_range = client.get(
        f"/exports/{export_id}/download", headers={"Range": "bytes=0-2"}
    )
    assert good_range.status_code == 206
    assert good_range.content == b"pre"


def test_http_export_api_rejects_malformed_export_ids(client):
    assert client.get("/exports/not-an-id").status_code == 404
    assert client.get("/exports/not-an-id/download").status_code == 404
    assert client.post("/exports/not-an-id/download-grant").status_code == 404
    assert client.post("/exports/not-an-id/save").status_code == 404
    assert client.post("/exports/not-an-id/save", json={}).status_code == 404
    assert client.delete("/exports/not-an-id").status_code == 404


def test_download_grant_is_single_use_and_expires():
    now = [10.0]
    store = DownloadGrantStore(ttl_seconds=5, max_grants=2, clock=lambda: now[0])
    token, ttl = store.issue("a" * 32)
    assert ttl == 5
    assert len(token) == 43
    assert store.consume("b" * 32, token) is False
    assert store.consume("a" * 32, token) is True
    assert store.consume("a" * 32, token) is False
    assert store.consume("a" * 32, "short") is False
    assert store.consume("a" * 32, "b" * 43) is False

    second, _ = store.issue("a" * 32)
    now[0] = 16
    assert store.consume("a" * 32, second) is False


def test_download_grant_cap_keeps_the_live_grant():
    store = DownloadGrantStore(ttl_seconds=30, max_grants=1)
    token, _ = store.issue("a" * 32)
    with pytest.raises(DownloadGrantsFull):
        store.issue("b" * 32)
    assert store.consume("a" * 32, token) is True
    store.clear()
    replacement, _ = store.issue("b" * 32)
    assert store.consume("b" * 32, replacement) is True


def _publish_export(client, fmt="opus"):
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
    client.app.state.exports.builder = _builder
    response = client.post("/exports", json={"job_id": job_id, "format": fmt})
    assert response.status_code in (200, 202)
    export_id = response.json()["export_id"]
    deadline = time.monotonic() + 3
    status = None
    while time.monotonic() < deadline:
        status = client.get(f"/exports/{export_id}").json()
        if status["state"] == "ready":
            return export_id
        time.sleep(0.01)
    raise AssertionError(f"export {export_id} did not become ready: {status}")


def test_download_grant_authorizes_one_fetch_without_the_operator_key(client, caplog):
    export_id = _publish_export(client)
    store = client.app.state.auth_store
    raw, _info = store.generate("download-test")
    store.required = True
    download = f"/exports/{export_id}/download"
    grant_url = f"/exports/{export_id}/download-grant"
    assert client.get(download).status_code == 401
    assert client.post(grant_url).status_code == 401

    minted = client.post(grant_url, headers={"Authorization": f"Bearer {raw}"})
    assert minted.status_code == 200
    assert minted.headers["cache-control"] == "no-store"
    token = minted.json()["grant"]
    assert token not in str(minted.request.url)
    assert token not in caplog.text
    assert minted.json()["expires_in"] == 60

    rejected = client.get(download, headers={
        "Authorization": "Bearer nm_" + "0" * 64,
        "X-Nomusic-Download-Grant": token,
    })
    assert rejected.status_code == 401
    assert token not in rejected.text

    other = client.get(f"/exports/{'d' * 32}/download", headers={
        "X-Nomusic-Download-Grant": token,
    })
    assert other.status_code == 401

    saved = client.get(download, headers={"X-Nomusic-Download-Grant": token})
    assert saved.status_code == 200
    assert saved.content == b"prepared"
    assert saved.headers["content-type"].startswith("audio/ogg")
    assert client.get(download, headers={"X-Nomusic-Download-Grant": token}).status_code == 401
    again = client.get(download, headers={"Authorization": f"Bearer {raw}"})
    assert again.status_code == 200
    assert again.content == b"prepared"


def test_safe_download_name_keeps_the_artifact_suffix():
    from nomusic.routes import exports as export_routes

    assert export_routes.safe_download_name(
        "../../etc/passwd.mp4", "built.mp3"
    ) == "passwd.mp3"
    assert export_routes.safe_download_name("   ", "built.mp3") == "built.mp3"
    assert export_routes.safe_download_name(None, "built.mp3") == "built.mp3"
    assert export_routes.safe_download_name("Dr. Who.mp3", "built.mp3") == "Dr. Who.mp3"
    assert export_routes.safe_download_name("a/b\\c.mp3", "built.mp3") == "c.mp3"
    assert export_routes.safe_download_name("..", "built.mp3") == "nomusic.mp3"
    long_name = "n" * 150 + ".mp3"
    assert export_routes.safe_download_name(long_name, "built.mp3") == ("n" * 120) + ".mp3"
    with pytest.raises(ValueError):
        export_routes.safe_download_name("song.mp3", "built.txt")
    with pytest.raises(ValueError):
        export_routes.safe_download_name("song.mp3", "../built.mp3")


def test_copy_export_into_leaves_existing_names_and_quarantine_behind(tmp_path):
    import os
    import stat

    from nomusic.routes import exports as export_routes

    source = tmp_path / "built.mp3"
    source.write_bytes(b"prepared")
    try:
        os.setxattr(source, b"user.quarantine", b"marked")
    except OSError:
        marked = False
    else:
        marked = True
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    secret = tmp_path / "secret"
    secret.write_bytes(b"secret")
    link = downloads / "Song.mp3"
    link.symlink_to(secret)
    saved = export_routes.copy_export_into(source, downloads, "Song.mp3")
    assert saved == "Song (1).mp3"
    assert link.is_symlink()
    assert secret.read_bytes() == b"secret"
    copied = downloads / saved
    assert copied.read_bytes() == b"prepared"
    assert copied.stat().st_ino != source.stat().st_ino
    assert copied.stat().st_nlink == 1
    assert stat.S_ISREG(copied.stat().st_mode)
    assert stat.S_IMODE(copied.stat().st_mode) == 0o644
    if marked:
        assert b"user.quarantine" not in os.listxattr(copied)
    again = export_routes.copy_export_into(source, downloads, "Song (1).mp3")
    assert again == "Song (1) (1).mp3"
    assert copied.read_bytes() == b"prepared"


def test_copy_export_into_removes_a_partial_file(tmp_path, monkeypatch):
    from nomusic.routes import exports as export_routes

    source = tmp_path / "built.mp3"
    source.write_bytes(b"prepared")
    downloads = tmp_path / "Downloads"

    def fail(*_args, **_kwargs):
        raise OSError("disk full at /home/secret/Downloads/Song.mp3")

    monkeypatch.setattr(export_routes.shutil, "copyfileobj", fail)
    with pytest.raises(OSError, match="disk full"):
        export_routes.copy_export_into(source, downloads, "Song.mp3")
    assert list(downloads.iterdir()) == []
    assert source.read_bytes() == b"prepared"


def test_copy_export_into_stops_after_the_collision_cap(tmp_path, monkeypatch):
    from nomusic.routes import exports as export_routes

    source = tmp_path / "built.mp3"
    source.write_bytes(b"prepared")
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "Song.mp3").write_bytes(b"old")
    (downloads / "Song (1).mp3").write_bytes(b"old")
    monkeypatch.setattr(export_routes, "_MAX_DOWNLOAD_COLLISIONS", 1)
    with pytest.raises(FileExistsError):
        export_routes.copy_export_into(source, downloads, "Song.mp3")
    assert (downloads / "Song.mp3").read_bytes() == b"old"
    assert (downloads / "Song (1).mp3").read_bytes() == b"old"


def test_downloads_directory_follows_a_directory_symlink(monkeypatch, tmp_path):
    from nomusic.routes import exports as export_routes

    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "Downloads").symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(export_routes.Path, "home", staticmethod(lambda: tmp_path))
    assert export_routes.downloads_directory() == real.resolve()


def test_client_is_local_only_for_loopback_peers():
    from types import SimpleNamespace

    from nomusic.routes import exports as export_routes

    def request(host):
        return SimpleNamespace(client=SimpleNamespace(host=host))

    assert export_routes._client_is_local(request("127.0.0.1"))
    assert export_routes._client_is_local(request("::1"))
    assert export_routes._client_is_local(request("::ffff:127.0.0.1"))
    assert export_routes._client_is_local(request("testclient"))
    assert not export_routes._client_is_local(request("203.0.113.8"))
    assert not export_routes._client_is_local(request("::ffff:203.0.113.8"))
    assert not export_routes._client_is_local(SimpleNamespace(client=None))


def test_save_export_copies_a_loopback_mp3_into_downloads(client, monkeypatch, tmp_path):
    from nomusic.routes import exports as export_routes

    downloads = tmp_path / "Downloads"
    monkeypatch.setattr(export_routes, "downloads_directory", lambda: downloads)
    export_id = _publish_export(client, "mp3")
    response = client.post(
        f"/exports/{export_id}/save",
        json={"filename": "../../etc/passwd.mp4"},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == {"filename"}
    assert response.json()["filename"] == "passwd.mp3"
    assert str(downloads) not in response.text
    assert str(tmp_path) not in response.text
    saved = downloads / "passwd.mp3"
    assert saved.read_bytes() == b"prepared"
    assert saved.is_symlink() is False

    collision = client.post(
        f"/exports/{export_id}/save",
        json={"filename": "passwd.mp3"},
    )
    assert collision.status_code == 200
    assert collision.json()["filename"] == "passwd (1).mp3"
    assert saved.read_bytes() == b"prepared"
    assert (downloads / "passwd (1).mp3").read_bytes() == b"prepared"
    assert client.post(f"/exports/{'e' * 32}/save", json={}).status_code == 404


def test_save_export_hides_filesystem_errors(client, monkeypatch, tmp_path):
    from nomusic.routes import exports as export_routes

    export_id = _publish_export(client, "mp3")
    secret = tmp_path / "secret-downloads"

    def boom():
        raise OSError(f"permission denied: {secret}")

    monkeypatch.setattr(export_routes, "downloads_directory", boom)
    response = client.post(f"/exports/{export_id}/save", json={})
    assert response.status_code == 500
    assert response.json()["detail"] == "Could not save the export into Downloads."
    assert str(secret) not in response.text
    assert "permission denied" not in response.text


def test_save_export_refuses_a_remote_peer(client, monkeypatch, tmp_path):
    from nomusic.routes import exports as export_routes

    downloads = tmp_path / "Downloads"
    monkeypatch.setattr(export_routes, "downloads_directory", lambda: downloads)
    monkeypatch.setattr(export_routes, "_client_is_local", lambda _request: False)
    export_id = _publish_export(client, "mp3")
    response = client.post(
        f"/exports/{export_id}/save",
        json={"filename": "Song.mp3"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == (
        "Local save is only available for a loopback connection."
    )
    assert not downloads.exists()
