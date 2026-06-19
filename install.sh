#!/usr/bin/env bash
# Set up the nomusic backend.
#
# macOS / Apple Silicon:
#   - Installs Python 3.11 and ffmpeg via Homebrew if missing
#   - torch comes from PyPI (arm64 / MPS wheel)
#
# Linux (Debian/Ubuntu):
#   - Installs Python, ffmpeg and a JS runtime via apt
#   - Installs torch from the CUDA wheel index if an NVIDIA GPU is present,
#     otherwise from the CPU wheel index
#
# Common:
#   - Creates backend/.venv and installs Python deps from requirements.txt
#
# Re-running this script is safe; each step is idempotent.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
step() { printf '\n\033[1;36m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$1" >&2; }
die()  { printf '\033[1;31m[err ]\033[0m %s\n' "$1" >&2; exit 1; }

OS="$(uname -s)"
PY=""  # set by the platform installer below

# --- macOS / Apple Silicon ---------------------------------------------------

install_macos() {
  if [[ "$(uname -m)" != "arm64" ]]; then
    die "On macOS, nomusic's default engine targets Apple Silicon (arm64)."
  fi
  if ! command -v brew >/dev/null 2>&1; then
    die "Homebrew is required. Install it from https://brew.sh and re-run."
  fi

  step "Checking Homebrew packages"
  # deno is yt-dlp's preferred JavaScript runtime; without it (or node/bun)
  # many YouTube videos extract as "This video is not available" because the
  # signature-cipher challenge can't be solved.
  for pkg in python@3.11 ffmpeg git deno; do
    if brew list --formula "$pkg" >/dev/null 2>&1; then
      echo "  $pkg already installed"
    else
      echo "  installing $pkg"
      brew install "$pkg"
    fi
  done

  PY="$(brew --prefix python@3.11)/bin/python3.11"
  [[ -x "$PY" ]] || die "python3.11 not found at $PY after brew install"
}

# --- Linux (Debian/Ubuntu) ---------------------------------------------------

install_linux() {
  if ! command -v apt-get >/dev/null 2>&1; then
    die "Linux auto-install currently supports Debian/Ubuntu (apt). On other distros, install python3 (+venv/pip), ffmpeg, git and a JS runtime (node/deno), then run: python3 -m venv backend/.venv && backend/.venv/bin/pip install torch torchaudio && backend/.venv/bin/pip install -r backend/requirements.txt (PyPI's default Linux torch wheel is CUDA-enabled; pin a build from https://download.pytorch.org/whl/cu128 if your driver needs a specific CUDA)."
  fi

  step "Installing system packages via apt"
  # nodejs is yt-dlp's JavaScript runtime here (deno isn't in apt); without it
  # (or node/bun) many YouTube videos fail the signature-cipher challenge.
  sudo apt-get update
  sudo apt-get install -y python3 python3-venv python3-pip ffmpeg git nodejs

  PY="$(command -v python3)"
  [[ -x "$PY" ]] || die "python3 not found after apt install"

  # The downloader looks for `deno`/`node`/`bun` on PATH. Older apt builds ship
  # the binary as `nodejs`; if so, point the backend at it explicitly.
  if ! command -v deno >/dev/null 2>&1 && ! command -v node >/dev/null 2>&1 \
     && command -v nodejs >/dev/null 2>&1; then
    warn "No 'node' on PATH (only 'nodejs'). For best YouTube support, start the server with: NOMUSIC_JS_RUNTIME=$(command -v nodejs) backend/.venv/bin/python backend/server.py"
  fi
}

case "$OS" in
  Darwin) install_macos ;;
  Linux)  install_linux ;;
  *)      die "Unsupported OS: $OS (supported: macOS/Apple Silicon, Debian/Ubuntu Linux)" ;;
esac

# --- optional: pin a specific Python -----------------------------------------
# NOMUSIC_PYTHON=3.11 forces a particular interpreter. Useful on Linux to drive
# older GPUs (e.g. Pascal / GTX 10-series) whose torch builds with legacy CUDA
# archs only ship for older Python versions. The interpreter must already be
# installed (Ubuntu: add the deadsnakes PPA, then apt-get install python3.11
# python3.11-venv); we just use it.

if [[ -n "${NOMUSIC_PYTHON:-}" ]]; then
  alt="$(command -v "python${NOMUSIC_PYTHON}" || true)"
  [[ -n "$alt" ]] || die "NOMUSIC_PYTHON=${NOMUSIC_PYTHON} requested but 'python${NOMUSIC_PYTHON}' is not on PATH. Install it first (Ubuntu: sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt-get update && sudo apt-get install python${NOMUSIC_PYTHON} python${NOMUSIC_PYTHON}-venv), then re-run."
  PY="$alt"
  # Debian/Ubuntu split venv's pip bootstrap (ensurepip) into a separate
  # package; without it `python -m venv` dies at ensurepip. Make sure it's
  # present for the chosen interpreter (idempotent).
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get install -y "python${NOMUSIC_PYTHON}-venv" \
      || warn "Could not install python${NOMUSIC_PYTHON}-venv; venv creation may fail."
  fi
fi

# --- venv --------------------------------------------------------------------

step "Creating backend/.venv"
want_pyver="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if [[ -d backend/.venv ]]; then
  have_pyver="$(backend/.venv/bin/python -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "")"
  if [[ "$have_pyver" != "$want_pyver" ]]; then
    warn "Existing venv is Python ${have_pyver:-unknown}; recreating with ${want_pyver}"
    rm -rf backend/.venv
  elif ! backend/.venv/bin/python -m pip --version >/dev/null 2>&1; then
    # A previous run created the venv but ensurepip failed — no usable pip.
    warn "Existing venv has no working pip; recreating"
    rm -rf backend/.venv
  fi
fi
if [[ ! -d backend/.venv ]]; then
  "$PY" -m venv backend/.venv
fi
# shellcheck disable=SC1091
source backend/.venv/bin/activate

pip install --upgrade pip wheel

# --- torch (Linux only; macOS gets the arm64/MPS wheel via requirements) ------

if [[ "$OS" == "Linux" ]]; then
  # Install torch BEFORE requirements.txt so the generic ``torch>=2.2`` pin is
  # already satisfied and pip doesn't re-resolve it.

  # Optional exact version, e.g. NOMUSIC_TORCH=2.4.1 to get a build that still
  # ships Pascal (sm_61) kernels for older GPUs. Empty = newest available.
  if [[ -n "${NOMUSIC_TORCH:-}" ]]; then
    torch_pkgs=("torch==${NOMUSIC_TORCH}" "torchaudio==${NOMUSIC_TORCH}")
  else
    torch_pkgs=(torch torchaudio)
  fi

  if command -v nvidia-smi >/dev/null 2>&1; then
    if [[ -n "${NOMUSIC_CUDA:-}" ]]; then
      # Explicit CUDA build, e.g. NOMUSIC_CUDA=cu118 for an older GPU/driver.
      # Maintained tags are cu118 / cu126 / cu128 (cu124 and older are frozen).
      step "NVIDIA GPU detected — installing torch for CUDA ${NOMUSIC_CUDA}"
      pip install "${torch_pkgs[@]}" --index-url "https://download.pytorch.org/whl/${NOMUSIC_CUDA}"
    else
      # PyPI's default Linux torch wheel IS the CUDA build, and it covers the
      # widest Python-version matrix, so it just works on a current driver.
      # Pin NOMUSIC_CUDA (cu118/cu126/cu128) only if your driver needs a
      # specific CUDA version.
      step "NVIDIA GPU detected — installing CUDA torch (PyPI default)"
      pip install "${torch_pkgs[@]}"
    fi
  else
    step "No NVIDIA GPU detected — installing CPU torch"
    pip install "${torch_pkgs[@]}" --index-url https://download.pytorch.org/whl/cpu
  fi
fi

# --- remaining Python deps ---------------------------------------------------

step "Installing Python dependencies (this may take a few minutes for torch)"
pip install -r backend/requirements.txt

# --- verify torch can see the GPU (Linux) ------------------------------------

if [[ "$OS" == "Linux" ]]; then
  step "Verifying torch device"
  python - <<'PY'
import torch
print(f"  torch {torch.__version__} — CUDA available: {torch.cuda.is_available()}")
if not torch.cuda.is_available():
    print("  [warn] torch can't see a GPU. If this box has an NVIDIA GPU, re-run")
    print("         with a pinned CUDA build, e.g.: NOMUSIC_CUDA=cu128 ./install.sh")
    print("         (try cu126 or cu118 if your driver is older).")
PY
fi

# --- done --------------------------------------------------------------------

bold "Install complete."
cat <<EOF

Start the backend:
  backend/.venv/bin/python backend/server.py

Sanity-check it:
  curl -s http://127.0.0.1:8723/capabilities | python3 -m json.tool

Load the extension:
  1. Open chrome://extensions
  2. Toggle Developer mode
  3. Click "Load unpacked" and pick: $REPO_DIR/extension
EOF
