"""Private staging cleanup respects live API and inherited child leases."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from nomusic.pipeline.cache import JobCache


def _wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("fixture did not reach expected state")


def test_cache_cleanup_preserves_live_scratch_and_unrecognized_files(tmp_path):
    cache = JobCache(tmp_path / "cache")
    active = cache.scratch.path / "meta.json.part"
    active.write_text("half-written")
    unknown = cache.scratch.root / "someone-elses-files"
    unknown.mkdir()
    (unknown / "keep").write_bytes(b"unowned")
    second = JobCache(cache.root)
    try:
        assert active.exists()
        assert cache.stats()["job_count"] == 0
        second.clear_all()
        assert active.exists()
        assert (unknown / "keep").read_bytes() == b"unowned"
        assert second.sweep_older_than(0.001) == (0, 0)
    finally:
        second.close()
        cache.close()
    assert not active.exists()
    assert unknown.exists()


def test_restart_does_not_reclaim_scratch_while_surviving_ffmpeg_writes(tmp_path):
    import nomusic
    env = {**os.environ, "PYTHONPATH": str(Path(nomusic.__file__).parent.parent)}
    # The parent exits abruptly while FFmpeg is writing a private export. Its
    # inherited lease must survive that exit; PID age/name guesses cannot prove
    # whether these files are still in use.
    owner = r'''
import json, os, pathlib, subprocess, sys
from nomusic.pipeline.cache import JobCache
root = pathlib.Path(sys.argv[1])
cache = JobCache(root / "cache")
output = cache.scratch.path / "export.part"
child = subprocess.Popen([
    "ffmpeg", "-y", "-nostdin", "-loglevel", "error", "-re",
    "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "3",
    "-f", "wav", str(output),
], pass_fds=(cache.scratch.fd,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
(root / "owner.json").write_text(json.dumps({"scratch": str(cache.scratch.path), "child": child.pid}))
os._exit(124)
'''
    completed = tmp_path / "cache" / "complete" / "chunk_000.opus"
    completed.parent.mkdir(parents=True)
    completed.write_bytes(b"published media")
    shared_model = tmp_path / "models" / "checkpoint.incomplete"
    shared_model.parent.mkdir()
    shared_model.write_bytes(b"resume model fetch")
    result = subprocess.run([sys.executable, "-c", owner, str(tmp_path)], env=env, timeout=10)
    assert result.returncode == 124
    info = json.loads((tmp_path / "owner.json").read_text())
    scratch = Path(info["scratch"])
    restarted = JobCache(tmp_path / "cache")
    try:
        assert scratch.exists()  # child still owns its lease
        _wait_for(lambda: (scratch / "export.part").exists())
        assert scratch.exists()
        assert completed.read_bytes() == b"published media"
        assert shared_model.read_bytes() == b"resume model fetch"
        # Reaping is safe both at startup and through explicit cache cleanup.
        # Wait for the actual FFmpeg exit/lease release, then reclaim it.
        def reclaimed():
            restarted.scratch.reap()
            return not scratch.exists()
        _wait_for(reclaimed)
        assert completed.exists()
        assert shared_model.exists()
    finally:
        try:
            os.kill(info["child"], signal.SIGKILL)
        except ProcessLookupError:
            pass
        restarted.close()
