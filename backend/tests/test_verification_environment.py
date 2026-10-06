"""Local pytest preferences must not turn the full verifier into a subset run."""

from pathlib import Path
import runpy
import subprocess
import sys
import xml.etree.ElementTree as ET


def test_verification_ignores_ambient_pytest_selection_and_plugins(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[2] / "scripts/verify.py"
    verification_env = runpy.run_path(str(script))["verification_env"]
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k kept")
    monkeypatch.setenv("PYTEST_PLUGINS", "nonexistent_local_plugin")
    monkeypatch.setenv("PYTEST_DEBUG", "1")
    env = verification_env(tmp_path, tmp_path)
    (tmp_path / "test_example.py").write_text(
        "def test_kept():\n    pass\n\n"
        "def test_excluded():\n    assert False, 'must run despite the ambient filter'\n"
    )
    report = tmp_path / "results.xml"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "test_example.py", "--import-mode=importlib",
         "-q", f"--junitxml={report}"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    cases = ET.parse(report).findall(".//testcase")
    assert {case.get("name") for case in cases} == {"test_kept", "test_excluded"}
    assert sum(case.find("failure") is not None for case in cases) == 1
    assert not any(key.startswith("PYTEST_") for key in env)
