"""Private staging files that can be reclaimed after an interrupted process.

Only directories created here are swept. The owner holds an advisory POSIX
lease and passes that same descriptor to every FFmpeg writer. A surviving child
therefore protects its files until it exits, even if the API was force-killed.
Model caches and resumable yt-dlp media downloads are outside this directory.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path
import re
import shutil
import uuid
import weakref


class ScratchWorkspace:
    def __init__(self, cache_root: Path):
        self.root = cache_root / ".scratch"
        self.root.mkdir(parents=True, exist_ok=True)
        with self._guard():
            self._reap()
            self.path = self.root / uuid.uuid4().hex
            self.path.mkdir(mode=0o700)
            fd = os.open(self.path / "lease", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
        self.fd = fd
        self._release = weakref.finalize(self, os.close, fd)

    @contextlib.contextmanager
    def _guard(self):
        # Serialize directory creation and discovery; a reaper must not see a
        # freshly created owner before its lease has been locked.
        fd = os.open(self.root / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _reap(self):
        for directory in self.root.iterdir():
            if (not re.fullmatch(r"[0-9a-f]{32}", directory.name)
                    or directory.is_symlink() or not directory.is_dir()):
                continue
            try:
                fd = os.open(directory / "lease", os.O_RDWR | os.O_NOFOLLOW)
            except OSError:
                continue  # missing/unknown ownership is never permission to delete
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                shutil.rmtree(directory)
            finally:
                os.close(fd)

    def reap(self):
        """Reclaim inactive owners, preserving this owner and all live children."""
        with self._guard():
            self._reap()

    def close(self):
        """Release this owner after its Python workers drain; reap if child-free."""
        self._release()
        self.reap()
