# nomusic

Watch YouTube, Facebook, and other videos with the music stripped out.
Built for people who want to avoid music for religious or personal
reasons but still want to follow the dialogue, news, lectures, or
videos they care about.

## What it does

You click a small button that nomusic adds to any video player. The
video keeps playing as normal, but the music is removed — you hear the
voices and (optionally) the background sounds, without the music.

It works on YouTube, Facebook, Twitter/X, Instagram, TikTok, Vimeo, and
hundreds of other sites with `<video>` elements.

## What you need

- A Mac with an **Apple Silicon** chip (M1, M2, M3, or M4)
- **Homebrew** installed. If you don't have it, run this in Terminal:

  ```bash
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  ```
- **Chrome**, **Brave**, **Edge**, or another Chromium-based browser

That's it. The installer handles everything else.

## Install

Open Terminal, go into the project folder, and run:

```bash
./install.sh
```

This installs everything nomusic needs (Python, ffmpeg, the audio
separation model). The first install takes a few minutes because it
downloads the AI model. After that it's quick.

## Run it

Two things have to be running for nomusic to work:

### 1. Start the local helper

In Terminal, from the project folder:

```bash
backend/.venv/bin/python backend/server.py
```

Leave that Terminal window open. You'll see log messages — that's
normal. Press `Ctrl+C` in that window when you want to stop it.

### 2. Load the browser extension (only once)

1. Open your browser and go to `chrome://extensions`
2. Toggle **Developer mode** on (top-right corner)
3. Click **Load unpacked**
4. Pick the `extension` folder inside this project
5. You'll see **nomusic** appear in your extensions list

You only have to do this once. It stays installed.

## How to use it

1. Go to any video — YouTube, Facebook, a news clip, anything with a
   video player.
2. You'll see a small **nomusic** button on the video.
3. Click it.
4. The first time you click on a new video, the button will show:
   - **Inspecting video** (a couple of seconds)
   - **Downloading video** with a percentage
   - **Removing music** with a percentage
5. Once the first chunk is ready (about 10 seconds of audio), the
   video plays automatically without music.
6. Click the button again any time to turn nomusic off.

The video pauses while waiting for the next chunk if needed, so you
never miss content.

**Re-watching the same video is instant** — nomusic remembers what it
already processed.

## Settings

Click the nomusic icon in your browser toolbar to open the settings
popup. You can change:

- **Backend URL** — leave this alone unless you know what you're doing
- **Model** — `htdemucs` (default) is the right choice for most people
- **Keep stems** — which parts of the audio to keep:
  - `vocals` (default) — just speech and singing. Most aggressive
    music removal. Best for music videos, songs, and anything where
    you mostly want to hear talking.
  - `other` — background sounds and instruments. Adding this back
    keeps cartoon sound effects, ambient noise, etc., but also lets
    some music through.
  - `drums` / `bass` — these *are* the music. Leave them off unless
    you're experimenting.

A good rule: turn on `other` if you're watching cartoons, movies, or
TV where the sound effects matter. Leave it off for music videos or
videos where you only care about the dialogue.

## Cache and storage

Each video you process is saved to your computer so re-watching is
instant. The popup shows how much space the cache is using.

- **Clear** button — wipes everything cached (click twice to confirm)
- Old cache is **automatically deleted after 7 days** so it doesn't
  grow forever

A 3-hour video is roughly 130 MB of cache. A short clip is a few MB.

## Common problems

**The button says "backend unreachable"**
The local helper isn't running. Open Terminal and run the start
command in the "Run it" section.

**The button says "Error"**
Usually the video can't be downloaded (private, age-restricted, or
the site doesn't support it). Try another video to confirm nomusic
itself is working.

**I still hear music**
Open the nomusic popup and check that **vocals** is the only stem
checked. If `other` is also checked, music will partially come through.

**Audio is slightly out of sync**
Click the nomusic button to turn it off, then click again to restart
the session. The audio resyncs to the video.

**It's taking forever on a long video**
For a 3-hour video, expect to wait 30-60 seconds before the first
audio plays (downloading the full source), then it should keep up
with playback. You can pause and come back later — nomusic remembers
where it left off.

## Privacy

Everything runs on **your computer**. The local helper downloads the
audio from the video site (just like yt-dlp), processes it with the
audio separation model locally, and serves it back to your browser.
Nothing is sent to any other server. The browser extension only talks
to `http://127.0.0.1:8723` — your own machine.

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
MLX engine ── demucs (PyTorch on MPS) ── htdemucs
```

Engines are swappable. A future ONNX, CUDA, or native MLX engine drops
in by implementing the `Engine` interface in `backend/engines/base.py`.
The current "MLX engine" is named for the Apple Silicon target; today
it runs htdemucs through `demucs` on the MPS backend.

### Layout

```
backend/
  server.py             FastAPI entrypoint
  config.py             Settings dataclass
  jobs.py               In-process job registry + worker threads
  engines/
    base.py             Abstract Engine interface
    mlx_engine.py       Apple Silicon implementation
  pipeline/
    downloader.py       yt-dlp + ffmpeg slicing
    processor.py        Chunking, mixing, encoding
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
| `NOMUSIC_ENGINE` | `mlx` | Engine name |
| `NOMUSIC_CACHE_DIR` | `~/.cache/nomusic` | Cache root |
| `NOMUSIC_CACHE_TTL_DAYS` | `7` | Days before unused entries are deleted (0 disables) |
| `NOMUSIC_CACHE_SWEEP_INTERVAL_SECONDS` | `3600` | How often the TTL sweep runs |
| `NOMUSIC_KEEP_SOURCE_AFTER_COMPLETE` | `false` | Keep yt-dlp source audio after processing (faster stem switching, more disk) |
| `NOMUSIC_CHUNK_SECONDS` | `10` | Chunk size |
| `NOMUSIC_CHUNK_OVERLAP_SECONDS` | `0.5` | Per-chunk overlap for separator context |
| `NOMUSIC_JS_RUNTIME` | auto-detected | Path to a JS runtime (deno/node/bun) for yt-dlp |

### API contract

| Method | Path | Body / response |
|---|---|---|
| GET | `/healthz` | `{ok: true}` |
| GET | `/capabilities` | Engine info, defaults, cache settings |
| POST | `/process` | `{url, model?, keep_stems?}` → `JobStatus` |
| GET | `/status/{job_id}` | `JobStatus` |
| GET | `/chunk/{job_id}/{idx}` | OGG/Opus bytes (425 if not yet ready) |
| GET | `/audio/{job_id}` | Streams concatenated OGG/Opus (425 if not complete) |
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
