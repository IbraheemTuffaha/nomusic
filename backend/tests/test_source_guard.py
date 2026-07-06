"""_source_guard concurrency: a waiter must stay abortable and clean up refs.

A job queued behind a long-running same-URL job blocks on the per-url_key lock.
The guard polls ``abort_check`` while blocked so the waiter can idle-abandon /
hit its deadline instead of pinning its admission slot forever.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from pipeline.processor import Processor


class _Abort(Exception):
    pass


def _processor():
    cache = SimpleNamespace(url_key=lambda url: "urlkey00deadbeef")
    return Processor(
        engine=SimpleNamespace(),
        cache=cache,
        chunk_seconds=10.0,
        chunk_overlap_seconds=0.5,
    )


def test_waiter_aborts_instead_of_blocking_forever():
    proc = _processor()
    url = "https://example/vid"

    started = threading.Event()
    release = threading.Event()

    def _holder():
        with proc._source_guard(url):
            started.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=_holder)
    holder.start()
    assert started.wait(timeout=2)

    # Second same-URL job: abort_check raises on the 2nd poll while it waits.
    calls = {"n": 0}

    def abort_check():
        calls["n"] += 1
        if calls["n"] >= 2:
            raise _Abort()

    with pytest.raises(_Abort):
        with proc._source_guard(url, abort_check):
            pytest.fail("waiter should never have acquired the held lock")

    # The aborted waiter must not leave a dangling ref/lock entry for its key.
    # (Only the still-holding thread's ref remains.)
    assert proc._source_refs.get("urlkey00deadbeef") == 1

    release.set()
    holder.join(timeout=5)
    # Holder released and dropped the last ref -> maps fully cleaned.
    assert "urlkey00deadbeef" not in proc._source_refs
    assert "urlkey00deadbeef" not in proc._source_locks


def test_single_holder_cleans_up():
    proc = _processor()
    with proc._source_guard("https://example/vid") as k:
        assert k == "urlkey00deadbeef"
        assert proc._source_refs["urlkey00deadbeef"] == 1
    assert "urlkey00deadbeef" not in proc._source_refs
    assert "urlkey00deadbeef" not in proc._source_locks
