"""Exercise the installer entrypoint without packages, network, or real GPUs.

Only external executables are faked. The actual shell scripts choose profiles,
preserve environments, construct uv commands, report recovery steps, and write
and load the macOS background-service definition.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import pty
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
PYTHON_VERSION = (ROOT / ".python-version").read_text().strip()
MAC = {"TEST_OS": "Darwin", "TEST_ARCH": "arm64"}


def executable(path, source):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n" + source)
    path.chmod(0o755)


@pytest.fixture
def installer(tmp_path):
    # Spaces and a quote also exercise the shell-escaped recovery command.
    repo = tmp_path / "review's checkout"
    repo.mkdir()
    for name in ("install.sh", "service.sh"):
        shutil.copy2(ROOT / name, repo / name)
    (repo / ".python-version").write_text(PYTHON_VERSION)
    for name in ("pyproject.toml", "uv.lock"):
        (repo / name).write_text("# fixture; fake uv does not resolve packages\n")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name in ("dirname", "tr", "mv", "rm", "cat"):
        (binaries / name).symlink_to(shutil.which(name))
    (binaries / "sleep").symlink_to(shutil.which("true"))  # waiting for launchd takes no time here
    executable(binaries / "uname", "import os, sys\nprint(os.environ.get('TEST_OS', 'Linux') if sys.argv[1] == '-s' else os.environ.get('TEST_ARCH', 'x86_64'))\n")
    executable(binaries / "sw_vers", "import os\nprint(os.environ.get('TEST_MACOS', '14.0'))\n")
    executable(binaries / "node", "import os\nprint(os.environ.get('TEST_NODE', 'v24.19.0'))\n")
    for name in ("ffmpeg", "ffprobe"):
        executable(binaries / name, "pass\n")
    executable(binaries / "plutil", "import plistlib, sys\nwith open(sys.argv[-1], 'rb') as definition:\n    print(plistlib.load(definition)['ProgramArguments'][0])\n")
    home = tmp_path / "home"
    home.mkdir()
    # launchd as service.sh meets it: bootout returns before the job is gone,
    # and a label that is still loaded cannot be loaded again.
    executable(binaries / "launchctl", r'''
import json
import os
from pathlib import Path
import plistlib
import sys

home = Path(os.environ["HOME"])
loaded, stopping = home / "launchd-loaded", home / "launchd-stopping"
uv_calls = Path(os.environ["TEST_UV_CALLS"])
with open(home / "launchctl-calls.jsonl", "a") as calls:
    calls.write(json.dumps({
        "args": sys.argv[1:],
        "uv_calls": len(uv_calls.read_text().splitlines()) if uv_calls.exists() else 0,
    }) + "\n")
command = sys.argv[1]
if command == "print":
    if stopping.exists():
        remaining = int(stopping.read_text())
        if remaining:
            stopping.write_text(str(remaining - 1))
        else:
            stopping.unlink()
            loaded.unlink()
    raise SystemExit(0 if loaded.exists() else 113)
if command == "bootout":
    if not loaded.exists():
        raise SystemExit(3)
    stopping.write_text(os.environ.get("TEST_STOP_POLLS", "1"))
elif os.environ.get("TEST_FAIL") == command:
    raise SystemExit(5)
elif command == "bootstrap":
    if loaded.exists():
        print("Bootstrap failed: 5: Input/output error", file=sys.stderr)
        raise SystemExit(5)
    with open(sys.argv[3], "rb") as definition:
        plistlib.load(definition)
    loaded.touch()
''')
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
    # probe, so its mismatch and fallback branches are covered too. The real
    # service-definition code runs here as well, reading the fake settings.
    cuda = os.environ.get('TEST_TORCH_CUDA', '12.6' if PROFILE == 'cu126' else 'none')
    torch = ModuleType('torch')
    torch.__version__ = '2.14.0+' + PROFILE
    torch.version = SimpleNamespace(cuda=None if cuda == 'none' else cuda)
    sys.modules['torch'] = torch
    engine = ModuleType('nomusic.engines.mlx_engine')
    engine._pick_device = lambda: os.environ.get('TEST_DEVICE', 'cpu')
    sys.modules[engine.__name__] = engine
    config = ModuleType('nomusic.config')
    config.SETTINGS = SimpleNamespace(shutdown_grace_seconds=float(os.environ.get('NOMUSIC_SHUTDOWN_GRACE_SECONDS', 60)))
    sys.modules[config.__name__] = config
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
    # A private HOME keeps the service definition out of the developer's own.
    env.update(PATH=str(binaries), NOMUSIC_UV=str(uv), HOME=str(home),
               TEST_UV_CALLS=str(tmp_path / "uv-calls.jsonl"))
    default = repo / "backend/.venv"

    def old_env(path=default, version="3.11.10"):
        path.mkdir(parents=True)
        (path / "pyvenv.cfg").write_text("fixture = true\n")
        (path / "original").write_text(version)
        executable(path / "bin/python", f"print({version!r})\n")
        return path

    def script(name, *args, answer=None, typed_early="", timeout=10, **overrides):
        command = [shutil.which("bash"), str(repo / name), *args]
        if answer is None:
            # Never attached to the developer's own terminal.
            return subprocess.run(command, env=env | overrides, text=True, capture_output=True,
                                  timeout=timeout, stdin=subprocess.DEVNULL)
        # The installer asks only at a terminal. ``typed_early`` is already
        # waiting there; ``answer`` is typed when the question appears.
        keyboard, terminal = pty.openpty()
        os.write(keyboard, typed_early.encode())
        process = subprocess.Popen(command, env=env | overrides, text=True, stdin=terminal,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        watchdog = threading.Timer(timeout, process.kill)
        watchdog.start()
        try:
            shown = ""
            while answer and "[Y/n]" not in shown and (character := process.stdout.read(1)):
                shown += character
            os.write(keyboard, answer.encode())
            stdout, stderr = process.communicate()
            return subprocess.CompletedProcess(command, process.returncode, shown + stdout, stderr)
        finally:
            watchdog.cancel()
            os.close(keyboard)
            os.close(terminal)

    def run(*args, fetch_model=False, **options):
        return script("install.sh", "--skip-system-packages",
                      *([] if fetch_model else ["--skip-model-download"]), *args, **options)

    def service(*args, **options):
        return script("service.sh", *args, **options)

    def calls():
        path = Path(env["TEST_UV_CALLS"])
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def launchctl():
        path = home / "launchctl-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def nvidia(*, names="Test GPU", cuda="12.6", status=0):
        executable(binaries / "nvidia-smi", f"import sys\nprint({names!r} if len(sys.argv) > 1 else {'CUDA Version: ' + cuda!r})\nraise SystemExit({status})\n")

    return SimpleNamespace(repo=repo, default=default, backup=default.with_name(".venv.bak"),
                           env=env, run=run, old_env=old_env, calls=calls, nvidia=nvidia,
                           service=service, launchctl=launchctl, home=home,
                           plist=home / "Library/LaunchAgents/com.nomusic.backend.plist",
                           service_loaded=(home / "launchd-loaded").exists)


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


@pytest.mark.parametrize("names", [
    "NVIDIA GeForce RTX 5090",
    "NVIDIA GeForce RTX 5070 Ti",
    "NVIDIA GeForce RTX 5050 Laptop GPU",
    "NVIDIA RTX PRO 6000 Blackwell Workstation Edition",
    "NVIDIA RTX PRO 5000 Blackwell",
    "NVIDIA GeForce RTX 4090\nNVIDIA GeForce RTX 5090",
])
def test_automatic_profile_skips_cuda_for_blackwell_models(installer, names):
    installer.nvidia(names=names, cuda="13.0")
    result = installer.run()
    assert result.returncode == 0, result.stderr
    assert selected_profile(installer) == "cpu"
    assert "Blackwell" in result.stderr and "selecting CPU" in result.stderr
    assert "Installed PyTorch 2.14.0+cpu; CUDA build: none" in result.stdout


@pytest.mark.parametrize("names", [
    "NVIDIA GeForce RTX 4090",
    "Quadro RTX 5000",
    "NVIDIA RTX 5000 Ada Generation",
])
def test_automatic_cuda_keeps_supported_rtx_models(installer, names):
    installer.nvidia(names=names, cuda="13.0")
    result = installer.run(TEST_DEVICE="cuda")
    assert result.returncode == 0, result.stderr
    assert selected_profile(installer) == "cu126"


def test_explicit_cuda_profile_bypasses_blackwell_auto_fallback(installer):
    installer.nvidia(names="NVIDIA GeForce RTX 5090", cuda="13.0")
    result = installer.run("--profile=cu126")
    assert result.returncode == 0, result.stderr
    assert selected_profile(installer) == "cu126"
    assert "falls back to CPU" in result.stderr


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
    (["--service"], {}),
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


def service_changes(installer, since=0):
    return [(call["args"][0], call["uv_calls"]) for call in installer.launchctl()[since:]
            if call["args"][0] != "print"]


def test_mac_service_runs_this_installation_with_the_terminal_settings(installer):
    tools = installer.env["PATH"]
    # launchd refuses a definition that others can write, such as a leftover one.
    installer.plist.parent.mkdir(parents=True)
    installer.plist.touch()
    installer.plist.chmod(0o666)
    result = installer.run("--service", NOMUSIC_SHUTDOWN_GRACE_SECONDS="90.5", HF_HOME="/models",
                           PATH=f"{tools}:relative:{tools}", **MAC)
    assert result.returncode == 0, result.stderr
    definition = plistlib.loads(installer.plist.read_bytes())
    log = str(installer.home / "Library/Logs/nomusic-backend.log")
    assert definition == {
        "Label": "com.nomusic.backend",
        "ProgramArguments": [str(installer.default / "bin/nomusic"), "serve"],
        "WorkingDirectory": str(installer.repo),
        # The installer's own NOMUSIC_UV is not a backend setting.
        "EnvironmentVariables": {"PATH": tools, "NOMUSIC_SHUTDOWN_GRACE_SECONDS": "90.5", "HF_HOME": "/models"},
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
        "ExitTimeOut": 96,  # launchd waits for the backend's own deadline
        "StandardOutPath": log, "StandardErrorPath": log,
    }
    assert installer.plist.stat().st_mode & 0o777 == 0o644
    domain = f"gui/{os.getuid()}"
    assert [call["args"] for call in installer.launchctl() if call["args"][0] != "print"] == [
        ["enable", f"{domain}/com.nomusic.backend"], ["bootstrap", domain, str(installer.plist)]]
    assert "Saved settings: HF_HOME, NOMUSIC_SHUTDOWN_GRACE_SECONDS" in result.stdout
    assert "set to run in the background" in result.stdout and "Start the backend" not in result.stdout


def test_mac_service_uses_a_custom_environment(installer):
    custom = installer.repo / "custom"
    result = installer.run("--service", NOMUSIC_VENV=str(custom), **MAC)
    assert result.returncode == 0, result.stderr
    definition = plistlib.loads(installer.plist.read_bytes())
    assert definition["ProgramArguments"] == [str(custom / "bin/nomusic"), "serve"]
    assert definition["ExitTimeOut"] == 65


@pytest.mark.parametrize("grace", ["0", "-5", "inf", "nan"])
def test_service_rejects_a_shutdown_grace_launchd_cannot_honor(installer, grace):
    assert installer.run("--service", **MAC).returncode == 0
    running = installer.plist.read_bytes()
    result = installer.service("install", NOMUSIC_SHUTDOWN_GRACE_SECONDS=grace, **MAC)
    assert result.returncode != 0
    assert "finite and positive" in result.stderr
    assert installer.plist.read_bytes() == running and installer.service_loaded()


@pytest.mark.parametrize("answer,started", [
    ("\n", True), ("y\n", True), ("Yes\n", True), ("n\n", False), ("no\n", False), ("later\n", False),
])
def test_mac_install_at_a_terminal_asks_before_starting_service(installer, answer, started):
    result = installer.run(answer=answer, **MAC)
    assert result.returncode == 0, result.stderr
    assert "Keep nomusic running in the background? [Y/n]" in result.stdout
    assert installer.plist.exists() is installer.service_loaded() is started
    assert ("Start the backend" in result.stdout) is not started


@pytest.mark.parametrize("typed_early", ["\n", "y\n", "backend/.venv/bin/nomusic doctor\n"])
def test_keys_pressed_during_installation_do_not_answer_the_question(installer, typed_early):
    result = installer.run(answer="n\n", typed_early=typed_early, **MAC)
    assert result.returncode == 0, result.stderr
    assert "Keep nomusic running in the background? [Y/n]" in result.stdout
    assert not installer.plist.exists() and not installer.service_loaded()


def test_scripted_mac_install_does_not_ask_or_start_service(installer):
    result = installer.run(**MAC)
    assert result.returncode == 0, result.stderr
    assert "Keep nomusic running" not in result.stdout
    assert service_changes(installer) == [] and not installer.plist.exists()
    assert "Start the backend" in result.stdout and "service.sh install" in result.stdout


def test_linux_install_at_a_terminal_does_not_offer_service(installer):
    result = installer.run(answer="")  # an unanswered question would time out
    assert result.returncode == 0, result.stderr
    assert installer.launchctl() == [] and "service.sh" not in result.stdout


def test_reinstall_turns_running_service_off_while_environment_changes(installer):
    assert installer.run("--service", **MAC).returncode == 0
    before = len(installer.launchctl())
    result = installer.run(answer="", **MAC)
    assert result.returncode == 0, result.stderr
    assert "Keep nomusic running" not in result.stdout
    # One uv call belongs to the first installation, the second to this one.
    assert service_changes(installer, before) == [("bootout", 1), ("enable", 2), ("bootstrap", 2)]
    assert installer.service_loaded()


def test_install_elsewhere_does_not_take_over_a_running_service(installer):
    def backend():
        return plistlib.loads(installer.plist.read_bytes())["ProgramArguments"][0]

    other = installer.repo / "other"
    assert installer.run("--service", NOMUSIC_VENV=str(other), **MAC).returncode == 0
    before = len(installer.launchctl())
    scripted = installer.run(**MAC)
    assert scripted.returncode == 0, scripted.stderr
    assert service_changes(installer, before) == [] and backend() == str(other / "bin/nomusic")
    status = installer.service("status", **MAC)
    assert status.returncode == 1 and "another nomusic installation" in status.stdout
    # At a terminal the usual question decides whether it moves here.
    asked = installer.run(answer="\n", **MAC)
    assert asked.returncode == 0, asked.stderr
    assert "Keep nomusic running" in asked.stdout
    assert backend() == str(installer.default / "bin/nomusic") and installer.service_loaded()


def test_no_service_never_asks_and_leaves_running_service_alone(installer):
    assert installer.run("--service", **MAC).returncode == 0
    before = installer.launchctl()
    result = installer.run("--no-service", answer="", **MAC)
    assert result.returncode == 0, result.stderr
    assert "Keep nomusic running" not in result.stdout
    assert installer.launchctl() == before and installer.service_loaded()


def test_failed_reinstall_reports_that_service_is_off(installer):
    assert installer.run("--service", **MAC).returncode == 0
    result = installer.run(TEST_FAIL="sync", **MAC)
    assert result.returncode != 0
    assert "background service was turned off" in result.stderr
    assert not installer.plist.exists() and not installer.service_loaded()


@pytest.mark.parametrize("failure", ["enable", "bootstrap"])
def test_service_start_failure_follows_a_complete_installation(installer, failure):
    installer.old_env()
    result = installer.run("--service", TEST_FAIL=failure, **MAC)
    assert result.returncode != 0
    assert "Install complete" in result.stdout
    assert "nothing was installed" in result.stderr and "nomusic serve" in result.stderr
    # The new environment works, so restoring the backup is not suggested.
    assert "restore it" not in result.stderr
    assert not installer.plist.exists()


def test_service_script_reports_and_removes_service(installer):
    assert installer.run("--service", **MAC).returncode == 0
    assert installer.service("status", **MAC).returncode == 0
    for _ in range(2):
        result = installer.service("uninstall", **MAC)
        assert result.returncode == 0, result.stderr
        assert not installer.plist.exists() and not installer.service_loaded()
    assert installer.service("status", **MAC).returncode == 1
    restarted = installer.service("install", **MAC)
    assert restarted.returncode == 0, restarted.stderr
    assert installer.plist.exists() and installer.service_loaded()


def test_service_script_does_not_wait_forever_for_backend_to_stop(installer):
    assert installer.run("--service", **MAC).returncode == 0
    # Ninety polls of the fake launchctl are slow on a busy runner.
    result = installer.service("uninstall", TEST_STOP_POLLS="99", timeout=60, **MAC)
    assert result.returncode != 0
    assert "finish active work" in result.stdout and "still stopping" in result.stderr
    # Already removed from login, so giving up cannot bring it back later.
    assert not installer.plist.exists()


@pytest.mark.parametrize("args,platform,message", [
    (["install"], MAC, "Run ./install.sh first"),
    (["install"], {}, "macOS only"),
    (["restart"], MAC, "Unknown command"),
    ([], MAC, "Usage:"),
])
def test_service_script_rejects_unusable_requests(installer, args, platform, message):
    result = installer.service(*args, **platform)
    assert result.returncode != 0
    assert message in result.stderr
    assert service_changes(installer) == [] and not installer.plist.exists()
