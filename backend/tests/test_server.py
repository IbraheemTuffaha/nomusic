"""HTTP-layer tests for server.py via FastAPI's TestClient.

The engine is stubbed (no torch) and no real downloads happen — these exercise
request validation, routing, and the error responses, which is where most of
server.py's branching lives.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import server
from engines.base import Engine, EngineCapabilities, SeparationResult
from routes.jobs import ProcessRequest


class _CapsOnlyEngine(Engine):
    """Engine stub that can report capabilities but never runs inference. The
    HTTP-layer tests don't reach the separation path, so prepare/infer aren't
    needed."""

    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            name="fake",
            device="cpu",
            supported_models=("fake",),
            default_model="fake",
        )

    def prepare(self, audio_path: Path, *, model: str | None = None) -> Any:
        raise NotImplementedError

    def infer_batch(self, prepared: list[Any]) -> list[SeparationResult]:
        raise NotImplementedError


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "get_engine", lambda name: _CapsOnlyEngine())
    app = server.create_app()
    with TestClient(app) as test_client:
        yield test_client


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_capabilities_reports_stub_engine(client):
    resp = client.get("/capabilities")
    assert resp.status_code == 200
    body = resp.json()
    assert body["engine"]["name"] == "fake"
    assert "vocals" in body["engine"]["supported_stems"]
    assert body["defaults"]["chunk_seconds"] > 0


# A well-formed-but-unknown job_id: 16 hex chars (the shape the routes enforce),
# so these exercise the unknown-job path rather than the path-param 422.
_UNKNOWN_JOB = "0123456789abcdef"


def test_status_unknown_job_is_404(client):
    assert client.get(f"/status/{_UNKNOWN_JOB}").status_code == 404


def test_status_malformed_job_id_is_422(client):
    # Not 16 hex chars → rejected by the path-param pattern before any lookup.
    assert client.get("/status/does-not-exist").status_code == 422


def test_events_unknown_job_is_204(client):
    # 204 (not 404) so EventSource stops reconnecting per the SSE spec.
    assert client.get(f"/events/{_UNKNOWN_JOB}").status_code == 204


def test_process_rejects_unknown_stem(client):
    resp = client.post("/process", json={"url": "http://x", "keep_stems": ["banana"]})
    assert resp.status_code == 422


def test_process_rejects_empty_stems(client):
    resp = client.post("/process", json={"url": "http://x", "keep_stems": []})
    assert resp.status_code == 422


def test_process_requires_url(client):
    assert client.post("/process", json={}).status_code == 422


def test_process_request_url_validator(monkeypatch):
    # A page can drive /process, so the URL is an SSRF / local-file primitive:
    # only public http(s) URLs are allowed. (Unit-level so no worker is spawned.)
    import socket as _socket

    from pydantic import ValidationError

    # Hermetic DNS: hostnames resolve to whatever we map here (default public),
    # so the test never touches the network. Literal/encoded IP hosts are
    # checked without resolution, so they don't depend on this stub.
    resolved = {
        "evil-loopback.test": "127.0.0.1",      # a name that points at loopback
        "evil-metadata.test": "169.254.169.254",  # ...or the metadata endpoint
    }

    def _fake_getaddrinfo(host, *args, **kwargs):
        ip = resolved.get(host, "93.184.216.34")  # default: a real public IP
        return [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(_socket, "getaddrinfo", _fake_getaddrinfo)

    assert ProcessRequest(url="https://www.youtube.com/watch?v=abc").url
    assert ProcessRequest(url="http://example.com/v").url
    for bad in (
        "file:///etc/passwd",   # local-file read via yt-dlp
        "ftp://example.com/x",  # non-http(s) scheme
        "http://127.0.0.1:8723/x",
        "http://localhost/x",
        "http://192.168.1.5/x",  # private network (SSRF)
        "http://169.254.169.254/latest/meta-data",  # link-local metadata
        "http://[::1]/x",
        "http://[::ffff:127.0.0.1]/x",  # IPv4-mapped IPv6 loopback
        "http://2130706433/x",          # decimal-encoded 127.0.0.1
        "http://0x7f000001/x",          # hex-encoded 127.0.0.1
        "http://0177.0.0.1/x",          # octal-encoded 127.0.0.1
        "http://127.1/x",               # short-form 127.0.0.1
        "http://2852039166/x",          # decimal-encoded 169.254.169.254
        "http://evil-loopback.test/x",  # hostname resolving to loopback
        "http://evil-metadata.test/x",  # hostname resolving to metadata IP
    ):
        with pytest.raises(ValidationError):
            ProcessRequest(url=bad)


def test_process_rejects_non_public_url_returns_422(client):
    # The HTTP layer surfaces a rejected URL as a 422 (before any worker spawns).
    # The encoded-loopback form is the regression that motivated tightening the
    # validator; it must 422 like the canonical loopback literal does.
    for url in (
        "file:///etc/passwd",
        "http://127.0.0.1/x",
        "http://localhost/x",
        "http://2130706433/x",
    ):
        resp = client.post("/process", json={"url": url, "keep_stems": ["vocals"]})
        assert resp.status_code == 422, url


def test_raise_open_file_limit_lifts_soft_toward_target(monkeypatch):
    # macOS-style low soft, high hard: soft is raised to the 8192 target while
    # the hard limit is passed through untouched.
    import resource

    recorded = {}
    monkeypatch.setattr(resource, "getrlimit", lambda which: (256, 1_000_000))
    monkeypatch.setattr(
        resource, "setrlimit", lambda which, limits: recorded.__setitem__("l", limits)
    )
    server._raise_open_file_limit()
    assert recorded["l"] == (8192, 1_000_000)


def test_raise_open_file_limit_caps_at_finite_hard(monkeypatch):
    # The soft limit can't exceed the hard limit, so the target is clamped to it.
    import resource

    recorded = {}
    monkeypatch.setattr(resource, "getrlimit", lambda which: (256, 512))
    monkeypatch.setattr(
        resource, "setrlimit", lambda which, limits: recorded.__setitem__("l", limits)
    )
    server._raise_open_file_limit()
    assert recorded["l"] == (512, 512)


def test_raise_open_file_limit_noop_when_soft_already_high(monkeypatch):
    # Already at/above the target: no setrlimit call (don't lower it, don't churn).
    import resource

    called = []
    monkeypatch.setattr(resource, "getrlimit", lambda which: (8192, 8192))
    monkeypatch.setattr(resource, "setrlimit", lambda which, limits: called.append(limits))
    server._raise_open_file_limit()
    assert called == []


def test_raise_open_file_limit_is_best_effort(monkeypatch):
    # A setrlimit failure (e.g. sandboxed host) must not propagate out of startup.
    import resource

    def boom(which, limits):
        raise OSError("denied")

    monkeypatch.setattr(resource, "getrlimit", lambda which: (256, 1_000_000))
    monkeypatch.setattr(resource, "setrlimit", boom)
    server._raise_open_file_limit()  # must not raise


def test_chunk_unknown_job_is_404(client):
    assert client.get(f"/chunk/{_UNKNOWN_JOB}/0").status_code == 404


def test_audio_bad_format_is_400(client):
    # Format is validated before the job lookup, so a bad format wins over 404.
    assert client.get(f"/audio/{_UNKNOWN_JOB}?format=flac").status_code == 400


def test_audio_unknown_job_is_404(client):
    assert client.get(f"/audio/{_UNKNOWN_JOB}").status_code == 404


def test_video_unknown_job_is_404(client):
    assert client.get(f"/video/{_UNKNOWN_JOB}").status_code == 404


def test_video_progress_defaults_to_idle(client):
    resp = client.get(f"/video/{_UNKNOWN_JOB}/progress")
    assert resp.status_code == 200
    assert resp.json()["phase"] == "idle"


def test_cache_stats_shape(client):
    # Dev mode: /cache is open (admin gate is a no-op) and reports stats without
    # leaking the on-disk root path.
    resp = client.get("/cache")
    assert resp.status_code == 200
    body = resp.json()
    assert "total_bytes" in body
    assert "job_count" in body
    assert "root" not in body


def test_cache_clear_returns_deleted_bytes(client):
    resp = client.post("/cache/clear")
    assert resp.status_code == 200
    assert "deleted_bytes" in resp.json()


# --- Happy path: a fully-cached job streams without any download/separation ---


def _seed_complete_job(client) -> str:
    """Write a complete cache entry (meta + one chunk) the way the processor
    would, and return its job_id. Lets the HTTP happy path run with no network
    or engine work."""
    from config import SETTINGS
    from pipeline.cache import CacheMeta

    cache = client.app.state.cache
    job_id = cache.key(
        "http://example.com/v",
        "fake",
        ["vocals"],
        chunk_seconds=SETTINGS.chunk_seconds,
        chunk_overlap_seconds=SETTINGS.chunk_overlap_seconds,
    )
    cache.save_meta(
        job_id,
        CacheMeta(
            url="http://example.com/v",
            model="fake",
            keep_stems=["vocals"],
            duration_seconds=10.0,
            chunk_seconds=SETTINGS.chunk_seconds,
            chunk_overlap_seconds=SETTINGS.chunk_overlap_seconds,
            total_chunks=1,
            title="Cached",
            extractor="fake",
            chunks_ready=[0],
            complete=True,
        ),
    )
    cache.chunk_path(job_id, 0).write_bytes(b"OggS-fake-opus-bytes")
    return job_id


def test_process_cache_hit_returns_ready(client):
    _seed_complete_job(client)
    resp = client.post(
        "/process", json={"url": "http://example.com/v", "keep_stems": ["vocals"]}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "ready"
    assert body["total_chunks"] == 1


def test_status_and_chunk_for_cached_job(client):
    job_id = _seed_complete_job(client)
    status = client.get(f"/status/{job_id}")
    assert status.status_code == 200
    assert status.json()["state"] == "ready"

    chunk = client.get(f"/chunk/{job_id}/0")
    assert chunk.status_code == 200
    assert chunk.content == b"OggS-fake-opus-bytes"

    # Out-of-range chunk index is a 404, not a 200 of the wrong file.
    assert client.get(f"/chunk/{job_id}/5").status_code == 404


def test_events_terminal_job_single_shot(client):
    job_id = _seed_complete_job(client)
    resp = client.get(f"/events/{job_id}")
    assert resp.status_code == 200
    # A terminal (ready) job yields one SSE data frame and closes.
    assert "data:" in resp.text
    assert '"state": "ready"' in resp.text or '"state":"ready"' in resp.text
