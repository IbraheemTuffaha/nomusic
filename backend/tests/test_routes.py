"""Direct unit tests for the route modules' module-level pieces."""

from __future__ import annotations

from nomusic.routes import exports, media, system

def test_routers_expose_expected_paths():
    media_paths = {r.path for r in media.router.routes}
    assert "/chunk/{job_id}/{chunk_idx}" in media_paths
    assert "/audio/{job_id}" in media_paths

    export_paths = {r.path for r in exports.router.routes}
    assert {"/exports", "/exports/{export_id}",
            "/exports/{export_id}/download"} <= export_paths

    system_paths = {r.path for r in system.router.routes}
    assert {"/healthz", "/capabilities", "/readyz"} <= system_paths
    assert "/cache" not in system_paths and "/cache/clear" not in system_paths
