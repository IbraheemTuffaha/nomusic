#!/usr/bin/env bash
# Manage the authenticated nomusic backend as a per-user macOS LaunchAgent.
set -euo pipefail

SCRIPT_PATH="$0"
REPO_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
ENV_DIR="$REPO_DIR/backend/.venv"
if [[ -n "${NOMUSIC_VENV-}" ]]; then
  ENV_DIR="$NOMUSIC_VENV"
elif [[ -n "${UV_PROJECT_ENVIRONMENT-}" ]]; then
  ENV_DIR="$UV_PROJECT_ENVIRONMENT"
fi
[[ "$ENV_DIR" == /* ]] || {
  printf 'Error: the virtual-environment path must be absolute: %s\n' "$ENV_DIR" >&2
  exit 1
}

LABEL="com.nomusic.backend"
DOMAIN="gui/$UID"
TARGET="$DOMAIN/$LABEL"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/nomusic-backend.log"
HEALTH_TIMEOUT="${NOMUSIC_SERVICE_HEALTH_TIMEOUT_SECONDS-30}"
SKIP_HEALTH="${NOMUSIC_SERVICE_SKIP_HEALTH-0}"

die() {
  printf 'Error: %s\n' "$1" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: ./service.sh install|restart|status|uninstall

Manage the authenticated backend as a per-user macOS LaunchAgent.
  install    Generate the definition, start now, and start at each login.
  restart    Reload this checkout's definition and restart the backend.
  status     Check ownership, launchd state, and local HTTP liveness.
  uninstall  Stop the backend and remove its login definition.

This is a user LaunchAgent because Apple Silicon MPS requires a logged-in GUI
session. It does not run before login and it never removes cache, model,
operator-key, or log data. The generated definition contains absolute paths
and the allowlisted NOMUSIC_* settings from this shell; it never copies keys
or arbitrary environment variables.
EOF
}

[[ $# -eq 1 ]] || { usage >&2; exit 2; }
COMMAND="$1"
case "$COMMAND" in
  install|restart|status|uninstall) ;;
  -h|--help) usage; exit 0 ;;
  *) die "Unknown command '$COMMAND'. Run ./service.sh --help." ;;
esac

[[ "$(uname -s)" == "Darwin" ]] || die "The background service is available on macOS only."
command -v launchctl >/dev/null 2>&1 || die "launchctl is unavailable; run this from macOS."
command -v plutil >/dev/null 2>&1 || die "plutil is unavailable; run this from macOS."

loaded() {
  launchctl print "$TARGET" >/dev/null 2>&1
}

definition_value() {
  local key="$1"
  plutil -extract "$key" raw -o - "$PLIST" 2>/dev/null
}

configured_command() {
  definition_value ProgramArguments.0
}

configured_project() {
  definition_value WorkingDirectory
}

ours() {
  [[ -f "$PLIST" ]] \
    && [[ "$(configured_command)" == "$ENV_DIR/bin/nomusic" ]] \
    && [[ "$(configured_project)" == "$REPO_DIR" ]]
}

configured_port() {
  local port
  port="$(definition_value EnvironmentVariables.NOMUSIC_PORT || true)"
  printf '%s\n' "${port:-8723}"
}

health_url() {
  printf 'http://127.0.0.1:%s/healthz\n' "$(configured_port)"
}

healthy() {
  command -v curl >/dev/null 2>&1 || die "curl is unavailable; install it before checking service health."
  curl -fsS --max-time 2 -o /dev/null "$(health_url)"
}

wait_for_health() {
  [[ "$SKIP_HEALTH" == "1" ]] && return 0
  local deadline=$((SECONDS + HEALTH_TIMEOUT))
  while ! healthy; do
    [[ "$SECONDS" -lt "$deadline" ]] || return 1
    sleep 1
  done
}

stop_loaded() {
  local waited=0
  loaded || return 0
  launchctl bootout "$TARGET" >/dev/null 2>&1 || true
  while loaded; do
    [[ "$waited" -lt 90 ]] \
      || die "The background backend is still stopping. Try again in a moment."
    [[ "$waited" -ne 3 ]] \
      || printf 'Waiting for the backend to finish active work...\n'
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
    raise SystemExit(
        "Error: NOMUSIC_SHUTDOWN_GRACE_SECONDS must be finite and positive."
    )

# Persist only backend configuration and runtime selectors. In particular, do
# not persist arbitrary NOMUSIC_* names that could contain a secret.
allowed = {
    "NOMUSIC_" + name for name in (
        "AUTH_FILE", "CACHE_DIR", "CACHE_SWEEP_INTERVAL_SECONDS",
        "CACHE_TTL_DAYS", "CHUNK_OVERLAP_SECONDS", "CHUNK_SECONDS",
        "CLIENT_HEARTBEAT_SECONDS", "CLIENT_LEASE_SECONDS",
        "ENGINE", "EXECUTION_TIMEOUT_SECONDS", "EXPORT_SWEEP_INTERVAL_SECONDS",
        "EXPORT_TTL_SECONDS", "FINAL_CHUNK_TOLERANCE_SECONDS",
        "GPU_BATCH", "HOST", "IDLE_TIMEOUT_SECONDS",
        "INTEREST_SWEEP_INTERVAL_SECONDS", "MAX_CACHE_BYTES",
        "MAX_CHUNK_BYTES", "MAX_DECODE_BYTES", "MAX_DURATION_SECONDS",
        "MAX_EXPORT_BYTES", "MAX_EXPORT_DOWNLOADS", "MAX_EXPORT_JOBS",
        "MAX_INFERENCE_BATCH", "MAX_PREFETCH_CHUNKS", "MAX_QUEUED_JOBS",
        "MAX_SOURCE_BYTES", "MAX_VIDEO_BYTES", "MAX_VIDEO_HEIGHT",
        "MEMORY_GC_INTERVAL_SECONDS", "MIN_FREE_BYTES", "PORT",
        "SHUTDOWN_GRACE_SECONDS", "SSE_KEEPALIVE_SECONDS", "SSE_QUEUE_SIZE",
        "SUPERVISED_WORKER", "WORKER_CANCEL_GRACE_SECONDS",
        "WORKER_WARMUP_TIMEOUT_SECONDS", "KEEP_SOURCE_AFTER_COMPLETE",
        "PROGRESSIVE",
    )
}
allowed.update({
    "NOMUSIC_DEVICE",
    "NOMUSIC_DEBUG",
    "NOMUSIC_DOWNLOAD_RATELIMIT",
    "NOMUSIC_JS_RUNTIME",
    "NOMUSIC_RELOAD",
    "HF_HOME",
    "HF_HUB_CACHE",
    "HOME",
})
environment = {key: value for key, value in os.environ.items() if key in allowed}
path_entries = [
    entry for entry in os.environ.get("PATH", os.defpath).split(os.pathsep)
    if entry and os.path.isabs(entry)
]
environment["PATH"] = os.pathsep.join(dict.fromkeys(path_entries)) or "/usr/bin:/bin"

plist_path = Path(plist)
log_path = Path(log)
plist_path.parent.mkdir(parents=True, exist_ok=True)
log_path.parent.mkdir(parents=True, exist_ok=True)
try:
    plist_path.parent.chmod(0o700)
    log_path.parent.chmod(0o700)
except OSError:
    pass

with plist_path.open("wb") as file:
    plistlib.dump({
        "Label": label,
        "ProgramArguments": [command, "serve"],
        "WorkingDirectory": project,
        "EnvironmentVariables": environment,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ExitTimeOut": math.ceil(grace) + 5,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }, file, sort_keys=False)
os.chmod(plist, 0o600)
saved = sorted(key for key in environment if key != "PATH")
if saved:
    print("Saved settings: " + ", ".join(saved))
PY
}

check_environment() {
  [[ -x "$ENV_DIR/bin/nomusic" && -x "$ENV_DIR/bin/python" ]] \
    || die "nomusic is not installed in $ENV_DIR. Run ./install.sh first."
}

check_auth_file() {
  local auth_file="${NOMUSIC_AUTH_FILE:-$HOME/.config/nomusic/operator-keys.json}"
  [[ -f "$auth_file" ]] \
    || die "No operator key file exists at $auth_file. Run '$ENV_DIR/bin/nomusic auth generate' before starting the service."
}

check_ownership() {
  [[ ! -L "$PLIST" ]] \
    || die "$PLIST is a symlink; it was not changed."
  [[ ! -e "$PLIST" || -f "$PLIST" ]] \
    || die "$PLIST is not a regular file; it was not changed."
  if loaded && [[ ! -e "$PLIST" ]]; then
    die "A different nomusic installation owns the loaded LaunchAgent, but its definition is not at $PLIST. Stop that service before continuing."
  fi
  if [[ -e "$PLIST" ]] && ! ours; then
    die "The LaunchAgent belongs to another nomusic checkout or environment. Run that checkout's service.sh uninstall first."
  fi
}

backup_definition() {
  BACKUP_FILE=""
  if [[ -f "$PLIST" ]]; then
    local tmp_dir
    tmp_dir="${TMPDIR-/tmp}"
    BACKUP_FILE="$(mktemp "$tmp_dir/nomusic-service.XXXXXX")"
    cp -p "$PLIST" "$BACKUP_FILE"
  fi
}

restore_definition() {
  local status="$1"
  if [[ "$status" -eq 0 ]]; then
    [[ -z "$BACKUP_FILE" ]] || rm -f -- "$BACKUP_FILE"
    return
  fi
  rm -f -- "$PLIST"
  if [[ -n "$BACKUP_FILE" ]]; then
    mv -- "$BACKUP_FILE" "$PLIST"
    if ! loaded; then
      launchctl enable "$TARGET" >/dev/null 2>&1 || true
      launchctl bootstrap "$DOMAIN" "$PLIST" >/dev/null 2>&1 || true
    fi
  fi
}

install_service() {
  check_environment
  check_auth_file
  check_ownership
  backup_definition
  write_definition
  stop_loaded
  if ! launchctl enable "$TARGET" >/dev/null 2>&1 \
      || ! launchctl bootstrap "$DOMAIN" "$PLIST" >/dev/null 2>&1; then
    restore_definition 1
    die "launchd could not start the backend. Check that you are logged in to this Mac's desktop."
  fi
  restore_definition 0
  if ! wait_for_health; then
    die "The service was loaded but did not answer the health check within ${HEALTH_TIMEOUT}s. Inspect $LOG."
  fi
  printf 'nomusic is running as a background service.\n'
  printf '  Starts:   at login in the current user session\n'
  printf '  Log:      %s\n' "$LOG"
  printf '  Status:   %q status\n' "$REPO_DIR/service.sh"
}

case "$COMMAND" in
  install)
    install_service
    ;;
  restart)
    check_ownership
    if [[ ! -f "$PLIST" ]]; then
      printf 'Error: The background service is not installed. Run %q install.\n' "$REPO_DIR/service.sh" >&2
      exit 1
    fi
    install_service
    ;;
  uninstall)
    check_ownership
    # Remove the login definition first so a delayed stop cannot relaunch it.
    rm -f -- "$PLIST"
    stop_loaded
    printf 'nomusic background service removed; cache, models, keys, and logs were preserved.\n'
    ;;
  status)
    if [[ -e "$PLIST" ]] && ! ours; then
      die "A different nomusic installation owns the LaunchAgent. Run that checkout's service.sh uninstall first."
    fi
    if ! loaded; then
      if [[ -f "$PLIST" ]] && ours; then
        printf 'The background service is installed but stopped. Run %q install.\n' "$REPO_DIR/service.sh"
      else
        printf 'The nomusic background service is off.\n'
      fi
      exit 1
    fi
    ours || die "A different nomusic installation owns the loaded LaunchAgent."
    if healthy; then
      printf 'The nomusic background service is running and reachable at %s.\n' "$(health_url)"
    else
      printf 'The nomusic background service is loaded but not reachable at %s. Inspect %s.\n' "$(health_url)" "$LOG"
      exit 1
    fi
    ;;
esac
