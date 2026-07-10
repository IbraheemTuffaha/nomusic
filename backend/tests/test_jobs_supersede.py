"""Worker-identity / supersede races in JobRegistry.

Removing the whole-pipeline `_gpu_lock` (F2) let a superseded/abandoned worker
and its fresh successor overlap. These tests pin the invariants that keep that
overlap from corrupting the successor:

* a retiring worker that no longer owns ``_threads[key]`` must retract nothing
  (esp. not the successor's ``_controls`` entry);
* ``_on_probed`` must rebuild its own control rather than adopt a predecessor's
  stale one;
* admission caps must not count a superseded predecessor's own charge against a
  client resuming that same job.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

import config
from jobs import JobRegistry, JobState, JobStatus, WorkerAbandoned, _JobControl
from pipeline.cache import CacheMeta


class _FakeCache:
    def __init__(self):
        self.root = "/tmp"

    def key(self, url, model, keep_stems, **kw):
        return "0123456789abcdef"

    def load_meta(self, key):
        return None

    def touch(self, key):
        pass


def _registry(run=None):
    processor = SimpleNamespace(
        chunk_seconds=10.0, chunk_overlap_seconds=0.5, run=run or (lambda *a, **k: None)
    )
    return JobRegistry(processor=processor, cache=_FakeCache())


def _status(key):
    return JobStatus(job_id=key, cache_key=key, state=JobState.PROCESSING)


def test_retire_worker_noop_when_superseded():
    """A retiring predecessor must not touch a successor's shared state."""
    reg = _registry()
    key = "0123456789abcdef"
    t1 = threading.Thread(target=lambda: None)
    t2 = threading.Thread(target=lambda: None)
    successor_status = _status(key)
    successor_control = _JobControl(total_chunks=3, done=set())

    # Successor T2 owns everything now.
    reg._threads[key] = t2
    reg._jobs[key] = successor_status
    reg._controls[key] = successor_control
    reg._inflight.add(key)
    reg._ip_counts["ip"] = 1
    reg._ip_by_key[key] = "ip"

    # Predecessor T1 retires (abandoned). It is NOT the registered thread.
    with reg._lock:
        reg._retire_worker(key, my_thread=t1, my_status=_status(key), abandoned=True)

    # Nothing the successor owns was disturbed.
    assert reg._threads[key] is t2
    assert reg._jobs[key] is successor_status
    assert reg._controls[key] is successor_control
    assert reg._inflight == {key}
    assert reg._ip_counts["ip"] == 1


def test_retire_worker_cleans_up_when_owner():
    reg = _registry()
    key = "0123456789abcdef"
    t1 = threading.Thread(target=lambda: None)
    st = _status(key)
    reg._threads[key] = t1
    reg._jobs[key] = st
    reg._controls[key] = _JobControl(total_chunks=1, done=set())
    reg._inflight.add(key)
    reg._ip_counts["ip"] = 1
    reg._ip_by_key[key] = "ip"

    with reg._lock:
        reg._retire_worker(key, my_thread=t1, my_status=st, abandoned=True)

    assert key not in reg._threads
    assert key not in reg._controls
    assert key not in reg._inflight
    assert "ip" not in reg._ip_counts
    assert key not in reg._jobs  # abandoned + still our status -> dropped


def test_on_probed_rebuilds_control_over_predecessor_stale_one():
    """_on_probed must overwrite a predecessor's stale control (which is missing
    the chunk the predecessor popped-but-never-finished), not adopt it."""
    reg = _registry()
    key = "0123456789abcdef"
    reg._jobs[key] = _status(key)

    # Predecessor left a control whose chunk 0 was popped (not in the deque) but
    # never finished — i.e. it is NOT on disk, so a correct rebuild must re-queue
    # it.
    stale = _JobControl(total_chunks=3, done={0})  # deque = [1, 2]
    reg._controls[key] = stale

    meta = CacheMeta(
        url="u", model="m", keep_stems=["vocals"], duration_seconds=30.0,
        chunk_seconds=10.0, chunk_overlap_seconds=0.5, total_chunks=3,
        chunks_ready=[],  # nothing actually on disk
    )
    info = SimpleNamespace(title="t", duration_seconds=30.0)

    reg._on_probed(key, info, plans=[], meta=meta)

    # A brand-new control seeded from chunks_ready=[] -> all three chunks pending.
    control = reg._controls[key]
    assert control is not stale
    assert sorted(control.pending) == [0, 1, 2]


def _set_settings(**overrides):
    """Flip frozen SETTINGS fields; returns a restore() to undo them."""
    saved = {k: getattr(config.SETTINGS, k) for k in overrides}
    for k, v in overrides.items():
        object.__setattr__(config.SETTINGS, k, v)
    return lambda: [object.__setattr__(config.SETTINGS, k, v) for k, v in saved.items()]


def test_cache_clear_abandon_survives_supersede():
    """A worker flagged by abandon_all must still abandon even after a submit()
    supersedes its key and clears the per-key abandon flag (the zombie-worker
    bug): the thread-identity flag can't be cleared by the resubmit."""
    reg = _registry()
    key = "0123456789abcdef"
    t1 = threading.Thread(target=lambda: None)  # the "running" worker
    reg._threads[key] = t1
    reg._jobs[key] = _status(key)

    # Cache clear flags the running worker (both per-key and per-thread).
    reg.abandon_all()
    assert t1 in reg._abandon_threads

    # A racing resubmit supersedes the key and clears the per-key abandon flag.
    reg._abandoning.discard(key)
    assert key not in reg._abandoning

    # The old worker's next abort_check must STILL raise (thread flag intact),
    # so it can't run on as a zombie and stomp the fresh job's status.
    with pytest.raises(WorkerAbandoned):
        reg._raise_if_abandoned(key, idle_timeout=0, my_thread=t1)

    # A different (fresh) worker thread for the same key is NOT abandoned.
    t2 = threading.Thread(target=lambda: None)
    reg._raise_if_abandoned(key, idle_timeout=0, my_thread=t2)  # must not raise


def test_retire_worker_drops_abandon_thread_even_when_superseded():
    reg = _registry()
    key = "0123456789abcdef"
    t1 = threading.Thread(target=lambda: None)
    t2 = threading.Thread(target=lambda: None)
    reg._abandon_threads.add(t1)
    reg._threads[key] = t2  # t1 was superseded by t2

    with reg._lock:
        reg._retire_worker(key, my_thread=t1, my_status=None, abandoned=True)

    # t1 removed from the abandon set despite not owning the key slot (no leak).
    assert t1 not in reg._abandon_threads
    assert reg._threads[key] is t2  # successor untouched


def test_supersede_does_not_spurious_429_for_same_client(monkeypatch):
    """A client resuming its own abandoned job at the per-IP cap is admitted (the
    supersede keeps the net count unchanged), not 429'd."""
    import shutil as _shutil

    restore = _set_settings(public=True, max_jobs_per_ip=1, max_inflight_jobs=3)
    monkeypatch.setattr(
        _shutil, "disk_usage", lambda p: SimpleNamespace(free=10**15)
    )

    # Park the spawned worker in run() so the admission counts are observed
    # deterministically (before its finally retires the slot).
    release = threading.Event()
    reg = _registry(run=lambda *a, **k: release.wait(timeout=5))
    key = "0123456789abcdef"

    # Simulate the client already holding one (now abandoning) job for this key.
    reg._inflight.add(key)
    reg._ip_counts["1.2.3.4"] = 1
    reg._ip_by_key[key] = "1.2.3.4"
    reg._abandoning.add(key)
    # No live JobStatus (abandon dropped it), so submit() takes the spawn path.

    try:
        # Re-submit from the same client for the same URL -> supersede, not reject.
        status = reg.submit(
            "https://x", model="m", keep_stems=["vocals"], client_ip="1.2.3.4"
        )
        assert status.state in (JobState.QUEUED, JobState.PROBING)
        # Net per-IP count stays at 1 (predecessor retracted, successor charged).
        assert reg._ip_counts["1.2.3.4"] == 1
    finally:
        release.set()
        reg.abandon_all()
        restore()
