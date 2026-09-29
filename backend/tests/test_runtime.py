"""Real selection rules around fake executable version responses."""

import pytest

from nomusic import runtime


def test_old_deno_does_not_hide_supported_node(monkeypatch):
    monkeypatch.delenv("NOMUSIC_JS_RUNTIME", raising=False)
    monkeypatch.setattr(runtime.shutil, "which", lambda name: f"/tools/{name}")
    monkeypatch.setattr(runtime, "_version_output", lambda path: "deno 1.40.0" if path.endswith("deno") else "v24.19.0")
    selected = runtime.javascript_runtime()
    assert (selected.name, selected.path, selected.version) == ("node", "/tools/node", "24.19.0")


def test_override_is_identified_by_version_not_filename(monkeypatch):
    monkeypatch.setenv("NOMUSIC_JS_RUNTIME", "/tools/custom-nodejs")
    monkeypatch.setattr(runtime.shutil, "which", lambda name: name)
    monkeypatch.setattr(runtime, "_version_output", lambda path: "v22.0.0")
    assert runtime.javascript_runtime().name == "node"


@pytest.mark.parametrize("output", ["v20.18.0", "deno 2.2.0", "1.3.14"])
def test_unsupported_explicit_runtime_is_actionable(monkeypatch, output):
    monkeypatch.setenv("NOMUSIC_JS_RUNTIME", "/tools/javascript")
    monkeypatch.setattr(runtime.shutil, "which", lambda name: name)
    monkeypatch.setattr(runtime, "_version_output", lambda path: output)
    with pytest.raises(RuntimeError, match=r"Node.js 22\+ or Deno 2.3\+"):
        runtime.javascript_runtime()


def test_missing_ffmpeg_is_reported_before_other_checks(monkeypatch):
    monkeypatch.setattr(runtime.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="ffmpeg not found"):
        runtime.check_runtime()
