"""Readiness transitions use controlled initialization, never timed sleeps."""

import threading
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from nomusic import server, services


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "SETTINGS", replace(
        server.SETTINGS, cache_dir=tmp_path / "cache", cache_ttl_days=0,
        memory_gc_interval_seconds=0,
    ))
    monkeypatch.setattr(services, "check_runtime", lambda: {})
    monkeypatch.setattr(services, "check_working_storage", lambda settings: {})
    return server.create_app()


def join_warmup(owner):
    for thread in owner._threads:
        if thread.name == "nomusic-engine-warmup":
            thread.join(timeout=3)
            assert not thread.is_alive()


def test_warming_is_live_but_not_ready_then_ready_checks_are_cheap(app, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    class Engine:
        def warmup(self):
            entered.set()
            assert release.wait(3)
    monkeypatch.setattr(server, "get_engine", lambda name: Engine())
    with TestClient(app) as client:
        try:
            assert entered.wait(2)
            assert client.get("/healthz").json() == {"ok": True}
            response = client.get("/readyz")
            assert response.status_code == 503
            assert response.json() == {"ok": False, "state": "warming", "check": "model"}
        finally:
            release.set()
        join_warmup(app.state.services)
        monkeypatch.setattr(services, "check_runtime", lambda: pytest.fail("probe did I/O"))
        monkeypatch.setattr(services, "check_working_storage", lambda _: pytest.fail("probe did I/O"))
        for _ in range(3):
            response = client.get("/readyz")
            assert response.status_code == 200
            assert response.json() == {"ok": True, "state": "ready"}
            assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("failed_check", ["runtime", "storage", "model"])
def test_failed_initialization_is_actionable_but_http_does_not_leak_details(app, monkeypatch, failed_check):
    def fail(*args):
        raise RuntimeError("private file /secret/model and token=test-private")
    class Engine:
        def warmup(self):
            if failed_check == "model":
                fail()
    monkeypatch.setattr(server, "get_engine", lambda name: Engine())
    if failed_check != "model":
        monkeypatch.setattr(services, "check_runtime" if failed_check == "runtime" else "check_working_storage", fail)
    with TestClient(app) as client:
        join_warmup(app.state.services)
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["check"] == failed_check
        assert "nomusic doctor" in response.json()["message"]
        assert "private" not in response.text and "/secret" not in response.text
        assert client.get("/healthz").status_code == 200


@pytest.mark.parametrize("signal_only", [False, True])
def test_late_warmup_cannot_overwrite_stopping(app, monkeypatch, signal_only):
    entered, release = threading.Event(), threading.Event()
    class Engine:
        def warmup(self):
            entered.set()
            assert release.wait(3)
    monkeypatch.setattr(server, "get_engine", lambda name: Engine())
    with TestClient(app) as client:
        try:
            assert entered.wait(2)
            owner = app.state.services
            if signal_only:
                owner.shutdown_requested = True
            else:
                owner.begin_shutdown()
            assert client.get("/readyz").status_code == 503
            assert owner.readiness() == {"ok": False, "state": "stopping"}
        finally:
            release.set()
        join_warmup(owner)
        assert client.get("/readyz").status_code == 503
        assert owner.readiness() == {"ok": False, "state": "stopping"}


def test_signal_revokes_ready_before_async_shutdown(app, monkeypatch):
    class Engine:
        def warmup(self):
            pass
    monkeypatch.setattr(server, "get_engine", lambda name: Engine())
    with TestClient(app) as client:
        owner = app.state.services
        join_warmup(owner)
        assert client.get("/readyz").status_code == 200
        owner.shutdown_requested = True
        assert not owner._stop.is_set()
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json() == {"ok": False, "state": "stopping"}


def test_signal_between_startup_checks_prevents_next_check(app, monkeypatch):
    class Engine:
        def warmup(self):
            pytest.fail("model preload started after signal")
    monkeypatch.setattr(server, "get_engine", lambda name: Engine())
    def stop():
        app.state.services.shutdown_requested = True
    monkeypatch.setattr(services, "check_runtime", stop)
    monkeypatch.setattr(services, "check_working_storage",
                        lambda _: pytest.fail("storage check started after signal"))
    with TestClient(app) as client:
        join_warmup(app.state.services)
        assert client.get("/readyz").json() == {"ok": False, "state": "stopping"}


def test_new_lifespan_rechecks_a_previous_failure(app, monkeypatch):
    attempts = []
    class Engine:
        def warmup(self):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("initial failure")
    monkeypatch.setattr(server, "get_engine", lambda name: Engine())
    for expected in (503, 200):
        with TestClient(app) as client:
            join_warmup(app.state.services)
            assert client.get("/readyz").status_code == expected
    assert len(attempts) == 2


def test_without_lifespan_cannot_claim_readiness(app):
    client = TestClient(app)
    assert client.get("/readyz").status_code == 503
    assert client.get("/readyz").json()["state"] == "not_started"
