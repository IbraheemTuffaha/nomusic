#!/usr/bin/env bash
# Install a locked CPU, NVIDIA CUDA, or native Apple Silicon profile.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UV_VERSION="0.12.19"
WITH_DEV=0
SKIP_SYSTEM=0
SKIP_MODEL=0
REQUESTED_PROFILE="auto"
PROFILE_EXPLICIT=0

step() { printf '\n==> %s\n' "$1"; }
die() { printf 'Error: %s\n' "$1" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: ./install.sh [--profile auto|cpu|cu126] [--dev] [--skip-system-packages] [--skip-model-download]

Installs the committed Python/dependency profile into backend/.venv.
  --profile PROFILE       auto detects NVIDIA on Linux; cpu and cu126 override it.
  --dev                   Include development and test dependencies.
  --skip-system-packages  Check prerequisites without running apt or Homebrew.
  --skip-model-download   Fetch model weights on first use instead of now.

NOMUSIC_VENV may select another dedicated, absolute virtual-environment path.
NOMUSIC_UV may point to an existing uv 0.12.19 executable.
NOMUSIC_JS_RUNTIME may select a supported Node.js or Deno executable.
Stop the backend before installing. An old default backend/.venv is moved to
backend/.venv.bak when its Python differs; an existing backup is never replaced.
Incompatible explicitly selected environments are left untouched.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)
      [[ $# -ge 2 ]] || die "--profile requires auto, cpu, or cu126."
      REQUESTED_PROFILE="$2"; PROFILE_EXPLICIT=1; shift ;;
    --profile=*) REQUESTED_PROFILE="${1#*=}"; PROFILE_EXPLICIT=1 ;;
    --dev) WITH_DEV=1 ;;
    --skip-system-packages) SKIP_SYSTEM=1 ;;
    --skip-model-download) SKIP_MODEL=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option '$1'. Run ./install.sh --help." ;;
  esac
  shift
done

cd "$REPO_DIR"
[[ -f .python-version && -f pyproject.toml && -f uv.lock ]] \
  || die "The checkout must include .python-version, pyproject.toml and uv.lock."
PYTHON_VERSION="$(tr -d '[:space:]' < .python-version)"
[[ -n "$PYTHON_VERSION" ]] || die ".python-version is empty."

if [[ -n "${NOMUSIC_TORCH:-}" ]]; then
  die "NOMUSIC_TORCH cannot override the locked torch version. Unset it and select --profile cpu or --profile cu126."
fi
if [[ -n "${NOMUSIC_CUDA:-}" ]]; then
  [[ "$NOMUSIC_CUDA" == "cu126" ]] \
    || die "NOMUSIC_CUDA=$NOMUSIC_CUDA is not a locked profile. Unset it or use NOMUSIC_CUDA=cu126 (equivalent to --profile cu126)."
  if [[ "$PROFILE_EXPLICIT" -eq 1 && "$REQUESTED_PROFILE" != "cu126" ]]; then
    die "NOMUSIC_CUDA conflicts with --profile $REQUESTED_PROFILE. Unset NOMUSIC_CUDA to use that profile."
  fi
  REQUESTED_PROFILE="cu126"
fi
case "$REQUESTED_PROFILE" in auto|cpu|cu126) ;; *) die "Unknown profile '$REQUESTED_PROFILE'; choose auto, cpu, or cu126." ;; esac
if [[ -n "${NOMUSIC_PYTHON:-}" && "$NOMUSIC_PYTHON" != "$PYTHON_VERSION" ]]; then
  die "This profile requires Python $PYTHON_VERSION. Unset NOMUSIC_PYTHON or set it to $PYTHON_VERSION; uv will obtain that interpreter."
fi

ENV_DIR="${NOMUSIC_VENV:-${UV_PROJECT_ENVIRONMENT:-$REPO_DIR/backend/.venv}}"
[[ "$ENV_DIR" == /* ]] || die "NOMUSIC_VENV/UV_PROJECT_ENVIRONMENT must be an absolute path."
CUSTOM_ENV=0
if [[ -n "${NOMUSIC_VENV:-}" || -n "${UV_PROJECT_ENVIRONMENT:-}" ]]; then CUSTOM_ENV=1; fi
MIGRATE_ENV=0
BACKUP_DIR="$REPO_DIR/backend/.venv.bak"
[[ ! -L "$ENV_DIR" ]] || die "$ENV_DIR is a symlink. Choose a dedicated virtual-environment directory; it has not been changed."
if [[ -e "$ENV_DIR" ]]; then
  [[ -f "$ENV_DIR/pyvenv.cfg" && -x "$ENV_DIR/bin/python" ]] \
    || die "$ENV_DIR exists but is not a usable virtual environment. Move it aside or choose a new NOMUSIC_VENV; it has not been changed."
  EXISTING_PYTHON="$("$ENV_DIR/bin/python" -c 'import platform; print(platform.python_version())' 2>/dev/null || true)"
  if [[ "$EXISTING_PYTHON" != "$PYTHON_VERSION" ]]; then
    [[ "$CUSTOM_ENV" -eq 0 ]] \
      || die "$ENV_DIR uses Python ${EXISTING_PYTHON:-unknown}; this profile needs $PYTHON_VERSION. Move the custom environment aside or choose a new NOMUSIC_VENV; it has not been changed."
    [[ ! -e "$BACKUP_DIR" && ! -L "$BACKUP_DIR" ]] \
      || die "$BACKUP_DIR already exists. Move that backup elsewhere before upgrading; neither environment has been changed."
    MIGRATE_ENV=1
  fi
fi
export UV_PROJECT_ENVIRONMENT="$ENV_DIR"

OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS/$ARCH" in
  Linux/x86_64)
    EXTRA="$REQUESTED_PROFILE"
    if [[ "$EXTRA" == "auto" ]]; then
      EXTRA="cpu"
      if command -v nvidia-smi >/dev/null 2>&1; then
        # This is a conservative driver precheck, not an inference test. The
        # installed engine checks the wheel's GPU architecture support below.
        if GPU_NAMES="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null)" \
          && [[ -n "$GPU_NAMES" ]] \
          && NVIDIA_STATUS="$(nvidia-smi 2>/dev/null)" \
          && [[ "$NVIDIA_STATUS" =~ CUDA[[:space:]]+Version:[[:space:]]+([0-9]+)\.([0-9]+) ]] \
          && [[ "${BASH_REMATCH[1]}" -gt 12 || ( "${BASH_REMATCH[1]}" -eq 12 && "${BASH_REMATCH[2]}" -ge 6 ) ]]; then
          EXTRA="cu126"
        else
          printf 'NVIDIA CUDA detection did not find a usable device/driver advertising CUDA 12.6+; selecting CPU. Use --profile cu126 to install that build explicitly.\n' >&2
        fi
      else
        printf 'No nvidia-smi found; selecting CPU. Use --profile cu126 to install the CUDA build explicitly.\n'
      fi
    fi
    PROFILE="Linux x86_64 $EXTRA"
    ;;
  Darwin/arm64)
    MACOS_VERSION="$(sw_vers -productVersion)"
    [[ "${MACOS_VERSION%%.*}" -ge 14 ]] \
      || die "The native Apple Silicon profile requires macOS 14 or newer."
    [[ "$REQUESTED_PROFILE" != "cu126" ]] || die "cu126 is a Linux NVIDIA profile. Use --profile auto or cpu for native Apple Silicon with MPS support."
    EXTRA="cpu"
    PROFILE="native Apple Silicon"
    ;;
  *) die "No locked installer profile for $OS/$ARCH. Supported: Linux x86_64 CPU/CUDA and macOS 14+ Apple Silicon." ;;
esac

# Match the runtime by its output, so a nodejs binary or an explicitly named
# executable works too. Detailed installed-package checks run after sync.
supported_js() {
  local output major minor
  output="$("$1" --version 2>/dev/null)" || return 1
  if [[ "$output" =~ ^v([0-9]+)\. ]]; then
    major="${BASH_REMATCH[1]}"
    [[ "$major" -ge 22 ]]
  elif [[ "$output" =~ ^deno[[:space:]]+([0-9]+)\.([0-9]+)\. ]]; then
    major="${BASH_REMATCH[1]}"
    minor="${BASH_REMATCH[2]}"
    [[ "$major" -gt 2 || ( "$major" -eq 2 && "$minor" -ge 3 ) ]]
  else
    return 1
  fi
}

have_supported_js() {
  local candidate
  if [[ -n "${NOMUSIC_JS_RUNTIME:-}" ]]; then
    supported_js "$NOMUSIC_JS_RUNTIME"
    return
  fi
  for candidate in deno node nodejs; do
    if command -v "$candidate" >/dev/null 2>&1 && supported_js "$candidate"; then
      return 0
    fi
  done
  return 1
}

if [[ -n "${NOMUSIC_JS_RUNTIME:-}" ]] && ! have_supported_js; then
  die "NOMUSIC_JS_RUNTIME must run Node.js 22+ or Deno 2.3+. Check the executable and its --version output."
fi

UV=""
if [[ -n "${NOMUSIC_UV:-}" ]]; then
  [[ -x "$NOMUSIC_UV" ]] || die "NOMUSIC_UV is not executable: $NOMUSIC_UV"
  UV="$NOMUSIC_UV"
elif command -v uv >/dev/null 2>&1; then
  UV_CANDIDATE="$(command -v uv)"
  UV_OUTPUT="$("$UV_CANDIDATE" --version)"
  if [[ "$UV_OUTPUT" == "uv $UV_VERSION" || "$UV_OUTPUT" == "uv $UV_VERSION "* ]]; then
    UV="$UV_CANDIDATE"
  fi
fi
if [[ -n "$UV" ]]; then
  UV_OUTPUT="$("$UV" --version)"
  [[ "$UV_OUTPUT" == "uv $UV_VERSION" || "$UV_OUTPUT" == "uv $UV_VERSION "* ]] \
    || die "NOMUSIC_UV must use uv $UV_VERSION; found '$UV_OUTPUT'. Unset it to bootstrap the pinned version locally."
fi

step "Checking prerequisites for $PROFILE"
if [[ "$OS" == "Darwin" && "$SKIP_SYSTEM" -eq 0 ]]; then
  BREW_PACKAGES=()
  if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
    BREW_PACKAGES+=(ffmpeg)
  fi
  if ! have_supported_js; then BREW_PACKAGES+=(deno); fi
  if [[ -z "$UV" ]] && ! python3 -c 'import venv, ensurepip' >/dev/null 2>&1; then
    BREW_PACKAGES+=(python@3.12)
  fi
  if [[ -n "${BREW_PACKAGES[*]-}" ]]; then
    command -v brew >/dev/null 2>&1 || die "Install Homebrew from https://brew.sh, or install prerequisites yourself and use --skip-system-packages."
    HOMEBREW_NO_AUTO_UPDATE=1 brew install "${BREW_PACKAGES[@]}"
  fi
elif [[ "$OS" == "Linux" && "$SKIP_SYSTEM" -eq 0 ]]; then
  # Older distro Node packages cannot solve modern YouTube challenges.
  have_supported_js || die "Install Node.js 22+ (https://nodejs.org/en/download) or Deno 2.3+ (https://docs.deno.com/runtime/getting_started/installation/), then rerun. The distro nodejs package may be too old."
  APT_PACKAGES=()
  if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
    APT_PACKAGES+=(ffmpeg)
  fi
  if [[ -z "$UV" ]] && ! python3 -c 'import venv, ensurepip' >/dev/null 2>&1; then
    APT_PACKAGES+=(python3 python3-venv)
  fi
  if [[ -n "${APT_PACKAGES[*]-}" ]]; then
    command -v apt-get >/dev/null 2>&1 || die "Install ffmpeg, ffprobe, and Python with venv using your distribution's package manager, then rerun with --skip-system-packages."
    APT_COMMAND=(apt-get)
    if [[ "$(id -u)" -ne 0 ]]; then
      command -v sudo >/dev/null 2>&1 || die "sudo is required to install missing system packages. Install prerequisites yourself and use --skip-system-packages."
      APT_COMMAND=(sudo apt-get)
    fi
    "${APT_COMMAND[@]}" update
    "${APT_COMMAND[@]}" install --no-install-recommends -y "${APT_PACKAGES[@]}"
  fi
fi

command -v ffmpeg >/dev/null 2>&1 || die "ffmpeg is missing; install it and rerun."
command -v ffprobe >/dev/null 2>&1 || die "ffprobe is missing; install the FFmpeg tools and rerun."
have_supported_js || die "Node.js 22+ or Deno 2.3+ is required. Install one or set NOMUSIC_JS_RUNTIME to a supported executable."

if [[ -z "$UV" ]]; then
  step "Bootstrapping uv $UV_VERSION in a separate installer environment"
  BOOTSTRAP_PYTHON="$(command -v python3 || true)"
  if [[ "$OS" == "Darwin" ]] && ! "${BOOTSTRAP_PYTHON:-python3}" -c 'import venv, ensurepip' >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then
      BOOTSTRAP_PYTHON="$(brew --prefix python@3.12)/bin/python3.12"
    fi
  fi
  [[ -n "$BOOTSTRAP_PYTHON" ]] || die "Python 3 with venv is needed to bootstrap uv. Install it, or set NOMUSIC_UV to uv $UV_VERSION."
  INSTALLER_ENV="$REPO_DIR/backend/.installer-venv"
  if [[ -e "$INSTALLER_ENV" ]]; then
    [[ -f "$INSTALLER_ENV/pyvenv.cfg" && -x "$INSTALLER_ENV/bin/python" ]] \
      || die "$INSTALLER_ENV is incomplete. Move it aside or supply NOMUSIC_UV; it has not been deleted."
  else
    "$BOOTSTRAP_PYTHON" -m venv "$INSTALLER_ENV" \
      || die "Could not create the installer environment. Install Python venv support or supply NOMUSIC_UV."
  fi
  "$INSTALLER_ENV/bin/python" -m pip install --disable-pip-version-check --only-binary=:all: "uv==$UV_VERSION"
  UV="$INSTALLER_ENV/bin/uv"
fi

# Leave an old environment in place until prerequisite checks and uv setup have
# succeeded. A failed application install keeps one backup for manual recovery.
BACKUP_CREATED=0
restore_hint() {
  local status=$?
  if [[ "$status" -ne 0 && "$BACKUP_CREATED" -eq 1 ]]; then
    printf '\nInstallation failed; the previous environment is preserved at %s.\n' "$BACKUP_DIR" >&2
    printf 'To discard the incomplete new environment and restore it, run:\n  rm -rf -- %q && mv -- %q %q\n' "$ENV_DIR" "$BACKUP_DIR" "$ENV_DIR" >&2
  fi
}
trap restore_hint EXIT
if [[ "$MIGRATE_ENV" -eq 1 ]]; then
  [[ ! -e "$BACKUP_DIR" && ! -L "$BACKUP_DIR" ]] || die "$BACKUP_DIR appeared during setup; the current environment has not been changed."
  step "Preserving the previous Python environment at $BACKUP_DIR"
  mv -- "$ENV_DIR" "$BACKUP_DIR"
  BACKUP_CREATED=1
fi

step "Installing locked dependencies into $ENV_DIR (Python $PYTHON_VERSION)"
# Rebuild the local package even when only Python source files changed.
SYNC_ARGS=(sync --locked --no-editable --reinstall-package nomusic --python "$PYTHON_VERSION" --extra "$EXTRA")
if [[ "$WITH_DEV" -eq 1 ]]; then
  SYNC_ARGS+=(--group dev)
else
  SYNC_ARGS+=(--no-dev)
fi
"$UV" "${SYNC_ARGS[@]}"
"$ENV_DIR/bin/nomusic" check-runtime
"$ENV_DIR/bin/python" - "$EXTRA" <<'PY'
import os
import sys

import torch
from nomusic.engines.mlx_engine import _pick_device

# Report automatic capability independently of a user's per-run override.
os.environ["NOMUSIC_DEVICE"] = "auto"
device = _pick_device()
print(f"Installed PyTorch {torch.__version__}; CUDA build: {torch.version.cuda or 'none'}; automatic device: {device}")
expected_cuda = "12.6" if sys.argv[1] == "cu126" else None
if torch.version.cuda != expected_cuda:
    raise SystemExit(f"Wrong PyTorch build for profile {sys.argv[1]}: expected CUDA {expected_cuda or 'none'}, found {torch.version.cuda or 'none'}.")
if sys.argv[1] == "cu126" and device != "cuda":
    print("Warning: the CUDA build is installed, but this machine cannot use it. Automatic device selection falls back to CPU; check the NVIDIA driver and GPU architecture, or reinstall with --profile cpu.", file=sys.stderr)
PY

if [[ "$SKIP_MODEL" -eq 0 ]]; then
  step "Fetching the default model"
  "$ENV_DIR/bin/nomusic" models fetch
fi

printf '\nInstall complete (%s).\n' "$PROFILE"
printf 'Start the backend:\n  %q serve\n' "$ENV_DIR/bin/nomusic"
printf '\nLoad the unpacked browser extension from:\n  %s/extension\n' "$REPO_DIR"
if [[ "$SKIP_MODEL" -eq 1 ]]; then
  printf '\nModel download was skipped; first use requires internet access.\n'
fi
