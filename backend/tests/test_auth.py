"""Operator-key storage and the protected HTTP boundary."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nomusic.auth import (
    AuthConfigurationError,
    AuthStore,
    InvalidCredential,
    RevokedCredential,
)


def test_generate_stores_only_a_digest_and_owner_only_file(tmp_path):
    path = tmp_path / "config" / "operator-keys.json"
    store = AuthStore(path)
    raw, info = store.generate("laptop")

    assert raw.startswith("nm_") and len(raw) == 67
    assert info.key_id and info.label == "laptop"
    payload = json.loads(path.read_text())
    assert payload["keys"][0]["digest"] != raw
    assert raw not in path.read_text()
    if os.name == "posix":
        assert path.stat().st_mode & 0o077 == 0
        assert path.parent.stat().st_mode & 0o077 == 0
        lock_path = path.with_name(f".{path.name}.lock")
        assert lock_path.exists()
        assert lock_path.stat().st_mode & 0o077 == 0


def test_authentication_observes_revoke_without_restart(tmp_path):
    store = AuthStore(tmp_path / "keys.json")
    raw, info = store.generate()
    principal = store.authenticate(raw)
    assert principal.key_id == info.key_id
    with pytest.raises(InvalidCredential):
        store.authenticate("nm_" + "0" * 64)

    store.revoke(info.key_id)
    with pytest.raises(RevokedCredential):
        store.authenticate(raw)
    assert store.list_keys()[0].revoked


def test_rotate_replaces_a_key_atomically(tmp_path):
    store = AuthStore(tmp_path / "keys.json")
    old, old_info = store.generate("old")
    new, new_info = store.rotate(label="new", revoke_id=old_info.key_id)
    assert store.authenticate(new).key_id == new_info.key_id
    with pytest.raises(RevokedCredential):
        store.authenticate(old)
    assert [entry.label for entry in store.list_keys()] == ["old", "new"]


def test_required_store_rejects_missing_or_all_revoked_configuration(tmp_path):
    store = AuthStore(tmp_path / "missing.json")
    with pytest.raises(AuthConfigurationError, match="nomusic auth generate"):
        store.validate()
    _, info = store.generate()
    store.revoke(info.key_id)
    with pytest.raises(AuthConfigurationError, match="No active operator keys"):
        store.validate()


def test_required_app_fails_startup_without_keys(monkeypatch, tmp_path):
    from nomusic import server

    monkeypatch.setattr("nomusic.services.check_runtime", lambda: {})
    settings = replace(
        server.SETTINGS,
        cache_dir=tmp_path / "cache",
        auth_file=tmp_path / "missing.json",
    )
    app = server.create_app(settings=settings, auth_store=AuthStore(settings.auth_file))
    with pytest.raises(AuthConfigurationError, match="nomusic auth generate"):
        with TestClient(app):
            pass


@pytest.fixture
def authenticated_client(monkeypatch, tmp_path):
    from nomusic import server

    class Engine:
        def warmup(self):
            return None

        def capabilities(self):
            from nomusic.engines.base import EngineCapabilities

            return EngineCapabilities("fake", "cpu", ("fake",), "fake")

        def prepare(self, audio_path, *, model=None):
            raise NotImplementedError

        def infer_batch(self, prepared):
            raise NotImplementedError

    monkeypatch.setattr("nomusic.services.check_runtime", lambda: {})
    monkeypatch.setattr(server, "get_engine", lambda name: Engine())
    store = AuthStore(tmp_path / "keys.json")
    raw, _ = store.generate("tests")
    settings = replace(
        server.SETTINGS,
        cache_dir=tmp_path / "cache",
        auth_file=tmp_path / "keys.json",
    )
    app = server.create_app(settings=settings, auth_store=store)
    with TestClient(app) as client:
        client.operator_key = raw
        yield client


def test_health_is_public_but_capabilities_require_a_key(authenticated_client):
    client = authenticated_client
    assert client.get("/healthz").status_code == 200
    assert client.get("/capabilities").status_code == 401
    response = client.get(
        "/capabilities", headers={"Authorization": f"Bearer {client.operator_key}"}
    )
    assert response.status_code == 200
    assert response.json()["engine"]["name"] == "fake"


def test_rejected_requests_do_not_reach_processing(authenticated_client):
    client = authenticated_client
    response = client.post(
        "/process", json={"url": "https://example.com/video"},
        headers={"Authorization": "Bearer nm_" + "0" * 64},
    )
    assert response.status_code == 401
    response = client.post(
        "/process", json={"url": "https://example.com/video"},
        headers={"Authorization": f"Bearer {client.operator_key}"},
    )
    assert response.status_code == 200


def test_revocation_blocks_subsequent_requests(authenticated_client):
    client = authenticated_client
    store = client.app.state.auth_store
    key_id = store.list_keys()[0].key_id
    assert client.get(
        "/capabilities",
        headers={"Authorization": f"Bearer {client.operator_key}"},
    ).status_code == 200
    store.revoke(key_id)
    response = client.get(
        "/capabilities",
        headers={"Authorization": f"Bearer {client.operator_key}"},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "credential_revoked"
