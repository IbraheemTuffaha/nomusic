# nomusic

A browser extension + local backend that strips music from videos as you watch
them on YouTube, Facebook, and other sites — for people who want to avoid music
for religious or personal reasons.

## How it works

1. You click the **🎙 nomusic** button injected into any page with a `<video>`.
2. The extension sends the page URL to a local FastAPI server (`127.0.0.1:8723`).
3. The server downloads the audio with `yt-dlp`, runs it through a Demucs MLX
   model on your Apple Silicon GPU, keeps the vocals + ambient stems and drops
   drums + bass.
4. The extension mutes the video and plays the music-free audio in sync.

Processing is **chunked and streaming** — playback starts on the first 30-second
chunk while the rest is still being separated. Re-watches are instant (cached).

## Requirements

- macOS on Apple Silicon (M1/M2/M3/...). The default engine uses MLX.
- Homebrew (for `ffmpeg`, Python 3.11+)
- Chrome, Edge, Brave, or another Chromium-based browser (Manifest V3)

## Install

```bash
./install.sh
```

This installs Python 3.11 + ffmpeg via Homebrew if needed, creates a venv in
`backend/.venv`, installs the backend dependencies, and clones the `demucs-mlx`
implementation into `vendor/`.

## Run

```bash
# Start the backend
backend/.venv/bin/python backend/server.py
```

Then load the extension:

1. Open `chrome://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked** and pick the `extension/` folder
4. Visit any video page and click the **🎙 nomusic** button

## Architecture

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
MLX engine ── demucs-mlx ── htdemucs
```

Engines are swappable. A future ONNX or CUDA engine can drop in by implementing
the `Engine` interface in `backend/engines/base.py` — no server changes needed.

## Layout

```
backend/
  server.py             FastAPI entrypoint
  config.py             Settings dataclass
  engines/
    base.py             Abstract Engine
    mlx_engine.py       Apple Silicon implementation
  pipeline/
    downloader.py       yt-dlp wrapper (full + chunked)
    processor.py        Chunking + crossfade
    cache.py            ~/.cache/nomusic
  tests/
extension/
  manifest.json
  background.js
  content.js            Button + audio sync
  content.css
  popup.html / popup.js
```

## License

MIT
