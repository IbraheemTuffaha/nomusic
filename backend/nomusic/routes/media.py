"""Media-serving endpoints for per-chunk audio and the concatenated track.

Split out of ``server.create_app`` so the handlers live at module scope. Each
reads the shared :class:`~pipeline.cache.JobCache` from ``request.app.state``;
export preparation lives in :mod:`nomusic.exports` and its dedicated routes.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse

from nomusic.pipeline.cache import CHUNK_MEDIA_TYPE
from nomusic.pipeline.export import (
    complete_manifest,
    snapshot_chunk_files,
)


router = APIRouter()

# Block size (64 KiB) for streaming concatenated audio chunks to the client.
_STREAM_BLOCK_BYTES = 65536


class _LeasedFileResponse(FileResponse):
    """Close a cache lease even when a client disconnects mid-response."""

    def __init__(self, path: str, lease, **kwargs) -> None:
        super().__init__(path, **kwargs)
        self._lease = lease

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._lease.close()


class _LeasedStreamingResponse(StreamingResponse):
    """Close a cache lease for both completed and cancelled streams."""

    def __init__(self, content, lease, **kwargs) -> None:
        super().__init__(content, **kwargs)
        self._lease = lease

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._lease.close()

@router.get("/chunk/{job_id}/{chunk_idx}")
def chunk(job_id: str, chunk_idx: int, request: Request) -> FileResponse:
    cache = request.app.state.cache
    meta = cache.load_meta(job_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="unknown job_id")
    if chunk_idx < 0 or chunk_idx >= meta.total_chunks:
        raise HTTPException(status_code=404, detail="chunk index out of range")
    path = cache.chunk_path(job_id, chunk_idx)
    if not path.exists():
        # 425 Too Early: client should poll /status and retry.
        # no-store is critical — without it, the browser can cache the
        # 425 and serve it forever, so a chunk that landed on disk a
        # second later would still appear "not ready" to the client.
        raise HTTPException(
            status_code=425,
            detail="chunk not ready",
            headers={"Cache-Control": "no-store"},
        )
    # Chunks are atomically published one at a time. A shared lease keeps the
    # namespace alive without waiting for the processor's lifetime lease, so
    # progressive playback can consume ready chunks during a long job.
    lease = cache.job_lease(job_id, shared=True)
    try:
        if not path.exists():
            raise HTTPException(status_code=425, detail="chunk not ready")
    except BaseException:
        lease.close()
        raise
    return _LeasedFileResponse(
        str(path),
        lease,
        media_type=CHUNK_MEDIA_TYPE,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/audio/{job_id}")
def audio(job_id: str, request: Request) -> Response:
    """On-demand concatenation of every chunk into a single track.

    We no longer keep a precomputed full file on disk (cut storage in
    half), so this endpoint stitches the per-chunk OGG/Opus files
    together. OGG containers concatenate cleanly: writing one file's bytes
    after another produces a valid combined stream that Web Audio, VLC,
    and ffplay all decode as one track.

    """
    cache = request.app.state.cache
    meta = cache.load_meta(job_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="unknown job_id")
    if not complete_manifest(meta):
        raise HTTPException(status_code=425, detail="full audio not ready")

    # Snapshot the contiguous run of chunk files ONCE. The advertised
    # Content-Length and the streamed body must come from the same view of
    # disk; computing them in two passes lets a gap (or a concurrent TTL
    # sweep / cache clear) advertise more bytes than _gen actually yields,
    # which clients read as a truncated/hung response.
    lease = cache.job_lease(job_id, shared=True)
    chunk_files = snapshot_chunk_files(
        cache, job_id, meta.total_chunks, require_complete=True
    )
    if not chunk_files:
        lease.close()
        raise HTTPException(status_code=425, detail="full audio not ready")

    def _gen():
        for p, _ in chunk_files:
            try:
                f = open(p, "rb")
            except FileNotFoundError:
                return  # deleted after the snapshot; stop short
            with f:
                while True:
                    block = f.read(_STREAM_BLOCK_BYTES)
                    if not block:
                        break
                    yield block

    total = sum(size for _, size in chunk_files)
    headers = {"Cache-Control": "public, max-age=86400"}
    if total:
        headers["Content-Length"] = str(total)
    return _LeasedStreamingResponse(
        _gen(), lease, media_type=CHUNK_MEDIA_TYPE, headers=headers,
    )
