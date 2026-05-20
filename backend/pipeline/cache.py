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
