"""Tests for the engine registry (engines/__init__.py) and the CLI parser.

These don't load model weights — ``get_engine`` instantiates lazily and the
parser test never touches the pipeline.
"""

from __future__ import annotations

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
