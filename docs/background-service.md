# Optional background service (macOS)

This guide covers the optional macOS path for running the backend as a per-user
LaunchAgent instead of in a Terminal window. It starts when the user logs in,
including after a restart, and launchd relaunches it after an unexpected exit.

The active M6 deployment runs inference on the Ubuntu/NVIDIA laptop. This
LaunchAgent is therefore a convenience for running nomusic locally on a Mac;
it is not required by the laptop deployment. Linux service management is
handled by the host's supervisor rather than by `service.sh`.

## Turn it on

Run this from the repository root after install.sh has created the virtual
environment and you have generated at least one operator key:

    ./service.sh install

The command generates an absolute-path LaunchAgent for this checkout, loads it,
and waits for the unauthenticated local liveness endpoint (/healthz). The
backend's authenticated readiness endpoint remains protected; the extension
reports when model warmup is complete.

The installer offers the same action at the end of an interactive Mac install.
Use explicit flags in scripts:

    ./install.sh --service
    ./install.sh --no-service

The installer does not create credentials. If you use --service during the
same install, generate the key first on a later command and then run
service.sh install; the helper refuses to start without an operator-key file.

The service is a per-user agent, so it starts after login rather than before
login. MPS requires that logged-in GUI session. It runs as the current user,
with that user's cache, model, operator-key file and permissions.

Do not also run nomusic serve in a Terminal: both use the same port.

## Check, restart and turn it off

    ./service.sh status
    ./service.sh restart
    ./service.sh uninstall

status checks that this checkout owns the LaunchAgent, that launchd has loaded
it, and that the local HTTP liveness endpoint responds. A loaded but unhealthy
service is reported as an error. restart reloads the generated definition and
restarts this checkout. uninstall removes only the LaunchAgent; it preserves
cache, model, operator-key and log data.

The helper writes its log to:

    ~/Library/Logs/nomusic-backend.log

Inspect it with:

    tail -f ~/Library/Logs/nomusic-backend.log

Stopping gives active work the same shutdown grace period as Control+C in a
terminal, 60 seconds by default. Finish long exports before uninstalling.

## Settings

The LaunchAgent cannot read a shell profile. service.sh install saves the
backend settings exported in the current shell, plus PATH, HF_HOME and
HF_HUB_CACHE. It derives the backend settings from the installed configuration
schema and excludes installer-only variables and arbitrary environment names;
operator keys are never copied into the plist. The saved names are printed.

To change a setting, export the new value and run:

    ./service.sh restart

Unexported settings are removed from the generated definition and return to
application defaults.

## Upgrades and moving the checkout

install.sh stops an owned running service before changing the environment and
restores it when the installation completes. If installation fails, the
service stays off and the installer prints the recovery command.

There is one service label per user. If another checkout owns it, installation
fails without changing that service. Run the other checkout's service.sh
uninstall first, or intentionally stop it through launchd before installing
this checkout.

Turn the service off before moving or deleting the project folder. After moving
it, run install.sh in the new location and then service.sh install.

## How it works

The generated definition is:

    ~/Library/LaunchAgents/com.nomusic.backend.plist

It runs the installed nomusic serve entry point from the repository's
environment, with the repository as its working directory. The file is
generated on every install/restart, has user-only permissions, and should not
be edited by hand. Launchd starts it again after a crash, with a ten-second
throttle to avoid a tight restart loop.
