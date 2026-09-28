"""Local, offline diagnostics. No API process or source website is required.

Readiness uses only the lightweight storage helper here. Doctor is an explicit
operator command: it verifies cached model hashes and, by default, runs real
inference. Nothing runs at import time.
"""

from __future__ import annotations

import os
import math
import platform
import shutil
import tempfile
import time
import wave
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from typing import Any, Callable

from nomusic.config import Settings
from nomusic.engines import get_engine
from nomusic.engines.model_store import fetch_model_files
from nomusic.runtime import check_runtime


def check_storage(root: Path) -> dict[str, Any]:
    """Exercise actual file operations as the service user; leave no probe files.

    os.access/mode bits alone miss ACLs, read-only mounts and exhausted storage.
    Creating a missing data directory is intentional; existing data is untouched.
    """
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".nomusic-check-", dir=root) as directory:
        original = Path(directory) / "write"
        renamed = Path(directory) / "read"
        with original.open("wb") as stream:
            stream.write(b"nomusic storage check\n")
            stream.flush()
            os.fsync(stream.fileno())
        original.replace(renamed)
        if renamed.read_bytes() != b"nomusic storage check\n":
            raise RuntimeError("Storage check read back different bytes")
        renamed.unlink()
    return {"path": str(root.resolve()), "free_bytes": shutil.disk_usage(root).free}


def check_working_storage(settings: Settings) -> dict[str, Any]:
    return {
        "cache": check_storage(settings.cache_dir),
        "temporary": check_storage(Path(tempfile.gettempdir())),
    }


def inference_smoke(engine, model: str, directory: Path) -> dict[str, Any]:
    """Decode and separate two seconds of generated sound; validate every stem.

    This checks execution and output integrity, not intelligibility, separation
    quality, sustained throughput, or availability of any remote source.
    """
    import numpy as np

    rate, seconds = 44100, 2
    samples = rate * seconds
    t = np.arange(samples) / rate
    signal = 0.15 * np.sin(2 * np.pi * 173 * t) + 0.04 * np.sin(2 * np.pi * 347 * t)
    stereo = np.column_stack((signal, signal * 0.95))
    path = directory / "input.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes((stereo * 32767).astype("<i2").tobytes())
    started = time.perf_counter()
    result = engine.infer(engine.prepare(path, model=model))
    if (result.sample_rate <= 0 or not math.isfinite(result.duration_seconds)
            or abs(result.duration_seconds - seconds) > 0.01):
        raise RuntimeError("Inference returned an invalid duration/sample rate")
    expected = round(seconds * result.sample_rate)
    stems = {}
    for name in engine.capabilities().supported_stems:
        audio = np.asarray(result.stems.get(name))
        if audio.shape != (expected, 2) or not np.isfinite(audio).all():
            raise RuntimeError(f"Inference returned incomplete or non-finite {name} audio")
        stems[name] = {"frames": len(audio), "rms": float(np.sqrt(np.mean(audio.astype('float64') ** 2)))}
    if not stems or not any(stem["rms"] > 1e-8 for stem in stems.values()):
        raise RuntimeError("Inference returned no signal for the non-silent test input")
    return {
        "input_seconds": seconds, "elapsed_seconds": round(time.perf_counter() - started, 3),
        "sample_rate": result.sample_rate, "stems": stems,
    }


def doctor(settings: Settings, *, model: str | None = None, skip_inference: bool = False) -> dict[str, Any]:
    """Collect independent failures, then run inference only if prerequisites pass."""
    checks: list[dict[str, Any]] = []

    def check(name: str, action: Callable[[], Any], remedy: str) -> Any:
        try:
            details = action()
        except Exception as error:
            checks.append({"name": name, "status": "failed", "error": str(error), "remedy": remedy})
            return None
        checks.append({"name": name, "status": "passed", "details": details})
        return details

    check("runtime", check_runtime, "Install FFmpeg (ffmpeg and ffprobe) and Node.js 22+ or Deno 2.3+; check PATH/NOMUSIC_JS_RUNTIME.")
    check("packages", lambda: {name: version(name) for name in (
        "nomusic", "torch", "demucs", "numpy", "soundfile", "fastapi",
    )}, "Reinstall using the documented locked installation profile.")
    check("storage", lambda: check_working_storage(settings), "Choose writable NOMUSIC_CACHE_DIR and temporary storage (TMPDIR); check free space and permissions.")
    engine = None

    def select_engine():
        nonlocal engine, model
        engine = get_engine(settings.engine_name, local_files_only=True)
        caps = engine.capabilities()
        model = model or caps.default_model
        if model not in caps.supported_models:
            raise ValueError(f"Model {model!r} is not supported by {settings.engine_name}")
        return {**asdict(caps), "selected_model": model,
                "requested_device": os.environ.get("NOMUSIC_DEVICE", "auto")}

    selected = check("engine", select_engine, "Check NOMUSIC_ENGINE/NOMUSIC_DEVICE and the installed PyTorch profile; use NOMUSIC_DEVICE=cpu for a CPU check.")
    if selected is not None:
        check("model", lambda: {
            "model": model, "sha256_verified": True, "local_files_only": True,
            "files": {name: str(path) for name, path in fetch_model_files(model, local_files_only=True).items()},
        }, f"Run nomusic models fetch --model {model} to install the pinned files; follow any corrupt-file error before retrying.")
    else:
        checks.append({"name": "model", "status": "skipped", "reason": "Engine selection failed"})
    if skip_inference or any(row["status"] == "failed" for row in checks):
        checks.append({"name": "inference", "status": "skipped", "reason":
                       "Requested --skip-inference" if skip_inference else "A prerequisite failed"})
    else:
        def smoke():
            with tempfile.TemporaryDirectory(prefix="nomusic-doctor-") as directory:
                return inference_smoke(engine, model, Path(directory))
        check("inference", smoke, "Inspect the error, verify the locked runtime/model installation and available memory; retry with NOMUSIC_DEVICE=cpu if diagnosing acceleration.")
    return {
        "ok": not any(row["status"] == "failed" for row in checks),
        "inference_verified": any(row["name"] == "inference" and row["status"] == "passed" for row in checks),
        "python": platform.python_version(), "platform": platform.platform(),
        "scope": "Local installation only; remote-source access and audio quality are not tested.",
        "checks": checks,
    }


def format_report(report: dict[str, Any]) -> str:
    lines = [f"nomusic doctor — Python {report['python']} on {report['platform']}"]
    for row in report["checks"]:
        lines.append(f"[{row['status'].upper()}] {row['name']}")
        if row["status"] == "failed":
            lines.extend((f"  {row['error']}", f"  Fix: {row['remedy']}"))
        elif row["status"] == "skipped":
            lines.append(f"  {row['reason']}")
        else:
            # JSON preserves structured versions/paths without a second schema.
            import json
            lines.append("  " + json.dumps(row["details"], ensure_ascii=False))
    lines.append("Local checks passed." if report["ok"] else "Local checks failed; fix the reported problems and retry.")
    if not report["inference_verified"]:
        lines.append("Inference was NOT verified.")
    lines.append(report["scope"])
    return "\n".join(lines)
