# nomusic

<p align="center">
  <img src="assets/logo.png" alt="nomusic" width="180" />
</p>

A free tool that lets you watch videos on YouTube, Facebook, and other
sites without hearing the music. The dialogue, narration, and other
sounds keep playing. The music is removed.

Made for people who want to avoid music for religious or personal
reasons.

---

## What you need

- A **Mac with an Apple Silicon chip** — that's any Mac sold from late
  2020 onwards. To check, click the Apple menu (top left of your
  screen) and pick **About This Mac**. If you see **M1**, **M2**,
  **M3**, or **M4** anywhere, you're good.
- About **20 minutes** for first-time setup. After that, starting
  nomusic takes 10 seconds.

> Running on a **Linux** box instead — with an NVIDIA GPU or just a CPU?
> See [Running on Linux](#running-on-linux) below. The Mac steps that
> follow are the easy path for everyone else.

You don't need to know anything technical. The steps below tell you
exactly what to type or click.

---

## Step 1: Get the project files

1. Open this link in your browser:
   **https://github.com/IbraheemTuffaha/nomusic**
2. Look for a green button labeled **Code** near the top right of the
   list of files. Click it.
3. A small menu appears. Click **Download ZIP** at the bottom.
4. Your browser saves a file called `nomusic-main.zip` to your
   **Downloads** folder.
5. Open the **Downloads** folder (it's in the dock, or in the Finder
   sidebar). Double-click `nomusic-main.zip`. It unzips into a folder
   called `nomusic-main`.
6. Move that folder somewhere you can find it again — for example, drag
   it to your **Documents** folder.

That's it for downloading.

---

## Step 2: Open the Terminal app

Terminal is a built-in Mac app that lets you type commands. Don't worry
if you've never used it — you'll just copy and paste a few things.

To open Terminal:
1. Press **Command + Space** on your keyboard. A search box appears in
   the middle of the screen.
2. Type **Terminal** and press **Return**.

A window opens with some text and a blinking cursor. That's Terminal.
You'll type things into it and press **Return** to make them happen.

Tip: if you ever can't see the cursor, click inside the Terminal
window first.

---

## Step 3: Install Homebrew (one time only)

Homebrew is a free helper program that nomusic uses to install a few
pieces it needs. If you don't already have it, install it now. (If
you're not sure, just do this step — it won't break anything.)

Copy the line below, paste it into Terminal, and press **Return**:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

It will ask for your Mac's login password. Type it and press Return.
**You won't see anything as you type — that's normal**, the characters
are hidden for security. Just type it carefully and press Return.

The install takes a few minutes. When it's done you'll see a message
ending in **Installation successful!** and the cursor will come back.

---

## Step 4: Install nomusic

You'll now tell Terminal to "go into" the nomusic folder and run the
installer.

In Terminal:

1. Type **`cd `** — that's the letters `c`, `d`, then a single space.
   Leave the space at the end. Don't press Return yet.
2. Open Finder, find the `nomusic-main` folder you saved in Step 1,
   and **drag it onto the Terminal window**. The folder's full path
   appears automatically after what you typed.
3. Now press **Return**. The prompt will change to show that you're
   "inside" the nomusic folder.
4. Type this and press **Return**:

   ```bash
   ./install.sh
   ```

This downloads everything nomusic needs, including an 80 MB audio
model. It takes 5-10 minutes the first time. Lots of text will scroll
by — that's normal. When you see a message like **Install complete.**
the installer is done.

You only do this once.

---

## Step 5: Start nomusic

Every time you want to use nomusic, you start the "helper" first. This
is the part that actually removes the music from videos.

In Terminal, still inside the nomusic folder from Step 4, type this
and press **Return**:

```bash
backend/.venv/bin/python backend/server.py
```

You'll see a few log lines, the last one ending with something like
`Uvicorn running on http://127.0.0.1:8723`. That means it's ready.

**Keep this Terminal window open** while you're using nomusic. Closing
it stops nomusic.

To stop nomusic later, click the Terminal window and press
**Control + C** on your keyboard.

Next time you want to use nomusic after a restart, you only need to
repeat Step 5 — Steps 1 through 4 stay done.

---

## Step 6: Add nomusic to your browser (one time only)

This step adds the nomusic button to videos in your browser. It works
for **Chrome**, **Brave**, **Edge**, **Arc**, and most other modern
browsers.

1. Open your browser.
2. Click in the address bar and type **`chrome://extensions`** then
   press Return.
3. Look at the top right of the page. There's a switch labeled
   **Developer mode**. Turn it on.
4. Three new buttons appear at the top left. Click **Load unpacked**.
5. A file picker opens. Find your `nomusic-main` folder, then **go
   into the `extension` folder inside it**, then click **Select**.
6. **nomusic** appears in your list of extensions. Done.

You only do this once. The extension stays installed.

---

## How to use it

1. Make sure the helper is running (Step 5). If you're not sure, look
   at the Terminal window — if it's still showing log messages and
   doesn't say a prompt is back, it's running.
2. Go to any video — YouTube, Facebook, Twitter/X, Instagram, news
   sites, anywhere with a video player.
3. You'll see a small **nomusic** button on the video.
4. Click it.
5. The first time you click on a video, you'll see status messages
   ticking through:
   - **Preparing** (a couple of seconds)
   - **Fetching 45%**
   - **Removing 23%**
6. As soon as the first chunk is processed, the video plays on its own
   without music — it doesn't wait for the whole thing to download. The
   video pauses briefly any time it's waiting for more audio.
7. Click the button again any time to turn nomusic off and hear the
   original audio.

**Save the result.** A small download chevron (⤵) appears on the button
while it's working and once it's done. Click it for a menu:
   - **MP3** — saves just the music-stripped audio.
   - **MP4 at a chosen resolution** (Best / 4K / 1440p / 1080p / 720p /
     480p) — saves the original video with its audio replaced by the
     music-stripped track. The button shows live **Fetching %** then
     **Encoding %** while it prepares. (The first save at a given
     resolution re-downloads the video, so it can take a while and use
     more disk; repeat saves are fast.) Resolution is a ceiling — if the
     video tops out below your pick, you get its best available.

You can pick a format **before** processing finishes — the button shows
**Preparing %**, keeps working even if you pause the video or stop
watching (just leave the tab open), and saves automatically the moment
it's ready. No need to sit through the whole video to get the file.

**Re-watching the same video is instant** — nomusic remembers the work
it already did, for up to 7 days.

---

## Settings

Click the small nomusic icon in your browser's toolbar (usually top
right, sometimes hidden under a puzzle-piece icon) to open the
settings panel.

The main option is **Keep stems** — which parts of the audio to keep:

- **vocals only** *(default)*: just speech and singing. The most
  aggressive music removal. Best for music videos, songs, or
  anything where you mainly want to hear talking.
- **vocals + other**: also keeps background sounds like cartoon
  sound effects, ambient noise, and so on. Best for cartoons,
  movies, or TV shows where sound effects matter. Some background
  music can come through this way.
- **drums** and **bass** *are* the music. Leave them off unless
  you're experimenting.

The panel also shows how much space the cache is using on your Mac,
with a **Clear** button if you want to wipe it. The cache also clears
itself automatically after 7 days.

---

## If something goes wrong

**The button says "backend unreachable"**
The helper isn't running. Go back to Step 5 and start it.

**The button says "Error"**
The video can't be downloaded. This usually means it's private,
age-restricted, or from a site that isn't supported. Try a different
video first to make sure nomusic itself is working.

**I can still hear music**
Open the settings panel. If **other** is checked alongside vocals,
some music can come through. Uncheck it for stronger removal.

**The audio drifts out of sync**
Click the nomusic button to turn it off, then click it again. It
resyncs.

**A long video is slow to start**
The first few seconds go to looking up the video and starting the
download. Separation then begins on the early part while the rest keeps
downloading, so playback starts well before the whole file is fetched.
You can pause and come back later — nomusic remembers where it left off
and only does each chunk once.

**I closed Terminal by accident**
That stops the helper. Open Terminal again, run Step 5, and you're
back. (Steps 1-4 don't have to be repeated.)

---

## Privacy

Everything happens on **your own computer**. The helper downloads the
audio from the video site you're watching, processes it locally using
the audio-separation model on your machine, and sends the result back
to your browser. Nothing goes to anyone else's server. The browser
extension only talks to your own machine.

---

## Running on Linux

nomusic also runs on Linux — with an **NVIDIA GPU** (CUDA) for full speed, or
on **CPU** anywhere (slower, but works). Run the browser and the backend on the
**same machine**; the extension talks to `http://127.0.0.1:8723` out of the box,
so no extra configuration is needed.

The installer is `apt`-based (Debian/Ubuntu). On other distros, install
`python3` (+ `venv`/`pip`), `ffmpeg`, `git` and a JS runtime (`node`/`deno`)
yourself, then create the venv, `pip install torch torchaudio` (PyPI's default
Linux wheel is CUDA-enabled), and `pip install -r backend/requirements.txt`.

1. Get the project files (`git clone https://github.com/IbraheemTuffaha/nomusic`
   or download + unzip the ZIP).
2. From the project folder, run the installer:

   ```bash
   ./install.sh
   ```

   It detects your platform automatically. On Linux it installs the system
   packages via `apt`, then installs **CUDA torch if it finds an NVIDIA GPU**
   (via `nvidia-smi`) or **CPU torch** otherwise, and prints whether torch can
   see the GPU. If your driver needs a specific CUDA build, pin it with
   `NOMUSIC_CUDA` — maintained tags are `cu118` / `cu126` / `cu128`
   (e.g. `NOMUSIC_CUDA=cu126 ./install.sh`).

3. Start the backend:

   ```bash
   backend/.venv/bin/python backend/server.py
   ```

   Check it picked the right device:

   ```bash
   curl -s http://127.0.0.1:8723/capabilities | python3 -m json.tool
   ```

   The `device` field reads `cuda (via demucs)` on a GPU box, or
   `cpu (via demucs)` on a CPU-only box.

4. Load the extension in Chrome on the same machine: open `chrome://extensions`,
   turn on **Developer mode**, click **Load unpacked**, and pick the
   `extension` folder. Then use it exactly as on a Mac (see **How to use it**).

On an NVIDIA GPU with spare VRAM you can raise throughput by separating more
chunks per inference call — set `NOMUSIC_GPU_BATCH` higher (default `2`, tuned
for Apple Silicon).

---

## For developers

### Architecture

```
Browser extension (Manifest V3)
    │  HTTP
    ▼
FastAPI on 127.0.0.1:8723
    │
    ▼
Engine abstraction (engines/base.py)
    │
    ▼
MLX engine ── demucs (PyTorch on MPS / CUDA / CPU) ── htdemucs
```

Engines are swappable. A future ONNX or native MLX engine drops in by
implementing the `Engine` interface in `backend/engines/base.py`. The current
"MLX engine" is named for the strategic Apple Silicon target, but it runs
htdemucs through `demucs` on whatever torch device is available — MPS on Apple
Silicon, CUDA on an NVIDIA GPU, or CPU — auto-detected by `_pick_device()`. It's
also registered under the alias `demucs`.

### Layout

```
backend/
  server.py             FastAPI entrypoint
  config.py             Settings dataclass
  jobs.py               In-process job registry + worker threads
  engines/
    base.py             Abstract Engine interface
    mlx_engine.py       demucs via torch (MPS / CUDA / CPU)
  pipeline/
    downloader.py       yt-dlp + ffmpeg slicing (audio + on-demand video)
    processor.py        Chunking, mixing, encoding
    export.py           Chunk concat + ffmpeg commands for MP3/MP4 download
    cache.py            ~/.cache/nomusic with TTL sweep
  tests/                pytest suite (no torch / yt-dlp needed)
extension/
  manifest.json
  background.js
  content.js            Button + Web Audio sync + buffer pausing
  content.css
  page-script.js        Main-world prototype patch for volume control
  popup.html / popup.js
```

### Environment variables

All optional, all prefixed `NOMUSIC_`:

| Variable | Default | What it does |
|---|---|---|
| `NOMUSIC_HOST` | `127.0.0.1` | Bind address |
| `NOMUSIC_PORT` | `8723` | Listen port |
| `NOMUSIC_ENGINE` | `mlx` | Engine name (alias: `demucs`) |
| `NOMUSIC_CACHE_DIR` | `~/.cache/nomusic` | Cache root |
| `NOMUSIC_CACHE_TTL_DAYS` | `7` | Days before unused entries are deleted (0 disables) |
| `NOMUSIC_CACHE_SWEEP_INTERVAL_SECONDS` | `3600` | How often the TTL sweep runs |
| `NOMUSIC_KEEP_SOURCE_AFTER_COMPLETE` | `false` | Keep yt-dlp source audio after processing (faster stem switching, more disk) |
| `NOMUSIC_CHUNK_SECONDS` | `10` | Chunk size |
| `NOMUSIC_CHUNK_OVERLAP_SECONDS` | `0.5` | Per-chunk overlap for separator context |
| `NOMUSIC_GPU_BATCH` | `2` | Chunks separated per batched inference call (1 disables batching); raise it on a big GPU with spare VRAM |
| `NOMUSIC_IDLE_TIMEOUT_SECONDS` | `10` | How long a worker keeps running after you pause or close the tab before it abandons the job and releases the GPU; resume re-spawns from cache (0 disables) |
| `NOMUSIC_SSE_KEEPALIVE_SECONDS` | `15` | Gap between SSE keep-alive comments on a quiet status stream |
| `NOMUSIC_MEMORY_GC_INTERVAL_SECONDS` | `3600` | How often the in-memory job map is reclaimed for jobs whose disk cache is gone (0 disables) |
| `NOMUSIC_DOWNLOAD_RATELIMIT` | unset | Artificial download cap for testing slow links — raw bytes/sec or `K`/`M` suffix (e.g. `200K`) |
| `NOMUSIC_PROGRESSIVE` | `true` | Start separating early chunks from the partial download instead of waiting for the whole file (falls back automatically if the partial isn't decodable); set `0` to force download-once |
| `NOMUSIC_RELOAD` | `false` | Dev only: watch `backend/*.py` and auto-restart on save (`1`/`true`) |
| `NOMUSIC_DEBUG` | `false` | Raise backend logging to DEBUG (progressive download/gate diagnostics, etc.) |
| `NOMUSIC_JS_RUNTIME` | auto-detected | Path to a JS runtime (deno/node/bun) for yt-dlp |

### API contract

| Method | Path | Body / response |
|---|---|---|
| GET | `/healthz` | `{ok: true}` |
| GET | `/capabilities` | Engine info, defaults, cache settings |
| POST | `/process` | `{url, model?, keep_stems?}` → `JobStatus` |
| POST | `/process/{job_id}/prioritize` | `{from_chunk}` → `{applied}`; reorder pending chunks around a seek |
| GET | `/status/{job_id}` | `JobStatus` |
| GET | `/events/{job_id}` | `text/event-stream` of `JobStatus` updates (204 if unknown); replaces polling |
| GET | `/chunk/{job_id}/{idx}` | OGG/Opus bytes (425 if not yet ready) |
| GET | `/audio/{job_id}` | Streams concatenated OGG/Opus; `?format=mp3` transcodes to MP3 (425 if not complete) |
| GET | `/video/{job_id}` | Original video with the stripped audio muxed in, as MP4; `?max_height=N` caps resolution (425 if not complete) |
| GET | `/video/{job_id}/progress` | `{phase, percent}` for the in-flight MP4 export (polled by the menu) |
| GET | `/cache` | Cache stats |
| POST | `/cache/clear` | Wipes the cache |

`JobStatus` includes `phase` (`queued` / `probing` / `downloading` /
`processing` / `ready` / `error`), `phase_progress` (0..1 or null),
`phase_label`, plus debugging fields (`chunks_ready`, `total_chunks`,
`duration_seconds`, `title`).

### Running the tests

```bash
PYTHONPATH=backend backend/.venv/bin/python -m pytest backend/tests -v
```

The suite stubs the engine and the downloader, so it runs on any
platform without torch or yt-dlp installed.

## License

MIT
