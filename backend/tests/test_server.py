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


def test_status_unknown_job_is_404(client):
    assert client.get("/status/does-not-exist").status_code == 404


def test_events_unknown_job_is_204(client):
    # 204 (not 404) so EventSource stops reconnecting per the SSE spec.
    assert client.get("/events/does-not-exist").status_code == 204


def test_process_rejects_unknown_stem(client):
    resp = client.post("/process", json={"url": "http://x", "keep_stems": ["banana"]})
    assert resp.status_code == 422


def test_process_rejects_empty_stems(client):
    resp = client.post("/process", json={"url": "http://x", "keep_stems": []})
    assert resp.status_code == 422


def test_process_requires_url(client):
    assert client.post("/process", json={}).status_code == 422


def test_chunk_unknown_job_is_404(client):
    assert client.get("/chunk/nope/0").status_code == 404


def test_audio_bad_format_is_400(client):
    # Format is validated before the job lookup, so a bad format wins over 404.
    assert client.get("/audio/whatever?format=flac").status_code == 400


def test_audio_unknown_job_is_404(client):
    assert client.get("/audio/whatever").status_code == 404


def test_video_unknown_job_is_404(client):
    assert client.get("/video/whatever").status_code == 404


def test_video_progress_defaults_to_idle(client):
    resp = client.get("/video/whatever/progress")
    assert resp.status_code == 200
    assert resp.json()["phase"] == "idle"


def test_cache_stats_shape(client):
    resp = client.get("/cache")
    assert resp.status_code == 200
    body = resp.json()
    assert "total_bytes" in body
    assert "job_count" in body
    assert "root" in body
