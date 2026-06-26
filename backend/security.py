"""Identity, admin auth, edge trust, and path-param validation.

All gates here are no-ops when ``NOMUSIC_PUBLIC`` is unset, so local/dev use is
unchanged. Identity is the real client IP from ``CF-Connecting-IP`` — trusted
*only* because the app listens on loopback and the sole path to it is the
Cloudflare tunnel; ``X-Forwarded-For`` is never trusted.
"""

from __future__ import annotations

import hmac
from typing import Annotated, Optional

from fastapi import Header, HTTPException, Path, Request

from config import SETTINGS

# Job ids are sha256(...)[:16] — exactly 16 lowercase hex chars. Pinning the
# shape here (before any filesystem use) closes path-traversal via job_id.
JOB_ID_RE = r"^[0-9a-f]{16}$"
JobId = Annotated[str, Path(pattern=JOB_ID_RE)]
ChunkIdx = Annotated[int, Path(ge=0, le=100_000)]


def client_ip(request: Request) -> str:
    """The caller's IP. In the tunnelled deployment the only trustworthy source
    is ``CF-Connecting-IP`` (Cloudflare sets it; clients can't, since they never
    reach the loopback socket directly). Falls back to the socket peer for
    local/dev."""
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    return request.client.host if request.client else "unknown"


async def require_edge(request: Request) -> None:
    """Reject anything that didn't traverse the tunnel, when a tunnel secret is
    configured. No-op in dev or when no secret is set."""
    if not SETTINGS.public or not SETTINGS.tunnel_secret:
        return
    got = request.headers.get("x-nomusic-tunnel", "")
    if not hmac.compare_digest(got, SETTINGS.tunnel_secret):
        # 404 (not 403) so the origin looks absent to a LAN-direct probe.
        raise HTTPException(status_code=404)


async def require_admin(
    x_admin_token: Annotated[Optional[str], Header()] = None,
) -> None:
    """Gate destructive/admin endpoints behind the private admin token in public
    mode. Dev/local (``not public``) leaves them open as before. Returns 404 on a
    missing/bad token so the endpoint looks absent."""
    if not SETTINGS.public:
        return
    token = SETTINGS.admin_token
    if not token:  # public but unconfigured ⇒ fail closed
        raise HTTPException(status_code=404)
    if not x_admin_token or not hmac.compare_digest(x_admin_token, token):
        raise HTTPException(status_code=404)


async def enforce_origin(request: Request) -> None:
    """Server-side Origin check for state-changing routes. CORS only blocks
    *reading* a cross-origin response, not the side effect, so a present-but-
    disallowed ``Origin`` (a drive-by from some other site) is 403'd here. A
    missing Origin (curl / non-browser) is allowed — those are bounded by the
    rate limit + admission cap. No-op in dev."""
    if not SETTINGS.public:
        return
    origin = request.headers.get("origin")
    if origin is None:
        return
    if not SETTINGS.is_origin_allowed(origin):
        raise HTTPException(status_code=403, detail="origin not allowed")
