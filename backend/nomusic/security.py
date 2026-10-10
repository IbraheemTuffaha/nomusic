"""FastAPI authentication dependencies for protected backend operations."""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request

from .auth import (
    AuthConfigurationError,
    AuthPrincipal,
    InvalidCredential,
    RevokedCredential,
)


def _auth_response(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": code, "message": message},
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_operator(
    request: Request,
    authorization: str | None = Header(default=None),
) -> AuthPrincipal:
    """Require an active operator key before entering a protected route.

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
        raise HTTPException(status_code=503, detail={
            "code": "auth_not_configured",
            "message": str(exc),
        }) from exc


Operator = AuthPrincipal


__all__ = ["Operator", "require_operator"]
