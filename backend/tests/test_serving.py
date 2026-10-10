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


def test_shutdown_grace_defaults_to_sixty_seconds_and_can_be_overridden(monkeypatch):
    from nomusic.config import Settings
    monkeypatch.delenv("NOMUSIC_SHUTDOWN_GRACE_SECONDS", raising=False)
    assert Settings().shutdown_grace_seconds == 60
    monkeypatch.setenv("NOMUSIC_SHUTDOWN_GRACE_SECONDS", "12.5")
    assert Settings().shutdown_grace_seconds == 12.5


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
def _running_server(root, *, reload=False, fail_startup=False, port=None):
    (root / "fixture_app.py").write_text(_APP)
    if port is None:
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


@pytest.mark.skipif(os.name != "posix", reason="Supported server profiles use POSIX signals")
def test_reload_group_sigterm_announces_stop_once_while_draining(tmp_path):
    with _running_server(tmp_path, reload=True) as (proc, base):
        child = _wait_for(lambda: _pid(base))
        assert child != proc.pid
        with ThreadPoolExecutor(1) as pool:
            export = pool.submit(httpx.get, f"{base}/export", timeout=15)
            _wait_for(lambda: any(e["event"] == "export-start" for e in _events(tmp_path)))

            # Both processes receive this signal. The real Uvicorn reloader
            # forwards another SIGTERM to its child while joining it.
            os.killpg(proc.pid, signal.SIGTERM)
            _wait_for(lambda: any(e["event"] == "stopping" for e in _events(tmp_path)))
            assert proc.poll() is None
            assert not export.done()
            (tmp_path / "release-export").touch()
            assert export.result(timeout=5).json() == {"complete": True}
        assert proc.wait(timeout=10) == 0
        assert _pid(base) is None
    text = (tmp_path / "server.log").read_text()
    assert text.count("Stopping: finishing active processing/requests") == 1
    assert "Forced shutdown" not in text


def test_failed_startup_exits_nonzero(tmp_path):
    with _running_server(tmp_path, fail_startup=True) as (proc, base):
        assert proc.wait(timeout=10) == 3
        assert _events(tmp_path) == []
        assert _pid(base) is None


# Real application lifespan/registry; only expensive model/network work is
# replaced with file barriers so interrupt timing is deterministic offline.
_WORK_APP = '''\
import os
import time
from dataclasses import replace
from pathlib import Path
from nomusic import server

ROOT = Path(__file__).parent
MODE = os.environ.get("FIXTURE_WORK", "none")

def record(event):
    with (ROOT / "work-events").open("a") as out:
        out.write(event + "\\n")

def wait_for_release(name):
    record(name + "-start")
    while not (ROOT / ("release-" + name)).exists():
        time.sleep(0.01)
    record(name + "-end")

class Engine:
    def warmup(self):
        if MODE == "warmup":
            wait_for_release("warmup")
        elif MODE == "warmup-executor":
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(1) as pool:
                pool.submit(wait_for_release, "warmup").result()

server.SETTINGS = replace(server.SETTINGS, cache_dir=ROOT / "cache",
                          cache_ttl_days=0, memory_gc_interval_seconds=0)
server.get_engine = lambda _: Engine()
from nomusic.auth import AuthStore
app = server.create_app(auth_store=AuthStore(ROOT / 'fixture-keys', required=False))

@app.get("/pid")
def pid():
    return {"pid": os.getpid()}

@app.post("/job")
def job():
    registry = app.state.registry
    def run(*args, hooks, **kwargs):
        (app.state.cache.scratch.path / "chunk.part").write_bytes(b"unfinished")
        wait_for_release("job")
        hooks.abort_check()
    registry.processor.run = run
    registry.submit("https://example.test/interrupt", model="fixture", keep_stems=["vocals"])
    return {"accepted": True}

@app.get("/export")
def export():
    (app.state.cache.scratch.path / "export.part").write_bytes(b"unfinished")
    wait_for_release("export")
    return {"complete": True}
'''


@contextmanager
def _work_server(root, *, mode="none", grace=None):
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sys.modules[__name__], "_APP", _WORK_APP)
        patch.setenv("FIXTURE_WORK", mode)
        if grace is None:
            patch.delenv("NOMUSIC_SHUTDOWN_GRACE_SECONDS", raising=False)
        else:
            patch.setenv("NOMUSIC_SHUTDOWN_GRACE_SECONDS", str(grace))
        with _running_server(root) as running:
            yield running


def _work_started(root, kind):
    path = root / "work-events"
    return path.exists() and f"{kind}-start" in path.read_text()


@pytest.mark.parametrize("mode", ["warmup", "warmup-executor"])
def test_first_interrupt_does_not_wait_for_model_download(tmp_path, mode):
    with _work_server(tmp_path, mode=mode) as (proc, base):
        assert _wait_for(lambda: _pid(base)) == proc.pid
        _wait_for(lambda: _work_started(tmp_path, "warmup"))
        started = time.monotonic()
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=3) == 0
        assert time.monotonic() - started < 2
    text = (tmp_path / "server.log").read_text()
    assert "Model preload alone will not be awaited" in text
    assert "Service shutdown complete" in text
    assert "Forced shutdown" not in text
    assert "warmup-end" not in (tmp_path / "work-events").read_text()
    # A subsequent first start is independent of the abandoned daemon preload.
    with _work_server(tmp_path) as (proc, base):
        assert _wait_for(lambda: _pid(base)) == proc.pid
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=3) == 0


def test_first_interrupt_drains_active_chunk_before_clean_exit(tmp_path):
    with _work_server(tmp_path, grace=3) as (proc, base):
        assert _wait_for(lambda: _pid(base)) == proc.pid
        assert httpx.post(f"{base}/job").status_code == 200
        _wait_for(lambda: _work_started(tmp_path, "job"))
        proc.send_signal(signal.SIGINT)
        time.sleep(0.2)
        assert proc.poll() is None
        (tmp_path / "release-job").touch()
        assert proc.wait(timeout=3) == 0
    text = (tmp_path / "server.log").read_text()
    assert "up to 3s" in text and "press Ctrl+C again" in text
    assert "Service shutdown complete" in text and "Forced shutdown" not in text
    assert "job-end" in (tmp_path / "work-events").read_text()


@pytest.mark.parametrize("kind", ["job", "export"])
@pytest.mark.parametrize("second_interrupt", [False, True])
def test_stuck_work_has_total_deadline_and_immediate_second_interrupt(
    tmp_path, kind, second_interrupt,
):
    from nomusic.pipeline.cache import JobCache
    grace = 30 if second_interrupt else 0.5
    completed = tmp_path / "cache" / "completed" / "chunk_000.opus"
    completed.parent.mkdir(parents=True)
    completed.write_bytes(b"already complete")
    with _work_server(tmp_path, grace=grace) as (proc, base):
        assert _wait_for(lambda: _pid(base)) == proc.pid
        with ThreadPoolExecutor(1) as pool:
            response = None
            if kind == "job":
                assert httpx.post(f"{base}/job").status_code == 200
            else:
                response = pool.submit(httpx.get, f"{base}/export", timeout=4)
            _wait_for(lambda: _work_started(tmp_path, kind))
            started = time.monotonic()
            proc.send_signal(signal.SIGINT)
            if second_interrupt:
                time.sleep(0.1)
                assert proc.poll() is None
                started = time.monotonic()
                proc.send_signal(signal.SIGINT)
            assert proc.wait(timeout=3) == (130 if second_interrupt else 124)
            elapsed = time.monotonic() - started
            assert elapsed < 1 if second_interrupt else 0.45 <= elapsed < 2
            if response is not None:
                with pytest.raises(httpx.HTTPError):
                    response.result(timeout=5)
    text = (tmp_path / "server.log").read_text()
    assert "Forced shutdown" in text
    assert "Service shutdown complete" not in text
    leftovers = list((tmp_path / "cache" / ".scratch").glob(f"*/{kind if kind == 'export' else 'chunk'}.part"))
    assert len(leftovers) == 1
    cache = JobCache(tmp_path / "cache")
    try:
        assert not leftovers[0].exists()
        assert completed.read_bytes() == b"already complete"
    finally:
        cache.close()


def test_first_signal_silences_streams_before_uvicorn_reaches_shutdown():
    from nomusic.config import SETTINGS
    from nomusic.services import Services
    services = Services(SETTINGS, lambda _: None)
    app = SimpleNamespace(state=SimpleNamespace(services=services))
    server = LifecycleServer(uvicorn.Config(app))
    server._application = app
    assert not services.stopping
    server.handle_exit(signal.SIGINT, None)
    assert services.stopping
    assert not services._stop.is_set()  # full, lock-taking shutdown has not run
    assert server.should_exit


def test_shortening_idle_drain_never_extends_original_deadline(monkeypatch):
    import nomusic.serving as serving
    now = [10.0]
    timers = []
    class Timer:
        def __init__(self, seconds, callback):
            self.seconds = seconds
            self.cancelled = False
            timers.append(self)
        def start(self):
            pass
        def cancel(self):
            self.cancelled = True
    monkeypatch.setattr(serving.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(serving.threading, "Timer", Timer)
    server = LifecycleServer(uvicorn.Config(object()))
    server._running = True
    server.grace_seconds = 0.5
    server._start_watchdog()
    now[0] = 10.2
    server._start_watchdog(0.5)
    assert server._shutdown_deadline == 10.5
    assert len(timers) == 1 and not timers[0].cancelled
    server._start_watchdog(0.1)
    assert server._shutdown_deadline == pytest.approx(10.3)
    assert len(timers) == 2 and timers[0].cancelled


def test_closed_stderr_cannot_prevent_interrupt_or_forced_exit(monkeypatch):
    import nomusic.serving as serving
    def closed(*args):
        raise BrokenPipeError("closed test pipe")
    def exited(code):
        raise SystemExit(code)
    monkeypatch.setattr(serving.os, "write", closed)
    monkeypatch.setattr(serving.os, "_exit", exited)
    server = LifecycleServer(uvicorn.Config(object()))
    server.handle_exit(signal.SIGINT, None)
    assert server.should_exit
    with pytest.raises(SystemExit) as second:
        server.handle_exit(signal.SIGINT, None)
    assert second.value.code == 130
    with pytest.raises(SystemExit) as deadline:
        server._force_exit()
    assert deadline.value.code == 124


@pytest.mark.parametrize("mode", ["warmup", "warmup-executor"])
def test_bind_failure_discards_active_preload_and_preserves_failure_status(tmp_path, monkeypatch, mode):
    # Select the real startup race where preload is underway when socket binding
    # fails. Uvicorn calls lifespan.shutdown directly on this path.
    app = _WORK_APP + '''
import asyncio
from contextlib import asynccontextmanager

original_lifespan = app.router.lifespan_context
@asynccontextmanager
async def wait_for_preload(app):
    async with original_lifespan(app):
        while not (ROOT / "work-events").exists():
            await asyncio.sleep(0.01)
        yield
app.router.lifespan_context = wait_for_preload
'''
    monkeypatch.setattr(sys.modules[__name__], "_APP", app)
    monkeypatch.setenv("FIXTURE_WORK", mode)
    monkeypatch.setenv("NOMUSIC_SHUTDOWN_GRACE_SECONDS", "3")
    completed = tmp_path / "cache" / "completed" / "chunk_000.opus"
    completed.parent.mkdir(parents=True)
    completed.write_bytes(b"published audio")
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        with _running_server(tmp_path, port=occupied.getsockname()[1]) as (proc, _):
            _wait_for(lambda: _work_started(tmp_path, "warmup"))
            started = time.monotonic()
            assert proc.wait(timeout=3) == 3
            assert time.monotonic() - started < 2
    text = (tmp_path / "server.log").read_text()
    assert "address already in use" in text.lower()
    assert "Service shutdown complete" in text
    assert "Model preload interrupted" in text
    assert "Forced shutdown" not in text
    assert "warmup-end" not in (tmp_path / "work-events").read_text()
    assert completed.read_bytes() == b"published audio"


@pytest.mark.parametrize("error, expected", [
    (SystemExit(3), 3),
    (SystemExit("startup failed"), 1),
    (RuntimeError("startup failed"), 1),
])
def test_exception_exit_disposes_preload_without_changing_failure_status(monkeypatch, error, expected):
    server = LifecycleServer(uvicorn.Config(object()))
    server._shutdown_services = SimpleNamespace(
        discarded_preload=SimpleNamespace(is_alive=lambda: True),
    )
    cancelled = []
    server._watchdog = SimpleNamespace(cancel=lambda: cancelled.append(True))
    original_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    def failed_run(self, sockets=None):
        raise error

    class ProcessExit(BaseException):
        pass

    def exit_process(code):
        raise ProcessExit(code)

    monkeypatch.setattr(uvicorn.Server, "run", failed_run)
    monkeypatch.setattr("nomusic.serving.os._exit", exit_process)
    with pytest.raises(ProcessExit) as stopped:
        server.run()
    assert stopped.value.args == (expected,)
    assert cancelled == [True]
    assert not server._running
    assert {sig: signal.getsignal(sig) for sig in original_handlers} == original_handlers
