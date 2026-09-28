"""External tools needed by source acquisition and media processing.

These checks describe installation prerequisites. They do not load the model
or claim service readiness; the processing smoke verifies the complete path.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from importlib.metadata import version


@dataclass(frozen=True)
class JavaScriptRuntime:
    name: str
    path: str
    version: str


def _version_output(executable: str, flag: str = "--version") -> str:
    try:
        result = subprocess.run(
            [executable, flag], capture_output=True, text=True, check=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"Could not run {executable!r} {flag}: {error}") from error
    return result.stdout.strip()


def _identify_runtime(path: str) -> JavaScriptRuntime:
    output = _version_output(path)
    first_line = output.splitlines()[0] if output else "(no version output)"
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", first_line)
    if match and int(match[1]) >= 22:
        return JavaScriptRuntime("node", path, first_line.removeprefix("v"))
    match = re.match(r"deno (\d+)\.(\d+)\.(\d+)(?:\s|$)", first_line)
    if match and tuple(map(int, match.groups())) >= (2, 3, 0):
        return JavaScriptRuntime("deno", path, ".".join(match.groups()))
    raise RuntimeError(
        f"Unsupported JavaScript runtime at {path!r}: {first_line!r}. "
        "Install Node.js 22+ or Deno 2.3+."
    )


def javascript_runtime() -> JavaScriptRuntime:
    """Select a supported runtime, identifying overrides by version output.

    A binary called nodejs or a custom absolute path is still passed to yt-dlp
    as a Node runtime. An old Deno on PATH does not hide a supported Node.
    """
    override = os.environ.get("NOMUSIC_JS_RUNTIME")
    if override:
        path = shutil.which(os.path.expanduser(override))
        if path is None:
            raise RuntimeError(f"NOMUSIC_JS_RUNTIME executable not found: {override!r}")
        return _identify_runtime(path)
    failures = []
    for name in ("deno", "node", "nodejs"):
        path = shutil.which(name)
        if path:
            try:
                return _identify_runtime(path)
            except RuntimeError as error:
                failures.append(str(error))
    details = " ".join(failures)
    raise RuntimeError(
        "YouTube processing requires Node.js 22+ or Deno 2.3+ on PATH. "
        "NOMUSIC_JS_RUNTIME can select an executable explicitly. " + details
    )


def check_runtime() -> dict[str, object]:
    """Check binaries and report the installed acquisition packages."""
    binaries = {}
    for name in ("ffmpeg", "ffprobe"):
        path = shutil.which(name)
        if path is None:
            raise RuntimeError(f"{name} not found on PATH; install FFmpeg first.")
        binaries[name] = {"path": path, "version": _version_output(path, "-version").splitlines()[0]}
    return {
        **binaries,
        "javascript": asdict(javascript_runtime()),
        "yt-dlp": version("yt-dlp"),
        "yt-dlp-ejs": version("yt-dlp-ejs"),
    }
