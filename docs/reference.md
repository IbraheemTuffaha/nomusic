# Architecture and reference

## Architecture

The Manifest V3 extension discovers videos, submits processing jobs and follows
their progress over Server-Sent Events (SSE). It fetches Opus chunks and schedules
decoded audio against the video's clock. A page bridge suppresses original
audio; the scheduler handles drift and pitch-preserving speed changes.

FastAPI owns an engine, cache, job registry and maintenance work per application
lifespan. By default, one supervised child process owns model loading and native
inference; the registry thread remains the authority for job state, leases and
publication. `NOMUSIC_SUPERVISED_WORKER=0` keeps the direct in-process path as a
temporary rollback. Demucs/PyTorch selects MPS, CUDA or CPU. The current engine
is named `mlx`, with `demucs` as an alias; it is not an MLX implementation.
Cache metadata and completed chunks permit reuse. MP3/MP4 exports preserve
sample-aware chunk concatenation and use FFmpeg.
Published chunks use shared namespace leases, so playback can fetch them while a
long job is still producing later chunks; clear and eviction retain the namespace
until all readers and the processor release it.

| Location | Responsibility |
| --- | --- |
| `backend/nomusic/__main__.py`, `cli.py` | Installed commands and direct URL processing |
| `server.py`, `serving.py`, `services.py` within that package | API composition, serving/shutdown and owned service lifecycle |
| `routes/`, `jobs.py`, `config.py` | HTTP routes, job state/workers and settings |
| `engines/` | Engine interface, PyTorch implementation and pinned model store |
| `pipeline/` | Source download, chunk processing, cache and export assembly |
| `extension/main.js`, `button.js`, `session.js` | Video discovery, controls and per-video networking |
| `extension/chunk-loader.js` | Bounded chunk acquisition, retries and decoded-audio retention |
| `audio-scheduler.js`, `mute-controller.js`, `stretch.js` within the extension | Audio scheduling, volume mirroring and time-stretch |
| `settings.js`, `background.js`, `popup.js`, `page-script.js` within the extension | Saved settings, service worker, popup and page bridge |

Ordinary shutdown stops new admission and quietly closes progress streams before
draining active work. It has a bounded grace period and an immediate second-interrupt
escape; forced exit terminates the supervised worker before the process exits, and
the child watches its parent so a crashed server cannot leave native work behind.
A startup interrupted during model download can resume from the shared model cache.
App-owned abandoned staging files are cleaned without removing completed media or
shared model-download partials.

### Playback ownership and memory

A session captures its source URL, backend, model and stems. Popup changes take
effect on the next session. Resuming the same job keeps valid audio; adopting a
different job aborts old requests and clears decoded, stretched and scheduled
audio together. Late decode or stretch results cannot repopulate a replaced job.

The chunk loader keeps server availability separate from pending requests and
decoded buffers. It fetches at most three chunks concurrently, including their
decode work. Network/body reads time out after 15 seconds. Failed chunks have
four attempts with 0.5, 1 and 2 second backoffs; retries continue independently
after the final SSE status. Browser decoding cannot be cancelled, so an obsolete
decode still occupies its slot until it settles, and its result is discarded.
A decode exceeding 15 seconds reports a terminal error if it is still needed
or blocks acquisition; it does not silently free a slot for more work.

Decoded audio stays within 20 seconds behind and 45 seconds ahead of the video
clock, rounded to whole chunks. Distant seeks release the old window and refetch
the new one. With the default 9.5-second stride this retains at most eight chunks:
about 29 MiB of stereo float PCM at 48 kHz and ten seconds per chunk. Up to three
pending decodes are additional temporary work. Encoded browser HTTP cache and
backend disk cache are separate from this decoded window.

The scheduler prepares/schedules chunk starts no more than 30 video seconds
ahead, including direct arrivals. Stretched buffers are retained only for decoded
chunks and the current playback rate. Their size scales inversely with that
rate (half-speed audio has twice as many frames); changing rates releases the
previous prepared rate. These are time-window limits, not a fixed browser heap
limit: channel count, sample rate and backend chunk settings affect memory.

Volume and playback intent have separate owners. The volume controller mirrors
the latest slider/mute choice, including zero, while suppressing original audio.
Disabling nomusic restores that latest choice. A buffering hold pauses the media
without changing whether the user wants it to play. The page bridge observes
explicit pause calls even when the video is already held; newly available audio
therefore cannot resume a video the user deliberately paused.

Selecting nomusic pauses and suppresses the original track before contacting
the backend. A terminal setup, processing, chunk or stream error retains that
suppression and shows persistent **Retry** and **Return to original** actions.
Retry restarts processing in the same session without resetting volume or
play/pause intent. Only an explicit return restores the original track.

Playback checks the final decoded chunk's actual end, including any span beyond
the usual stride. Once the job is ready, a finite native video end up to one
second later may finish with original audio still suppressed. A larger or unknown
uncovered tail reports a persistent playback error instead of buffering forever.
This tolerance does not stretch audio or correct larger source-duration mismatches.

The session owns a bounded processing lease as well as SSE reconnection. The
lease is renewed independently of status transport, so switching between SSE
and `/status` does not abandon work. An ordinary pause stops the heartbeat and
retains the job for the client lease plus the idle timeout (30 seconds and 10
seconds by default); resuming re-submits the same cache key and reuses completed
chunks. Disabling nomusic releases only that
session's lease. If another tab has a lease, its work continues. Servers from
before the interest protocol are supported temporarily through the existing
SSE subscriber idle clock.

SSE errors in either connecting or closed state
receive three retries with 0.5, 1 and 2 second delays. Each retry re-submits the
job before opening its stream, allowing a restarted backend to resume cached
work. A valid status for that job resets the retry budget. An initial stream
snapshot has a 15-second deadline; processing requests have 30 seconds, and
the required capabilities request has a five-second timeout. User pause cancels reconnect work
unless a queued export still needs processing.

Discovery reconciles mutation batches against the final DOM. Reparenting a
connected player preserves its session; removing a video retires its button,
menus, timers, requests and audio without resuming the detached element. Source
replacement also stops the old session. YouTube navigation uses the playing
video ID so opening the miniplayer's surrounding home page does not replace it.
Layout changes and navigation trigger bounded discovery refreshes; there is no
continuous full-page scan. Export requests and progress polling belong to their
button and are cancelled on retry, disable or retirement.

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
| `NOMUSIC_IDLE_TIMEOUT_SECONDS` | `10` | Abandon work after the last client lease and status subscriber leave; `0` disables |
| `NOMUSIC_SUPERVISED_WORKER` | `true` | Run model inference in a restartable child process; set `false` only for rollback/debugging |
| `NOMUSIC_MAX_QUEUED_JOBS` | `1` | Maximum queued jobs behind the active execution |
| `NOMUSIC_EXECUTION_TIMEOUT_SECONDS` | `1800` | Maximum wall time for one supervised execution before a clear error |
| `NOMUSIC_WORKER_CANCEL_GRACE_SECONDS` | `5` | Cooperative cancellation grace before a stuck child is replaced |
| `NOMUSIC_WORKER_WARMUP_TIMEOUT_SECONDS` | `300` | Startup model-warmup deadline for the supervised child |
| `NOMUSIC_SSE_KEEPALIVE_SECONDS` | `15` | Interval between SSE keep-alive comments |
| `NOMUSIC_CLIENT_LEASE_SECONDS` | `30` | Maximum processing-interest lease; pause retention ends when it expires |
| `NOMUSIC_CLIENT_HEARTBEAT_SECONDS` | `10` | Extension heartbeat interval while a session is active |
| `NOMUSIC_INTEREST_SWEEP_INTERVAL_SECONDS` | `5` | Expire abandoned client leases; `0` disables the maintenance pass |
| `NOMUSIC_SSE_QUEUE_SIZE` | `64` | Maximum pending status snapshots per SSE subscriber; older snapshots coalesce |
| `NOMUSIC_MEMORY_GC_INTERVAL_SECONDS` | `3600` | Reclaim in-memory entries whose disk cache disappeared; `0` disables |
| `NOMUSIC_PROGRESSIVE` | `true` | Process decodable early audio while its download continues |
| `NOMUSIC_DOWNLOAD_RATELIMIT` | Unset | Test download cap in bytes/sec, with optional `K`/`M` suffix |
| `NOMUSIC_MAX_DURATION_SECONDS` | `7200` | Reject unknown, non-finite or longer source durations |
| `NOMUSIC_MAX_SOURCE_BYTES` | `512 MiB` | Bound source downloads and reject oversized cached sources |
| `NOMUSIC_MAX_VIDEO_BYTES` | `2 GiB` | Bound video export downloads and cached video reuse |
| `NOMUSIC_MAX_VIDEO_HEIGHT` | `1080` | Cap video export format selection and validation; larger requests fail clearly |
| `NOMUSIC_MAX_DECODE_BYTES` | `64 MiB` | Bound one decoded WAV working buffer |
| `NOMUSIC_MAX_CHUNK_BYTES` | `16 MiB` | Reject unexpectedly large encoded chunks before publication |
| `NOMUSIC_MAX_INFERENCE_BATCH` | `2` | Bound model working-set batch size even when `NOMUSIC_GPU_BATCH` is higher |
| `NOMUSIC_MAX_PREFETCH_CHUNKS` | `2` | Bound decoded chunks waiting for model execution |
| `NOMUSIC_FINAL_CHUNK_TOLERANCE_SECONDS` | `1` | Allow only this measured source-duration remainder when validating a download |
| `NOMUSIC_MAX_CACHE_BYTES` | `4 GiB` | Shared completed-media budget; leased namespaces are retained until release |
| `NOMUSIC_MIN_FREE_BYTES` | `256 MiB` | Minimum filesystem free space required for a new reservation |
| `NOMUSIC_MAX_EXPORT_BYTES` | `2 GiB` | Reservation and final-size bound for one MP3/MP4 artifact |
| `NOMUSIC_MAX_EXPORT_JOBS` | `2` | Maximum queued/building export preparations |
| `NOMUSIC_EXPORT_TTL_SECONDS` | `86400` | Independent retention for completed/failed export records and files |
| `NOMUSIC_EXPORT_SWEEP_INTERVAL_SECONDS` | `300` | Export-retention sweep interval; `0` disables the pass |
| `NOMUSIC_MAX_EXPORT_DOWNLOADS` | `4` | Simultaneous prepared-artifact readers |
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
| POST | `/process` | `{url, model?, keep_stems?, client_id?}` → `JobStatus`; a client id also acquires a bounded interest lease; `429` means the bounded queue is full |
| POST | `/process/{job_id}/prioritize` | `{from_chunk}` → `{applied}`; prioritize pending chunks around a seek |
| POST | `/process/{job_id}/interest` | `{client_id, lease_seconds?}` → lease; acquire or heartbeat one client's interest |
| DELETE | `/process/{job_id}/interest?client_id=...` | Release only that client's interest; another client's lease is unaffected |
| GET | `/status/{job_id}` | `JobStatus`; 404 for unknown job |
| GET | `/events/{job_id}` | SSE `JobStatus` updates; 204 for unknown job; planned shutdown closes without a fabricated error |
| GET | `/chunk/{job_id}/{idx}` | OGG/Opus chunk; 425 while unavailable |
| GET | `/audio/{job_id}` | Full OGG/Opus; 425 before completion |
| POST | `/exports` | `{job_id, format, max_height?, client_id?}` → durable export status; source must be ready; `429` when the bounded export queue is full |
| GET | `/exports/{export_id}` | Export status; ready records include `download_url`, `filename`, size and expiry |
| DELETE | `/exports/{export_id}?client_id=...` | Release one export owner; a queued/building export is cancelled when its last owner leaves |
| GET | `/exports/{export_id}/download` | Leased prepared artifact; `425` while building, `409` for failed/cancelled, `410` after expiry; concurrent readers are bounded |
| GET | `/cache` | Cache path and storage statistics |
| POST | `/cache/clear` | Remove processed media → `{deleted_bytes}` |

`JobStatus` includes `job_id`, `state`, `phase`, `phase_progress` (0–1 or null),
`phase_label`, `chunks_ready`, `ready_chunks`, `total_chunks`, `duration_seconds`,
`title` and an error when relevant. `chunks_ready` is the count; `ready_chunks`
is the sorted list of completed chunk indices, which may be noncontiguous after
a seek. States are `queued`, `probing`, `downloading`, `processing`, `ready` and `error`.

Readiness describes startup runtime/storage/default-model checks; doctor adds
a tiny real inference. Neither establishes source availability or ongoing
capacity. Export preparation is asynchronous: wait for the source job to become
ready, submit once, poll the export record, then download its leased artifact.
The video policy still applies the configured height and byte bounds, and every
export is revalidated before it is published.
