"""Run the installed CPU backend with one test-only acquisition adapter.

Only the exact fixture URL receives generated local media; all other URLs are
rejected before acquisition. Processing, Demucs, chunk encoding, SSE, exports,
readiness, and shutdown remain real. This is not a deployment entry point and
does not validate YouTube acquisition. Prefetch the pinned model before use.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fixture import FIXTURE_ID, FIXTURE_URL


def verify_fixture(fixture: Path) -> float:
    manifest = json.loads((fixture / "manifest.json").read_text(encoding="utf-8"))
    duration = manifest.get("duration_seconds")
    if (manifest.get("fixture_url") != FIXTURE_URL or isinstance(duration, bool)
            or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0):
        raise ValueError("Fixture manifest does not match this runner; regenerate with fixture.py")
    for name in ("soundtrack.wav", "clip.mp4"):
        with (fixture / name).open("rb") as source:
            actual = hashlib.file_digest(source, "sha256").hexdigest()
        if actual != manifest.get("sha256", {}).get(name):
            raise ValueError(f"Fixture file {name} does not match its manifest; regenerate with fixture.py")
        probe = json.loads(subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(fixture / name)],
            text=True, timeout=15,
        ))
        actual_duration = float(probe["format"]["duration"])
        expected_types = {"audio", "video"} if name == "clip.mp4" else {"audio"}
        if (abs(actual_duration - duration) > 0.05
                or {stream["codec_type"] for stream in probe["streams"]} != expected_types):
            raise ValueError(f"Fixture file {name} has unexpected duration or streams; regenerate with fixture.py")
    return float(duration)


def install_adapter(fixture: Path, duration: float, record) -> None:
    import nomusic.pipeline.downloader as downloader

    def check_url(url: str) -> None:
        if url != FIXTURE_URL:
            record("source_rejected")
            raise ValueError("The controlled smoke backend only accepts its exact generated fixture URL")

    def metadata(url: str):
        check_url(url)
        record("fixture_metadata")
        return downloader.VideoMetadata(
            id=FIXTURE_ID,
            title="nomusic generated smoke fixture",
            duration_seconds=duration,
            extractor="local-e2e-fixture-adapter",
            webpage_url=FIXTURE_URL,
        )

    def copy_fixture(out_dir: Path, filename: str, progress_hook=None) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        destination = out_dir / filename
        shutil.copyfile(fixture / "clip.mp4", destination)
        size = destination.stat().st_size
        if progress_hook:
            progress_hook({"status": "finished", "downloaded_bytes": size, "total_bytes": size})
        record("fixture_copied", filename=filename, bytes=size)
        return destination

    class FixtureFetcher(downloader.SourceFetcher):
        def extract(self):
            return metadata(self.url)

        def download(self, progress_hook=None):
            check_url(self.url)
            return copy_fixture(self.out_dir, "source.mp4", progress_hook)

    def audio(url, out_dir, *, progress_hook=None):
        check_url(url)
        return copy_fixture(out_dir, "source.mp4", progress_hook)

    def video(url, out_dir, *, max_height=None, progress_hook=None):
        check_url(url)
        if max_height is not None and max_height < 360:
            raise ValueError("The generated fixture supports 360p or higher export requests")
        return copy_fixture(out_dir, "video.mp4", progress_hook)

    downloader.SourceFetcher = FixtureFetcher
    downloader.download_source = audio
    downloader.download_video = video
    downloader.probe = metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True, help="New or empty job cache")
    parser.add_argument("--events", type=Path, required=True, help="New JSONL lifecycle event file")
    parser.add_argument("--port", type=int, default=8723)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    fixture, cache, events = args.fixture.resolve(), args.cache_dir.resolve(), args.events.resolve()
    try:
        duration = verify_fixture(fixture)
        cache.mkdir(parents=True, exist_ok=True)
        if any(cache.iterdir()):
            raise ValueError("Refusing a populated job cache; choose a fresh --cache-dir")
        if events.is_relative_to(cache):
            raise ValueError("Keep --events outside --cache-dir")
        events.parent.mkdir(parents=True, exist_ok=True)
        events.touch(exist_ok=False)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Smoke backend setup failed: {exc}\n")

    event_lock = threading.Lock()

    def record(event: str, **fields: object) -> None:
        with event_lock, events.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"time": time.time(), "event": event, **fields}) + "\n")

    # The test owns its job cache and scratch files. Shared model weights are
    # read from the caller's HF_HUB_CACHE; offline mode forbids a hidden fetch.
    # Ignore personal NOMUSIC_* tuning so this stays the default chunk profile.
    js_runtime = os.environ.get("NOMUSIC_JS_RUNTIME")
    for name in tuple(os.environ):
        if name.startswith("NOMUSIC_"):
            del os.environ[name]
    with tempfile.TemporaryDirectory(prefix="backend-tmp-", dir=cache.parent) as scratch:
        os.environ.update({
            "NOMUSIC_HOST": "127.0.0.1",
            "NOMUSIC_PORT": str(args.port),
            "NOMUSIC_CACHE_DIR": str(cache),
            "NOMUSIC_ENGINE": "demucs",
            "NOMUSIC_DEVICE": "cpu",
            # The fixture adapter is installed in this process. The production
            # supervisor intentionally runs acquisition in a spawned child, so
            # keep this controlled smoke path on the in-process test seam.
            "NOMUSIC_SUPERVISED_WORKER": "0",
            "NOMUSIC_RELOAD": "0",
            "NOMUSIC_CACHE_TTL_DAYS": "0",
            "NOMUSIC_CACHE_SWEEP_INTERVAL_SECONDS": "0",
            "NOMUSIC_MEMORY_GC_INTERVAL_SECONDS": "0",
            "OMP_NUM_THREADS": "2",
            "MKL_NUM_THREADS": "2",
            "HF_HUB_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": scratch,
        })
        if js_runtime:
            os.environ["NOMUSIC_JS_RUNTIME"] = js_runtime
        sys.dont_write_bytecode = True
        # TemporaryDirectory above initialized tempfile's cache before TMPDIR.
        tempfile.tempdir = scratch
        install_adapter(fixture, duration, record)

        # Processor imports acquisition symbols directly, so the adapter must
        # precede the first server import. No app/model behavior is replaced.
        from nomusic import server

        original_lifespan = server.app.router.lifespan_context
        stopped_cleanly = False

        @asynccontextmanager
        async def observed_lifespan(app):
            nonlocal stopped_cleanly
            try:
                async with original_lifespan(app):
                    capabilities = app.state.engine.capabilities()
                    if not capabilities.device.startswith("cpu"):
                        raise RuntimeError(f"Expected CPU profile, got {capabilities.device}")
                    record(
                        "backend_started", pid=os.getpid(), port=args.port,
                        device=capabilities.device, model=capabilities.default_model,
                        fixture_url=FIXTURE_URL, server_module=server.__file__,
                    )
                    yield
            finally:
                remaining = [
                    thread.name for thread in threading.enumerate()
                    if thread.name.startswith(("nomusic-", "nm-decode", "nm-write"))
                ]
                stopped_cleanly = not remaining
                record("backend_stopped", remaining_owned_threads=remaining)
                if remaining:
                    raise RuntimeError(f"Backend shutdown left owned threads: {remaining}")

        server.app.router.lifespan_context = observed_lifespan
        server.main()
        if not stopped_cleanly:
            raise SystemExit("Smoke backend exited without a clean lifespan shutdown")


if __name__ == "__main__":
    main()
