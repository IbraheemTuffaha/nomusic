"""Browser ownership survives its Node helper and never targets another session."""

import json
import os
from pathlib import Path
import runpy
import shlex
import signal
import subprocess
import sys
import time

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/verify.py"
VERIFY = runpy.run_path(str(SCRIPT))


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Owned process did not reach the expected state")


def alive(pid):
    result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                            capture_output=True, text=True, timeout=5)
    return result.returncode == 0 and not result.stdout.strip().startswith("Z")


def spawn(*args):
    return subprocess.Popen([sys.executable, "-c", *args], start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_abrupt_node_exit_cleans_browser_and_preserves_unrelated(tmp_path):
    launcher, ownership = VERIFY["browser_launcher"](tmp_path)
    profile = tmp_path / "browser/profile"
    browser = tmp_path / "browser-process"
    # Retain the profile argument in the actual process command, as Chromium
    # does. Sleeping despite closed pipes makes leaked cleanup observable.
    code = ("import os,time; from pathlib import Path; "
            "Path(os.environ['TEST_BROWSER_RUNNING']).touch(); time.sleep(60)")
    browser.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(code)} "$@"\n')
    browser.chmod(0o700)
    ready = tmp_path / "ready"
    running = tmp_path / "browser-running"
    helper = subprocess.Popen([
        "node", "--input-type=module", "-e",
        "import {spawn} from 'node:child_process'; import fs from 'node:fs';"
        "const child=spawn(process.env.NOMUSIC_VERIFY_BROWSER_LAUNCHER,"
        " ['--user-data-dir='+process.env.TEST_PROFILE], {detached:true,stdio:'ignore',"
        " env:{...process.env,NOMUSIC_VERIFY_BROWSER_HELPER:String(process.pid)}});"
        "while(!fs.existsSync(process.env.NOMUSIC_VERIFY_BROWSER_OWNERSHIP+'/'+child.pid+'.json'))"
        " await new Promise(r=>setTimeout(r,10));"
        "while(!fs.existsSync(process.env.TEST_BROWSER_RUNNING)) await new Promise(r=>setTimeout(r,10));"
        "fs.writeFileSync(process.env.TEST_READY,String(child.pid));"
        "process.kill(process.pid,'SIGKILL');",
    ], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
       env={**os.environ, "NOMUSIC_VERIFY_BROWSER_LAUNCHER": str(launcher),
            "NOMUSIC_VERIFY_BROWSER_OWNERSHIP": str(ownership),
            "NOMUSIC_VERIFY_CHROMIUM": str(browser), "TEST_PROFILE": str(profile),
            "TEST_READY": str(ready), "TEST_BROWSER_RUNNING": str(running)})
    unrelated = spawn("import time; time.sleep(60)")
    browser_pid = None
    try:
        wait_for(ready.exists)
        browser_pid = int(ready.read_text())
        assert helper.wait(timeout=5) == -signal.SIGKILL
        assert alive(browser_pid)
        # A bogus handoff for another session must not grant kill authority.
        (ownership / "unrelated.json").write_text(json.dumps(
            {"helper_pid": helper.pid, "pid": unrelated.pid, "profile": str(profile)}))
        assert VERIFY["stop_owned"](helper, grace=0.1, browser_ownership=ownership)
        wait_for(lambda: not alive(browser_pid))
        assert alive(unrelated.pid)
    finally:
        VERIFY["stop_owned"](helper, grace=0.1, browser_ownership=ownership)
        VERIFY["stop_owned"](unrelated, grace=0.1)
        if browser_pid is not None:
            try:
                os.killpg(browser_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_helper_death_before_handoff_never_starts_browser(tmp_path):
    launcher, ownership = VERIFY["browser_launcher"](tmp_path)
    release = tmp_path / "release-launch"
    ready = tmp_path / "ready"
    executed = tmp_path / "browser-executed"
    # Hold the launcher before Python registration, then kill Node and let the
    # runner clean up before publishing a record. This is the former leak gap.
    launcher.write_text(launcher.read_text().replace("exec ",
        f"while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.01; done\nexec ", 1))
    browser = tmp_path / "browser-process"
    browser.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(executed))}\nsleep 60\n")
    browser.chmod(0o700)
    helper = subprocess.Popen([
        "node", "--input-type=module", "-e",
        "import {spawn} from 'node:child_process'; import fs from 'node:fs';"
        "const child=spawn(process.env.NOMUSIC_VERIFY_BROWSER_LAUNCHER,"
        " ['--user-data-dir='+process.env.TEST_PROFILE], {detached:true,stdio:'ignore',"
        " env:{...process.env,NOMUSIC_VERIFY_BROWSER_HELPER:String(process.pid)}});"
        "fs.writeFileSync(process.env.TEST_READY,String(child.pid));"
        "process.kill(process.pid,'SIGKILL');",
    ], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
       env={**os.environ, "NOMUSIC_VERIFY_BROWSER_LAUNCHER": str(launcher),
            "NOMUSIC_VERIFY_BROWSER_OWNERSHIP": str(ownership),
            "NOMUSIC_VERIFY_CHROMIUM": str(browser),
            "TEST_PROFILE": str(tmp_path / "browser/profile"), "TEST_READY": str(ready)})
    try:
        wait_for(ready.exists)
        pid = int(ready.read_text())
        assert helper.wait(timeout=5) == -signal.SIGKILL
        assert not list(ownership.glob("*.json"))
        VERIFY["stop_owned"](helper, grace=0.1, browser_ownership=ownership)
        release.touch()
        wait_for(lambda: not alive(pid))
        assert not executed.exists()
    finally:
        release.touch()
        VERIFY["stop_owned"](helper, grace=0.1, browser_ownership=ownership)
        if ready.exists():
            try:
                os.killpg(int(ready.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("ignore_term", [False, True], ids=["normal", "timeout"])
def test_normal_cleanup_and_forced_timeout(ignore_term):
    started_read, started_write = os.pipe()
    code = ("import os,signal,time; " +
            ("signal.signal(signal.SIGTERM,signal.SIG_IGN); " if ignore_term else "") +
            f"os.write({started_write},b'ready'); time.sleep(60)")
    process = subprocess.Popen([sys.executable, "-c", code], start_new_session=True,
                               pass_fds=(started_write,))
    os.close(started_write)
    try:
        assert os.read(started_read, 5) == b"ready"
        assert VERIFY["stop_owned"](process, grace=0.1) is (not ignore_term)
        assert process.returncode == (-signal.SIGKILL if ignore_term else -signal.SIGTERM)
    finally:
        os.close(started_read)
        VERIFY["stop_owned"](process, grace=0.1)
