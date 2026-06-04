"""Disk cache for finished jobs.

Cache key = SHA-256 of ``(SCHEMA_VERSION, url, model, sorted(keep_stems))``.
Bumping ``SCHEMA_VERSION`` orphans every existing cache entry — the new code
won't find them so it produces fresh ones, and the TTL sweep reaps the
abandoned dirs within a week.

The cache stores:

* ``meta.json``       - normalized job metadata (url, model, stems, chunk plan)
* ``chunk_NNN.opus``  - per-chunk OGG/Opus, 48 kHz stereo, ~96 kbps VBR.
  Opus instead of WAV is ~15x smaller and decodes natively in Web Audio.

A 'full audio' fallback was previously cached on disk; we don't write it any
more because the extension always plays chunks back-to-back, and the
concatenated copy doubled disk usage. The ``/audio/{id}`` endpoint now
streams chunks together on demand.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

log = logging.getLogger(__name__)

# Bump when the on-disk chunk encoding, sample rate, or directory layout
# changes. Old entries become invisible to the new code and the TTL sweep
# (or the user's "Clear cache" button) reclaims their disk.
SCHEMA_VERSION = 3

CHUNK_EXT = ".opus"
CHUNK_MEDIA_TYPE = "audio/ogg"

# Top-level dirs under the cache root that hold url-keyed caches, not jobs.
# The job-dir scans (stats, TTL sweep) skip these and handle them separately.
_RESERVED_DIRS = frozenset({"sources", "videos"})


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
    def key(
        url: str,
        model: str,
        keep_stems: list[str],
        *,
        chunk_seconds: float | None = None,
        chunk_overlap_seconds: float | None = None,
    ) -> str:
        # ``chunk_seconds`` is in the key so changing the default value
        # auto-invalidates existing cache entries — old chunks would have
        # incorrect timing for the new playback plan. Callers that don't
        # care (legacy code, tests) can omit it.
        payload: dict = {
            "v": SCHEMA_VERSION,
            "url": url,
            "model": model,
            "stems": sorted(keep_stems),
        }
        if chunk_seconds is not None:
            payload["chunk_seconds"] = round(chunk_seconds, 4)
        if chunk_overlap_seconds is not None:
            payload["chunk_overlap_seconds"] = round(chunk_overlap_seconds, 4)
        normalized = json.dumps(payload, sort_keys=True).encode()
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

    def _key_dir(self, key: str) -> Path:
        """Resolve a job dir WITHOUT creating it. Read paths must not mkdir, or
        any request with an unknown ``job_id`` (GET /status, /chunk, /audio,
        the GC re-checking swept keys) leaves a phantom empty dir behind that
        then inflates ``stats().job_count`` until the TTL sweep reclaims it."""
        return self.root / key

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

    def video_dir(self, url: str, max_height: int | None = None) -> Path:
        """Dir for a cached *video* download backing the MP4 export.

        The pipeline only ever fetches audio, so the video stream is pulled
        on demand by ``GET /video/{job_id}``. We cache it here so repeat
        exports of the same video don't re-download a multi-GB stream. The key
        includes the requested ``max_height`` so switching the download-quality
        setting fetches the new resolution instead of reusing the old one.
        Reaped by the TTL sweep and the "Clear cache" button just like
        ``sources/``."""
        tag = str(max_height) if max_height else "best"
        key = hashlib.sha256(f"{url}\x00{tag}".encode()).hexdigest()[:16]
        path = self.root / "videos" / key
        path.mkdir(parents=True, exist_ok=True)
        return path

    # -- meta ----------------------------------------------------------------

    def load_meta(self, key: str) -> CacheMeta | None:
        meta_path = self._key_dir(key) / "meta.json"
        if not meta_path.exists():
            return None
        try:
            data = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            log.warning("Corrupt cache meta at %s; ignoring", meta_path)
            return None
        if not isinstance(data, dict):
            log.warning("Cache meta at %s is not an object; ignoring", meta_path)
            return None
        # Drop unknown fields so a meta.json written by a newer schema (or hand
        # edited) is read as a cache miss instead of a 500. Missing required
        # fields still raise TypeError below, which we also treat as a miss.
        known = {f.name for f in fields(CacheMeta)}
        try:
            return CacheMeta(**{k: v for k, v in data.items() if k in known})
        except TypeError as exc:
            log.warning("Incompatible cache meta at %s (%s); ignoring", meta_path, exc)
            return None

    def save_meta(self, key: str, meta: CacheMeta) -> None:
        meta_path = self.dir_for(key) / "meta.json"
        meta_path.write_text(json.dumps(asdict(meta), indent=2, sort_keys=True))

    # -- chunks --------------------------------------------------------------

    def chunk_path(self, key: str, idx: int) -> Path:
        # Read-safe: writers (processor) call save_meta first, which creates the
        # dir, so resolving the path here must not mkdir on its own.
        return self._key_dir(key) / f"chunk_{idx:03d}{CHUNK_EXT}"

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

    def touch(self, key: str) -> None:
        """Bump the cached job's mtime so the TTL sweep treats it as recent.

        Called when a re-watch hits an already-complete cache entry — without
        this, replays don't extend the entry's lifetime and a video you
        watch every day still gets reaped 7 days after the original
        processing. We touch ``meta.json`` (always exists for a tracked job)
        rather than every chunk file because the sweep uses the *newest*
        file mtime in the directory.
        """
        meta_path = self.dir_for(key) / "meta.json"
        if meta_path.exists():
            try:
                import os
                import time

                now = time.time()
                os.utime(meta_path, (now, now))
            except OSError as err:
                log.debug("touch(%s) failed: %s", key, err)

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

        videos_root = self.root / "videos"
        video_count = 0
        video_bytes = 0
        if videos_root.exists():
            for p in videos_root.glob("*"):
                if p.is_dir():
                    video_count += 1
                    video_bytes += _dir_bytes(p)

        job_count = 0
        job_bytes = 0
        for p in self.root.iterdir():
            if not p.is_dir() or p.name in _RESERVED_DIRS:
                continue
            job_count += 1
            job_bytes += _dir_bytes(p)

        return {
            "total_bytes": source_bytes + video_bytes + job_bytes,
            "source_bytes": source_bytes,
            "video_bytes": video_bytes,
            "job_bytes": job_bytes,
            "source_count": source_count,
            "video_count": video_count,
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
            if not child.is_dir() or child.name in _RESERVED_DIRS:
                continue
            if _dir_newest_mtime(child) < now - ttl_seconds:
                freed += _dir_bytes(child)
                shutil.rmtree(child, ignore_errors=True)
                removed += 1

        # Sources + videos: ~/.cache/nomusic/{sources,videos}/<url_hash>. Both
        # are url-keyed caches swept per-entry (a single old dir doesn't drag
        # the whole tree down with it).
        for tree in ("sources", "videos"):
            tree_root = self.root / tree
            if not tree_root.exists():
                continue
            for child in list(tree_root.iterdir()):
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
