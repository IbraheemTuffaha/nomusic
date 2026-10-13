"""Asynchronous export preparation and artifact serving."""

from __future__ import annotations

import errno
import logging
import os
import re
import shutil
import stat
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

log = logging.getLogger("nomusic.server")

router = APIRouter()
_EXPORT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_UNSAFE_NAME_RE = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]')
_DOWNLOAD_SUFFIXES = (".mp3", ".mp4", ".opus")
_MAX_DOWNLOAD_STEM = 120
_MAX_DOWNLOAD_COLLISIONS = 100
_SAVE_FAILURE = "Could not save the export into Downloads."
_LOCAL_SAVE_DETAIL = "Local save is only available for a loopback connection."
# Peer addresses only. ``testclient`` is Starlette's in-process TestClient
# name and is not a routable host. A remote TCP peer must not be able to
# write into this machine's Downloads folder.
_LOCAL_PEERS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


class ExportRequest(BaseModel):
    job_id: str = Field(..., min_length=16, max_length=16, pattern=r"^[0-9a-fA-F]{16}$")
    format: str = Field(..., min_length=1)
    max_height: int | None = Field(default=None, ge=-1, le=10000)
    client_id: str | None = Field(default=None, min_length=1, max_length=128)


class SaveExportRequest(BaseModel):
    filename: str | None = Field(default=None, max_length=180)


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


def _client_is_local(request: Request) -> bool:
    client = request.client
    if client is None or not client.host:
        return False
    host = client.host.lower()
    if host.startswith("::ffff:"):
        host = host.removeprefix("::ffff:")
    return host in _LOCAL_PEERS


def downloads_directory() -> Path:
    """The current user's Downloads folder, following a directory symlink."""
    path = Path.home() / "Downloads"
    path.mkdir(parents=True, exist_ok=True)
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise NotADirectoryError(errno.ENOTDIR, "downloads directory is unavailable")
    return resolved


def safe_download_name(requested: str | None, artifact_name: str) -> str:
    """Basename whose suffix is the artifact's, never a caller-chosen path.

    A requested ``.mp4`` on an MP3 artifact becomes ``.mp3``. Path segments,
    control characters, and a different media suffix cannot escape Downloads
    or change the file type.
    """
    artifact = Path(str(artifact_name)).name
    suffix = Path(artifact).suffix.lower()
    if (
        suffix not in _DOWNLOAD_SUFFIXES
        or artifact != str(artifact_name)
        or artifact in {".", ".."}
    ):
        raise ValueError("export artifact name is invalid")
    raw = requested.strip() if isinstance(requested, str) else ""
    if not raw:
        raw = artifact
    raw = raw.replace("\\", "/").rsplit("/", 1)[-1]
    lower = raw.lower()
    for candidate in _DOWNLOAD_SUFFIXES:
        if lower.endswith(candidate):
            raw = raw[: -len(candidate)]
            break
    stem = _UNSAFE_NAME_RE.sub(" ", raw)
    stem = re.sub(r"\s+", " ", stem).strip(" .")[:_MAX_DOWNLOAD_STEM].rstrip(" .")
    return f"{stem or 'nomusic'}{suffix}"


def copy_export_into(source: Path, directory: Path, filename: str) -> str:
    """Copy ``source`` into ``directory`` under ``filename``.

    The copy is bytes only: metadata and extended attributes, including a
    quarantine mark, stay behind. An existing file or symlink keeps its name
    and the copy uses ``Title (1).ext`` instead. The partial file is removed
    if the copy does not finish.
    """
    if Path(filename).name != filename or filename in {".", ".."}:
        raise ValueError("download filename is invalid")
    directory.mkdir(parents=True, exist_ok=True)
    directory = directory.resolve(strict=True)
    if not directory.is_dir():
        raise NotADirectoryError(errno.ENOTDIR, "downloads directory is unavailable")
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    for index in range(_MAX_DOWNLOAD_COLLISIONS + 1):
        candidate = filename if index == 0 else f"{stem} ({index}){suffix}"
        destination = directory / candidate
        if destination.parent != directory:
            raise ValueError("download filename is invalid")
        try:
            fd = os.open(destination, flags, 0o644)
        except OSError as exc:
            if exc.errno in (errno.EEXIST, errno.ELOOP, errno.EISDIR):
                continue
            raise
        try:
            os.fchmod(fd, 0o644)
            with os.fdopen(fd, "wb") as out:
                fd = -1
                _copy_regular_file(source, out)
                out.flush()
                os.fsync(out.fileno())
        except BaseException:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(destination)
            except OSError:
                pass
            raise
        return candidate
    raise FileExistsError(errno.EEXIST, "download name is already in use")


def _copy_regular_file(source: Path, out) -> None:
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
            raise OSError(errno.EINVAL, "export artifact is unavailable")
        with os.fdopen(fd, "rb") as inp:
            fd = -1
            shutil.copyfileobj(inp, out)
    finally:
        if fd >= 0:
            os.close(fd)


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


@router.post("/exports/{export_id}/save")
def save_export(
    export_id: str,
    request: Request,
    body: SaveExportRequest | None = None,
    _operator=Depends(require_operator),
) -> JSONResponse:
    """Copy a ready artifact into the local Downloads folder.

    Chrome marks every file it writes, and macOS then blocks an MP3 whose
    opener does not declare that document type. A copy made here has no
    quarantine attribute. The extension calls this for a loopback MP3. The
    response is the saved basename and never a filesystem path.
    """
    _require_export_id(export_id)
    if not _client_is_local(request):
        raise HTTPException(status_code=403, detail=_LOCAL_SAVE_DETAIL)
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
        filename = safe_download_name(None if body is None else body.filename, status.filename)
        saved = copy_export_into(artifact, downloads_directory(), filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="download filename is invalid") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=410, detail="export artifact is unavailable") from exc
    except OSError as exc:
        log.warning("could not save export %s (%s)", export_id, type(exc).__name__)
        raise HTTPException(status_code=500, detail=_SAVE_FAILURE) from exc
    if Path(saved).name != saved or "/" in saved or "\\" in saved:
        raise HTTPException(status_code=500, detail=_SAVE_FAILURE)
    return JSONResponse({"filename": saved}, headers={"Cache-Control": "no-store"})


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
