"""Direct unit tests for the route modules' module-level pieces.

The endpoints themselves are exercised end-to-end via FastAPI's TestClient in
test_server.py; these cover the parts that don't need the full app — chiefly the
thread-safe MP4-export progress map — and import the route modules directly so
their coverage is tracked rather than only reached transitively through server.
"""

from __future__ import annotations

from routes import media, system
from routes.media import _ExportProgress


def test_export_progress_key_collapses_no_cap():
    assert _ExportProgress.key("job", 1080) == "job:1080"
    # None and 0 both mean "best available", so they share one key.
    assert _ExportProgress.key("job", None) == "job:0"
    assert _ExportProgress.key("job", 0) == "job:0"


def test_export_progress_set_get_clear_roundtrip():
    p = _ExportProgress()
    key = _ExportProgress.key("job", None)
    # An unknown key reports the idle sentinel rather than raising.
    assert p.get(key) == {"phase": "idle", "percent": 0}

    p.set(key, "downloading", 50.0)
    assert p.get(key) == {"phase": "downloading", "percent": 50.0}

    # Percent is rounded to one decimal place.
    p.set(key, "encoding", 99.99)
    assert p.get(key) == {"phase": "encoding", "percent": 100.0}

    p.clear(key)
    assert p.get(key) == {"phase": "idle", "percent": 0}
    # Clearing an absent key is a no-op.
    p.clear(key)


def test_routers_expose_expected_paths():
    media_paths = {r.path for r in media.router.routes}
    assert "/chunk/{job_id}/{chunk_idx}" in media_paths
    assert "/audio/{job_id}" in media_paths
    assert "/video/{job_id}" in media_paths
    assert "/video/{job_id}/progress" in media_paths

    system_paths = {r.path for r in system.router.routes}
    assert {"/healthz", "/capabilities", "/cache", "/cache/clear"} <= system_paths
