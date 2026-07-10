"""Edge-trust + admin-token gates: fail closed, no 500s, no off-tunnel leaks."""

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
def client(monkeypatch):
    monkeypatch.setattr(server, "get_engine", lambda name: _CapsOnlyEngine())
    app = server.create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def public(monkeypatch):
    def _set(**kw):
        for k, v in kw.items():
            object.__setattr__(config.SETTINGS, k, v)
    saved = {
        k: getattr(config.SETTINGS, k)
        for k in ("public", "admin_token", "tunnel_secret")
    }
    _set(public=True, admin_token="secret-token", tunnel_secret="tunnel-secret")
    try:
        yield
    finally:
        for k, v in saved.items():
            object.__setattr__(config.SETTINGS, k, v)


def test_non_ascii_admin_token_fails_closed_404_not_500(public):
    # Over the wire a non-ASCII header arrives as latin-1 bytes that Starlette
    # decodes to a non-ASCII str; compare_digest used to raise TypeError on that
    # (-> 500). require_admin must fail closed (404) instead. Called directly
    # since httpx won't transmit a non-ASCII header string.
    import asyncio

    from fastapi import HTTPException

    import security

    with pytest.raises(HTTPException) as exc:
        asyncio.run(security.require_admin(x_admin_token="tökén"))
    assert exc.value.status_code == 404


def test_non_ascii_tunnel_secret_fails_closed_404(public):
    import asyncio

    from fastapi import HTTPException

    import security

    class _Req:
        headers = {"x-nomusic-tunnel": "tünnel"}

    with pytest.raises(HTTPException) as exc:
        asyncio.run(security.require_edge(_Req()))
    assert exc.value.status_code == 404


def test_admin_and_capabilities_require_edge_off_tunnel(client, public):
    # Off-tunnel (no/incorrect X-Nomusic-Tunnel) -> looks absent.
    assert client.get("/capabilities").status_code == 404
    assert (
        client.post(
            "/cache/clear", headers={"X-Admin-Token": "secret-token"}
        ).status_code
        == 404
    )
    # Through the tunnel + correct admin token -> allowed.
    hdr = {"X-Nomusic-Tunnel": "tunnel-secret"}
    assert client.get("/capabilities", headers=hdr).status_code == 200
    assert (
        client.post(
            "/cache/clear", headers={**hdr, "X-Admin-Token": "secret-token"}
        ).status_code
        == 200
    )


def test_healthz_stays_open_off_tunnel(client, public):
    # Liveness probe hits loopback directly with no tunnel header -> still 200.
    assert client.get("/healthz").status_code == 200
