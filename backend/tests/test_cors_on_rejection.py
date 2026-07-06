"""A middleware body-size rejection must still carry CORS headers.

MaxBodySizeMiddleware sits inside CORSMiddleware so its 411/413/400 responses are
readable by a browser caller (the extension content script fetches cross-origin).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import config
import server
from engines.base import Engine, EngineCapabilities, SeparationResult


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
def public_client(monkeypatch):
    monkeypatch.setattr(server, "get_engine", lambda name: _CapsOnlyEngine())
    for k, v in {
        "public": True,
        "extension_origin": "chrome-extension://testid",
        "max_request_bytes": 100,
    }.items():
        object.__setattr__(config.SETTINGS, k, v)
    app = server.create_app()
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for k in ("public", "extension_origin", "max_request_bytes"):
            object.__setattr__(config.SETTINGS, k, getattr(config.Settings(), k))


def test_413_carries_cors_header(public_client):
    origin = "https://www.youtube.com"
    body = '{"url":"' + "x" * 400 + '"}'
    r = public_client.post(
        "/process",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Origin": origin,
            "Content-Length": str(len(body)),
        },
    )
    assert r.status_code == 413
    assert r.headers.get("access-control-allow-origin") == origin


def test_411_carries_cors_header(public_client):
    origin = "https://www.youtube.com"
    r = public_client.post(
        "/process",
        headers={"Content-Type": "application/json", "Origin": origin},
        content=iter([b"{}"]),  # chunked, no Content-Length
    )
    assert r.status_code == 411
    assert r.headers.get("access-control-allow-origin") == origin
