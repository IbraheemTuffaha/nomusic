# nomusic

<p align="center">
  <img src="assets/logo.png" alt="nomusic" width="180" />
</p>

Watch videos with less background music. nomusic adds a button to videos in
Chrome and separates their audio on your own computer. It is made for people
who want to avoid music for religious or personal reasons.

The default keeps **vocals**, including speech and singing. Some music may
remain, and some wanted sounds may be removed. Start with a **public YouTube
video with a fixed duration**; other sites are experimental.

This version is for local use and has no user authentication. Keep the backend
on its default loopback address; authenticated internet sharing is not ready.

## What you need

- **Apple Silicon Mac, macOS 14+**, or **Linux x86_64, glibc 2.28+**.
  On Mac, choose **Apple menu → About This Mac** to check the chip and OS.
- **Google Chrome**. Other Chromium browsers need separate testing.
- Internet access for installation, model downloads and source videos.
- On Mac, [Homebrew](https://brew.sh/); on Linux, **Node.js 22+ or Deno 2.3+**.

Linux installation selects CPU or NVIDIA CUDA automatically. Native Mac
installation provides MPS acceleration when available. CPU processing can be
slower than playback. See [platforms and profiles](docs/installation.md#platforms-and-profiles)
for requirements and hardware-testing limits.

## 1. Get the files and open Terminal

1. On this repository's GitHub page, click **Code → Download ZIP**.
2. Unzip it and move the project folder somewhere you will keep it, such as
   Documents. The extension will continue to use files from this folder.
3. On Mac, press **Command + Space**, type **Terminal**, and press Return.
4. Type `cd `, including the trailing space, drag the project folder from
   Finder into Terminal, and press Return. Linux users can open a terminal
   in the project folder.

If you use Git, these commands replace the download/unzip steps:

```sh
git clone https://github.com/IbraheemTuffaha/nomusic.git
cd nomusic
```

## 2. Install and check

On Mac, install Homebrew first by following the command and **Next steps** at
[brew.sh](https://brew.sh/). If it asks for your login password, Terminal hides
characters while you type. On Linux, install a supported Node.js or Deno
runtime before continuing.

From the project folder, with any existing nomusic helper stopped:

```sh
./install.sh
backend/.venv/bin/nomusic doctor
```

Installation creates the pinned Python environment, checks prerequisites and
downloads the default model, about 84 MB. It can request permission to install
missing system packages. Doctor runs a short, silent inference check and should
finish with **Local checks passed.** Follow any reported remedy before continuing.

An older default Python environment is preserved as `backend/.venv.bak` during
migration. See [upgrading and rollback](docs/installation.md#upgrading-and-rollback)
if you already have a backup or use a custom environment.

## 3. Start the helper

```sh
backend/.venv/bin/nomusic serve
```

Keep this Terminal window open. The helper listens at `http://127.0.0.1:8723`;
its model may still be loading after the listening message appears. The
[local-use guide](docs/local-workflow.md) explains checking readiness.

To stop, press **Control + C**. Active processing or requests get up to
60 seconds to finish stopping; a second **Control + C** forces exit immediately.
Stopping while only the startup model load is running exits promptly.
Next time, open Terminal in the project folder and run the same serve command.

## 4. Add the extension

1. Open `chrome://extensions` in Chrome and turn on **Developer mode**.
2. Click **Load unpacked** and select the project's `extension` folder.
3. Open nomusic from the toolbar, possibly under the puzzle-piece icon.
   Keep the backend URL `http://127.0.0.1:8723`, model **htdemucs** and
   **vocals** selected. Settings save automatically.
4. Open a public YouTube video. If Chrome requests **Local network access**,
   allow it for the video site so the page can reach the helper.

If permission was denied, open the site's settings beside the address bar,
allow **Local network access**, and reload. After updating extension files,
reload the extension on `chrome://extensions`, then reload your video tabs.

## 5. Watch and save

Click **nomusic** on the video. Playback starts when processed audio becomes
available and can pause while more is prepared. Click again to return to the
original audio. For a short first test, turn YouTube autoplay off.

Use the download chevron beside the button for **MP3 audio** or **MP4 video**.
You can request an export before processing finishes; leave the tab open until
the file downloads. MP4 preparation may download the video separately.

**A processing error keeps original audio suppressed and pauses the player.**
The recovery panel offers **Retry**, which preserves the current volume and
play/pause intent, and **Return to original**, which explicitly restores the
native track.

See [local use and troubleshooting](docs/local-workflow.md) for stem settings,
exports, cache clearing and recovery after a restart.

## Privacy and development

Separation runs on the configured backend, your own computer by default.
The backend downloads media from the source site and stores processed audio
and metadata locally. Installation also contacts package and model hosts.

- [Installation and development](docs/installation.md): profiles, upgrades,
  model storage and editable development.
- [Reference](docs/reference.md): architecture, all backend settings and API.
- [Verification](docs/verification.md): installed-package tests, CPU/extension
  smoke and their limits.

## License

MIT
