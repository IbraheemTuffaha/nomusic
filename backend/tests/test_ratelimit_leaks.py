"""Rate-limit / SSE-counter bookkeeping must not grow without bound.

Both structures are process-lifetime singletons on the public server, so a leak
per distinct client IP or job id is a slow OOM.
"""

from __future__ import annotations

import time

from ratelimit import SseCounter, _Window


def test_window_evicts_expired_buckets():
    w = _Window(limit=5, window=0.05)
    for i in range(500):
        w.check(f"ip-{i}")
    assert len(w._hits) == 500
    # Let every bucket's hits expire, then drive one more check past the sweep
    # interval so the sweep fires.
    time.sleep(0.06)
    w.check("trigger")
    # Only the freshly-touched bucket survives.
    assert len(w._hits) == 1
    assert "trigger" in w._hits


def test_window_keeps_active_buckets():
    w = _Window(limit=5, window=100.0)
    w.check("a")
    w.check("b")
    # A sweep now (forced via a long-idle third key) must not drop still-active a/b.
    w._last_sweep = time.monotonic() - 1000  # force sweep on next check
    w.check("c")
    assert {"a", "b", "c"} <= set(w._hits)


def test_sse_counter_no_leak_on_reject():
    import config

    saved = {
        k: getattr(config.SETTINGS, k)
        for k in ("max_sse_global", "max_sse_per_job", "max_sse_per_ip")
    }
    object.__setattr__(config.SETTINGS, "max_sse_per_ip", 1)
    object.__setattr__(config.SETTINGS, "max_sse_per_job", 4)
    object.__setattr__(config.SETTINGS, "max_sse_global", 1000)
    try:
        sc = SseCounter()
        ip = "1.2.3.4"
        assert sc.acquire("job-a", ip) is True  # IP now at its per-ip cap (1)
        # Reject 500 fresh job ids for the capped IP; none may leak a bucket.
        for i in range(500):
            assert sc.acquire(f"fresh-{i}", ip) is False
        assert len(sc._per_job) == 1  # only job-a
        assert len(sc._per_ip) == 1
    finally:
        for k, v in saved.items():
            object.__setattr__(config.SETTINGS, k, v)
