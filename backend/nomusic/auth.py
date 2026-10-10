"""Local operator-key storage and request authentication.

The backend deliberately has no account database.  A trusted operator creates
high-entropy bearer keys with ``nomusic auth generate`` and gives a key to an
approved extension installation.  Only SHA-256 digests are persisted, so a
copy of the local file cannot be used as a credential.  Revocation is read
from the file on every authentication attempt; a running server therefore
honours a local revoke/rotate command without a restart.

The storage file is an administrative artifact.  It is created with owner-only
permissions and never belongs in the extension, a page bridge, a URL, or a
normal application log.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


KEY_PREFIX = "nm_"
KEY_BYTES = 32
_KEY_RE = re.compile(r"\Anm_[0-9a-f]{64}\Z")
_KEY_ID_RE = re.compile(r"\A[0-9a-f]{16}\Z")
_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_SCHEMA_VERSION = 1


class AuthError(RuntimeError):
    """Base class for configuration and credential failures."""


class AuthConfigurationError(AuthError):
    """The server cannot enforce authentication with its current config."""


class InvalidCredential(AuthError):
    """The supplied bearer credential is absent or does not match a key."""


class RevokedCredential(AuthError):
    """The supplied credential was intentionally revoked."""


class KeyNotFound(AuthError):
    """An administrative operation referenced an unknown key id."""


@dataclass(frozen=True)
class AuthPrincipal:
    key_id: str
    label: str | None
    created_at: str


@dataclass(frozen=True)
class KeyInfo:
    key_id: str
    label: str | None
    created_at: str
    revoked_at: str | None = None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _label(value: str | None) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    if len(value) > 80 or any(ord(char) < 32 for char in value):
        raise ValueError("key label must be at most 80 printable characters")
    return value


def _digest(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("ascii")).hexdigest()


class AuthStore:
    """Owner-controlled key file with atomic writes and revocation checks."""

    def __init__(self, path: Path | str, *, required: bool = True) -> None:
        self.path = Path(path).expanduser()
        self.required = bool(required)
        self._lock = threading.RLock()

    # -- file format -----------------------------------------------------

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": _SCHEMA_VERSION, "keys": []}

    def _check_file_permissions(self, path: Path) -> None:
        if os.name != "posix" or not path.exists():
            return
        try:
            mode = path.stat().st_mode & 0o777
        except OSError as exc:
            raise AuthConfigurationError(
                f"Cannot inspect operator key file {path}: {exc}"
            ) from exc
        if mode & 0o077:
            raise AuthConfigurationError(
                f"Operator key file {path} is readable by other users; "
                "run chmod 600 on it before starting nomusic."
            )

    @staticmethod
    def _validate_key(entry: Any) -> dict[str, Any]:
        if not isinstance(entry, dict):
            raise AuthConfigurationError("operator key file contains a non-object key")
        key_id = entry.get("id")
        digest = entry.get("digest")
        created_at = entry.get("created_at")
        revoked_at = entry.get("revoked_at")
        if not isinstance(key_id, str) or not _KEY_ID_RE.fullmatch(key_id):
            raise AuthConfigurationError("operator key file contains an invalid key id")
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            raise AuthConfigurationError("operator key file contains an invalid key digest")
        if not isinstance(created_at, str) or not created_at:
            raise AuthConfigurationError("operator key file contains an invalid creation time")
        if revoked_at is not None and (not isinstance(revoked_at, str) or not revoked_at):
            raise AuthConfigurationError("operator key file contains an invalid revocation time")
        label = entry.get("label")
        if label is not None and (not isinstance(label, str) or len(label) > 80):
            raise AuthConfigurationError("operator key file contains an invalid label")
        return {
            "id": key_id,
            "digest": digest,
            "label": label,
            "created_at": created_at,
            "revoked_at": revoked_at,
        }

    def _read(self, *, allow_missing: bool = False) -> dict[str, Any]:
        if not self.path.exists():
            if allow_missing:
                return self._empty()
            raise AuthConfigurationError(
                f"No operator keys are configured at {self.path}. "
                "Run 'nomusic auth generate' before starting the backend."
            )
        if not self.path.is_file():
            raise AuthConfigurationError(f"Operator key path is not a regular file: {self.path}")
        self._check_file_permissions(self.path)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AuthConfigurationError(f"Cannot read operator key file {self.path}: {exc}") from exc
        if not isinstance(data, dict) or data.get("version") != _SCHEMA_VERSION:
            raise AuthConfigurationError("Unsupported operator key file format")
        entries = data.get("keys")
        if not isinstance(entries, list):
            raise AuthConfigurationError("operator key file 'keys' must be a list")
        normalized = [self._validate_key(entry) for entry in entries]
        ids = [entry["id"] for entry in normalized]
        if len(set(ids)) != len(ids):
            raise AuthConfigurationError("operator key file contains duplicate key ids")
        digests = [entry["digest"] for entry in normalized]
        if len(set(digests)) != len(digests):
            raise AuthConfigurationError("operator key file contains duplicate key digests")
        return {"version": _SCHEMA_VERSION, "keys": normalized}

    def _write(self, data: dict[str, Any]) -> None:
        parent = self.path.parent
        parent_existed = parent.exists()
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix" and not parent_existed:
            try:
                os.chmod(parent, 0o700)
            except OSError:
                pass
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(data, file, indent=2, sort_keys=True)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            if os.name == "posix":
                os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            if os.name == "posix":
                os.chmod(self.path, 0o600)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    @staticmethod
    def _info(entry: dict[str, Any]) -> KeyInfo:
        return KeyInfo(
            key_id=entry["id"],
            label=entry.get("label"),
            created_at=entry["created_at"],
            revoked_at=entry.get("revoked_at"),
        )

    # -- administrative operations -------------------------------------

    def validate(self) -> None:
        """Validate startup configuration and require one active key."""
        with self._lock:
            data = self._read(allow_missing=not self.required)
            if self.required and not any(entry.get("revoked_at") is None for entry in data["keys"]):
                raise AuthConfigurationError(
                    "No active operator keys are configured. Run 'nomusic auth generate' "
                    "or 'nomusic auth rotate'."
                )

    def list_keys(self) -> tuple[KeyInfo, ...]:
        with self._lock:
            return tuple(self._info(entry) for entry in self._read(allow_missing=True)["keys"])

    def generate(self, label: str | None = None) -> tuple[str, KeyInfo]:
        label = _label(label)
        with self._lock:
            data = self._read(allow_missing=True)
            raw = KEY_PREFIX + secrets.token_hex(KEY_BYTES)
            entry = {
                "id": secrets.token_hex(8),
                "digest": _digest(raw),
                "label": label,
                "created_at": _now(),
                "revoked_at": None,
            }
            data["keys"].append(entry)
            self._write(data)
            return raw, self._info(entry)

    def revoke(self, key_id: str) -> KeyInfo:
        if not _KEY_ID_RE.fullmatch(str(key_id)):
            raise KeyNotFound("unknown operator key id")
        with self._lock:
            data = self._read()
            for entry in data["keys"]:
                if entry["id"] == key_id:
                    if entry.get("revoked_at") is None:
                        entry["revoked_at"] = _now()
                        self._write(data)
                    return self._info(entry)
        raise KeyNotFound("unknown operator key id")

    def rotate(
        self, *, label: str | None = None, revoke_id: str | None = None
    ) -> tuple[str, KeyInfo]:
        label = _label(label)
        if revoke_id is not None and not _KEY_ID_RE.fullmatch(str(revoke_id)):
            raise KeyNotFound("unknown operator key id")
        with self._lock:
            data = self._read()
            if revoke_id is not None and not any(
                entry["id"] == revoke_id for entry in data["keys"]
            ):
                raise KeyNotFound("unknown operator key id")
            raw = KEY_PREFIX + secrets.token_hex(KEY_BYTES)
            entry = {
                "id": secrets.token_hex(8),
                "digest": _digest(raw),
                "label": label,
                "created_at": _now(),
                "revoked_at": None,
            }
            data["keys"].append(entry)
            if revoke_id is not None:
                for old in data["keys"]:
                    if old["id"] == revoke_id and old.get("revoked_at") is None:
                        old["revoked_at"] = _now()
                        break
            self._write(data)
            return raw, self._info(entry)

    # -- request authentication ----------------------------------------

    def authenticate(self, raw_key: str | None) -> AuthPrincipal:
        if not self.required:
            return AuthPrincipal("local-anonymous", None, "")
        if not isinstance(raw_key, str) or not _KEY_RE.fullmatch(raw_key.strip()):
            raise InvalidCredential("invalid operator key")
        candidate = _digest(raw_key.strip())
        with self._lock:
            data = self._read()
        revoked: dict[str, Any] | None = None
        active_count = 0
        for entry in data["keys"]:
            if entry.get("revoked_at") is None:
                active_count += 1
            if hmac.compare_digest(entry["digest"], candidate):
                if entry.get("revoked_at") is not None:
                    revoked = entry
                else:
                    return AuthPrincipal(entry["id"], entry.get("label"), entry["created_at"])
        if revoked is not None:
            raise RevokedCredential("operator key has been revoked")
        if active_count == 0:
            raise AuthConfigurationError(
                "No active operator keys are configured. Run 'nomusic auth generate' "
                "or 'nomusic auth rotate'."
            )
        raise InvalidCredential("invalid operator key")

    def authenticate_header(self, header: str | None) -> AuthPrincipal:
        if not self.required:
            return AuthPrincipal("local-anonymous", None, "")
        if not isinstance(header, str):
            raise InvalidCredential("operator key is required")
        scheme, separator, value = header.partition(" ")
        if scheme.lower() != "bearer" or not separator or not value.strip():
            raise InvalidCredential("operator key is required")
        return self.authenticate(value.strip())
