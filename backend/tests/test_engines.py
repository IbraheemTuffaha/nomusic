"""Tests for the engine registry (engines/__init__.py) and the CLI parser.

These don't load model weights — ``get_engine`` instantiates lazily and the
parser test never touches the pipeline.
"""

from __future__ import annotations

import threading
import time

import pytest

import engines
from engines.base import Engine
from tools.cli import build_parser


def test_get_engine_known_name():
    assert isinstance(engines.get_engine("mlx"), Engine)


def test_get_engine_demucs_alias():
    assert isinstance(engines.get_engine("demucs"), Engine)


def test_get_engine_unknown_raises():
    with pytest.raises(ValueError):
        engines.get_engine("no-such-engine")


def test_ensure_loaded_loads_once_under_concurrency():
    # The startup warmup thread and the first job's decode thread can both miss
    # the cache and call _ensure_loaded at the same time; the load must happen
    # exactly once (no duplicate weight download / double GPU copy). A lockless
    # check-then-set would invoke the factory more than once here.
    from engines.mlx_engine import MLXEngine

    calls: list[str] = []
    calls_lock = threading.Lock()
    n = 6
    ready = threading.Barrier(n)

    def slow_factory(model_name: str, device: str) -> object:
        # Hold the critical section briefly so a missing lock would let other
        # threads slip past the cache check.
        time.sleep(0.02)
        with calls_lock:
            calls.append(model_name)
        return object()

    eng = MLXEngine(separator_factory=slow_factory)
    results: list[object] = []
    results_lock = threading.Lock()

    def load() -> None:
        ready.wait()  # release all threads into _ensure_loaded together
        sep = eng._ensure_loaded("htdemucs")
        with results_lock:
            results.append(sep)

    threads = [threading.Thread(target=load) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1  # factory ran exactly once despite the race
    assert len(results) == n and all(r is results[0] for r in results)


def test_cli_parser_defaults():
    args = build_parser().parse_args(["http://example.com/v"])
    assert args.url == "http://example.com/v"
    assert args.model is None
    assert args.stems is None


def test_cli_parser_overrides():
    args = build_parser().parse_args(
        ["u", "--stems", "vocals,other", "--model", "htdemucs", "--engine", "demucs"]
    )
    assert args.stems == "vocals,other"
    assert args.model == "htdemucs"
    assert args.engine == "demucs"
