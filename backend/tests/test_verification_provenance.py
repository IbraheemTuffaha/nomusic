"""The release verifier must fail if pytest substitutes checkout modules."""

from pathlib import Path
import os
import shutil
import subprocess
import sys

import pytest


@pytest.fixture
def suite(tmp_path):
    installed = tmp_path / "site/nomusic"
    source = tmp_path / "backend/nomusic"
    tests = tmp_path / "backend/tests"
    for package in (installed, source, tests):
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
    shutil.copyfile(Path(__file__).with_name("conftest.py"), tests / "conftest.py")
    (tests / "test_example.py").write_text("def test_example():\n    import nomusic\n")
    env = {key: value for key, value in os.environ.items()
           if key not in ("PYTHONPATH", "PYTHONHOME", "PYTEST_ADDOPTS")}
    env.update({"PYTHONPATH": str(installed.parent), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "NOMUSIC_TEST_INSTALLED_ROOT": str(installed)})

    def run(mode="importlib", *, shadow=False, enforce=True, code=None):
        if shadow:
            env["PYTHONPATH"] = os.pathsep.join((str(source.parent), str(installed.parent)))
        if not enforce:
            env.pop("NOMUSIC_TEST_INSTALLED_ROOT")
        if code:
            (tests / "test_example.py").write_text(code)
        return subprocess.run(
            [sys.executable, "-m", "pytest", "backend/tests", f"--import-mode={mode}", "-q"],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
        )

    return run


def test_importlib_uses_installed_package(suite):
    result = suite()
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("mode,shadow", [("prepend", False), ("importlib", True)])
def test_checkout_shadowing_fails_inside_pytest(suite, mode, shadow):
    result = suite(mode, shadow=shadow)
    assert result.returncode != 0
    assert "Installed-package provenance failed" in result.stdout + result.stderr


@pytest.mark.parametrize("during_test", [False, True], ids=["collection", "execution"])
def test_submodule_from_other_directory_is_rejected(suite, during_test):
    code = (
        "import sys, types\n"
        "foreign = types.ModuleType('nomusic.foreign')\n"
        "foreign.__file__ = __file__\n"
        "sys.modules['nomusic.foreign'] = foreign\n"
    )
    if during_test:
        code = "def test_example():\n" + "".join("    " + line for line in code.splitlines(True))
    else:
        code += "def test_example(): pass\n"
    result = suite(code=code)
    assert result.returncode != 0
    assert "nomusic.foreign loaded outside" in result.stdout + result.stderr


def test_development_runs_can_use_source(suite):
    result = suite("prepend", enforce=False)
    assert result.returncode == 0, result.stdout + result.stderr
