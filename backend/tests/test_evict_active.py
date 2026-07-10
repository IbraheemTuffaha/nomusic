"""evict_to_fit must not rmtree a recently-active dir (possible live worker)."""

from __future__ import annotations

import os
import time

from pipeline.cache import JobCache, _EVICT_MIN_IDLE_SECONDS


def _mkentry(cache: JobCache, name: str, size: int, age_seconds: float):
    d = cache.root / name
    d.mkdir(parents=True, exist_ok=True)
    f = d / "blob"
    f.write_bytes(b"x" * size)
    mtime = time.time() - age_seconds
    os.utime(f, (mtime, mtime))
    return d


def test_evict_skips_recently_active_entries(tmp_path):
    cache = JobCache(tmp_path)
    # One old, evictable entry and one freshly-written (active) entry, both large.
    old = _mkentry(cache, "0000000000000001", 1000, age_seconds=_EVICT_MIN_IDLE_SECONDS + 60)
    active = _mkentry(cache, "0000000000000002", 1000, age_seconds=1)

    # Cap forces eviction of one entry.
    removed, freed = cache.evict_to_fit(max_bytes=1500)

    assert removed == 1
    assert not old.exists()  # idle one evicted
    assert active.exists()  # active one preserved despite being over cap


def test_evict_removes_old_entries_normally(tmp_path):
    cache = JobCache(tmp_path)
    _mkentry(cache, "0000000000000001", 1000, age_seconds=_EVICT_MIN_IDLE_SECONDS + 60)
    _mkentry(cache, "0000000000000002", 1000, age_seconds=_EVICT_MIN_IDLE_SECONDS + 120)
    removed, freed = cache.evict_to_fit(max_bytes=1500)
    assert removed == 1
    assert freed == 1000
