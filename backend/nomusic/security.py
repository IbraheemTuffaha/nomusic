"""FastAPI authentication dependencies for protected backend operations."""

from __future__ import annotations

import logging

from fastapi import Depends, Header, HTTPException, Request

from .auth import (
    AuthConfigurationError,
    AuthPrincipal,
    InvalidCredential,
    RevokedCredential,
)


log = logging.getLogger("nomusic.auth")


def _auth_response(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": code, "message": message},
        headers={"WWW-Authenticate": "Bearer"},
    )


def authenticate_request(request: Request, authorization: str | None) -> AuthPrincipal:
    """Authenticate one ``Authorization`` header, or reject it.

    Authentication is a dependency rather than middleware so the public
    ``/healthz`` probe stays intentionally tiny and route tests can exercise
    the exact protected surface.  The dependency runs before route handlers,
    so rejected requests never consume processing, export, or download slots.
    """
    store = getattr(request.app.state, "auth_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail={
            "code": "auth_unavailable",
            "message": "Authentication is not configured.",
        })
    try:
        return store.authenticate_header(authorization)
    except RevokedCredential as exc:
        raise _auth_response("credential_revoked", str(exc)) from exc
    except InvalidCredential as exc:
        raise _auth_response("authentication_required", str(exc)) from exc
    except AuthConfigurationError as exc:
        log.error("operator authentication is not configured: %s", exc)
        raise HTTPException(status_code=503, detail={
            "code": "auth_not_configured",
            "message": "Authentication is not configured. Run 'nomusic auth generate'.",
        }) from exc


def require_operator(
    request: Request,
    authorization: str | None = Header(default=None),
) -> AuthPrincipal:
    """Require an active operator key before entering a protected route."""
    return authenticate_request(request, authorization)


def authorize_export_download(request: Request, export_id: str) -> None:
    """Allow the operator key, or exactly one matching download grant.

    A grant is considered only when the request sends no ``Authorization``
    header. An invalid bearer therefore cannot be combined with a grant, and
    the grant stays available for a retry. The raw grant is never logged.
    """
    authorization = request.headers.get("authorization")
    grant = request.headers.get("x-nomusic-download-grant")
    if authorization is not None or not grant:
        authenticate_request(request, authorization)
        return
    grants = getattr(request.app.state, "download_grants", None)
    if grants is None or not grants.consume(export_id, grant):
        raise _auth_response("authentication_required", "operator key is required")


Operator = AuthPrincipal


__all__ = ["Operator", "authenticate_request", "authorize_export_download", "require_operator"]
