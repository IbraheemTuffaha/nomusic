"""Disk cache for finished jobs.

Cache key = SHA-256 of ``(url, model, sorted(keep_stems))``. The cache stores:

* ``meta.json``   - normalized job metadata (url, model, stems, chunk plan)
* ``chunk_NNN.wav`` - per-chunk WAVs (16-bit PCM, 44.1 kHz stereo)
* ``full.wav``    - concatenated full-length WAV (written once all chunks ready)

We deliberately keep the on-disk format trivial so a debugging session can use
``afplay`` / ``ffplay`` directly without a Python interpreter.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class CacheMeta:
    url: str
    model: str
    keep_stems: list[str]
    duration_seconds: float
    chunk_seconds: float
    chunk_overlap_seconds: float
    total_chunks: int
    title: str = ""
    extractor: str = ""
    chunks_ready: list[int] = field(default_factory=list)
    complete: bool = False


class JobCache:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    # -- key helpers ---------------------------------------------------------

    @staticmethod
    def key(url: str, model: str, keep_stems: list[str]) -> str:
        normalized = json.dumps(
            {"url": url, "model": model, "stems": sorted(keep_stems)},
            sort_keys=True,
        ).encode()
        return hashlib.sha256(normalized).hexdigest()[:16]

    @staticmethod
    def url_key(url: str) -> str:
        """Stable hash for ``url`` alone. Used to cache the downloaded source
        audio independently of (model, stems), so changing the kept stems or
        model doesn't trigger a re-download of a 3h SpongeBob compilation."""
        return hashlib.sha256(url.encode()).hexdigest()[:16]

    def dir_for(self, key: str) -> Path:
        path = self.root / key
        path.mkdir(parents=True, exist_ok=True)
        return path

    def source_dir(self, url: str) -> Path:
        path = self.root / "sources" / self.url_key(url)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def drop_source(self, url: str) -> int:
        """Delete the cached source audio for ``url``. Returns bytes freed."""
        path = self.root / "sources" / self.url_key(url)
        if not path.exists():
            return 0
        freed = _dir_bytes(path)
        shutil.rmtree(path, ignore_errors=True)
        log.info("Dropped source cache for %s (%d bytes)", url, freed)
        return freed

    # -- meta ----------------------------------------------------------------

    def load_meta(self, key: str) -> CacheMeta | None:
        meta_path = self.dir_for(key) / "meta.json"
        if not meta_path.exists():
            return None
        try:
            data = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            log.warning("Corrupt cache meta at %s; ignoring", meta_path)
            return None
        return CacheMeta(**data)

    def save_meta(self, key: str, meta: CacheMeta) -> None:
        meta_path = self.dir_for(key) / "meta.json"
        meta_path.write_text(json.dumps(asdict(meta), indent=2, sort_keys=True))

    # -- chunks --------------------------------------------------------------

    def chunk_path(self, key: str, idx: int) -> Path:
        return self.dir_for(key) / f"chunk_{idx:03d}.wav"

    def full_path(self, key: str) -> Path:
        return self.dir_for(key) / "full.wav"

    def record_chunk(self, key: str, idx: int) -> None:
        meta = self.load_meta(key)
        if meta is None:
            return
        if idx in meta.chunks_ready:
            return
        meta.chunks_ready.append(idx)
        meta.chunks_ready.sort()
        self.save_meta(key, meta)

    def mark_complete(self, key: str) -> None:
        meta = self.load_meta(key)
        if meta is None:
            return
        meta.complete = True
        self.save_meta(key, meta)

    # -- maintenance ---------------------------------------------------------

    def stats(self) -> dict:
        """Tally on-disk sizes. Returns bytes counts the popup can render."""
        sources_root = self.root / "sources"
        source_count = 0
        source_bytes = 0
        if sources_root.exists():
            for p in sources_root.glob("*"):
                if p.is_dir():
                    source_count += 1
                    source_bytes += _dir_bytes(p)

        job_count = 0
        job_bytes = 0
        for p in self.root.iterdir():
            if not p.is_dir() or p.name == "sources":
                continue
            job_count += 1
            job_bytes += _dir_bytes(p)

        return {
            "total_bytes": source_bytes + job_bytes,
            "source_bytes": source_bytes,
            "job_bytes": job_bytes,
            "source_count": source_count,
            "job_count": job_count,
        }

    def clear_all(self) -> int:
        """Delete every cached source and job. Returns bytes freed.

        Survives an in-flight job at the cost of that job's next chunk write
        failing (the worker thread crashes; the user re-clicks). The root
        directory itself is preserved so subsequent writes don't need to
        recreate it.
        """
        freed = 0
        for child in list(self.root.iterdir()):
            try:
                freed += _dir_bytes(child) if child.is_dir() else child.stat().st_size
            except OSError:
                pass
            try:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
            except OSError as err:
                log.warning("clear_all: couldn't remove %s: %s", child, err)
        return freed

    def sweep_older_than(self, ttl_seconds: float) -> tuple[int, int]:
        """Delete cache entries whose newest file is older than ``ttl_seconds``.

        Returns ``(entries_removed, bytes_freed)``. We use ``newest file``
        (rather than directory mtime) because writing a chunk doesn't always
        bump the parent dir's mtime on every filesystem, and we want the
        access pattern "I rewatched it yesterday" to keep the entry alive.
        """
        if ttl_seconds <= 0:
            return (0, 0)

        import time

        now = time.time()
        removed = 0
        freed = 0

        # Jobs: ~/.cache/nomusic/<key>
        for child in list(self.root.iterdir()):
            if not child.is_dir() or child.name == "sources":
                continue
            if _dir_newest_mtime(child) < now - ttl_seconds:
                freed += _dir_bytes(child)
                shutil.rmtree(child, ignore_errors=True)
                removed += 1

        # Sources: ~/.cache/nomusic/sources/<url_hash>
        sources_root = self.root / "sources"
        if sources_root.exists():
            for child in list(sources_root.iterdir()):
                if not child.is_dir():
                    continue
                if _dir_newest_mtime(child) < now - ttl_seconds:
                    freed += _dir_bytes(child)
                    shutil.rmtree(child, ignore_errors=True)
                    removed += 1

        if removed:
            log.info(
                "TTL sweep removed %d entries (%d bytes)", removed, freed
            )
        return (removed, freed)


def _dir_bytes(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _dir_newest_mtime(path: Path) -> float:
    """Latest mtime among files inside ``path`` (recursive).

    Returns 0 for an empty directory — which causes ``sweep_older_than`` to
    delete it. That's fine: an empty cache dir has nothing to protect.
    """
    newest = 0.0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                m = p.stat().st_mtime
                if m > newest:
                    newest = m
        except OSError:
            pass
    return newest
