# Background service (macOS)

On a Mac the helper can run in the background instead of in a Terminal window
you keep open. It starts when you log in, including after a restart, and starts
again if it crashes. Linux has no equivalent yet; use `nomusic serve` there.

## Turn it on

`./install.sh` asks at the end when you run it in Terminal:

```text
Keep nomusic running in the background? [Y/n]
```

Press **Return** to accept. Nothing needs editing: the installer writes the
service definition for this project folder and starts the helper. To turn it on
later, or after answering no:

```sh
./service.sh install
```

Scripted installs are not asked. `./install.sh --service` turns it on without
the question; `--no-service` skips the question and leaves an existing service
as it is.

The extension's **backend up** label, or the
[readiness check](local-workflow.md#check-readiness), shows when the helper is
reachable. Do not also run `nomusic serve` in a terminal: both use the same port.

## Check, restart and turn off

```sh
./service.sh status      # on or off
./service.sh install     # restart, or apply changed settings
./service.sh uninstall   # stop it and remove it from login
```

The helper writes its log to `~/Library/Logs/nomusic-backend.log`:

```sh
tail -f ~/Library/Logs/nomusic-backend.log
```

Stopping gives active work the same grace period as **Control+C** in a
terminal, 60 seconds by default, and the command waits for it. launchd on
macOS 26 does not wait longer than 60 seconds even when
`NOMUSIC_SHUTDOWN_GRACE_SECONDS` is larger. Finish exports first. Media and
model caches are kept.

Turn the service off before moving or deleting the project folder. After
moving it, reinstall in the new location and turn the service on there.

## Settings

The background helper does not read your shell profile. `./service.sh install`,
and the installer when it turns the service on, save these from the Terminal
session that runs them:

- `PATH`, so the helper finds the same FFmpeg and JavaScript runtime.
- Exported [`NOMUSIC_*` settings](reference.md#backend-settings), `HF_HOME`
  and `HF_HUB_CACHE`. The names saved are printed.

To change a setting, export the new value and run `./service.sh install` again.
Values are not remembered between runs: a setting that is not exported next
time is dropped, including when `./install.sh` restarts the service after an
upgrade. Keep permanent settings exported in your shell profile.

## Upgrades

`./install.sh` turns a running service off before it changes the environment
and on again when it finishes, without asking. If the installation fails, the
service stays off and the installer prints the command that turns it back on.
See [upgrading and rollback](installation.md#upgrading-and-rollback).

There is one service per user. Installing in a second project folder leaves a
service that runs the first one alone; answering yes to the question, or
`./service.sh install`, moves it to the folder you run it from.

## How it works

The service is a per-user launchd agent labelled `com.nomusic.backend`. Its
definition, `~/Library/LaunchAgents/com.nomusic.backend.plist`, is generated;
the next install overwrites any edits. It runs `nomusic serve` from the
installed environment, with the project folder as its working directory.

It is an agent rather than a system daemon because MPS acceleration is only
available inside a logged-in session. The helper therefore starts at login,
not at boot.

launchd starts the helper again whenever it exits, at most once every ten
seconds. A helper that cannot start keeps retrying; the log shows why.
