"""Small ASGI/HTTP middlewares for the hardened deployment.

* :class:`MaxBodySizeMiddleware` rejects oversized POST bodies up front (413),
  before Starlette buffers them — a cheap guard against memory-abuse via a huge
  request body. Active only in public mode.
* :class:`SecurityHeadersMiddleware` adds conservative response headers. Cheap
  and harmless, so it's always on.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from config import SETTINGS

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-site",
    # The API serves JSON / media, never HTML documents that load scripts, so a
    # tight default CSP costs nothing and blunts any reflected-content surprise.
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}


class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if SETTINGS.public and request.method in ("POST", "PUT", "PATCH"):
            cl = request.headers.get("content-length")
            if cl is not None:
                try:
                    if int(cl) > SETTINGS.max_request_bytes:
                        return JSONResponse(
                            {"detail": "request body too large"}, status_code=413
                        )
                except ValueError:
                    return JSONResponse(
                        {"detail": "invalid Content-Length"}, status_code=400
                    )
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response
