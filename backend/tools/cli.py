"""Command-line driver for local testing.

Usage:
    backend/.venv/bin/python -m backend.tools.cli <url> [--model M] [--stems vocals,other]

Runs the full pipeline (download -> separate -> chunk -> cache) without touching
the HTTP server. Useful for benchmarking and debugging.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from config import SETTINGS  # noqa: E402
from engines import get_engine  # noqa: E402
from pipeline.cache import JobCache  # noqa: E402
from pipeline.processor import Processor  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="nomusic local CLI")
    parser.add_argument("url")
    parser.add_argument("--model", default=None)
    parser.add_argument(
        "--stems", default=None, help="comma-separated stem names to keep"
    )
    parser.add_argument("--engine", default=SETTINGS.engine_name)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    engine = get_engine(args.engine)
    cache = JobCache(SETTINGS.cache_dir)
    processor = Processor(
        engine=engine,
        cache=cache,
        chunk_seconds=SETTINGS.chunk_seconds,
        chunk_overlap_seconds=SETTINGS.chunk_overlap_seconds,
    )
    keep_stems = (
        [s.strip() for s in args.stems.split(",")]
        if args.stems
        else list(SETTINGS.default_keep_stems)
    )
    model = args.model or engine.capabilities().default_model

    t0 = time.time()

    def on_progress(meta):
        elapsed = time.time() - t0
        print(
            f"[{elapsed:5.1f}s] chunk {len(meta.chunks_ready)}/{meta.total_chunks}"
        )

    key = processor.run(
        args.url, model=model, keep_stems=keep_stems, on_progress=on_progress
    )
    meta = cache.load_meta(key)
    full = cache.full_path(key)
    print(f"\ncache key:  {key}")
    print(f"complete:   {meta.complete if meta else False}")
    print(f"full audio: {full if full.exists() else '(not yet written)'}")
    print(f"elapsed:    {time.time() - t0:.1f}s for {meta.duration_seconds:.1f}s of source")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
