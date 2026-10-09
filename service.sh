#!/usr/bin/env bash
# Keep the macOS backend running in the background as a per-user launchd agent.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_DIR="${NOMUSIC_VENV:-${UV_PROJECT_ENVIRONMENT:-$REPO_DIR/backend/.venv}}"
LABEL="com.nomusic.backend"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/nomusic-backend.log"

die() { printf 'Error: %s\n' "$1" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: ./service.sh install|uninstall|status

Runs the installed backend in the background on macOS.
  install    Start it now, at every login, and again if it crashes.
             Run it again to restart the backend or apply changed settings.
  uninstall  Stop it and remove it from login.
  status     Report whether the background service is on for this installation.

Exported NOMUSIC_* settings, HF_HOME, HF_HUB_CACHE and PATH are saved by
install; the background backend does not read your shell profile.
NOMUSIC_VENV selects the environment, as it does for ./install.sh.
EOF
}

[[ $# -eq 1 ]] || { usage >&2; exit 2; }
case "$1" in
  install|uninstall|status) ;;
  -h|--help) usage; exit 0 ;;
  *) die "Unknown command '$1'. Run ./service.sh --help." ;;
esac
[[ "$(uname -s)" == "Darwin" ]] || die "The background service is available on macOS only."

# A per-user agent, not a system daemon: MPS needs the logged-in session.
DOMAIN="gui/$UID"
TARGET="$DOMAIN/$LABEL"

loaded() { launchctl print "$TARGET" >/dev/null 2>&1; }

# Another checkout or environment may own the one service a user can have.
ours() {
  [[ "$(plutil -extract ProgramArguments.0 raw -o - "$PLIST" 2>/dev/null)" == "$ENV_DIR/bin/nomusic" ]]
}

stop() {
  local waited=0
  loaded || return 0
  launchctl bootout "$TARGET" >/dev/null 2>&1 || true
  # bootout returns while the backend is still stopping, and launchd refuses
  # to load a label again before it is gone. Active work may use the whole
  # ExitTimeOut written below, about a minute by default.
  while loaded; do
    [[ "$waited" -lt 90 ]] || die "The background backend is still stopping. Try again in a moment."
    [[ "$waited" -ne 3 ]] || printf 'Waiting for the backend to finish active work...\n'
    sleep 1
    waited=$((waited + 1))
  done
}

write_definition() {
  "$ENV_DIR/bin/python" - "$PLIST" "$LABEL" "$ENV_DIR/bin/nomusic" "$REPO_DIR" "$LOG" <<'PY'
import math
import os
import plistlib
import sys
from pathlib import Path

from nomusic.config import SETTINGS

plist, label, command, project, log = sys.argv[1:]
grace = SETTINGS.shutdown_grace_seconds
if not (math.isfinite(grace) and grace > 0):
    # launchd would also read a zero timeout as "wait forever" at shutdown.
    raise SystemExit("Error: NOMUSIC_SHUTDOWN_GRACE_SECONDS must be finite and positive.")
# launchd reads no shell profile. Keep this terminal's tool lookup and backend
# settings so the backend behaves as `nomusic serve` does here.
installer_only = {"NOMUSIC_VENV", "NOMUSIC_UV", "NOMUSIC_PYTHON", "NOMUSIC_CUDA", "NOMUSIC_TORCH"}
environment = {key: value for key, value in os.environ.items()
               if key in ("HF_HOME", "HF_HUB_CACHE")
               or (key.startswith("NOMUSIC_") and key not in installer_only)}
saved = sorted(environment)
environment["PATH"] = os.pathsep.join(dict.fromkeys(
    entry for entry in os.environ.get("PATH", os.defpath).split(os.pathsep)
    if os.path.isabs(entry)))
for path in (plist, log):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
with open(plist, "wb") as file:
    plistlib.dump({
        "Label": label,
        "ProgramArguments": [command, "serve"],
        "WorkingDirectory": project,
        "EnvironmentVariables": environment,
        "RunAtLoad": True,
        "KeepAlive": True,
        # Do not relaunch in a tight loop when startup itself fails.
        "ThrottleInterval": 10,
        # launchd otherwise kills a stopping job within seconds. Let the
        # backend's own shutdown deadline pass first; launchd may cap the wait.
        "ExitTimeOut": math.ceil(grace) + 5,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }, file)
os.chmod(plist, 0o644)  # launchd refuses a definition that others can write
if saved:
    print("Saved settings: " + ", ".join(saved))
PY
}

case "$1" in
  install)
    [[ -x "$ENV_DIR/bin/nomusic" && -x "$ENV_DIR/bin/python" ]] \
      || die "nomusic is not installed in $ENV_DIR. Run ./install.sh first."
    # Written first: a rejected setting leaves a running service as it was.
    write_definition
    # Also stops a backend that was started from another checkout.
    stop
    # A label disabled earlier cannot be loaded, so enable it first.
    if ! { launchctl enable "$TARGET" && launchctl bootstrap "$DOMAIN" "$PLIST"; }; then
      rm -f -- "$PLIST"
      die "launchd could not start the backend, so nothing was installed. Check that you are logged in to this Mac's desktop."
    fi
    printf 'nomusic is set to run in the background: it starts now and each time you log in.\n'
    printf '  Log:      %s\n' "$LOG"
    printf '  Turn off: %q uninstall\n' "$REPO_DIR/service.sh"
    ;;
  uninstall)
    # Removed first: an interrupted wait must not bring it back at next login.
    rm -f -- "$PLIST"
    stop
    printf 'nomusic no longer runs in the background.\n'
    ;;
  status)
    if ! loaded; then
      printf 'The background service is off. Turn it on with: %q install\n' "$REPO_DIR/service.sh"
      exit 1
    elif ! ours; then
      printf 'The background service is on, but for another nomusic installation. Move it here with: %q install\n' "$REPO_DIR/service.sh"
      exit 1
    fi
    printf 'The background service is on. Log: %s\n' "$LOG"
    ;;
esac
