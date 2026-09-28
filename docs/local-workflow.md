# First local run

This checklist uses Chrome and the helper on the same machine, with the
default `htdemucs` model and vocals stem. Choose a short, public YouTube video
with a fixed duration. Private, restricted and live videos are outside this
initial scope. Processing may be slower than playback on a CPU.

Keep the default loopback address. This version has no user authentication;
internet sharing is outside this workflow. See the
[installation guide](installation.md) for supported platforms and prerequisites.

## 1. Install and check the helper

From the project folder, with any existing helper stopped:

```sh
./install.sh
backend/.venv/bin/nomusic doctor
```

Installation fetches the pinned dependencies and default model. Doctor should
finish successfully, including its short inference check. It uses generated
audio and cached weights; it does not contact YouTube or play sound. If a
check fails, follow its remedy before continuing.

## 2. Start and check readiness

In the same Terminal window:

```sh
backend/.venv/bin/nomusic serve
```

Leave it running. In a second Terminal window:

```sh
curl --include http://127.0.0.1:8723/readyz
```

Expect HTTP **200** and `"state":"ready"`. HTTP **503** with `starting` or
`warming` means startup is still in progress; wait and check again. If the
state is `failed`, inspect the helper's log, stop it, fix the reported cause
and restart. A “Uvicorn running” message alone does not mean the model is ready.

## 3. Connect the extension

1. Open `chrome://extensions`, enable **Developer mode**, and **Load unpacked**
   from the project's `extension` folder. If already installed, reload the
   extension after updating its files, then reload the video page.
2. Open nomusic from Chrome's toolbar. Check the backend URL is
   `http://127.0.0.1:8723`, the model is **htdemucs**, and only **vocals** is
   selected. Settings save automatically. The **backend up** label confirms
   reachability, not model readiness or access to YouTube.
3. Open the chosen video. Allow the site's **Local network access** permission
   when Chrome asks. If previously denied, allow it in the site's settings
   beside the address bar and reload. This is separate from the extension's
   permissions.

## 4. Play and save

For this check, turn off YouTube autoplay so a short clip stays on the chosen
page. Click the **nomusic** button on the video and wait for processed playback.
The player may pause while waiting for a chunk. Try pause/resume, a backward
seek and a forward seek, allowing processing to catch up each time.

Use the button's download chevron to select **MP3 — audio only**, then try
**480p** under **Video (MP4)**. Leave the tab open until each download finishes.
An export requested before processing finishes waits for the rest of the
track; you can pause playback while it prepares. MP4 preparation may need a
separate source download. Check that both files finish downloading and have
the expected duration; actual video resolution depends on the source.

Click nomusic again to return to original audio. After changing model or stem
settings, toggle it off and on to start a new session. Vocals include speech
and singing; a working pipeline does not guarantee perfect music removal.

**Current failure behavior:** a processing error can restore original audio
and resume the player. Mute the site or browser before retrying if avoiding
that audio is essential. Keep browser output muted when testing through a
shared or forwarded environment; observing audio activity is not a listening
test.

## 5. Stop and restart

Finish exports, pause the video, and press **Control+C** in the helper's
Terminal. Wait for **Service shutdown complete**. An active model operation
or network request can delay shutdown.

Run `backend/.venv/bin/nomusic serve` again, check readiness, and toggle nomusic
on for the same video. Reopen the popup or reload the page if it still shows
the helper as offline. Completed work may be reused from disk. Media and model
caches survive an ordinary restart.

To clear processed media, finish playback and exports, then click **Clear**
and **Confirm** in the popup. Model weights are kept separately.

## When a check fails

| Observation | Next step |
| --- | --- |
| Doctor fails | Follow its runtime, model or storage remedy; repeat doctor before starting the helper. |
| Readiness stays failed | Check the helper's log, stop it, fix the cause and restart. |
| Popup works but the video says “backend unreachable” | Check the site's local-network permission and reload the page. |
| YouTube reports HTTP 429 or human verification | Source acquisition is blocked independently of local readiness. The browser page may still play. Retry later or test the same installation on a network where acquisition is available; record the blocked run as incomplete. |
| Playback stalls or loses sync | Mute the site or browser, toggle nomusic off/on, and retry. Recovery and long-session reliability still need further work. |

The [verification runner](verification.md) checks the local pipeline with
generated media when a source website is unavailable. Its success does not
complete a live YouTube check. Keep local diagnostics and browser profiles
private; share only reviewed error details when reporting a problem.
