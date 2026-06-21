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
from collections.abc import AsyncIterator
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from config import SETTINGS
from engines.base import DEMUCS_STEMS

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
    url: str = Field(..., min_length=1)
    model: Optional[str] = None
    keep_stems: Optional[list[str]] = None

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        # A web page can drive /process (the content script posts the page URL),
        # so an unvalidated URL is an SSRF / local-file-read primitive: yt-dlp
        # will happily open file:// paths or fetch internal hosts and serve the
        # result back via /audio and /video. This tool only ever strips public
        # web videos, so reject anything that isn't a public http(s) URL.
        import ipaddress
        import socket
        from urllib.parse import urlsplit

        v = v.strip()
        parts = urlsplit(v)
        if parts.scheme not in ("http", "https"):
            raise ValueError("url must be an http(s) URL")
        host = parts.hostname
        if not host:
            raise ValueError("url must include a host")
        lowered = host.lower()
        if lowered == "localhost" or lowered.endswith(".localhost"):
            raise ValueError("url host is not allowed")

        # Collect every IP the host could become, then block the internal ones.
        # Checking only ipaddress.ip_address(host) was bypassable: it parses just
        # canonical IPv4/IPv6, so decimal (http://2130706433/), hex
        # (0x7f000001), octal (0177.0.0.1) and short-form (127.1) literals — and
        # plain hostnames that simply resolve to an internal IP — sailed through
        # and yt-dlp would then fetch e.g. 127.0.0.1 or the cloud-metadata
        # address. We normalise all of those to the real address instead.
        candidates: list = []
        try:
            candidates.append(ipaddress.ip_address(host))  # canonical literal
        except ValueError:
            try:
                # inet_aton normalises the non-canonical IPv4 encodings above
                # (decimal/hex/octal/short-form) with no DNS lookup.
                candidates.append(ipaddress.ip_address(socket.inet_aton(host)))
            except OSError:
                # A real hostname: resolve it the way the fetch will. We reject
                # an unresolvable host (yt-dlp couldn't fetch it anyway) rather
                # than letting it through unchecked.
                try:
                    infos = socket.getaddrinfo(
                        host, None, type=socket.SOCK_STREAM
                    )
                except (socket.gaierror, UnicodeError, ValueError):
                    raise ValueError("url host could not be resolved")
                for info in infos:
                    try:
                        candidates.append(ipaddress.ip_address(info[4][0]))
                    except ValueError:
                        # A non-IP addrinfo entry (not expected for SOCK_STREAM);
                        # skip it but log so a silent drop is at least traceable.
                        log.debug("skipping unparseable resolved address %r", info[4][0])
                        continue

        for ip in candidates:
            # Unwrap IPv4-mapped IPv6 (::ffff:127.0.0.1) so the v4 rules apply.
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
                ip = ip.ipv4_mapped
            if (
                ip.is_loopback
                or ip.is_private
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_unspecified
                or ip.is_multicast
            ):
                raise ValueError("url host is not allowed")
        return v

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
        return v


class PrioritizeRequest(BaseModel):
    from_chunk: int = Field(..., ge=0)


@router.post("/process")
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
        status = registry.submit(req.url, model=model, keep_stems=keep_stems)
    except Exception as exc:
        log.exception("submit failed")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return status.to_dict()


@router.post("/process/{job_id}/prioritize")
def prioritize(job_id: str, req: PrioritizeRequest, request: Request) -> dict[str, bool]:
    """Re-order the worker's pending chunks so ``from_chunk`` is next.

    Fire-and-forget from the client's perspective. Returns ``applied``
    so a curious caller can tell whether the job was still mutable
    (it isn't once the job is fully done), but a normal seek doesn't
    need to inspect the response.
    """
    registry = request.app.state.registry
    ok = registry.prioritize(job_id, req.from_chunk)
    return {"applied": ok}


@router.get("/status/{job_id}")
def status(job_id: str, request: Request) -> JsonDict:
    registry = request.app.state.registry
    status = registry.get(job_id)
    if status is None:
        raise HTTPException(status_code=404, detail="unknown job_id")
    return status.to_dict()


@router.get("/events/{job_id}")
async def events(job_id: str, request: Request) -> Response:
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

    queue = registry.subscribe(job_id)

    async def stream() -> AsyncIterator[str]:
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

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers=_SSE_HEADERS
    )
