# nomusic

<p align="center">
  <img src="assets/logo.png" alt="nomusic" width="180" />
</p>

Watch videos with less background music. nomusic adds a button to your browser
and processes the audio on your own computer, using a local source-separation
model. It is made for people who want to avoid music for religious or personal
reasons.

The default keeps the **vocals** stem: speech and singing. Separation can leave
some music or remove wanted sounds; it does not perfectly distinguish every
kind of music from every other sound.

The current setup is for **local use with public, finite YouTube videos**.
Other sites are experimental. Authenticated internet sharing is planned; this
version does not provide user authentication.

## What you need

- **Apple Silicon Mac with macOS 14 or newer**, or **Linux x86_64 with glibc
  2.28 or newer**. Linux automatically selects the locked CPU or NVIDIA CUDA profile.
- **Google Chrome** for the extension. Other Chromium browsers may work but
  need separate testing.
- An internet connection for installation, model downloads and source videos.
- On Mac, [Homebrew](https://brew.sh/). On Linux, **Node.js 22+ or Deno 2.3+**
  installed before running the installer.

Processing speed depends on the machine and video. CPU processing may be
slower than playback. See the [installation guide](docs/installation.md) for
platform details, developer setup and explicit CPU/NVIDIA profile selection.

## Install

Download and unzip this repository using GitHub's **Code → Download ZIP**, or
clone it:

```sh
git clone https://github.com/IbraheemTuffaha/nomusic.git
cd nomusic
```

Open Terminal in the downloaded project folder. On Mac, you can type `cd `,
drag the folder into Terminal, and press Return. Then run:

```sh
./install.sh
```

The installer sets up the pinned Python environment, checks FFmpeg and the
JavaScript runtime, and downloads and verifies the default model (about
84 MB). It may ask for permission to install missing system packages. Keep the
project folder after installation.

If the installer reports an existing environment with the wrong Python
version, follow the [migration instructions](docs/installation.md#upgrading-and-rollback).
It preserves that environment rather than deleting it.

## Start and stop

From the project folder:

```sh
backend/.venv/bin/nomusic serve
```

Keep this Terminal window open while using nomusic. Press **Control + C** to
stop it, and wait for shutdown to finish. It closes progress streams, lets
active exports finish, and waits for processing and background work to stop.
An active model operation or network request can delay shutdown. Next time,
run the same command.

The server listens at `http://127.0.0.1:8723`. A “Uvicorn running” message means
the HTTP server is listening; the model can still be loading. The extension
shows progress when you start processing a video.

## Add the extension

1. Open `chrome://extensions` in Chrome.
2. Enable **Developer mode**.
3. Click **Load unpacked** and select the project's `extension` folder.
4. Open a public YouTube video with a fixed duration.
5. When Chrome asks whether the site may access devices on your local network,
   **allow it for that site**. This lets the video page reach the local helper.

If that permission was denied, open the site's settings using the control
beside the address bar, allow **Local network access**, and reload the page.
Extension host permissions and this browser permission are separate.

## Watch and save

1. Start the helper and open a supported video.
2. Click the **nomusic** button on the video.
3. Wait while it fetches and processes the audio. Playback starts when the
   first processed chunk is available and may pause while waiting for more.
4. Click the button again to return to the original audio.

Use the download chevron on the button to save **MP3 audio** or an **MP4 video**
with processed audio. Video export can require a separate video download and
additional processing. Leave the tab open while it prepares. Resolution
choices depend on what the source provides.

To change which sounds are kept, open nomusic from Chrome's toolbar, possibly
under the puzzle-piece menu:

- **Vocals only** is the default and retains speech and singing.
- **Vocals + other** may retain more effects and ambience, but also more music.
- **Drums** and **bass** retain those musical components.

The settings panel also shows the processed-media cache and provides a clear
button. Cached work can speed up later visits; the default retention is seven
days. Downloaded model weights use a separate cache.

## Troubleshooting

**“Backend unreachable.”** Start the helper, check the backend URL in the
extension settings, and check the site's local-network permission. A denied
permission can look like an offline backend.

**Processing fails.** Check the helper's Terminal output. Private, restricted
and live videos are outside the initial supported scope. Try another public
finite video, then run `backend/.venv/bin/nomusic check-runtime` to check
FFmpeg and the YouTube JavaScript runtime.

**Music remains or wanted sounds disappear.** Try the stem settings above.
Model quality varies with the recording.

**Playback stalls or loses sync.** Toggle nomusic off and on to start a new
playback session. Long sessions and recovery after interruptions still need
further reliability work.

## Privacy

Audio separation runs on the configured backend. With the default setup,
that is your own computer. The backend contacts the source video service to
download media; installation and model setup also contact package and model
hosts. Processed audio and metadata are stored in the local cache.

## Development

The backend is Python/FastAPI with Demucs/PyTorch inference. The plain
JavaScript Manifest V3 extension schedules processed audio against the video
clock. Backend code lives in `backend/nomusic/`; `backend/server.py` remains a
compatibility launcher.

Application lifespan owns the engine, jobs and background work; importing the
server does not start them. Use the documented launch commands so shutdown
can close progress streams before draining HTTP requests.

See [installation and development](docs/installation.md) for the dependency
lock, commands, model provenance, test boundaries and upgrade workflow.

## License

MIT
