"""Real signals, streaming requests, and reload against the Uvicorn adapter.

The fixture application has no model or network dependency. It records actual
process lifecycle events and holds a synchronous request open to prove that
shutdown closes SSE without abandoning an ordinary request thread.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from nomusic.serving import LifecycleServer, serve


def test_shutdown_announces_stop_before_http_drain(monkeypatch):
    order = []
    services = SimpleNamespace(begin_shutdown=lambda: order.append("stop"))
    app = SimpleNamespace(state=SimpleNamespace(services=services))

    async def drain(self, sockets=None):
        order.append("drain")

    monkeypatch.setattr(uvicorn.Server, "shutdown", drain)
    asyncio.run(LifecycleServer(uvicorn.Config(app)).shutdown())
    assert order == ["stop", "drain"]


def test_reload_rejects_app_object():
    with pytest.raises(ValueError, match="import string"):
        serve(object(), host="127.0.0.1", port=0, reload=True)


_APP = '''\
import asyncio
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

ROOT = Path(__file__).parent

def record(event):
    with (ROOT / "events.jsonl").open("a") as file:
        file.write(json.dumps({"event": event, "pid": os.getpid()}) + "\\n")

class Services:
    def __init__(self):
        self.stopping = threading.Event()

    def begin_shutdown(self):
        if not self.stopping.is_set():
            record("stopping")
            self.stopping.set()

@asynccontextmanager
async def lifespan(app):
    if os.environ.get("FIXTURE_FAIL_STARTUP"):
        raise RuntimeError("fixture startup failure")
    app.state.services = Services()
    record("startup")
    try:
        yield
    finally:
        app.state.services.begin_shutdown()
        record("shutdown")

app = FastAPI(lifespan=lifespan)

@app.get("/pid")
def pid():
    return {"pid": os.getpid()}

@app.get("/events")
async def events():
    async def stream():
        yield "data: connected\\n\\n"
        while not app.state.services.stopping.is_set():
            await asyncio.sleep(0.01)
        yield "data: stopped\\n\\n"
    return StreamingResponse(stream(), media_type="text/event-stream")

@app.get("/export")
def export():
    record("export-start")
    deadline = time.monotonic() + 15
    while not (ROOT / "release-export").exists():
        if time.monotonic() > deadline:
            raise RuntimeError("test did not release export")
        time.sleep(0.01)
    record("export-end")
    return {"complete": True}
'''


def _wait_for(check, *, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.03)
    raise AssertionError("Timed out waiting for fixture server")


def _events(root):
    path = root / "events.jsonl"
    if not path.exists():
        return []
    # Only complete lines: the writer may currently be appending an event.
    return [json.loads(line) for line in path.read_text().splitlines(keepends=True)
            if line.endswith("\n")]


def _pid(base):
    try:
        response = httpx.get(f"{base}/pid", timeout=0.3)
        return response.json()["pid"] if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


@contextmanager
def _running_server(root, *, reload=False, fail_startup=False):
    (root / "fixture_app.py").write_text(_APP)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    runner = root / "runner.py"
    runner.write_text(
        "from nomusic.serving import serve\n"
        "if __name__ == '__main__':\n"
        f"    serve('fixture_app:app', host='127.0.0.1', port={port}, "
        f"reload={reload!r}, reload_dirs=[{str(root)!r}])\n"
    )
    env = os.environ.copy()
    # Ensure a subprocess tests the same source/install as this pytest process.
    # Appending existing PYTHONPATH also preserves any caller's test adapters.
    import nomusic
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (
        str(Path(nomusic.__file__).resolve().parent.parent),
        env.get("PYTHONPATH"),
    )))
    if fail_startup:
        env["FIXTURE_FAIL_STARTUP"] = "1"
    with (root / "server.log").open("w+") as log:
        proc = subprocess.Popen(
            [sys.executable, str(runner)], cwd=root, env=env,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        try:
            yield proc, f"http://127.0.0.1:{port}"
        finally:
            (root / "release-export").touch()
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="Supported server profiles use POSIX signals")
def test_sigterm_closes_sse_and_drains_active_sync_request(tmp_path):
    with _running_server(tmp_path) as (proc, base):
        assert _wait_for(lambda: _pid(base)) == proc.pid
        with httpx.Client(timeout=10) as client, ThreadPoolExecutor(1) as pool:
            with client.stream("GET", f"{base}/events") as stream:
                lines = stream.iter_lines()
                assert next(lines) == "data: connected"
                export = pool.submit(httpx.get, f"{base}/export", timeout=15)
                _wait_for(lambda: any(e["event"] == "export-start" for e in _events(tmp_path)))

                proc.send_signal(signal.SIGTERM)
                _wait_for(lambda: any(e["event"] == "stopping" for e in _events(tmp_path)))
                assert "data: stopped" in list(lines)
                assert proc.poll() is None
                assert not export.done()

                (tmp_path / "release-export").touch()
                assert export.result(timeout=5).json() == {"complete": True}
        assert proc.wait(timeout=5) in (0, -signal.SIGTERM)
        events = [entry["event"] for entry in _events(tmp_path)]
        assert events == ["startup", "export-start", "stopping", "export-end", "shutdown"]


@pytest.mark.skipif(os.name != "posix", reason="Supported server profiles use POSIX signals")
def test_reload_stops_old_child_before_starting_new_owner(tmp_path):
    with _running_server(tmp_path, reload=True) as (proc, base):
        first = _wait_for(lambda: _pid(base))
        assert first != proc.pid
        # Give watchfiles its first snapshot before changing the watched file.
        time.sleep(0.4)
        with (tmp_path / "fixture_app.py").open("a") as source:
            source.write("\n# Trigger the actual development reloader.\n")

        second = _wait_for(lambda: (pid if (pid := _pid(base)) != first else None))
        assert second != proc.pid
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0

        assert _events(tmp_path) == [
            {"event": "startup", "pid": first},
            {"event": "stopping", "pid": first},
            {"event": "shutdown", "pid": first},
            {"event": "startup", "pid": second},
            {"event": "stopping", "pid": second},
            {"event": "shutdown", "pid": second},
        ]
        assert _pid(base) is None


def test_failed_startup_exits_nonzero(tmp_path):
    with _running_server(tmp_path, fail_startup=True) as (proc, base):
        assert proc.wait(timeout=10) == 3
        assert _events(tmp_path) == []
        assert _pid(base) is None
