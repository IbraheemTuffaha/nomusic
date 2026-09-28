"""Shared test setup.

Point the backend at a throwaway cache and disable the background daemon
threads (TTL sweep, memory GC, idle-abandon) *before* ``config`` is imported,
so the HTTP-layer tests never touch the real ``~/.cache/nomusic`` or spawn
sweepers. ``SETTINGS`` is a frozen dataclass built at import time, so these
env vars must be set here in conftest (loaded before any test module).
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile

import pytest

os.environ.setdefault(
    "NOMUSIC_CACHE_DIR", tempfile.mkdtemp(prefix="nomusic-test-cache-")
)
os.environ.setdefault("NOMUSIC_CACHE_TTL_DAYS", "0")
os.environ.setdefault("NOMUSIC_CACHE_SWEEP_INTERVAL_SECONDS", "0")
os.environ.setdefault("NOMUSIC_MEMORY_GC_INTERVAL_SECONDS", "0")
os.environ.setdefault("NOMUSIC_IDLE_TIMEOUT_SECONDS", "0")


def _check_installed_imports() -> None:
    """Check provenance inside pytest, after its import-path changes.

    Ordinary development runs may use an editable checkout. The verification
    runner supplies the non-editable package directory it already compared
    with the source, so pytest cannot silently test a different copy.
    """
    expected = os.environ.get("NOMUSIC_TEST_INSTALLED_ROOT")
    if not expected:
        return
    import nomusic

    root = Path(expected).resolve()
    if Path(nomusic.__file__).resolve().parent != root:
        raise pytest.UsageError(
            f"Installed-package provenance failed: nomusic loaded from {nomusic.__file__}; "
            f"expected {root}. Use --import-mode=importlib and remove source path overrides."
        )
    for name, module in tuple(sys.modules.items()):
        if name != "nomusic" and not name.startswith("nomusic."):
            continue
        paths = list(getattr(module, "__path__", ()))
        if path := getattr(module, "__file__", None):
            paths.append(path)
        if any(not Path(path).resolve().is_relative_to(root) for path in paths):
            raise pytest.UsageError(
                f"Installed-package provenance failed: {name} loaded outside {root}: {paths}"
            )


def pytest_configure():
    _check_installed_imports()


def pytest_collection_finish():
    _check_installed_imports()


def pytest_sessionfinish():
    # Also cover modules imported lazily by a test, after collection.
    _check_installed_imports()
