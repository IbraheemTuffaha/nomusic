"""Persistent, asynchronous export jobs.

Export preparation is intentionally separate from the HTTP response that
downloads the finished file.  A request creates a small durable record and a
worker prepares the artifact in its own leased cache directory.  Readers take
a shared lease, so expiry/clear maintenance cannot remove a file while a
response is still serving it.

The builder is injected rather than imported here.  This keeps the state
machine independent from ffmpeg/yt-dlp and makes the admission, deduplication,
restart, and cancellation rules unit-testable with a tiny fake builder.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import tempfile
import time
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Any

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
    """All bounded artifact-reader slots are currently occupied."""


class ExportRegistryClosed(RuntimeError):
    """The service is stopping and cannot accept another export."""


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
    """The file produced by a builder.

    ``path`` must be inside the destination directory supplied to the builder;
    the registry checks that invariant before publishing the manifest.
    """

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
    """A shared artifact lease and one reader slot, released after response."""

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
_PART_SUFFIX = ".part"
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
        wait_timeout_seconds: float = 7200.0,
        max_artifact_bytes: int | None = None,
        max_downloads: int = 4,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_jobs <= 0:
            raise ValueError("max_jobs must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if wait_timeout_seconds <= 0:
            raise ValueError("wait_timeout_seconds must be positive")
        if max_artifact_bytes is not None and max_artifact_bytes <= 0:
            raise ValueError("max_artifact_bytes must be positive")
        if max_downloads <= 0:
            raise ValueError("max_downloads must be positive")
        self.cache = cache
        self.jobs = jobs
        self.builder = builder
        self.max_jobs = int(max_jobs)
        self.ttl_seconds = float(ttl_seconds)
        self.wait_timeout_seconds = float(wait_timeout_seconds)
        self.max_artifact_bytes = max_artifact_bytes
        self.max_downloads = int(max_downloads)
        self._clock = clock
        self._lock = threading.RLock()
        self._statuses: dict[str, ExportStatus] = {}
        self._dedupe: dict[tuple[str, str, int | None], str] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._closed = False
        self._download_slots = threading.BoundedSemaphore(self.max_downloads)
        self._load_manifests()

    # -- persistence -----------------------------------------------------

    def _load_manifests(self) -> None:
        """Recover ready artifacts; discard interrupted builds after restart."""
        for directory in self.cache.export_entries():
            manifest = directory / _MANIFEST
            try:
                data = json.loads(manifest.read_text())
                status = self._status_from_manifest(data)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                log.warning("Removing unreadable export directory %s: %s", directory, exc)
                self._remove_directory(directory)
                continue
            artifact = directory / str(data.get("artifact", ""))
            if status.state is not ExportState.READY or not self._valid_artifact_path(
                artifact, directory
            ) or not artifact.is_file() or artifact.stat().st_size <= 0:
                # A queued/building/partial export cannot be safely resumed
                # without replaying the builder. It is cheap to discard and
                # prevents an interrupted .part file being served as complete.
                self._remove_directory(directory)
                continue
            if status.expires_at is not None and status.expires_at <= self._clock():
                self._remove_directory(directory)
                continue
            status.size_bytes = artifact.stat().st_size
            with self._lock:
                self._statuses[status.export_id] = status
                self._dedupe[ExportSpec(
                    status.job_id, status.format, status.max_height
                ).dedupe_key] = status.export_id

    @staticmethod
    def _status_from_manifest(data: dict[str, Any]) -> ExportStatus:
        state = ExportState(str(data["state"]))
        return ExportStatus(
            export_id=str(data["export_id"]),
            job_id=str(data["job_id"]),
            format=str(data["format"]),
            max_height=(
                int(data["max_height"]) if data.get("max_height") is not None else None
            ),
            state=state,
            phase=str(data.get("phase", state.value)),
            progress=(
                float(data["progress"]) if data.get("progress") is not None else None
            ),
            filename=str(data.get("filename", "")),
            media_type=str(data.get("media_type", "application/octet-stream")),
            size_bytes=(int(data["size_bytes"]) if data.get("size_bytes") is not None else None),
            error=str(data.get("error", "")),
            created_at=float(data.get("created_at", 0.0)),
            updated_at=float(data.get("updated_at", 0.0)),
            expires_at=(
                float(data["expires_at"]) if data.get("expires_at") is not None else None
            ),
        )

    def _write_manifest(self, status: ExportStatus, artifact: str | None = None) -> None:
        directory = self.cache.export_dir(status.export_id)
        payload = status.to_dict()
        if artifact is not None:
            payload["artifact"] = artifact
        elif (directory / _MANIFEST).exists():
            try:
                old = json.loads((directory / _MANIFEST).read_text())
                if isinstance(old, dict) and old.get("artifact"):
                    payload["artifact"] = old["artifact"]
            except (OSError, ValueError, TypeError):
                pass
        scratch = self.cache.scratch
        with tempfile.TemporaryDirectory(dir=scratch.path, prefix="export-manifest-") as work:
            part = Path(work) / (_MANIFEST + _PART_SUFFIX)
            part.write_text(json.dumps(payload, sort_keys=True, indent=2))
            os.replace(part, directory / _MANIFEST)

    @staticmethod
    def _valid_artifact_path(path: Path, directory: Path) -> bool:
        try:
            return path.resolve().is_relative_to(directory.resolve())
        except OSError:
            return False

    def _remove_directory(self, directory: Path) -> None:
        if not directory.exists():
            return
        # CacheLease creates/open-locks the .lease file and therefore protects
        # a concurrent download response from this removal.
        try:
            export_id = directory.name
            with self.cache.export_lease(export_id, shared=False):
                shutil.rmtree(directory, ignore_errors=True)
        except (OSError, ValueError):
            log.debug("Could not remove export directory %s", directory, exc_info=True)

    # -- admission/status ------------------------------------------------

    @staticmethod
    def _normalize_spec(job_id: str, format: str, max_height: int | None) -> ExportSpec:
        if not job_id:
            raise ValueError("job_id is required")
        fmt = str(format).strip().lower()
        if fmt not in _ALLOWED_FORMATS:
            raise ValueError("format must be opus, mp3, or mp4")
        if max_height is not None:
            max_height = int(max_height)
            if max_height <= 0:
                max_height = None
            else:
                max_height = max(144, min(4320, max_height))
        if fmt != "mp4":
            max_height = None
        return ExportSpec(job_id, fmt, max_height)

    def _active_count_locked(self) -> int:
        return sum(
            status.state in (ExportState.QUEUED, ExportState.BUILDING)
            for status in self._statuses.values()
        )

    def submit(self, job_id: str, format: str, max_height: int | None = None) -> ExportStatus:
        spec = self._normalize_spec(job_id, format, max_height)
        job = self.jobs.get(spec.job_id)
        if job is None:
            raise KeyError(spec.job_id)
        with self._lock:
            if self._closed:
                raise ExportRegistryClosed("export registry is shutting down")
            existing_id = self._dedupe.get(spec.dedupe_key)
            existing = self._statuses.get(existing_id) if existing_id else None
            if existing is not None and existing.state in (
                ExportState.QUEUED, ExportState.BUILDING, ExportState.READY
            ):
                return replace(existing)
            if existing_id is not None:
                self._dedupe.pop(spec.dedupe_key, None)
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
                phase="queued",
                progress=0.0,
                created_at=now,
                updated_at=now,
            )
            self.cache.export_dir(export_id)
            self._statuses[export_id] = status
            self._dedupe[spec.dedupe_key] = export_id
            cancel = threading.Event()
            self._cancel[export_id] = cancel
            self._write_manifest(status)
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
                self._dedupe.pop(spec.dedupe_key, None)
                self._remove_directory(self.cache.export_dir(export_id))
                raise
            return replace(status)

    def get(self, export_id: str) -> ExportStatus | None:
        with self._lock:
            status = self._statuses.get(export_id)
            return replace(status) if status is not None else None

    def open_download(self, export_id: str) -> ExportDownload | None:
        """Take a bounded reader slot and shared lease for a ready artifact."""
        with self._lock:
            status = self._statuses.get(export_id)
            if status is None or status.state is not ExportState.READY:
                return None
            filename = status.filename
        if not filename or Path(filename).name != filename:
            return None
        if not self._download_slots.acquire(blocking=False):
            raise ExportDownloadsFull("too many export downloads in progress")
        try:
            lease = self.cache.export_lease(export_id, shared=True)
            artifact = self.cache.export_dir(export_id) / filename
            with self._lock:
                current = self._statuses.get(export_id)
                if current is None or current.state is not ExportState.READY:
                    lease.close()
                    return None
            if not artifact.is_file() or artifact.stat().st_size <= 0:
                lease.close()
                return None
            return ExportDownload(self._download_slots, lease)
        except BaseException:
            self._download_slots.release()
            raise

    def cancel(self, export_id: str) -> ExportStatus | None:
        with self._lock:
            status = self._statuses.get(export_id)
            if status is None:
                return None
            if status.state in (
                ExportState.READY, ExportState.FAILED, ExportState.EXPIRED, ExportState.CANCELLED
            ):
                return replace(status)
            status.state = ExportState.CANCELLED
            status.phase = "cancelled"
            status.progress = None
            status.error = "cancelled"
            status.updated_at = self._clock()
            event = self._cancel.get(export_id)
            if event is not None:
                event.set()
            self._write_manifest(status)
            return replace(status)

    def progress(self, export_id: str, phase: str, fraction: float | None) -> None:
        with self._lock:
            status = self._statuses.get(export_id)
            if status is None or status.state in (
                ExportState.CANCELLED, ExportState.EXPIRED, ExportState.FAILED
            ):
                return
            status.phase = str(phase)
            status.progress = (
                None if fraction is None else max(0.0, min(1.0, float(fraction)))
            )
            status.updated_at = self._clock()

    def cleanup(self, *, now: float | None = None) -> int:
        now = self._clock() if now is None else now
        expired: list[tuple[str, ExportStatus]] = []
        with self._lock:
            for export_id, status in self._statuses.items():
                if status.state in (ExportState.QUEUED, ExportState.BUILDING):
                    continue
                if status.expires_at is not None and status.expires_at <= now:
                    status.state = ExportState.EXPIRED
                    status.phase = "expired"
                    status.progress = None
                    status.updated_at = now
                    expired.append((export_id, replace(status)))
                    self._dedupe.pop(
                        ExportSpec(status.job_id, status.format, status.max_height).dedupe_key,
                        None,
                    )
        for export_id, status in expired:
            self._remove_directory(self.cache.export_dir(export_id))
            with self._lock:
                self._statuses[export_id] = status
        return len(expired)

    def cancel_all(self) -> None:
        """Cancel active builders before the cache is cleared."""
        with self._lock:
            ids = [
                export_id
                for export_id, status in self._statuses.items()
                if status.state in (ExportState.QUEUED, ExportState.BUILDING)
            ]
        for export_id in ids:
            self.cancel(export_id)

    def clear_all(self) -> None:
        """Invalidate every export when the owning job cache is cleared."""
        with self._lock:
            ids = list(self._statuses)
        for export_id in ids:
            self.cancel(export_id)
        with self._lock:
            for export_id, status in self._statuses.items():
                if status.state not in (ExportState.QUEUED, ExportState.BUILDING):
                    status.state = ExportState.EXPIRED
                    status.phase = "expired"
                    status.progress = None
                    status.updated_at = self._clock()
                    status.expires_at = status.updated_at
            self._dedupe.clear()
            removable = [
                export_id
                for export_id, status in self._statuses.items()
                if status.state is ExportState.EXPIRED
            ]
        for export_id in removable:
            self._remove_directory(self.cache.export_dir(export_id))

    # -- workers/lifecycle -----------------------------------------------

    def _job_state(self, job_id: str) -> str:
        status = self.jobs.get(job_id)
        if status is None:
            return "missing"
        state = getattr(status, "state", status)
        return getattr(state, "value", str(state)).lower()

    def _run(self, export_id: str, spec: ExportSpec, cancel: threading.Event) -> None:
        started = self._clock()
        try:
            while True:
                if cancel.is_set():
                    return
                state = self._job_state(spec.job_id)
                if state == "ready":
                    break
                if state in ("error", "missing"):
                    raise ExportBuildError(
                        "source job is unavailable" if state == "missing" else "source job failed"
                    )
                if self._clock() - started >= self.wait_timeout_seconds:
                    raise ExportBuildError("source job did not become ready before timeout")
                cancel.wait(0.2)
            with self._lock:
                status = self._statuses.get(export_id)
                if status is None or status.state is ExportState.CANCELLED:
                    return
                status.state = ExportState.BUILDING
                status.phase = "building"
                status.progress = 0.0
                status.updated_at = self._clock()
                self._write_manifest(status)
            directory = self.cache.export_dir(export_id)
            with self.cache.export_lease(export_id, shared=False):
                artifact = self.builder(spec, directory, self._progress_for(export_id), cancel)
                if cancel.is_set():
                    return
                self._publish(export_id, artifact, directory)
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
                self._write_manifest(status)
        finally:
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
        if not filename or filename in (".", "..") or filename != path.name:
            raise ExportBuildError("export builder returned an invalid filename")
        now = self._clock()
        with self._lock:
            status = self._statuses.get(export_id)
            if status is None or status.state is ExportState.CANCELLED:
                return
            status.state = ExportState.READY
            status.phase = "ready"
            status.progress = 1.0
            status.filename = filename
            status.media_type = artifact.media_type
            status.size_bytes = size
            status.error = ""
            status.updated_at = now
            status.expires_at = now + self.ttl_seconds
            self._write_manifest(status, path.name)

    def shutdown(self) -> None:
        self.begin_shutdown()
        with self._lock:
            threads = list(self._threads.values())
        for thread in threads:
            thread.join(timeout=min(5.0, self.wait_timeout_seconds))
        with self._lock:
            self._threads.clear()
            self._cancel.clear()

    def begin_shutdown(self) -> None:
        with self._lock:
            self._closed = True
            for event in self._cancel.values():
                event.set()


__all__ = [
    "ExportArtifact", "ExportBuildError", "ExportBuilder", "ExportDownloadsFull",
    "ExportDownload", "ExportQueueFull",
    "ExportRegistry", "ExportRegistryClosed", "ExportSpec", "ExportState",
    "ExportStatus",
]
