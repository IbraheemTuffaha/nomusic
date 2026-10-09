"""Uvicorn adapter with quiet stream closure and a bounded process shutdown.

Uvicorn drains HTTP before ASGI lifespan shutdown. Stop admission and status
streams first, then let active requests and cooperative jobs finish within one
process-wide grace period. A first-start model preload alone is disposable.
A watchdog and repeated interrupt exit the process if native work cannot stop;
they do not pretend to cancel a running Python thread.

The signal context covers asyncio executor teardown as well as HTTP/lifespan.
The pinned Uvicorn integration is exercised by real signal and reload tests.
No application services are constructed in the reloader's parent process.
"""

from __future__ import annotations

import contextlib
import math
import os
import signal
import threading
import time
from socket import socket
from typing import Any

import uvicorn
from uvicorn.config import STARTUP_FAILURE
from uvicorn.supervisors import ChangeReload


class LifecycleServer(uvicorn.Server):
    """Graceful work drain with a process-level escape for stuck native calls.

    Cancelling an asyncio task cannot stop its worker thread. The watchdog is
    deliberately outside the event loop and exits the *process* on expiry.
    No partial cleanup is attempted while a writer may still be using files.
    """

    def __init__(self, config):
        super().__init__(config)
        from nomusic.config import SETTINGS
        self.grace_seconds = SETTINGS.shutdown_grace_seconds
        if not math.isfinite(self.grace_seconds) or self.grace_seconds <= 0:
            raise ValueError("NOMUSIC_SHUTDOWN_GRACE_SECONDS must be finite and positive")
        self._watchdog: threading.Timer | None = None
        self._shutdown_deadline: float | None = None
        self._received_signal = False
        self._running = False
        self._application = None
        self._shutdown_services = None

    async def startup(self, sockets=None):
        self._application = self.config.load_app()
        state = getattr(self._application, "state", None)
        previous = getattr(state, "prepare_process_shutdown", None)
        if state is not None:
            state.prepare_process_shutdown = self._prepare_shutdown
        try:
            await super().startup(sockets=sockets)
        finally:
            # The lifespan captures the callback before creating its owner.
            # Do not change subsequent embedded lifespans of this app object.
            if state is not None:
                if previous is None:
                    del state.prepare_process_shutdown
                else:
                    state.prepare_process_shutdown = previous

    @staticmethod
    def _announce(message):
        # Diagnostics must not prevent interruption when stderr is closed or
        # a logging pipe is full. Avoid Python logging locks in signal handlers.
        try:
            blocking = os.get_blocking(2)
            os.set_blocking(2, False)
        except OSError:
            return
        try:
            os.write(2, message.encode())
        except OSError:
            pass
        finally:
            try:
                os.set_blocking(2, blocking)
            except OSError:
                pass

    def _force_exit(self, *, repeated=False):
        reason = "second interrupt" if repeated else "shutdown grace period expired"
        self._announce(f"Forced shutdown ({reason}); unfinished work was interrupted; restart and retry.\n")
        services = getattr(getattr(self._application, "state", None), "services", None)
        worker = getattr(services, "worker", None)
        if worker is not None:
            try:
                worker.force_shutdown()
            except Exception:
                # The process is exiting immediately; diagnostics must not keep
                # a stuck native worker alive or block the signal path.
                pass
        os._exit(130 if repeated else 124)

    def _start_watchdog(self, seconds=None):
        if not self._running:
            return
        now = time.monotonic()
        deadline = now + (self.grace_seconds if seconds is None else seconds)
        if self._shutdown_deadline is not None and deadline >= self._shutdown_deadline:
            return  # never extend the total deadline while moving between phases
        self._shutdown_deadline = deadline
        if self._watchdog is not None:
            self._watchdog.cancel()
        self._watchdog = threading.Timer(max(0, deadline - now), self._force_exit)
        self._watchdog.daemon = True
        self._watchdog.start()

    def handle_exit(self, sig, frame):
        if self._received_signal:
            if sig == signal.SIGINT:
                self._force_exit(repeated=True)
            # A process-group SIGTERM can reach both the reloader and its
            # child, which the reloader then terminates again. Keep the first
            # announcement and deadline; a repeated Ctrl+C still forces exit.
            return
        self._received_signal = True
        services = getattr(getattr(self._application, "state", None), "services", None)
        if services is not None:
            # A terminal signal can interrupt FFmpeg before Uvicorn reaches
            # shutdown(). Silence streams immediately, without acquiring a lock
            # that this signal might itself have interrupted on the main thread.
            services.shutdown_requested = True
        self._announce(
            f"Stopping: finishing active processing/requests (up to {self.grace_seconds:g}s); "
            "press Ctrl+C again to force quit. Model preload alone will not be awaited.\n"
        )
        self._start_watchdog()
        self.should_exit = True

    @contextlib.contextmanager
    def capture_signals(self):
        # Uvicorn normally restores handlers before asyncio.run drains executor
        # threads. Keep ours active across that drain too (see run below).
        yield

    def run(self, sockets=None):
        handlers = {}
        if threading.current_thread() is threading.main_thread():
            handlers = {sig: signal.signal(sig, self.handle_exit)
                        for sig in (signal.SIGINT, signal.SIGTERM)}
        self._running = True
        exit_code = 1
        try:
            result = super().run(sockets=sockets)
            exit_code = 0 if self.started else STARTUP_FAILURE
            return result
        except SystemExit as error:
            exit_code = (error.code if isinstance(error.code, int)
                         else 0 if error.code is None else 1)
            raise
        except KeyboardInterrupt:
            exit_code = 130
            raise
        finally:
            try:
                preload = getattr(self._shutdown_services, "discarded_preload", None)
                if preload is not None and preload.is_alive():
                    # Hub downloads can own executor threads that Python joins
                    # after asyncio.run, including when startup raised SystemExit.
                    # Preserve failure status while disposing only the preload.
                    self._announce("Model preload interrupted; verified downloads can be reused on next start.\n")
                    os._exit(exit_code)
            finally:
                self._running = False
                if self._watchdog is not None:
                    self._watchdog.cancel()
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)

    def _prepare_shutdown(self, services) -> None:
        """Bound all process-owned teardown, including startup-error lifespans."""
        self._start_watchdog()
        self._shutdown_services = services
        services.begin_shutdown()
        registry = getattr(services, "registry", None)
        if not (registry is not None and registry.has_active_workers) and not self.server_state.tasks:
            # Preload alone is disposable; keep the same deadline if work was
            # already draining, and never extend it between shutdown phases.
            self._start_watchdog(min(1.0, self.grace_seconds))

    async def shutdown(self, sockets: list[socket] | None = None) -> None:
        self._start_watchdog()
        app = self.config.load_app()
        services = getattr(getattr(app, "state", None), "services", None)
        if services is not None:
            self._prepare_shutdown(services)
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
