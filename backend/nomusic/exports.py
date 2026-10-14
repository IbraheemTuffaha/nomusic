"""Durable, bounded export preparation and artifact serving.

The registry owns the export state machine. Builders only produce one staged
file; publication, leases, cleanup, persistence, and worker lifetime stay here
so a cancellation or process stop cannot silently outlive the service.
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)


class ExportState(str, Enum):
    QUEUED = "queued"
    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ExportQueueFull(RuntimeError):
    """The bounded export worker pool cannot admit another build."""


class ExportDownloadsFull(RuntimeError):
    """All bounded artifact-reader slots are occupied."""


class ExportRegistryClosed(RuntimeError):
    """The service is stopping and cannot accept another export."""


class ExportSourceNotReady(RuntimeError):
    """An export was requested before its source job reached READY."""


class ExportBuildError(RuntimeError):
    """A builder rejected or could not complete an export."""


@dataclass(frozen=True)
class ExportSpec:
    job_id: str
    format: str
    max_height: int | None = None

    @property
    def dedupe_key(self) -> tuple[str, str, int | None]:
        return (self.job_id, self.format, self.max_height)


@dataclass(frozen=True)
class ExportArtifact:
    path: Path
    filename: str
    media_type: str
    size_bytes: int | None = None


@dataclass
class ExportStatus:
    export_id: str
    job_id: str
    format: str
    max_height: int | None
    state: ExportState
    phase: str = "queued"
    progress: float | None = 0.0
    filename: str = ""
    media_type: str = "application/octet-stream"
    size_bytes: int | None = None
    error: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    expires_at: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "export_id": self.export_id,
            "job_id": self.job_id,
            "format": self.format,
            "max_height": self.max_height,
            "state": self.state.value,
            "phase": self.phase,
            "progress": self.progress,
            "filename": self.filename,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
        }


class ExportDownload:
    """Own one shared artifact lease and one reader slot."""

    def __init__(self, slots: threading.BoundedSemaphore, lease) -> None:
        self._slots = slots
        self._lease = lease
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._lease.close()
        finally:
            self._slots.release()


ProgressCallback = Callable[[str, float | None], None]
ExportBuilder = Callable[[ExportSpec, Path, ProgressCallback, threading.Event], ExportArtifact]

_MANIFEST = ".export.json"
_ALLOWED_FORMATS = frozenset({"opus", "mp3", "mp4"})
_MAX_ERROR_CHARS = 1000


class ExportRegistry:
    """Own export admission, workers, durable status, and artifact leases."""

    def __init__(
        self,
        cache,
        jobs,
        builder: ExportBuilder,
        *,
        max_jobs: int = 2,
        ttl_seconds: float = 86400.0,
        max_artifact_bytes: int | None = None,
        max_downloads: int = 4,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_jobs <= 0:
            raise ValueError("max_jobs must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_artifact_bytes is not None and max_artifact_bytes <= 0:
            raise ValueError("max_artifact_bytes must be positive")
        if max_downloads <= 0:
            raise ValueError("max_downloads must be positive")
        self.cache = cache
        self.jobs = jobs
        self.builder = builder
        self.max_jobs = int(max_jobs)
        self.ttl_seconds = float(ttl_seconds)
        self.max_artifact_bytes = max_artifact_bytes
        self.max_downloads = int(max_downloads)
        self._clock = clock
        self._lock = threading.RLock()
        self._statuses: dict[str, ExportStatus] = {}
        self._dedupe: dict[tuple[str, str, int | None], str] = {}
        self._clients: dict[str, set[str]] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._closed = False
        self._download_slots = threading.BoundedSemaphore(self.max_downloads)
        self._load_manifests()

    # -- persistence -----------------------------------------------------

    @staticmethod
    def _finite(value: Any, default: float = 0.0) -> float:
        if value is None:
            return default
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("manifest contains a non-finite number")
        return result

    def _load_manifests(self) -> None:
        """Recover ready and terminal records without trusting disk input."""
        for directory in self.cache.export_entries():
            try:
                if directory.is_symlink() or not directory.name:
                    raise ValueError("invalid export directory")
                data = json.loads((directory / _MANIFEST).read_text())
                status = self._status_from_manifest(data)
                if status.export_id != directory.name:
                    raise ValueError("manifest/export directory mismatch")
                if status.expires_at is not None and status.expires_at <= self._clock():
                    self._remove_directory(directory)
                    continue
                artifact = directory / status.filename
                if status.state is ExportState.READY:
                    if not self._valid_artifact_path(artifact, directory) or not artifact.is_file():
                        raise ValueError("ready export artifact is missing")
                    status.filename = artifact.name
                    status.size_bytes = artifact.stat().st_size
                    if status.size_bytes <= 0:
                        raise ValueError("ready export artifact is empty")
                    if self.max_artifact_bytes is not None and status.size_bytes > self.max_artifact_bytes:
                        raise ValueError("ready export artifact exceeds its size limit")
                elif status.state not in (ExportState.FAILED, ExportState.CANCELLED):
                    self._remove_directory(directory)
                    continue
                with self._lock:
                    self._statuses[status.export_id] = status
                    if status.state is ExportState.READY:
                        key = self._dedupe_key(status)
                        previous = self._statuses.get(self._dedupe.get(key))
                        if previous is None or previous.created_at < status.created_at:
                            self._dedupe[key] = status.export_id
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                log.warning("Removing invalid export directory %s: %s", directory, exc)
                self._remove_directory(directory)

    @staticmethod
    def _status_from_manifest(data: dict[str, Any]) -> ExportStatus:
        if not isinstance(data, dict):
            raise ValueError("manifest is not an object")
        export_id = str(data["export_id"])
        if len(export_id) != 32 or any(ch not in "0123456789abcdef" for ch in export_id):
            raise ValueError("invalid export id")
        job_id = str(data["job_id"])
        if len(job_id) != 16 or any(ch not in "0123456789abcdef" for ch in job_id.lower()):
            raise ValueError("invalid source job id")
        state = ExportState(str(data["state"]))
        fmt = str(data["format"])
        if fmt not in _ALLOWED_FORMATS:
            raise ValueError("invalid export format")
        if state in (ExportState.READY, ExportState.FAILED, ExportState.CANCELLED) and data.get("expires_at") is None:
            raise ValueError("terminal export has no expiry")
        return ExportStatus(
            export_id=export_id,
            job_id=job_id.lower(),
            format=fmt,
            max_height=(int(data["max_height"]) if data.get("max_height") is not None else None),
            state=state,
            phase=str(data.get("phase", state.value)),
            progress=(
                None if data.get("progress") is None else ExportRegistry._finite(data["progress"])
            ),
            filename=str(data.get("filename", "")),
            media_type=str(data.get("media_type", "application/octet-stream")),
            size_bytes=(int(data["size_bytes"]) if data.get("size_bytes") is not None else None),
            error=str(data.get("error", "")),
            created_at=ExportRegistry._finite(data.get("created_at")),
            updated_at=ExportRegistry._finite(data.get("updated_at")),
            expires_at=(
                None if data.get("expires_at") is None else ExportRegistry._finite(data["expires_at"])
            ),
        )

    def _write_manifest(self, status: ExportStatus) -> None:
        directory = self.cache.ensure_export_dir(status.export_id)
        with tempfile.TemporaryDirectory(dir=self.cache.scratch.path, prefix="export-manifest-") as work:
            part = Path(work) / (_MANIFEST + ".part")
            part.write_text(json.dumps(status.to_dict(), sort_keys=True, indent=2))
            os.replace(part, directory / _MANIFEST)

    @staticmethod
    def _valid_artifact_path(path: Path, directory: Path) -> bool:
        try:
            return (
                not path.is_symlink()
                and path.name not in ("", ".", "..")
                and path.resolve().is_relative_to(directory.resolve())
            )
        except OSError:
            return False

    def _remove_directory(self, directory: Path) -> bool:
        if directory.is_symlink():
            try:
                directory.unlink(missing_ok=True)
                return True
            except OSError:
                log.debug("Could not remove export symlink %s", directory, exc_info=True)
                return False
        if not directory.exists():
            return True
        exports_root = (self.cache.root / "exports").resolve()
        try:
            if directory.parent.resolve() != exports_root:
                return False
        except OSError:
            return False
        try:
            try:
                lease = self.cache.export_lease(
                    directory.name, shared=False, blocking=False
                )
            except ValueError:
                # A malformed directory can exist on disk after a manual copy
                # or an older version. It is already confined to exports_root,
                # so quarantine it with the path-level lease instead of letting
                # startup fail while trying to validate its name.
                from nomusic.pipeline.cache import CacheLease

                lease = CacheLease(directory, shared=False, blocking=False)
            with lease:
                shutil.rmtree(directory, ignore_errors=True)
            return True
        except BlockingIOError:
            return False
        except (OSError, ValueError):
            log.debug("Could not remove export directory %s", directory, exc_info=True)
            return False

    @staticmethod
    def _dedupe_key(status: ExportStatus) -> tuple[str, str, int | None]:
        return ExportSpec(status.job_id, status.format, status.max_height).dedupe_key

    def _remove_dedupe_if_owner(self, status: ExportStatus) -> None:
        key = self._dedupe_key(status)
        if self._dedupe.get(key) == status.export_id:
            self._dedupe.pop(key, None)

    # -- admission/status ------------------------------------------------

    @staticmethod
    def _normalize_spec(job_id: str, format: str, max_height: int | None) -> ExportSpec:
        job_id = str(job_id).strip().lower()
        if len(job_id) != 16 or any(ch not in "0123456789abcdef" for ch in job_id):
            raise ValueError("job_id must be a 16-character cache key")
        fmt = str(format).strip().lower()
        if fmt not in _ALLOWED_FORMATS:
            raise ValueError("format must be opus, mp3, or mp4")
        if max_height is not None:
            max_height = int(max_height)
            max_height = None if max_height <= 0 else max(144, min(4320, max_height))
        if fmt != "mp4":
            max_height = None
        return ExportSpec(job_id, fmt, max_height)

    @staticmethod
    def _state_value(status: Any) -> str:
        state = getattr(status, "state", status)
        return str(getattr(state, "value", state)).lower()

    def _active_count_locked(self) -> int:
        return len(self._threads)

    def submit(
        self,
        job_id: str,
        format: str,
        max_height: int | None = None,
        client_id: str | None = None,
    ) -> ExportStatus:
        spec = self._normalize_spec(job_id, format, max_height)
        job = self.jobs.get(spec.job_id)
        if job is None:
            raise KeyError(spec.job_id)
        if self._state_value(job) != "ready":
            raise ExportSourceNotReady("source job is not ready")
        with self._lock:
            if self._closed:
                raise ExportRegistryClosed("export registry is shutting down")
            existing_id = self._dedupe.get(spec.dedupe_key)
            existing = self._statuses.get(existing_id) if existing_id else None
            if existing is not None and existing.state in (
                ExportState.QUEUED, ExportState.BUILDING, ExportState.READY
            ):
                if client_id:
                    self._clients.setdefault(existing.export_id, set()).add(client_id)
                return replace(existing)
            if existing is not None:
                self._remove_dedupe_if_owner(existing)
            if self._active_count_locked() >= self.max_jobs:
                raise ExportQueueFull(f"export queue is full ({self.max_jobs} job(s))")
            now = self._clock()
            export_id = uuid.uuid4().hex
            status = ExportStatus(
                export_id=export_id,
                job_id=spec.job_id,
                format=spec.format,
                max_height=spec.max_height,
                state=ExportState.QUEUED,
                created_at=now,
                updated_at=now,
            )
            self.cache.ensure_export_dir(export_id)
            try:
                self._write_manifest(status)
            except BaseException:
                self._remove_directory(self.cache.export_path(export_id))
                raise
            cancel = threading.Event()
            self._statuses[export_id] = status
            self._clients[export_id] = {client_id} if client_id else set()
            self._dedupe[spec.dedupe_key] = export_id
            self._cancel[export_id] = cancel
            thread = threading.Thread(
                target=self._run,
                args=(export_id, spec, cancel),
                name=f"nomusic-export-{export_id[:6]}",
                daemon=True,
            )
            self._threads[export_id] = thread
            try:
                thread.start()
            except BaseException:
                self._threads.pop(export_id, None)
                self._cancel.pop(export_id, None)
                self._statuses.pop(export_id, None)
                self._clients.pop(export_id, None)
                self._remove_dedupe_if_owner(status)
                self._remove_directory(self.cache.export_path(export_id))
                raise
            return replace(status)

    def get(self, export_id: str) -> ExportStatus | None:
        with self._lock:
            status = self._statuses.get(export_id)
            return replace(status) if status is not None else None

    def open_download(self, export_id: str) -> ExportDownload | None:
        with self._lock:
            status = self._statuses.get(export_id)
            if status is None or status.state is not ExportState.READY:
                return None
            filename = status.filename
        if not filename or Path(filename).name != filename:
            return None
        if not self._download_slots.acquire(blocking=False):
            raise ExportDownloadsFull("too many export downloads in progress")
        lease = None
        transferred = False
        try:
            lease = self.cache.export_lease(export_id, shared=True, blocking=False)
            directory = self.cache.export_path(export_id)
            artifact = directory / filename
            with self._lock:
                current = self._statuses.get(export_id)
                if current is None or current.state is not ExportState.READY:
                    return None
            if not self._valid_artifact_path(artifact, directory):
                return None
            if not artifact.is_file() or artifact.stat().st_size <= 0:
                return None
            transferred = True
            return ExportDownload(self._download_slots, lease)
        except (BlockingIOError, FileNotFoundError, OSError, ValueError):
            return None
        finally:
            if not transferred:
                if lease is not None:
                    lease.close()
                self._download_slots.release()

    def cancel(self, export_id: str, client_id: str | None = None) -> ExportStatus | None:
        with self._lock:
            status = self._statuses.get(export_id)
            if status is None:
                return None
            if status.state in (
                ExportState.READY, ExportState.FAILED, ExportState.EXPIRED, ExportState.CANCELLED
            ):
                return replace(status)
            if client_id:
                owners = self._clients.get(export_id)
                if not owners or client_id not in owners:
                    return replace(status)
                owners.remove(client_id)
                if owners:
                    return replace(status)
            now = self._clock()
            status.state = ExportState.CANCELLED
            status.phase = "cancelled"
            status.progress = None
            status.error = "cancelled"
            status.updated_at = now
            status.expires_at = now + self.ttl_seconds
            event = self._cancel.get(export_id)
            if event is not None:
                event.set()
            self._clients.pop(export_id, None)
            self._remove_dedupe_if_owner(status)
            try:
                self._write_manifest(status)
            except OSError:
                log.warning("Could not persist cancellation for %s", export_id, exc_info=True)
            return replace(status)

    def progress(self, export_id: str, phase: str, fraction: float | None) -> None:
        with self._lock:
            status = self._statuses.get(export_id)
            if status is None or status.state in (
                ExportState.CANCELLED, ExportState.EXPIRED, ExportState.FAILED
            ):
                return
            status.phase = str(phase)
            status.progress = None if fraction is None else max(0.0, min(1.0, float(fraction)))
            status.updated_at = self._clock()

    def cleanup(self, *, now: float | None = None) -> int:
        now = self._clock() if now is None else now
        tombstone_until = now + min(self.ttl_seconds, 300.0)
        candidates: list[str] = []
        forget: list[str] = []
        with self._lock:
            for export_id, status in list(self._statuses.items()):
                if status.state in (ExportState.QUEUED, ExportState.BUILDING):
                    continue
                if status.state is ExportState.EXPIRED:
                    if status.expires_at is not None and status.expires_at <= now:
                        forget.append(export_id)
                    continue
                if status.expires_at is None or status.expires_at > now:
                    continue
                status.state = ExportState.EXPIRED
                status.phase = "expired"
                status.progress = None
                status.updated_at = now
                status.expires_at = tombstone_until
                self._remove_dedupe_if_owner(status)
                candidates.append(export_id)
        removed = 0
        for export_id in candidates:
            if self._remove_directory(self.cache.export_path(export_id)):
                removed += 1
        with self._lock:
            for export_id in forget:
                status = self._statuses.get(export_id)
                if status is None or status.state is not ExportState.EXPIRED:
                    continue
                if self._remove_directory(self.cache.export_path(export_id)):
                    self._statuses.pop(export_id, None)
                    self._clients.pop(export_id, None)
                    self._remove_dedupe_if_owner(status)
        return removed

    def clear_all(self) -> None:
        """Invalidate exports without waiting on active readers or builders."""
        now = self._clock()
        with self._lock:
            statuses = list(self._statuses.values())
            for status in statuses:
                if status.state in (ExportState.QUEUED, ExportState.BUILDING):
                    status.state = ExportState.CANCELLED
                    status.phase = "cancelled"
                    status.error = "cancelled"
                    event = self._cancel.get(status.export_id)
                    if event is not None:
                        event.set()
                else:
                    status.state = ExportState.EXPIRED
                    status.phase = "expired"
                    status.progress = None
                status.updated_at = now
                status.expires_at = now + min(self.ttl_seconds, 300.0)
                self._remove_dedupe_if_owner(status)
                try:
                    self._write_manifest(status)
                except OSError:
                    log.debug("Could not persist cache-clear state", exc_info=True)
        for status in statuses:
            self._remove_directory(self.cache.export_path(status.export_id))
        with self._lock:
            for status in statuses:
                self._clients.pop(status.export_id, None)

    # -- workers/lifecycle -----------------------------------------------

    @property
    def has_active_workers(self) -> bool:
        with self._lock:
            return any(thread.is_alive() for thread in self._threads.values())

    def _take_export_lease(self, export_id: str, cancel: threading.Event):
        while not cancel.is_set():
            try:
                return self.cache.export_lease(export_id, shared=False, blocking=False)
            except BlockingIOError:
                cancel.wait(0.1)
        raise ExportBuildError("cancelled")

    def _cleanup_worker_files(self, export_id: str) -> None:
        directory = self.cache.export_path(export_id)
        if not directory.exists() or directory.is_symlink():
            return
        for path in directory.iterdir():
            if path.name in (_MANIFEST, ".lease"):
                continue
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
            except OSError:
                log.debug("Could not clean export output %s", path, exc_info=True)

    def _run(self, export_id: str, spec: ExportSpec, cancel: threading.Event) -> None:
        lease = None
        try:
            if cancel.is_set():
                return
            with self._lock:
                status = self._statuses.get(export_id)
                if status is None or status.state is ExportState.CANCELLED:
                    return
                status.state = ExportState.BUILDING
                status.phase = "building"
                status.progress = 0.0
                status.updated_at = self._clock()
                self._write_manifest(status)
            lease = self._take_export_lease(export_id, cancel)
            if cancel.is_set():
                return
            artifact = self.builder(spec, self.cache.export_path(export_id), self._progress_for(export_id), cancel)
            if cancel.is_set():
                return
            self._publish(export_id, artifact, self.cache.export_path(export_id))
        except Exception as exc:
            if cancel.is_set():
                return
            message = str(exc).strip() or type(exc).__name__
            log.warning("Export %s failed: %s", export_id, message)
            with self._lock:
                status = self._statuses.get(export_id)
                if status is None or status.state is ExportState.CANCELLED:
                    return
                status.state = ExportState.FAILED
                status.phase = "failed"
                status.progress = None
                status.error = message[:_MAX_ERROR_CHARS]
                status.updated_at = self._clock()
                status.expires_at = self._clock() + self.ttl_seconds
                try:
                    self._write_manifest(status)
                except OSError:
                    log.warning("Could not persist failed export %s", export_id, exc_info=True)
        finally:
            with self._lock:
                status = self._statuses.get(export_id)
                terminal_without_artifact = status is None or status.state in (
                    ExportState.CANCELLED, ExportState.FAILED, ExportState.EXPIRED
                )
            try:
                if terminal_without_artifact:
                    self._cleanup_worker_files(export_id)
            finally:
                if lease is not None:
                    lease.close()
                with self._lock:
                    self._threads.pop(export_id, None)
                    self._cancel.pop(export_id, None)

    def _progress_for(self, export_id: str) -> ProgressCallback:
        def update(phase: str, fraction: float | None) -> None:
            self.progress(export_id, phase, fraction)

        return update

    def _publish(self, export_id: str, artifact: ExportArtifact, directory: Path) -> None:
        path = Path(artifact.path)
        if not self._valid_artifact_path(path, directory):
            raise ExportBuildError("export builder returned a path outside its directory")
        if not path.is_file():
            raise ExportBuildError("export builder did not produce a file")
        size = path.stat().st_size
        if size <= 0:
            raise ExportBuildError("export builder produced an empty file")
        if self.max_artifact_bytes is not None and size > self.max_artifact_bytes:
            raise ExportBuildError(
                f"export artifact exceeds the {self.max_artifact_bytes}-byte limit"
            )
        filename = Path(artifact.filename).name
        if not filename or filename != path.name:
            raise ExportBuildError("export builder returned an invalid filename")
        with self._lock:
            status = self._statuses.get(export_id)
            if status is None or status.state is not ExportState.BUILDING:
                return
            now = self._clock()
            status.state = ExportState.READY
            status.phase = "ready"
            status.progress = 1.0
            status.filename = filename
            status.media_type = artifact.media_type
            status.size_bytes = size
            status.error = ""
            status.updated_at = now
            status.expires_at = now + self.ttl_seconds
            self._write_manifest(status)

    def shutdown(self) -> None:
        self.begin_shutdown()
        while True:
            with self._lock:
                threads = list(self._threads.values())
            if not threads:
                return
            for thread in threads:
                # The serving watchdog is the final hard deadline. Waiting
                # here keeps cache/scratch resources alive until every worker
                # releases its lease instead of closing them underneath a
                # running encoder.
                thread.join()

    def begin_shutdown(self) -> None:
        with self._lock:
            self._closed = True
            for export_id in list(self._cancel):
                self.cancel(export_id)


__all__ = [
    "ExportArtifact", "ExportBuildError", "ExportBuilder", "ExportDownloadsFull",
    "ExportDownload", "ExportQueueFull", "ExportRegistry", "ExportRegistryClosed",
    "ExportSourceNotReady", "ExportSpec", "ExportState", "ExportStatus",
]
