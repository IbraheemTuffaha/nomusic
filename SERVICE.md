# Running the backend as a background service (macOS)

Keep the nomusic backend running all the time, started automatically every
time you log in (including after a restart) and relaunched if it ever crashes.

This uses a macOS **LaunchAgent**. It's a per-user agent (not a system
daemon) on purpose: the engine uses the GPU/Metal (`device: mps`), which is
only available inside your logged-in user session. So the flow is:
power on → log in → backend is up within a few seconds.

## Install

1. Copy the template and fill in your paths. Replace
   `/ABSOLUTE/PATH/TO/nomusic` with the full path to this folder (run `pwd`
   from the repo root to get it):

   ```bash
   cp com.nomusic.backend.plist ~/Library/LaunchAgents/com.nomusic.backend.plist
   # then edit ~/Library/LaunchAgents/com.nomusic.backend.plist and replace
   # every /ABSOLUTE/PATH/TO/nomusic with your real path.
   ```

2. Load it into launchd:

   ```bash
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.nomusic.backend.plist
   launchctl enable gui/$(id -u)/com.nomusic.backend
   ```

3. Confirm it's serving (the model takes a few seconds to load):

   ```bash
   curl -s http://127.0.0.1:8723/capabilities | python3 -m json.tool
   ```

Logs are written to `~/Library/Logs/nomusic-backend.log`.

## Handy commands

```bash
# Check status / pid
launchctl print gui/$(id -u)/com.nomusic.backend | grep -E "state|pid"

# Restart now
launchctl kickstart -k gui/$(id -u)/com.nomusic.backend

# Stop (launchd auto-relaunches it because KeepAlive is on)
launchctl kill SIGTERM gui/$(id -u)/com.nomusic.backend

# Watch logs
tail -f ~/Library/Logs/nomusic-backend.log

# Fully disable / re-enable auto-start
launchctl bootout gui/$(id -u)/com.nomusic.backend
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.nomusic.backend.plist
```

## After moving the folder or recreating the venv

The plist has absolute paths baked in. If you move the `nomusic` folder or
recreate `backend/.venv`, update the paths in
`~/Library/LaunchAgents/com.nomusic.backend.plist`, then reload it:

```bash
launchctl bootout gui/$(id -u)/com.nomusic.backend
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.nomusic.backend.plist
```
