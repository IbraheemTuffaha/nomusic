# Local use and troubleshooting

First install and load the extension using the [README](../README.md).
This guide assumes Chrome and the helper share a machine, using
`http://127.0.0.1:8723`. Start with a public YouTube video with a fixed duration;
private, restricted and live videos are outside the initial supported scope.

## Check readiness

Start `backend/.venv/bin/nomusic serve` in the project folder. In another terminal:

```sh
curl --include http://127.0.0.1:8723/readyz
```

HTTP **200** with `"state":"ready"` means startup checks and default-model loading
passed. HTTP **503** with `starting` or `warming` means wait and retry. A `failed`
state needs its logged cause fixed and the helper restarted. Readiness does not
check YouTube availability or perform inference on each request. The extension's
**backend up** label confirms reachability only.

## Playback and settings

Click nomusic on the video and wait for processed playback. Try pause/resume and
a backward/forward seek, allowing processing to catch up. CPU processing may be
slower than playback. The toolbar popup saves settings automatically:

| Setting | Default | Effect |
| --- | --- | --- |
| Backend URL | `http://127.0.0.1:8723` | Helper used by the extension |
| Model | `htdemucs` | `htdemucs_ft` uses four fine-tuned models and needs more processing |
| Keep stems | `vocals` | Retains speech and singing; `other` adds ambience/effects and potentially music; `drums`/`bass` retain those components |

Toggle nomusic off and on after changing model or stems. A successful process
does not guarantee perfect music removal; compare speech clarity and residual
music on your own recordings.

**Processing failures can restore original audio and resume playback.** Mute
the site or browser before retrying if avoiding that audio is essential.

## Exports and cache

The download chevron beside nomusic offers **MP3 — audio only** and **Video
(MP4)** at several resolutions. You can request either before processing
finishes; keep the tab open until the download completes. Pausing playback
does not cancel export preparation. MP4 may require another source download.
Check saved files have their full expected duration; resolution depends on
the source and current downloader fallbacks.

Completed work is cached for reuse, with seven-day default retention. Finish
playback and exports before clearing it: open the popup, click **Clear**, then
**Confirm**. Model weights and files already saved to Downloads are preserved.

## Stop and restart

Pause playback and finish exports, then press **Control+C** in the helper's
terminal. If work is active, it reports a shutdown wait of up to 60 seconds.
A second **Control+C** forces immediate exit. Startup model loading by itself
does not cause a long wait. Completed media/model caches survive shutdown;
unfinished exports may need to be requested again after a forced exit.

Start the helper again, check readiness, and toggle nomusic off/on for the
video. Reopen the popup or reload the page if needed. Completed audio can be
reused; interrupted jobs do not yet have reliable automatic reconnect/recovery.

## When a check fails

| Observation | Next step |
| --- | --- |
| Installation or doctor fails | Follow the displayed remedy; see [installation](installation.md). |
| Readiness remains failed | Inspect the helper log, fix the cause and restart. |
| Popup works but video says “backend unreachable” | Allow the site's **Local network access** permission in Chrome site settings, then reload. |
| YouTube returns HTTP 429 or human verification | Acquisition is blocked independently of local readiness, even if browser playback works. Retry later or on a network where downloads are available. |
| Music remains or effects disappear | Adjust retained stems; results depend on the recording. |
| Playback stalls or drifts | Mute the site/browser, toggle nomusic off/on, and retry; long-session recovery still has limitations. |

The [verification runner](verification.md) tests generated media independently
of YouTube. When reporting a problem, include versions, the video URL and reviewed
error details. Keep raw logs and browser profiles local unless checked for
private paths, credentials or personal information.
