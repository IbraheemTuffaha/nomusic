"""Public-mode hardening tests.

Every gate added for the public deployment is a no-op when ``NOMUSIC_PUBLIC`` is
unset (the default the other test modules run under). These tests flip the
frozen ``SETTINGS`` into public mode (via ``object.__setattr__``, the only way to
mutate a frozen dataclass) so the gates actually engage, and restore it after.
The gates read ``SETTINGS`` at call time, so flipping it around a request works
even though the app was built in dev mode.
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


@pytest.fixture
def public_mode():
    """Flip SETTINGS into public mode for the duration of a test, then restore."""
    overrides = {
        "public": True,
        "admin_token": "secret-token",
        "extension_origin": "chrome-extension://testextensionid",
    }
    saved = {k: getattr(config.SETTINGS, k) for k in overrides}
    for k, v in overrides.items():
        object.__setattr__(config.SETTINGS, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            object.__setattr__(config.SETTINGS, k, v)


# --- Admin token gate (F3/F19) ----------------------------------------------


def test_cache_routes_require_admin_token_in_public_mode(client, public_mode):
    # No token → the endpoints look absent (404), not 401/403.
    assert client.get("/cache").status_code == 404
    assert client.post("/cache/clear").status_code == 404
    # Wrong token → still 404.
    assert client.get("/cache", headers={"X-Admin-Token": "nope"}).status_code == 404
    # Correct token → through.
    ok = client.get("/cache", headers={"X-Admin-Token": "secret-token"})
    assert ok.status_code == 200
    assert "total_bytes" in ok.json()
    cleared = client.post("/cache/clear", headers={"X-Admin-Token": "secret-token"})
    assert cleared.status_code == 200
    assert "deleted_bytes" in cleared.json()


def test_cache_routes_open_in_dev_mode(client):
    # Sanity: with public unset the admin gate is a no-op (the dev experience).
    assert client.get("/cache").status_code == 200
    assert client.post("/cache/clear").status_code == 200


def test_capabilities_slimmed_in_public_mode(client, public_mode):
    body = client.get("/capabilities").json()
    assert "server_version" not in body
    assert "device" not in body["engine"]
    # The bits the extension actually consumes are still present.
    assert body["engine"]["name"] == "fake"
    assert "supported_stems" in body["engine"]


def test_job_id_pattern_rejects_traversal(client):
    # Mode-independent: the path-param pattern rejects anything but 16 hex.
    assert client.get("/status/../../etc/passwd").status_code in (404, 422)
    assert client.get("/status/ZZZZ").status_code == 422


# --- SSRF / URL allowlist (F4/F6) -------------------------------------------


def _stub_public_dns(monkeypatch):
    import socket as _socket

    def _fake_getaddrinfo(host, *args, **kwargs):
        return [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(_socket, "getaddrinfo", _fake_getaddrinfo)


def test_url_allowlist_enforced_in_public_mode(public_mode, monkeypatch):
    import netsec

    _stub_public_dns(monkeypatch)
    # Allowlisted host (youtube) passes; an arbitrary public host is rejected so
    # the box can't be an open download proxy.
    assert netsec.validate_public_url("https://www.youtube.com/watch?v=abc")
    assert netsec.validate_public_url("https://m.facebook.com/watch/?v=1")
    with pytest.raises(netsec.UrlNotAllowed):
        netsec.validate_public_url("https://evil.example.com/big.mkv")


def test_url_allowlist_not_enforced_in_dev(monkeypatch):
    import netsec

    _stub_public_dns(monkeypatch)
    # Dev mode: any public http(s) host is accepted (localhost-only tool).
    assert netsec.validate_public_url("https://evil.example.com/big.mkv")


# --- Rate limit + concurrency gate units ------------------------------------


def test_window_rate_limit_counts_and_blocks():
    from ratelimit import _Window

    w = _Window(limit=2, window=60.0)
    assert w.check("1.2.3.4")[0] is True
    assert w.check("1.2.3.4")[0] is True
    allowed, retry = w.check("1.2.3.4")
    assert allowed is False and retry > 0
    # A different key has its own budget.
    assert w.check("5.6.7.8")[0] is True


def test_gate_blocks_over_limit_in_public_mode(public_mode):
    from fastapi import HTTPException

    from ratelimit import Gate

    gate = Gate(1)
    with gate.slot():
        with pytest.raises(HTTPException) as exc:
            with gate.slot():
                pass
        assert exc.value.status_code == 503
    # Released after the outer slot exits.
    with gate.slot():
        pass


def test_gate_is_noop_in_dev():
    from ratelimit import Gate

    gate = Gate(1)
    # No public mode → nested slots never block.
    with gate.slot():
        with gate.slot():
            pass


# --- CORS origin policy ------------------------------------------------------


def test_is_origin_allowed_public(public_mode):
    s = config.SETTINGS
    assert s.is_origin_allowed("chrome-extension://testextensionid")
    assert s.is_origin_allowed("https://www.youtube.com")
    assert s.is_origin_allowed("https://m.facebook.com")
    assert not s.is_origin_allowed("https://evil.example.com")
    assert not s.is_origin_allowed("http://www.youtube.com")  # http, not https
