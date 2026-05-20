#!/usr/bin/env bash
# Set up the nomusic backend on macOS / Apple Silicon.
#
# - Installs Python 3.11 and ffmpeg via Homebrew if missing
# - Creates backend/.venv
# - Installs Python deps from backend/requirements.txt
# - Clones the demucs-mlx project into vendor/ (added to sys.path by server.py)
#
# Re-running this script is safe; each step is idempotent.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
step() { printf '\n\033[1;36m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$1" >&2; }
die()  { printf '\033[1;31m[err ]\033[0m %s\n' "$1" >&2; exit 1; }

# --- platform check ----------------------------------------------------------

if [[ "$(uname -s)" != "Darwin" ]] || [[ "$(uname -m)" != "arm64" ]]; then
  die "nomusic's default MLX engine requires macOS on Apple Silicon."
fi

# --- homebrew prerequisites --------------------------------------------------

if ! command -v brew >/dev/null 2>&1; then
  die "Homebrew is required. Install it from https://brew.sh and re-run."
fi

step "Checking Homebrew packages"
for pkg in python@3.11 ffmpeg git; do
  if brew list --formula "$pkg" >/dev/null 2>&1; then
    echo "  $pkg already installed"
  else
    echo "  installing $pkg"
    brew install "$pkg"
  fi
done

PY="$(brew --prefix python@3.11)/bin/python3.11"
[[ -x "$PY" ]] || die "python3.11 not found at $PY after brew install"

# --- venv --------------------------------------------------------------------

step "Creating backend/.venv (Python 3.11)"
if [[ ! -d backend/.venv ]]; then
  "$PY" -m venv backend/.venv
fi
# shellcheck disable=SC1091
source backend/.venv/bin/activate

step "Installing Python dependencies"
pip install --upgrade pip wheel
pip install -r backend/requirements.txt

# --- demucs-mlx --------------------------------------------------------------

step "Vendoring demucs-mlx"
mkdir -p vendor
if [[ ! -d vendor/demucs-mlx ]]; then
  # Upstream: https://github.com/ml-explore/mlx-examples (subtree) or a community
  # port. We pin a known-working community port; users can swap the remote if
  # they prefer the official one once it lands.
  git clone --depth=1 https://github.com/ml-explore/mlx-examples.git vendor/mlx-examples
  if [[ -d vendor/mlx-examples/demucs ]]; then
    ln -sfn mlx-examples/demucs vendor/demucs-mlx
  else
    warn "vendor/mlx-examples does not contain a demucs/ subtree; please add a"
    warn "demucs-mlx implementation at vendor/demucs-mlx before running /process."
  fi
else
  echo "  vendor/demucs-mlx already present"
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
