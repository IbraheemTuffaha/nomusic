"""Public-only hardening must be a no-op in dev (the PR's headline invariant)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import config
import server
from engines.base import Engine, EngineCapabilities, SeparationResult
from routes.jobs import ProcessRequest


class _CapsOnlyEngine(Engine):
    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            name="fake", device="cpu",
            supported_models=("fake",), default_model="fake",
        )

    def prepare(self, audio_path: Path, *, model: str | None = None) -> Any:
        raise NotImplementedError

    def infer_batch(self, prepared: list[Any]) -> list[SeparationResult]:
        raise NotImplementedError


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "get_engine", lambda name: _CapsOnlyEngine())
    app = server.create_app()
    with TestClient(app) as c:
        yield c


def test_dev_docs_have_no_blocking_csp(client):
    # In dev, /docs must render (no default-src 'none' CSP blanking Swagger UI).
    r = client.get("/docs")
    assert r.status_code == 200
    assert "content-security-policy" not in {k.lower() for k in r.headers}


def test_public_response_has_csp():
    saved = config.SETTINGS.public
    object.__setattr__(config.SETTINGS, "public", True)
    try:
        import middleware

        # Build a throwaway app just to exercise the middleware in public mode.
        from fastapi import FastAPI
        from fastapi.testclient import TestClient as TC

        app = FastAPI()
        app.add_middleware(middleware.SecurityHeadersMiddleware)

        @app.get("/x")
        def _x():
            return {"ok": True}

        r = TC(app).get("/x")
        assert r.headers.get("content-security-policy") == (
            "default-src 'none'; frame-ancestors 'none'"
        )
    finally:
        object.__setattr__(config.SETTINGS, "public", saved)


def test_dev_url_length_unbounded():
    # Pre-PR behavior: dev accepts an over-long URL (validator only caps in public).
    long_url = "https://example.com/?q=" + "a" * 5000
    req = ProcessRequest(url=long_url)  # must not raise
    assert req.url == long_url


def test_public_url_length_capped():
    saved = config.SETTINGS.public
    object.__setattr__(config.SETTINGS, "public", True)
    try:
        long_url = "https://www.youtube.com/?q=" + "a" * 5000
        with pytest.raises(ValueError):
            ProcessRequest(url=long_url)
    finally:
        object.__setattr__(config.SETTINGS, "public", saved)
