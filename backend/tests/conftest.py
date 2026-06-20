"""Shared test setup.

Point the backend at a throwaway cache and disable the background daemon
threads (TTL sweep, memory GC, idle-abandon) *before* ``config`` is imported,
so the HTTP-layer tests never touch the real ``~/.cache/nomusic`` or spawn
sweepers. ``SETTINGS`` is a frozen dataclass built at import time, so these
env vars must be set here in conftest (loaded before any test module).
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault(
    "NOMUSIC_CACHE_DIR", tempfile.mkdtemp(prefix="nomusic-test-cache-")
)
os.environ.setdefault("NOMUSIC_CACHE_TTL_DAYS", "0")
os.environ.setdefault("NOMUSIC_CACHE_SWEEP_INTERVAL_SECONDS", "0")
os.environ.setdefault("NOMUSIC_MEMORY_GC_INTERVAL_SECONDS", "0")
os.environ.setdefault("NOMUSIC_IDLE_TIMEOUT_SECONDS", "0")
