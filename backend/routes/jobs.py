"""Job lifecycle endpoints: submit a URL, reprioritize chunks, poll status, and
the Server-Sent Events stream that pushes status changes.

Split out of ``server.create_app``. Handlers read the shared
:class:`~jobs.JobRegistry` and engine from ``request.app.state``. The request
models (including the SSRF-guarding URL validator) live here because only these
routes accept request bodies.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from config import SETTINGS
from engines.base import DEMUCS_STEMS
from jobs import JobRejected
from netsec import validate_public_url
from ratelimit import default_rl, process_rl, rate_limit, sse_counter
from security import JobId, client_ip, enforce_origin, require_edge

from . import JsonDict

log = logging.getLogger("nomusic.server")

router = APIRouter()

# SSE responses must not be buffered: ``no-cache`` stops the browser caching
# the stream, ``X-Accel-Buffering: no`` tells nginx-style proxies (relevant
# once this runs behind a real server) to flush each event immediately.
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

# How often a quiet stream wakes to check whether the client has disconnected.
# Kept well below the keep-alive gap so unsubscribe() — which starts the
# idle-abandon clock — fires within ~1s of a pause/tab-close, rather than
# lagging up to a full keep-alive interval.
_SSE_DISCONNECT_POLL_SECONDS = 1.0


class ProcessRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=SETTINGS.max_url_length)
    model: Optional[str] = None
    keep_stems: Optional[list[str]] = Field(None, max_length=SETTINGS.max_keep_stems)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        # A web page can drive /process (the content script posts the page URL),
        # so an unvalidated URL is an SSRF / local-file-read primitive. The single
        # gate in :mod:`netsec` enforces scheme + (public-mode) host allowlist +
        # internal-IP block-list, and is reused verbatim at /video time on the
        # stored URL. ``UrlNotAllowed`` subclasses ValueError → pydantic 422.
        return validate_public_url(v)

    @field_validator("keep_stems")
    @classmethod
    def _validate_stems(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is None:
            return v
        bad = [s for s in v if s not in DEMUCS_STEMS]
        if bad:
            raise ValueError(f"unknown stems: {bad}; allowed: {DEMUCS_STEMS}")
        if not v:
            raise ValueError("keep_stems must not be empty")
        # De-dup while preserving order so a client can't pad the request.
        return list(dict.fromkeys(v))


class PrioritizeRequest(BaseModel):
    from_chunk: int = Field(..., ge=0)


@router.post(
    "/process",
    dependencies=[
        Depends(require_edge),
        Depends(enforce_origin),
        Depends(rate_limit(process_rl)),
    ],
)
def process(req: ProcessRequest, request: Request) -> JsonDict:
    engine = request.app.state.engine
    registry = request.app.state.registry
    caps = engine.capabilities()
    model = req.model or caps.default_model
    if model not in caps.supported_models:
        # Fall back instead of erroring: a client may carry a stale model in
        # its saved settings (e.g. one we've since dropped). Log it and use
        # the default rather than failing the job.
        log.warning(
            "unknown model %r requested; falling back to default %r",
            model, caps.default_model,
        )
        model = caps.default_model
    keep_stems = list(req.keep_stems or SETTINGS.default_keep_stems)
    try:
        status = registry.submit(
            req.url, model=model, keep_stems=keep_stems, client_ip=client_ip(request)
        )
    except JobRejected as exc:
        # Admission cap hit (global / per-IP / disk floor): a transient "busy",
        # not a client error. 429 + Retry-After so the extension can back off.
        raise HTTPException(
            status_code=429,
            detail="server busy; try again shortly",
            headers={"Retry-After": "10"},
        ) from exc
    except Exception as exc:
        log.exception("submit failed")
        # Generic message (F20): the real cause is in the server log.
        raise HTTPException(status_code=400, detail="could not start job") from exc
    return status.to_dict()


@router.post(
    "/process/{job_id}/prioritize",
    dependencies=[
        Depends(require_edge),
        Depends(enforce_origin),
        Depends(rate_limit(default_rl)),
    ],
)
def prioritize(
    job_id: JobId, req: PrioritizeRequest, request: Request
) -> dict[str, bool]:
    """Re-order the worker's pending chunks so ``from_chunk`` is next.

    Fire-and-forget from the client's perspective. Returns ``applied``
    so a curious caller can tell whether the job was still mutable
    (it isn't once the job is fully done), but a normal seek doesn't
    need to inspect the response.
    """
    registry = request.app.state.registry
    ok = registry.prioritize(job_id, req.from_chunk)
    return {"applied": ok}


@router.get(
    "/status/{job_id}",
    dependencies=[Depends(require_edge), Depends(rate_limit(default_rl))],
)
def status(job_id: JobId, request: Request) -> JsonDict:
    registry = request.app.state.registry
    status = registry.get(job_id)
    if status is None:
        raise HTTPException(status_code=404, detail="unknown job_id")
    return status.to_dict()


@router.get("/events/{job_id}", dependencies=[Depends(require_edge)])
async def events(job_id: JobId, request: Request) -> Response:
    """Server-Sent Events stream of a job's status.

    Replaces the extension's old /status polling: the client opens one
    EventSource and receives a snapshot on connect plus a push on every
    state change, ending with a terminal ``ready``/``error`` event.

    Three response shapes:
      * 204 No Content for an unknown job — per the SSE spec this tells
        EventSource to stop reconnecting (vs. a 404, which it would retry).
      * a single-event stream for a job that's already terminal (e.g. a
        fully-cached replay) — no subscription, so nothing to leak.
      * a live subscription for an in-flight job.
    """
    registry = request.app.state.registry
    initial = registry.get(job_id)
    if initial is None:
        return Response(status_code=204)

    if initial.state.value in ("ready", "error"):
        payload = json.dumps(initial.to_dict())

        async def one_shot():
            yield f"data: {payload}\n\n"

        return StreamingResponse(
            one_shot(), media_type="text/event-stream", headers=_SSE_HEADERS
        )

    # Cap concurrent streams per job / per IP / globally (F10) so a client can't
    # exhaust the event loop by holding connections open. No-op in dev.
    ip = client_ip(request)
    if SETTINGS.public and not sse_counter.acquire(job_id, ip):
        raise HTTPException(
            status_code=429, detail="too many streams", headers={"Retry-After": "5"}
        )

    queue = registry.subscribe(job_id)

    async def stream() -> AsyncIterator[str]:
        start = time.monotonic()
        try:
            # Emit the current state right away so the client paints
            # without waiting for the next change. Re-read after subscribe
            # to close the gap where the job finished mid-handshake.
            cur = registry.get(job_id)
            if cur is not None:
                yield f"data: {json.dumps(cur.to_dict())}\n\n"
                if cur.state.value in ("ready", "error"):
                    return
            # Poll for disconnect every _SSE_DISCONNECT_POLL_SECONDS but
            # only emit a keep-alive comment every sse_keepalive_seconds, so
            # a paused/closed client is noticed promptly (starting the idle
            # clock) without spamming the wire with keep-alives.
            polls_per_keepalive = max(
                1,
                round(
                    SETTINGS.sse_keepalive_seconds / _SSE_DISCONNECT_POLL_SECONDS
                ),
            )
            idle_polls = 0
            while True:
                if await request.is_disconnected():
                    return
                # Hard lifetime cap (F10): a held/slowloris stream self-closes so
                # it can't pin a connection forever. The absolute job deadline
                # (jobs.py) separately bounds the worker itself.
                if (
                    SETTINGS.public
                    and SETTINGS.sse_max_lifetime_seconds > 0
                    and time.monotonic() - start > SETTINGS.sse_max_lifetime_seconds
                ):
                    return
                try:
                    update = await asyncio.wait_for(
                        queue.get(), timeout=_SSE_DISCONNECT_POLL_SECONDS
                    )
                except asyncio.TimeoutError:
                    idle_polls += 1
                    if idle_polls >= polls_per_keepalive:
                        idle_polls = 0
                        yield ":\n\n"  # keep-alive comment; ignored by EventSource
                    continue
                idle_polls = 0
                yield f"data: {json.dumps(update)}\n\n"
                if update.get("state") in ("ready", "error"):
                    return
        finally:
            registry.unsubscribe(job_id, queue)
            if SETTINGS.public:
                sse_counter.release(job_id, ip)

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers=_SSE_HEADERS
    )
