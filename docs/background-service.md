# Optional background service (macOS)

This guide covers the optional macOS path for running the backend as a per-user
LaunchAgent instead of in a Terminal window. It starts when the user logs in,
including after a restart, and launchd relaunches it after an unexpected exit.
Linux service management is handled by the host's supervisor rather than by
`service.sh`.

## Turn it on

Run this from the repository root after `install.sh` has created the virtual
environment and `nomusic auth generate` has created at least one operator key:

```sh
./service.sh install
```

The command generates an absolute-path LaunchAgent for this checkout, loads it,
and waits for the unauthenticated local liveness endpoint (`/healthz`). The
backend's authenticated `/capabilities` endpoint remains protected and is what
the extension uses to discover the running backend.

The installer offers the same action at the end of an interactive Mac install.
Use explicit flags in scripts:

```sh
./install.sh --service
./install.sh --no-service
```

The installer does not create credentials. If no valid operator key exists,
an interactive install leaves the background service off and prints the two
commands needed to generate a key and enable it. An explicit `--service` stops
with the same prerequisite instead of loading a crash-looping agent.

The service is a per-user agent, so it starts after login rather than before
login. MPS requires that logged-in GUI session. It runs as the current user,
with that user's cache, model, operator-key file and permissions.

Do not also run `nomusic serve` in a Terminal: both use the same port.

## Check, restart and turn it off

```sh
./service.sh status
./service.sh restart
./service.sh uninstall
```

status checks that this checkout owns the LaunchAgent, that launchd has loaded
it, and that the local HTTP liveness endpoint responds. A loaded but unhealthy
service is reported as an error. restart reloads the generated definition and
restarts this checkout. uninstall removes only the LaunchAgent; it preserves
cache, model, operator-key and log data.

The helper writes its log to:

```text
~/Library/Logs/nomusic-backend.log
```

Inspect it with:

```sh
tail -f ~/Library/Logs/nomusic-backend.log
```

Stopping gives active work the same shutdown grace period as Control+C in a
terminal, 60 seconds by default. Finish long exports before uninstalling.

## Settings

The LaunchAgent cannot read a shell profile. `service.sh install` saves the
allowlisted backend settings exported in the current shell, plus `PATH`,
`HF_HOME` and `HF_HUB_CACHE`. Installer-only variables and arbitrary
environment names are excluded; operator keys are never copied into the plist.
The saved names are printed.

To change a setting, export the new value and run:

```sh
./service.sh restart
```

Unexported settings are removed from the generated definition and return to
application defaults.

## Upgrades and moving the checkout

`install.sh` stops an owned running service before changing the environment and
starts it again when the installation completes. Choosing `--no-service` leaves
it off. If installation or the service health check fails, the previous
definition is restored when possible and the installer prints the recovery
commands.

There is one service label per user. If another checkout owns it, installation
fails without changing that service. Run the other checkout's
`service.sh uninstall` first, or intentionally stop it through `launchctl`
before installing this checkout.

If the definition is damaged and cannot be parsed, the helper refuses to guess
which checkout owns it. Verify that no other nomusic service should remain,
then use the recovery commands printed by `service.sh`.

Turn the service off before moving or deleting the project folder. After moving
it, run `install.sh` in the new location and then `service.sh install`.

## How it works

The generated definition is:

```text
~/Library/LaunchAgents/com.nomusic.backend.plist
```

It runs the installed nomusic serve entry point from the repository's
environment, with the repository as its working directory. The file is
generated on every install/restart, has user-only permissions, and should not
be edited by hand. Launchd starts it again after a crash, with a ten-second
throttle to avoid a tight restart loop.
