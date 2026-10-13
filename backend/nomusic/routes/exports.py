"""Asynchronous export preparation and artifact serving."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from nomusic.exports import (
    DownloadGrantsFull,
    ExportDownloadsFull,
    ExportQueueFull,
    ExportRegistryClosed,
    ExportSourceNotReady,
    ExportState,
)
from nomusic.security import authorize_export_download, require_operator

from . import JsonDict
from .responses import LeasedFileResponse

router = APIRouter()
_EXPORT_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class ExportRequest(BaseModel):
    job_id: str = Field(..., min_length=16, max_length=16, pattern=r"^[0-9a-fA-F]{16}$")
    format: str = Field(..., min_length=1)
    max_height: int | None = Field(default=None, ge=-1, le=10000)
    client_id: str | None = Field(default=None, min_length=1, max_length=128)


def _payload(status) -> JsonDict:
    body = status.to_dict()
    if status.state is ExportState.READY:
        body["download_url"] = f"/exports/{status.export_id}/download"
    return body


def _require_export_id(export_id: str) -> str:
    if not _EXPORT_ID_RE.fullmatch(export_id):
        raise HTTPException(status_code=404, detail="unknown export_id")
    return export_id


def _require_downloadable(status) -> None:
    if status is None:
        raise HTTPException(status_code=404, detail="unknown export_id")
    if status.state in (ExportState.QUEUED, ExportState.BUILDING):
        raise HTTPException(
            status_code=425,
            detail="export is not ready",
            headers={"Cache-Control": "no-store"},
        )
    if status.state is ExportState.EXPIRED:
        raise HTTPException(status_code=410, detail="export has expired")
    if status.state is ExportState.CANCELLED:
        raise HTTPException(status_code=409, detail="export was cancelled")
    if status.state is ExportState.FAILED:
        raise HTTPException(status_code=409, detail=status.error or "export failed")
    if not status.filename or Path(status.filename).name != status.filename:
        raise HTTPException(status_code=410, detail="export artifact is unavailable")


@router.post("/exports")
def submit_export(
    req: ExportRequest, request: Request, _operator=Depends(require_operator)
) -> JSONResponse:
    registry = request.app.state.exports
    try:
        status = registry.submit(req.job_id, req.format, req.max_height, req.client_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown job_id") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ExportQueueFull as exc:
        return JSONResponse(
            {"detail": str(exc)},
            status_code=429,
            headers={"Retry-After": "2", "Cache-Control": "no-store"},
        )
    except ExportRegistryClosed as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ExportSourceNotReady as exc:
        raise HTTPException(
            status_code=425, detail=str(exc), headers={"Cache-Control": "no-store"}
        ) from exc
    code = 200 if status.state is ExportState.READY else 202
    return JSONResponse(_payload(status), status_code=code,
                        headers={"Cache-Control": "no-store"})


@router.post("/exports/{export_id}/download-grant")
def create_download_grant(
    export_id: str, request: Request, _operator=Depends(require_operator)
) -> JSONResponse:
    """Mint one short-lived download capability for an already-ready export.

    The operator key stays with the caller that holds it. The token in the
    response authorizes exactly one fetch of this export and is not written
    to the log.
    """
    _require_export_id(export_id)
    registry = request.app.state.exports
    status = registry.get(export_id)
    _require_downloadable(status)
    directory = request.app.state.cache.export_path(export_id)
    artifact = directory / status.filename
    try:
        if not artifact.is_file() or artifact.stat().st_size <= 0:
            raise HTTPException(status_code=410, detail="export artifact is unavailable")
    except OSError as exc:
        raise HTTPException(status_code=410, detail="export artifact is unavailable") from exc
    try:
        token, expires_in = request.app.state.download_grants.issue(export_id)
    except DownloadGrantsFull as exc:
        return JSONResponse(
            {"detail": str(exc)},
            status_code=429,
            headers={"Retry-After": "2", "Cache-Control": "no-store"},
        )
    return JSONResponse(
        {
            "grant": token,
            "expires_in": int(expires_in),
            "media_type": status.media_type,
            "filename": status.filename,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/exports/{export_id}/download")
def download_export(export_id: str, request: Request) -> Response:
    _require_export_id(export_id)
    # Authorize before revealing whether this id has an artifact. A grant is
    # consumed only after it matches this export, and only when no bearer
    # credential was sent with it.
    authorize_export_download(request, export_id)
    registry = request.app.state.exports
    status = registry.get(export_id)
    _require_downloadable(status)

    cache = request.app.state.cache
    directory = cache.export_path(export_id)
    artifact = directory / status.filename
    try:
        if not artifact.is_file() or artifact.stat().st_size <= 0:
            raise HTTPException(status_code=410, detail="export artifact is unavailable")
    except OSError as exc:
        raise HTTPException(status_code=410, detail="export artifact is unavailable") from exc
    try:
        lease = registry.open_download(export_id)
    except ExportDownloadsFull as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    if lease is None:
        raise HTTPException(status_code=410, detail="export artifact is unavailable")
    return LeasedFileResponse(
        str(artifact),
        lease,
        media_type=status.media_type,
        filename=status.filename,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/exports/{export_id}")
def get_export(
    export_id: str, request: Request, _operator=Depends(require_operator)
) -> JsonDict:
    _require_export_id(export_id)
    status = request.app.state.exports.get(export_id)
    if status is None:
        raise HTTPException(status_code=404, detail="unknown export_id")
    return _payload(status)


@router.delete("/exports/{export_id}")
def cancel_export(
    export_id: str,
    request: Request,
    client_id: str | None = Query(default=None, min_length=1, max_length=128),
    _operator=Depends(require_operator),
) -> JsonDict:
    _require_export_id(export_id)
    status = request.app.state.exports.cancel(export_id, client_id)
    if status is None:
        raise HTTPException(status_code=404, detail="unknown export_id")
    return _payload(status)
