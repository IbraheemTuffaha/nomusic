#!/usr/bin/env python3
"""Run the installed-package suites and controlled CPU/extension smoke.

Use the development Python environment described in docs/verification.md.
Results stay in ignored mds/verification/. No existing server is reused.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET


REPO = Path(__file__).resolve().parents[1]
E2E = REPO / "tests/e2e"


def announce(message: str) -> None:
    print(message, flush=True)


def browser_groups(process: subprocess.Popen, ownership: Path) -> set[int]:
    """Read launch-time handoffs, never rediscover browsers by name/ancestry."""
    groups = set()
    profile = str(ownership.parent / "browser/profile")
    for manifest in ownership.glob("*.json"):
        record = json.loads(manifest.read_text())
        pid = record.get("pid")
        if (record.get("helper_pid") != process.pid or record.get("profile") != profile
                or not isinstance(pid, int) or pid <= 1):
            continue
        try:
            if os.getpgid(pid) != pid:
                continue
            # A reused PID must not turn an old handoff into permission to kill
            # another process. The live leader must still use our fresh profile.
            command = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "args="],
                                     capture_output=True, text=True, timeout=5)
            if command.returncode == 0 and f"--user-data-dir={profile}" not in command.stdout:
                continue
        except ProcessLookupError:
            pass  # The browser leader exited; its session may retain children.
        groups.add(pid)
    return groups


def stop_owned(process: subprocess.Popen, grace: float = 10,
               browser_ownership: Path | None = None) -> bool:
    """Reap our session and explicitly handed-off browser sessions.

    Returns False if the leader needed SIGKILL. All callers create a new session;
    an unknown service or a process found by name is never a cleanup target.
    """
    graceful = True
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        graceful = False
    # Finish stopping the helper before taking its launch handoffs. Otherwise
    # a timed-out helper can start a detached browser just after the scan.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=10)
    if browser_ownership is not None:
        for group in browser_groups(process, browser_ownership):
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
    return graceful


def browser_launcher(run: Path) -> tuple[Path, Path]:
    """Create a private executable and handoff directory for this one helper."""
    ownership = run / "browser-ownership"
    ownership.mkdir(mode=0o700)
    launcher = ownership / "launch"
    source = E2E / "browser-launcher.py"
    launcher.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " "
                        + shlex.quote(str(source)) + ' "$@"\n')
    launcher.chmod(0o700)
    return launcher, ownership


def run_step(name: str, command: list[str], run: Path, env: dict[str, str],
             timeout: float = 600, *, owns_browser: bool = False) -> None:
    announce(f"Running {name}...")
    log = run / f"{name}.log"
    ownership = None
    if owns_browser:
        launcher, ownership = browser_launcher(run)
        env = {**env, "NOMUSIC_VERIFY_BROWSER_LAUNCHER": str(launcher),
               "NOMUSIC_VERIFY_BROWSER_OWNERSHIP": str(ownership)}
    with log.open("w") as output:
        process = subprocess.Popen(command, cwd=REPO, env=env, stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
            if code:
                raise RuntimeError(f"{name} exited {code}; see {log}")
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"{name} exceeded {timeout}s; see {log}") from error
        finally:
            stop_owned(process, browser_ownership=ownership)
    announce(f"Passed {name}")


def preflight() -> dict:
    for binary in ("node", "npm", "ffmpeg", "ffprobe"):
        if not shutil.which(binary):
            raise RuntimeError(f"Missing {binary}; see docs/verification.md")
    import nomusic
    from nomusic.runtime import check_runtime

    installed = Path(nomusic.__file__).resolve().parent
    distribution_root = Path(importlib.metadata.distribution("nomusic").locate_file("nomusic")).resolve()
    source = REPO / "backend/nomusic"
    if installed == source or installed != distribution_root:
        raise RuntimeError("Verification requires a non-editable installation. "
                           "Re-run ./install.sh --profile cpu --dev --skip-system-packages.")
    source_files = {p.relative_to(source) for p in source.rglob("*.py")}
    installed_files = {p.relative_to(installed) for p in installed.rglob("*.py")}
    if source_files != installed_files or any(
        (source / p).read_bytes() != (installed / p).read_bytes() for p in source_files
    ):
        raise RuntimeError("Installed nomusic differs from this checkout. "
                           "Re-run ./install.sh --profile cpu --dev --skip-system-packages.")
    encoders = subprocess.check_output(["ffmpeg", "-hide_banner", "-encoders"],
                                      text=True, stderr=subprocess.STDOUT, timeout=10)
    available = {fields[1] for line in encoders.splitlines()
                 if len(fields := line.split()) >= 2}
    missing = {"libopus", "libmp3lame", "aac", "libx264", "libvpx-vp9"} - available
    if missing:
        raise RuntimeError(f"FFmpeg lacks required test encoders: {', '.join(sorted(missing))}")
    return {
        "python": platform.python_version(), "platform": platform.platform(),
        "installed_package": str(installed),
        "packages": {name: importlib.metadata.version(name) for name in
                     ("nomusic", "pytest", "torch", "demucs", "numpy", "soundfile")},
        "node": subprocess.check_output(["node", "--version"], text=True, timeout=10).strip(),
        "runtime": check_runtime(),
        "lock_sha256": hashlib.sha256((REPO / "uv.lock").read_bytes()).hexdigest(),
    }


def unit_suites(run: Path, env: dict[str, str], timeout: float) -> dict:
    xml = run / "pytest.xml"
    run_step("backend-tests", [sys.executable, "-m", "pytest", "backend/tests", "-q", "-rs",
                              "--import-mode=importlib",
                              f"--junitxml={xml}"], run, env, timeout)
    cases = ET.parse(xml).findall(".//testcase")
    skips = []
    for case in cases:
        skip = case.find("skipped")
        if skip is None:
            continue
        reason = skip.get("message", "")
        skips.append({"test": case.get("name"), "reason": reason})
        if case.get("name") != "test_infer_batch_matches_single_mlx" or reason != "no GPU (MPS/CUDA) available":
            raise RuntimeError(f"Unexpected backend skip: {case.get('name')}: {reason}")
    run_step("extension-tests", ["npm", "--prefix", "extension", "test"], run, env, timeout)
    return {"backend_cases": len(cases), "skips": skips}


def wait_ready(process: subprocess.Popen, base: str, timeout: float) -> dict:
    # Bypass ambient proxy variables for our loopback-only test server.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Test backend exited before readiness; see backend.log")
        try:
            with opener.open(base + "/readyz", timeout=2) as response:
                ready = json.load(response)
            with opener.open(base + "/capabilities", timeout=2) as response:
                capabilities = json.load(response)
            if ready.get("state") == "ready":
                if capabilities["engine"]["device"].split()[0] != "cpu":
                    raise RuntimeError("Smoke backend did not select CPU")
                return capabilities
        except urllib.error.HTTPError as error:
            if error.code != 503:
                raise
            status = json.load(error)
            if status.get("state") == "failed":
                raise RuntimeError("Backend startup failed; see backend.log. "
                                   "Fetch the pinned model before the offline smoke.") from error
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(0.2)
    raise RuntimeError("Backend readiness timed out; see backend.log")


def smoke(run: Path, env: dict[str, str], port: int, timeout: float, *, playback: bool = False) -> dict:
    if not (E2E / "node_modules/playwright/package.json").is_file():
        raise RuntimeError("Run npm ci --prefix tests/e2e, then install its Chromium; "
                           "see docs/verification.md")
    with socket.socket() as check:
        check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            check.bind(("127.0.0.1", port))
        except OSError as error:
            raise RuntimeError(f"Port {port} is occupied; stop its owner or choose --port. "
                               "The runner will not reuse or stop an existing backend.") from error
    fixture = run / "fixture"
    fixture_command = [sys.executable, str(E2E / "fixture.py"), "--output", str(fixture)]
    if playback:
        fixture_command += ["--duration", "180"]
    run_step("fixture", fixture_command, run, env, 60)
    events = run / "backend-events.jsonl"
    base = f"http://127.0.0.1:{port}"
    with (run / "backend.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, str(E2E / "backend.py"), "--fixture", str(fixture),
             "--cache-dir", str(run / "media"), "--events", str(events), "--port", str(port)],
            cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        try:
            capabilities = wait_ready(process, base, min(timeout, 180))
            announce("Test backend and CPU model ready")
            browser_command = ["node", str(E2E / "browser.mjs"), "--backend", base,
                               "--fixture", str(fixture), "--output", str(run / "browser"),
                               "--timeout-seconds", str(timeout)]
            if playback:
                browser_command += ["--playback", "true"]
            run_step("browser", browser_command, run, env, timeout + 30, owns_browser=True)
        finally:
            announce("Stopping test-owned backend...")
            graceful = stop_owned(process, grace=120)
    if not graceful or process.returncode not in (0, -signal.SIGTERM):
        raise RuntimeError("Backend did not shut down normally; see backend.log")
    lifecycle = [json.loads(line) for line in events.read_text().splitlines()]
    stopped = [event for event in lifecycle if event.get("event") == "backend_stopped"]
    if len(stopped) != 1 or stopped[0].get("remaining_owned_threads") != []:
        raise RuntimeError("Backend did not confirm drained owned threads; see backend-events.jsonl")
    report = json.loads((run / "browser/report.json").read_text())
    if report.get("passed") is not True:
        raise RuntimeError("Browser did not confirm success; see browser/report.json")
    return {"device": capabilities["engine"]["device"], "backend_drained": True,
            "browser_report": "browser/report.json"}


def verification_env(run: Path, scratch: Path) -> dict[str, str]:
    """Use the reference settings, independent of local pytest selection/plugins."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("NOMUSIC_", "PYTEST_"))
           and key not in ("PYTHONPATH", "PYTHONHOME")}
    env.update({"NOMUSIC_DEVICE": "cpu", "NOMUSIC_CACHE_DIR": str(run / "unit-media"),
                "NOMUSIC_CACHE_TTL_DAYS": "0", "NOMUSIC_CACHE_SWEEP_INTERVAL_SECONDS": "0",
                "NOMUSIC_MEMORY_GC_INTERVAL_SECONDS": "0", "NOMUSIC_IDLE_TIMEOUT_SECONDS": "0",
                "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
                "OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "2", "TMPDIR": str(scratch),
                "PYTHONDONTWRITEBYTECODE": "1"})
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("all", "unit", "smoke", "playback"), default="all")
    parser.add_argument("--port", type=int, default=8723)
    parser.add_argument("--timeout-seconds", type=int, default=600, help="Per-suite deadline (default: 600)")
    args = parser.parse_args()
    if os.name != "posix":
        parser.error("This runner currently supports the POSIX installation profiles only")
    if not 1 <= args.port <= 65535 or not 30 <= args.timeout_seconds <= 1800:
        parser.error("Port must be 1..65535; timeout must be 30..1800 seconds")
    base = REPO / "mds/verification"
    base.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=base))
    # Chromium's Unix socket paths have a small length limit. This short,
    # test-owned directory stays within ignored local state and is removed.
    scratch = Path(tempfile.mkdtemp(prefix="v-", dir=REPO / "mds"))
    env = verification_env(run, scratch)
    # Preflight uses the same isolated settings as subprocesses.
    os.environ.clear()
    os.environ.update(env)
    report = {"passed": False, "suite": args.suite}
    started = time.monotonic()
    def interrupted(signum, _frame):
        raise KeyboardInterrupt(f"Received signal {signum}")
    signal.signal(signal.SIGTERM, interrupted)
    announce(f"Local evidence: {run}")
    try:
        report["environment"] = preflight()
        # Enforce this again inside pytest, whose collection can modify sys.path.
        env["NOMUSIC_TEST_INSTALLED_ROOT"] = report["environment"]["installed_package"]
        if args.suite in ("all", "unit"):
            report["unit"] = unit_suites(run, env, args.timeout_seconds)
        if args.suite in ("all", "smoke", "playback"):
            playback = args.suite == "playback"
            report["playback" if playback else "smoke"] = smoke(
                run, env, args.port, args.timeout_seconds, playback=playback)
        report["passed"] = True
    except (Exception, KeyboardInterrupt) as error:
        report["error"] = str(error)
        announce(f"Verification failed: {error}")
        if os.environ.get("GITHUB_ACTIONS") == "true":
            # CI contains generated media and disposable paths only. Preserve
            # useful failure text without uploading profiles/traces or emitting
            # raw logs from a developer's local machine.
            for log in sorted(run.glob("*.log")):
                announce(f"CI diagnostic tail: {log.name}")
                with log.open("rb") as handle:
                    handle.seek(max(0, log.stat().st_size - 6000))
                    for line in handle.read().decode(errors="replace").splitlines():
                        announce("  | " + line)
    finally:
        shutil.rmtree(scratch)
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        (run / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    announce(f"{'PASS' if report['passed'] else 'FAIL'}: {run / 'summary.json'}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
