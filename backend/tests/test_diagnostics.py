"""Diagnostic failures and output validation without loading model weights."""

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import wave

import numpy as np
import pytest

from nomusic import diagnostics
from nomusic.__main__ import main
from nomusic.config import Settings
from nomusic.engines.base import EngineCapabilities, SeparationResult


class TestEngine:
    __test__ = False

    def capabilities(self):
        return EngineCapabilities("demucs", "cpu", ("htdemucs",), "htdemucs")

    def prepare(self, path, *, model):
        assert model == "htdemucs"
        with wave.open(str(path)) as audio:
            assert audio.getnchannels() == 2
            assert audio.getnframes() == 88200
        return path

    def infer(self, prepared):
        return SeparationResult(
            {name: np.full((88200, 2), 0.01, dtype=np.float32)
             for name in self.capabilities().supported_stems},
            44100, 2.0,
        )


@pytest.fixture
def environment(monkeypatch, tmp_path):
    monkeypatch.setattr(diagnostics, "check_runtime", lambda: {"ffmpeg": "test-version"})
    monkeypatch.setattr(diagnostics, "version", lambda name: "test-version")
    engine = TestEngine()
    calls = []

    def select(name, *, local_files_only):
        assert local_files_only is True
        return engine

    def files(name, *, local_files_only):
        calls.append((name, local_files_only))
        assert local_files_only is True
        return {"test.safetensors": tmp_path / "weights"}

    monkeypatch.setattr(diagnostics, "get_engine", select)
    monkeypatch.setattr(diagnostics, "fetch_model_files", files)
    return SimpleNamespace(settings=replace(Settings(), cache_dir=tmp_path / "cache"),
                           engine=engine, model_calls=calls)


def row(report, name):
    return next(r for r in report["checks"] if r["name"] == name)


def test_doctor_checks_local_files_and_inference_without_server(environment):
    report = diagnostics.doctor(environment.settings)
    assert report["ok"] and report["inference_verified"]
    assert environment.model_calls == [("htdemucs", True)]
    assert row(report, "inference")["details"]["sample_rate"] == 44100
    assert list(environment.settings.cache_dir.iterdir()) == []


def test_fast_report_does_not_claim_inference(environment, monkeypatch):
    monkeypatch.setattr(environment.engine, "infer", lambda _: pytest.fail("inference ran"))
    report = diagnostics.doctor(environment.settings, skip_inference=True)
    assert report["ok"] and not report["inference_verified"]
    assert "NOT verified" in diagnostics.format_report(report)


def test_reports_independent_failures_and_skips_inference(environment, monkeypatch):
    def missing_runtime():
        raise RuntimeError("ffmpeg not found on PATH")

    def missing_model(*args, **kwargs):
        raise FileNotFoundError("pinned model is absent")

    monkeypatch.setattr(diagnostics, "check_runtime", missing_runtime)
    monkeypatch.setattr(diagnostics, "fetch_model_files", missing_model)
    monkeypatch.setattr(environment.engine, "infer", lambda _: pytest.fail("inference ran"))
    report = diagnostics.doctor(environment.settings)
    assert not report["ok"]
    assert row(report, "runtime")["status"] == "failed"
    assert row(report, "model")["status"] == "failed"
    assert "nomusic models fetch --model htdemucs" in row(report, "model")["remedy"]
    assert row(report, "inference")["status"] == "skipped"


def test_unavailable_device_is_actionable_and_does_not_fetch_model(environment, monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("NOMUSIC_DEVICE=cuda is unavailable")
    monkeypatch.setattr(diagnostics, "get_engine", unavailable)
    report = diagnostics.doctor(environment.settings)
    assert not report["ok"] and environment.model_calls == []
    assert "NOMUSIC_DEVICE=cpu" in row(report, "engine")["remedy"]


def test_actual_storage_probe_keeps_existing_data_and_removes_its_files(tmp_path):
    (tmp_path / "keep").write_bytes(b"existing data")
    result = diagnostics.check_storage(tmp_path)
    assert result["free_bytes"] > 0
    assert list(tmp_path.iterdir()) == [tmp_path / "keep"]
    assert (tmp_path / "keep").read_bytes() == b"existing data"


def test_denied_write_is_reported_even_when_directory_exists(environment, monkeypatch):
    original = Path.open
    def denied(path, *args, **kwargs):
        if path.name == "write":
            raise PermissionError("test read-only storage")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", denied)
    report = diagnostics.doctor(environment.settings)
    assert not report["ok"]
    assert "permissions" in row(report, "storage")["remedy"]
    assert row(report, "inference")["status"] == "skipped"


@pytest.mark.parametrize("fault", ["short", "nan", "missing", "silent", "duration"])
def test_inference_integrity_failure_is_not_success(environment, monkeypatch, fault):
    result = environment.engine.infer(None)
    if fault == "short":
        result.stems["vocals"] = result.stems["vocals"][:-100]
    elif fault == "nan":
        result.stems["vocals"][0, 0] = np.nan
    elif fault == "missing":
        del result.stems["vocals"]
    elif fault == "silent":
        for audio in result.stems.values():
            audio[:] = 0
    else:
        result = replace(result, duration_seconds=0.5)
    monkeypatch.setattr(environment.engine, "infer", lambda _: result)
    report = diagnostics.doctor(environment.settings)
    assert not report["ok"] and not report["inference_verified"]
    assert row(report, "inference")["status"] == "failed"


def test_cli_json_failure_has_nonzero_exit(environment, monkeypatch, capsys):
    def fail():
        raise RuntimeError("ffprobe absent")
    monkeypatch.setattr(diagnostics, "check_runtime", fail)
    assert main(["doctor", "--json", "--skip-inference"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert not report["ok"]
    assert row(report, "runtime")["status"] == "failed"


def test_cli_fast_success_is_explicit(environment, capsys):
    assert main(["doctor", "--skip-inference"]) == 0
    assert "Inference was NOT verified" in capsys.readouterr().out
