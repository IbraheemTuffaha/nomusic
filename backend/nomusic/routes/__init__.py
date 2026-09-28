"""HTTP route modules for the nomusic backend, split by concern.

``server.create_app`` builds the FastAPI app, wires the shared services onto
``app.state`` (``engine`` / ``cache`` / ``registry``), and includes these
routers. Each handler reads what it needs from ``request.app.state`` rather than
closing over create_app locals, so the endpoints live at module scope (testable,
no deeply-nested closures) while create_app stays a thin assembler.

Routers:
  * :mod:`routes.system` — health, capabilities, cache stats/clear.
  * :mod:`routes.jobs`   — submit/prioritize/status + the SSE event stream.
  * :mod:`routes.media`  — per-chunk audio, the concatenated track, MP4 export.
"""

from __future__ import annotations

# JSON object response bodies carry heterogeneous values, so this is as specific
# as a single alias gets; it documents intent better than a bare dict. Shared by
# the route modules and the export-progress map.
JsonDict = dict[str, object]

__all__ = ["JsonDict"]
