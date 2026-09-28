"""Exercise the installer entrypoint without packages, network, or real GPUs.

Only external executables are faked. The actual shell script chooses profiles,
preserves environments, constructs uv commands, and reports recovery steps.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
PYTHON_VERSION = (ROOT / ".python-version").read_text().strip()


def executable(path, source):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n" + source)
    path.chmod(0o755)


@pytest.fixture
def installer(tmp_path):
    # Spaces and a quote also exercise the shell-escaped recovery command.
    repo = tmp_path / "review's checkout"
    repo.mkdir()
    shutil.copy2(ROOT / "install.sh", repo / "install.sh")
    (repo / ".python-version").write_text(PYTHON_VERSION)
    for name in ("pyproject.toml", "uv.lock"):
        (repo / name).write_text("# fixture; fake uv does not resolve packages\n")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name in ("dirname", "tr", "mv", "rm"):
        (binaries / name).symlink_to(shutil.which(name))
    executable(binaries / "uname", "import os, sys\nprint(os.environ.get('TEST_OS', 'Linux') if sys.argv[1] == '-s' else os.environ.get('TEST_ARCH', 'x86_64'))\n")
    executable(binaries / "sw_vers", "import os\nprint(os.environ.get('TEST_MACOS', '14.0'))\n")
    executable(binaries / "node", "import os\nprint(os.environ.get('TEST_NODE', 'v24.19.0'))\n")
    for name in ("ffmpeg", "ffprobe"):
        executable(binaries / name, "pass\n")
    uv = binaries / "uv"
    executable(uv, r'''
import json
import os
from pathlib import Path
import sys

if sys.argv[1] == "--version":
    print(os.environ.get("TEST_UV_VERSION", "uv 0.12.19"))
    raise SystemExit(0)
with open(os.environ["TEST_UV_CALLS"], "a") as calls:
    calls.write(json.dumps(sys.argv[1:]) + "\n")
env = Path(os.environ["UV_PROJECT_ENVIRONMENT"])
(env / "bin").mkdir(parents=True, exist_ok=True)
(env / "partial-install").touch()
if os.environ.get("TEST_FAIL") == "sync":
    raise SystemExit(23)
(env / "pyvenv.cfg").write_text("fixture = true\n")
version = sys.argv[sys.argv.index("--python") + 1]
profile = sys.argv[sys.argv.index("--extra") + 1]
python = env / "bin/python"
python.write_text("#!" + sys.executable + "\n" + f"VERSION = {version!r}\nPROFILE = {profile!r}\n" + r"""
import os
import sys
from types import ModuleType, SimpleNamespace
if sys.argv[1] == "-c":
    print(VERSION)
else:
    # Execute the installer's real build-reporting code with a fake capability
    # probe, so its mismatch and fallback branches are covered too.
    cuda = os.environ.get('TEST_TORCH_CUDA', '12.6' if PROFILE == 'cu126' else 'none')
    torch = ModuleType('torch')
    torch.__version__ = '2.14.0+' + PROFILE
    torch.version = SimpleNamespace(cuda=None if cuda == 'none' else cuda)
    sys.modules['torch'] = torch
    engine = ModuleType('nomusic.engines.mlx_engine')
    engine._pick_device = lambda: os.environ.get('TEST_DEVICE', 'cpu')
    sys.modules[engine.__name__] = engine
    sys.argv = sys.argv[1:]
    exec(sys.stdin.read(), {'__name__': '__main__'})
""")
python.chmod(0o755)
nomusic = env / "bin/nomusic"
nomusic.write_text("#!" + sys.executable + "\nimport os, sys\nraise SystemExit(24 if os.environ.get('TEST_FAIL') == sys.argv[1] else 0)\n")
nomusic.chmod(0o755)
''')
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("NOMUSIC_", "UV_", "TEST_"))
           and key not in ("PYTHONPATH", "PYTHONHOME")}
    env.update(PATH=str(binaries), NOMUSIC_UV=str(uv),
               TEST_UV_CALLS=str(tmp_path / "uv-calls.jsonl"))
    default = repo / "backend/.venv"

    def old_env(path=default, version="3.11.10"):
        path.mkdir(parents=True)
        (path / "pyvenv.cfg").write_text("fixture = true\n")
        (path / "original").write_text(version)
        executable(path / "bin/python", f"print({version!r})\n")
        return path

    def run(*args, fetch_model=False, **overrides):
        return subprocess.run(
            [shutil.which("bash"), str(repo / "install.sh"),
             "--skip-system-packages", *([] if fetch_model else ["--skip-model-download"]), *args],
            env=env | overrides, text=True, capture_output=True, timeout=10,
        )

    def calls():
        path = Path(env["TEST_UV_CALLS"])
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def nvidia(*, names="Test GPU", cuda="12.6", status=0):
        executable(binaries / "nvidia-smi", f"import sys\nprint({names!r} if len(sys.argv) > 1 else {'CUDA Version: ' + cuda!r})\nraise SystemExit({status})\n")

    return SimpleNamespace(repo=repo, default=default, backup=default.with_name(".venv.bak"),
                           env=env, run=run, old_env=old_env, calls=calls, nvidia=nvidia)


def selected_profile(installer):
    args = installer.calls()[-1]
    return args[args.index("--extra") + 1]


def test_fresh_install_uses_locked_cpu_profile(installer):
    result = installer.run()
    assert result.returncode == 0, result.stderr
    assert (installer.default / "pyvenv.cfg").exists()
    assert not installer.backup.exists()
    args = installer.calls()[0]
    assert "--locked" in args and "--no-editable" in args and "--no-dev" in args
    assert args[args.index("--python") + 1] == PYTHON_VERSION
    assert selected_profile(installer) == "cpu"
    assert "Installed PyTorch 2.14.0+cpu; CUDA build: none" in result.stdout


@pytest.mark.parametrize("old_version", ["3.11.10", "3.12.3", "3.12.13"])
def test_upgrade_preserves_previous_default_environment(installer, old_version):
    installer.old_env(version=old_version)
    result = installer.run()
    assert result.returncode == 0, result.stderr
    assert (installer.backup / "original").read_text() == old_version
    assert not (installer.default / "original").exists()
    assert (installer.default / "pyvenv.cfg").exists()


def test_compatible_rerun_preserves_environment_and_existing_backup(installer):
    installer.old_env(version=PYTHON_VERSION)
    installer.backup.mkdir()
    (installer.backup / "keep").touch()
    result = installer.run("--dev")
    assert result.returncode == 0, result.stderr
    assert (installer.default / "original").read_text() == PYTHON_VERSION
    assert (installer.backup / "keep").exists()
    assert "--group" in installer.calls()[0]


@pytest.mark.parametrize("backup_kind", ["directory", "file", "broken-symlink"])
def test_upgrade_never_overwrites_a_backup(installer, backup_kind):
    installer.old_env()
    if backup_kind == "directory":
        installer.backup.mkdir()
    elif backup_kind == "file":
        installer.backup.touch()
    else:
        installer.backup.symlink_to(installer.repo / "absent")
    result = installer.run()
    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert (installer.default / "original").read_text() == "3.11.10"
    assert installer.calls() == []


@pytest.mark.parametrize("failure", ["sync", "check-runtime", "models"])
def test_failed_upgrade_retains_backup_and_prints_working_restore_command(installer, failure):
    installer.old_env()
    result = installer.run(TEST_FAIL=failure, fetch_model=True)
    assert result.returncode != 0
    assert (installer.backup / "original").read_text() == "3.11.10"
    assert (installer.default / "partial-install").exists()
    command = result.stderr.split("restore it, run:\n  ")[1].strip()
    restored = subprocess.run([shutil.which("bash"), "-c", command], env=installer.env,
                              capture_output=True, text=True, timeout=10)
    assert restored.returncode == 0, restored.stderr
    assert (installer.default / "original").read_text() == "3.11.10"
    assert not installer.backup.exists()


@pytest.mark.parametrize("override", [{"TEST_NODE": "v18.0.0"}, {"TEST_UV_VERSION": "uv 0.1.0"}])
def test_preflight_failure_does_not_move_old_environment(installer, override):
    installer.old_env()
    result = installer.run(**override)
    assert result.returncode != 0
    assert (installer.default / "original").exists()
    assert not installer.backup.exists()
    assert installer.calls() == []


@pytest.mark.parametrize("variable", ["NOMUSIC_VENV", "UV_PROJECT_ENVIRONMENT"])
@pytest.mark.parametrize("explicit_default", [False, True])
def test_incompatible_explicit_environment_is_never_migrated(installer, variable, explicit_default):
    custom = installer.default if explicit_default else installer.repo / "custom"
    installer.old_env(custom)
    result = installer.run(**{variable: str(custom)})
    assert result.returncode != 0
    assert "custom environment" in result.stderr
    assert (custom / "original").read_text() == "3.11.10"
    assert not installer.backup.exists()
    assert installer.calls() == []


def test_new_custom_environment_can_be_installed(installer):
    custom = installer.repo / "custom"
    result = installer.run(NOMUSIC_VENV=str(custom))
    assert result.returncode == 0, result.stderr
    assert (custom / "pyvenv.cfg").exists()
    assert not installer.default.exists()


def test_compatible_custom_environment_can_be_updated(installer):
    custom = installer.old_env(installer.repo / "custom", PYTHON_VERSION)
    result = installer.run(NOMUSIC_VENV=str(custom))
    assert result.returncode == 0, result.stderr
    assert (custom / "original").read_text() == PYTHON_VERSION
    assert not installer.backup.exists()


@pytest.mark.parametrize("kind", ["directory", "file", "symlink", "broken-symlink"])
def test_non_venv_or_symlink_is_not_replaced(installer, kind):
    installer.default.parent.mkdir()
    if kind == "directory":
        installer.default.mkdir()
    elif kind == "file":
        installer.default.touch()
    else:
        target = installer.repo / "target"
        if kind == "symlink":
            installer.old_env(target)
        installer.default.symlink_to(target)
    result = installer.run()
    assert result.returncode != 0
    assert "has not been changed" in result.stderr
    assert not installer.backup.exists()
    assert installer.calls() == []


@pytest.mark.parametrize("cuda", ["12.6", "13.0"])
def test_automatic_cuda_requires_successful_device_and_driver_checks(installer, cuda):
    installer.nvidia(cuda=cuda)
    result = installer.run(TEST_DEVICE="cuda")
    assert result.returncode == 0, result.stderr
    assert selected_profile(installer) == "cu126"
    assert "CUDA build: 12.6; automatic device: cuda" in result.stdout


@pytest.mark.parametrize("options", [{"status": 1}, {"names": ""}, {"cuda": "12.5"}, {"cuda": "N/A"}])
def test_unusable_nvidia_detection_falls_back_to_cpu(installer, options):
    installer.nvidia(**options)
    result = installer.run()
    assert result.returncode == 0, result.stderr
    assert selected_profile(installer) == "cpu"
    assert "selecting CPU" in result.stderr


def test_explicit_cpu_overrides_gpu_detection(installer):
    installer.nvidia()
    result = installer.run("--profile", "cpu")
    assert result.returncode == 0, result.stderr
    assert selected_profile(installer) == "cpu"


def test_explicit_cuda_installs_without_gpu_and_warns_about_cpu_fallback(installer):
    result = installer.run("--profile=cu126")
    assert result.returncode == 0, result.stderr
    assert selected_profile(installer) == "cu126"
    assert "falls back to CPU" in result.stderr


def test_profile_switch_requests_only_the_selected_extra(installer):
    first = installer.run("--profile", "cu126", "--dev")
    second = installer.run("--profile", "cpu", "--dev")
    assert first.returncode == second.returncode == 0
    assert [args[args.index("--extra") + 1] for args in installer.calls()] == ["cu126", "cpu"]
    assert all(args.count("--extra") == 1 and "--group" in args for args in installer.calls())
    assert not installer.backup.exists()


def test_installer_rejects_wrong_torch_build(installer):
    result = installer.run("--profile=cu126", TEST_TORCH_CUDA="none")
    assert result.returncode != 0
    assert "Wrong PyTorch build" in result.stderr


def test_legacy_cuda_variable_selects_the_locked_profile(installer):
    result = installer.run(NOMUSIC_CUDA="cu126")
    assert result.returncode == 0, result.stderr
    assert selected_profile(installer) == "cu126"


@pytest.mark.parametrize("args,env", [
    ([], {"NOMUSIC_CUDA": "cu128"}),
    (["--profile", "cpu"], {"NOMUSIC_CUDA": "cu126"}),
    ([], {"NOMUSIC_TORCH": "2.4.1"}),
    (["--profile", "other"], {}),
    (["--profile"], {}),
])
def test_invalid_profile_selection_fails_before_modifying_environment(installer, args, env):
    installer.old_env()
    result = installer.run(*args, **env)
    assert result.returncode != 0
    assert (installer.default / "original").exists()
    assert installer.calls() == []


def test_apple_silicon_uses_native_profile_with_mps_capability(installer):
    result = installer.run(TEST_OS="Darwin", TEST_ARCH="arm64", TEST_DEVICE="mps")
    assert result.returncode == 0, result.stderr
    assert selected_profile(installer) == "cpu"
    assert "native Apple Silicon" in result.stdout
    assert "automatic device: mps" in result.stdout


@pytest.mark.parametrize("args,extra", [(["--profile", "cu126"], {}), ([], {"TEST_MACOS": "13.7"})])
def test_mac_profile_errors_preserve_old_environment(installer, args, extra):
    installer.old_env()
    result = installer.run(*args, TEST_OS="Darwin", TEST_ARCH="arm64", **extra)
    assert result.returncode != 0
    assert (installer.default / "original").exists()
    assert installer.calls() == []
