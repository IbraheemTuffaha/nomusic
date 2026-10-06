# Architecture and reference

## Architecture

The Manifest V3 extension discovers videos, submits processing jobs and follows
their progress over Server-Sent Events (SSE). It fetches Opus chunks and schedules
decoded audio against the video's clock. A page bridge suppresses original
audio; the scheduler handles drift and pitch-preserving speed changes.

FastAPI owns an engine, cache, job registry and maintenance work per application
lifespan. Jobs run in threads with serialized processing; Demucs/PyTorch selects
MPS, CUDA or CPU. The current engine is named `mlx`, with `demucs` as an alias;
it is not an MLX implementation. Cache metadata and completed chunks permit
reuse. MP3/MP4 exports preserve sample-aware chunk concatenation and use FFmpeg.

| Location | Responsibility |
| --- | --- |
| `backend/nomusic/__main__.py`, `cli.py` | Installed commands and direct URL processing |
| `server.py`, `serving.py`, `services.py` within that package | API composition, serving/shutdown and owned service lifecycle |
| `routes/`, `jobs.py`, `config.py` | HTTP routes, job state/workers and settings |
| `engines/` | Engine interface, PyTorch implementation and pinned model store |
| `pipeline/` | Source download, chunk processing, cache and export assembly |
| `extension/main.js`, `button.js`, `session.js` | Video discovery, controls and per-video networking |
| `audio-scheduler.js`, `mute-controller.js`, `stretch.js` within the extension | Audio scheduling, volume mirroring and time-stretch |
| `settings.js`, `background.js`, `popup.js`, `page-script.js` within the extension | Saved settings, service worker, popup and page bridge |

Ordinary shutdown stops new admission and quietly closes progress streams before
draining active work. It has a bounded grace period and an immediate second-interrupt
escape; this is still an in-process worker design. A startup interrupted during
model download can resume from the shared model cache. App-owned abandoned staging
files are cleaned without removing completed media or shared model-download partials.

## Backend settings

Set variables before starting the helper. These are local tuning controls,
not resource or authorization guarantees for a public service.

| Variable | Default | Purpose |
| --- | --- | --- |
| `NOMUSIC_HOST` | `127.0.0.1` | Listen address |
| `NOMUSIC_PORT` | `8723` | Listen port |
| `NOMUSIC_ENGINE` | `mlx` | Engine name; `demucs` is an alias |
| `NOMUSIC_DEVICE` | `auto` | Prefer available MPS, then CUDA, then CPU; explicit `cpu`, `mps`, `cuda` fail if unavailable |
| `NOMUSIC_CACHE_DIR` | `~/.cache/nomusic` | Processed-media storage |
| `NOMUSIC_CACHE_TTL_DAYS` | `7` | Retention age; `0` disables TTL eviction |
| `NOMUSIC_CACHE_SWEEP_INTERVAL_SECONDS` | `3600` | Interval between TTL sweeps |
| `NOMUSIC_KEEP_SOURCE_AFTER_COMPLETE` | `false` | Keep downloaded source audio for later variants, using more disk |
| `NOMUSIC_CHUNK_SECONDS` | `10` | Processing chunk duration |
| `NOMUSIC_CHUNK_OVERLAP_SECONDS` | `0.5` | Separator context overlap |
| `NOMUSIC_GPU_BATCH` | `2` | Maximum chunks per inference batch; retry with `1` after a GPU out-of-memory error |
| `NOMUSIC_IDLE_TIMEOUT_SECONDS` | `10` | Abandon work after the last status subscriber leaves; `0` disables |
| `NOMUSIC_SSE_KEEPALIVE_SECONDS` | `15` | Interval between SSE keep-alive comments |
| `NOMUSIC_MEMORY_GC_INTERVAL_SECONDS` | `3600` | Reclaim in-memory entries whose disk cache disappeared; `0` disables |
| `NOMUSIC_PROGRESSIVE` | `true` | Process decodable early audio while its download continues |
| `NOMUSIC_DOWNLOAD_RATELIMIT` | Unset | Test download cap in bytes/sec, with optional `K`/`M` suffix |
| `NOMUSIC_JS_RUNTIME` | Auto-detected | Explicit Node.js 22+ or Deno 2.3+ executable |
| `NOMUSIC_SHUTDOWN_GRACE_SECONDS` | `60` | Positive finite shutdown deadline for active work; second Ctrl+C exits immediately |
| `NOMUSIC_DEBUG` | `false` | Enable debug logging |
| `NOMUSIC_RELOAD` | `false` | Watch the imported package; use an editable installation for source reload |

Model cache variables `HF_HOME` and `HF_HUB_CACHE`, installer options and
development commands are covered in [installation](installation.md).

## API

The local API has no user authentication. Interactive schemas are available at
`http://127.0.0.1:8723/docs` while the helper runs.

| Method | Path | Request/result |
| --- | --- | --- |
| GET | `/healthz` | `{ok: true}`: API reachability |
| GET | `/readyz` | Startup readiness; 200 when ready, otherwise 503 |
| GET | `/capabilities` | Engine/device/models/stems, defaults and cache configuration |
| POST | `/process` | `{url, model?, keep_stems?}` → `JobStatus` |
| POST | `/process/{job_id}/prioritize` | `{from_chunk}` → `{applied}`; prioritize pending chunks around a seek |
| GET | `/status/{job_id}` | `JobStatus`; 404 for unknown job |
| GET | `/events/{job_id}` | SSE `JobStatus` updates; 204 for unknown job; planned shutdown closes without a fabricated error |
| GET | `/chunk/{job_id}/{idx}` | OGG/Opus chunk; 425 while unavailable |
| GET | `/audio/{job_id}` | Full OGG/Opus; `?format=mp3` transcodes; 425 before completion |
| GET | `/video/{job_id}` | Original video with processed audio in MP4; `?max_height=N` requests a height limit; 425 before completion |
| GET | `/video/{job_id}/progress` | `{phase, percent}` for preparation; pass the same `max_height` as the export |
| GET | `/cache` | Cache path and storage statistics |
| POST | `/cache/clear` | Remove processed media → `{deleted_bytes}` |

`JobStatus` includes `job_id`, `state`, `phase`, `phase_progress` (0–1 or null),
`phase_label`, `chunks_ready`, `ready_chunks`, `total_chunks`, `duration_seconds`,
`title` and an error when relevant. `chunks_ready` is the count; `ready_chunks`
is the sorted list of completed chunk indices, which may be noncontiguous after
a seek. States are `queued`, `probing`, `downloading`, `processing`, `ready` and `error`.

Readiness describes startup runtime/storage/default-model checks; doctor adds
a tiny real inference. Neither establishes source availability or ongoing
capacity. Export preparation currently occurs inside the download request;
video fallback formats may exceed the requested height. Keep these limitations
in mind when writing another client.
