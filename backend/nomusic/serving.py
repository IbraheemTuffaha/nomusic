"""Uvicorn adapter that stops long-lived subscriptions before HTTP drain.

Uvicorn drains requests before sending ASGI lifespan shutdown. A status stream
can otherwise keep that drain open indefinitely, while the application waits
for lifespan shutdown to stop its jobs. The adapter only announces shutdown
early; lifespan remains responsible for joining application work. Ordinary
requests, including synchronous exports, still get Uvicorn's normal drain.

``Server.shutdown`` and ``ChangeReload`` are the small integration surface with
the pinned Uvicorn version. Real signal/reload tests cover that ordering. No
application services are constructed in the reloader's parent process.
"""

from __future__ import annotations

from socket import socket
from typing import Any

import uvicorn
from uvicorn.config import STARTUP_FAILURE
from uvicorn.supervisors import ChangeReload


class LifecycleServer(uvicorn.Server):
    async def shutdown(self, sockets: list[socket] | None = None) -> None:
        # load_app returns the same imported app, before Uvicorn's middleware
        # wrappers. Factory applications are deliberately not a launch mode.
        app = self.config.load_app()
        services = getattr(getattr(app, "state", None), "services", None)
        if services is not None:
            services.begin_shutdown()
        await super().shutdown(sockets=sockets)


def serve(
    app: Any,
    *,
    host: str,
    port: int,
    reload: bool = False,
    reload_dirs: list[str] | None = None,
) -> None:
    """Run one API worker, optionally supervised by Uvicorn's dev reloader.

    ``app`` is an import string or an already assembled ASGI application.
    Reload requires an import string so each child imports its own app.
    """
    if reload and not isinstance(app, str):
        raise ValueError("Reload requires an application import string")

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        reload=reload,
        reload_dirs=reload_dirs if reload else None,
        workers=1,
        lifespan="on",
        log_level="info",
        access_log=False,
    )
    server = LifecycleServer(config)
    sock: socket | None = None
    try:
        if config.should_reload:
            sock = config.bind_socket()
            ChangeReload(config, target=server.run, sockets=[sock]).run()
        else:
            server.run()
    except KeyboardInterrupt:
        pass
    finally:
        if sock is not None:
            sock.close()

    if not server.started and not config.should_reload:
        raise SystemExit(STARTUP_FAILURE)
