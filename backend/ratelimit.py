"""Per-IP request-rate limits, bounded-concurrency gates, and an SSE counter.

Hand-rolled rather than slowapi: we need concurrency gates and per-IP job caps
that slowapi doesn't provide, and the single-process server makes shared
in-memory state the correct and simplest model. Every control is a no-op when
``NOMUSIC_PUBLIC`` is unset.

Identity is the real client IP (``CF-Connecting-IP``); see :mod:`security`.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager

from fastapi import HTTPException, Request

from config import SETTINGS
from security import client_ip


class _Window:
    """Sliding-window request counter keyed by client IP."""

    def __init__(self, limit: int, window: float = 60.0) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, int]:
        """Record a hit for ``key``; return ``(allowed, retry_after_seconds)``."""
        now = time.monotonic()
        with self._lock:
            dq = self._hits[key]
            while dq and dq[0] <= now - self.window:
                dq.popleft()
            if len(dq) >= self.limit:
                return False, max(1, int(self.window - (now - dq[0])))
            dq.append(now)
            return True, 0


def rate_limit(window: _Window):
    """FastAPI dependency factory enforcing ``window`` per client IP."""

    def dep(request: Request) -> None:
        if not SETTINGS.public:
            return
        ok, retry = window.check(client_ip(request))
        if not ok:
            raise HTTPException(
                status_code=429,
                detail="rate limited",
                headers={"Retry-After": str(retry)},
            )

    return dep


class Gate:
    """Non-blocking bounded-concurrency gate. Used as ``with gate.slot():``.
    Raises 503 when full (public mode); a no-op in dev so local behavior is
    unchanged. The acquired flag is a per-call local, so concurrent ``slot()``
    users at limit>1 each release exactly their own permit."""

    def __init__(self, limit: int) -> None:
        self._sem = threading.BoundedSemaphore(max(1, limit))

    @contextmanager
    def slot(self):
        acquired = False
        if SETTINGS.public:
            if not self._sem.acquire(blocking=False):
                raise HTTPException(
                    status_code=503, detail="server busy", headers={"Retry-After": "5"}
                )
            acquired = True
        try:
            yield
        finally:
            if acquired:
                self._sem.release()


class SseCounter:
    """Tracks open /events streams per job, per IP, and globally so a single
    client (or job) can't exhaust the event-loop with held connections."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._per_job: dict[str, int] = defaultdict(int)
        self._per_ip: dict[str, int] = defaultdict(int)
        self._global = 0

    def acquire(self, job: str, ip: str) -> bool:
        with self._lock:
            if self._global >= SETTINGS.max_sse_global:
                return False
            if self._per_job[job] >= SETTINGS.max_sse_per_job:
                return False
            if self._per_ip[ip] >= SETTINGS.max_sse_per_ip:
                return False
            self._per_job[job] += 1
            self._per_ip[ip] += 1
            self._global += 1
            return True

    def release(self, job: str, ip: str) -> None:
        with self._lock:
            self._global = max(0, self._global - 1)
            if self._per_job.get(job):
                self._per_job[job] -= 1
                if self._per_job[job] <= 0:
                    del self._per_job[job]
            if self._per_ip.get(ip):
                self._per_ip[ip] -= 1
                if self._per_ip[ip] <= 0:
                    del self._per_ip[ip]


# Module-level singletons (single-process server). Limits are read from SETTINGS
# at import; the gating itself is still toggled by SETTINGS.public at call time.
process_rl = _Window(SETTINGS.rate_process_per_min)
video_rl = _Window(SETTINGS.rate_video_per_min)
default_rl = _Window(SETTINGS.rate_default_per_min)
video_export_gate = Gate(SETTINGS.max_video_exports)
audio_transcode_gate = Gate(SETTINGS.max_audio_transcodes)
sse_counter = SseCounter()
