# nomusic — Remote Deployment & Web Store Plan

This plan ships **nomusic** — a Chrome MV3 extension plus a FastAPI/PyTorch (demucs) backend that strips the music bed from a video and plays a vocals-only track in sync — as a **public hosted service**. The backend runs on a spare NVIDIA Linux laptop at home, exposed to the internet **only** through an outbound Cloudflare named tunnel at `https://nomusic.<DOMAIN>`. The published extension defaults to that URL, so anyone who installs it hits the owner's server. There is no per-user secret possible (a published extension cannot hold one), so every protection lives **server-side and at the Cloudflare edge**: rate limits, quotas, abuse caps, SSRF/domain allowlisting, and a private admin token for destructive operations.

The work is four phases that must land **in order**: harden the backend (Phase 1), prepare the extension for the public default and Web Store review (Phase 2), pass Web Store submission (Phase 3), then stand up the laptop + tunnel (Phase 4). Phase 4 depends on Phase 1: until the `NOMUSIC_PUBLIC` controls and `NOMUSIC_ADMIN_TOKEN` gate exist in code, the Cloudflare WAF rule on `/cache*` is the only thing protecting the destructive endpoints, and the document flags every such interim control loudly.

`<DOMAIN>` is a placeholder the owner fills in before implementation. Other placeholders (`<ADMIN_LAN_IP>`, `<GATEWAY_IP>`, `<DNS_IP>`, `<UUID>`, `<PUBLISHED_EXT_ID>`, `<your-repo-url>`) are listed in §7.

---

## 0. Decisions locked (owner, 2026-06-27)

These resolve the earlier open questions; everything below assumes them.

1. **Domain — placeholder.** Keep `<DOMAIN>` / `nomusic.<DOMAIN>` throughout; the owner substitutes the real domain at implementation time.
2. **Curated sites — YouTube + Facebook.** Backend `allowed_url_hosts`/`allowed_extractors` (§2.1) and the extension content-script matches (§3.3) both cover YouTube (first-party support) and Facebook (generic `<video>` path). The store description names exactly those two.
3. **Audience — full public, no client auth.** Protection is server-side + Cloudflare edge only. The Cloudflare Access privatization path stays documented as a later escape hatch but is **not** enabled now.
4. **No R2 for now.** Audio/video stream through the proxied tunnel; accept the §5.7 Cloudflare media-ToS risk under strict quotas + low volume. The R2 artifact-offload remains the pre-built contingency to switch on if Cloudflare sends a notice.
5. **GPU — GTX 1050 Ti (Pascal, sm_61, 4 GB), already validated.** Install **must** pin the legacy CUDA build: `NOMUSIC_PYTHON=3.11 NOMUSIC_CUDA=cu118 NOMUSIC_TORCH=2.4.1` (torch 2.4.1 is the last wheel shipping sm_61 kernels; newer CUDA-12 wheels silently fall back to CPU). Keep `gpu_batch` low (1–2) for 4 GB VRAM. See §5.1.

---

## 1. Overview and target architecture

### 1.1 Topology

```
                 ┌──────────────────────────── public internet ───────────────────────────┐
                 │                                                                          │
   Chrome user   │   Cloudflare edge (zone <DOMAIN>)                                        │
  ┌───────────┐  │   ┌───────────────────────────────────┐                                 │
  │ nomusic   │  │   │ DNS: nomusic.<DOMAIN> (proxied)    │                                 │
  │ extension │──┼──▶│ CNAME → <UUID>.cfargotunnel.com    │                                 │
  │ (MV3)     │  │   │ + WAF: rate-limit POST /process    │                                 │
  └───────────┘  │   │ + Custom rule: block /cache*       │                                 │
   HTTPS         │   │ + Bot Fight Mode + managed DDoS    │                                 │
   fetch / SSE   │   │ + injects CF-Connecting-IP         │                                 │
                 │   └────────────────┬──────────────────┘                                 │
                 └────────────────────┼────────────────────────────────────────────────────┘
                                      │  outbound-only QUIC (UDP/7844, fallback TCP/7844)
                                      │  NO inbound router port-forward exists
        ┌─────────────────────────────┼────────────────────────────────────────────────────┐
        │  Home LAN — spare NVIDIA Linux laptop (hardened)                                   │
        │                              ▼                                                     │
        │   ┌──────────────────────────────────────┐   loopback only   ┌──────────────────┐ │
        │   │ cloudflared.service (user cloudflared)│ ───────────────▶  │ nomusic.service  │ │
        │   │  - Go binary, MDWE on, PrivateDevices │  http://127.0.0.1 │ (user nomusic)   │ │
        │   │  - injects X-Nomusic-Tunnel secret    │      :8723        │ FastAPI/uvicorn  │ │
        │   └──────────────────────────────────────┘                   │ demucs/torch+CUDA│ │
        │                                                               │ binds 127.0.0.1  │ │
        │   ufw: default-deny inbound, allow lo                         │ NOMUSIC_PUBLIC=1 │ │
        │   nftables: egress drops RFC1918 except GW+DNS                └────────┬─────────┘ │
        │                                                                        │ spawns    │
        │                                                          yt-dlp + ffmpeg + JS rt   │
        └────────────────────────────────────────────────────────────────────────┼─────────┘
                                                                                   ▼
                                                          public video sites (YouTube, …) via egress
```

### 1.2 Request / data flow

1. The extension content script reads the current page's **video URL** and the user's chosen model/stems, then `POST /process` to `https://nomusic.<DOMAIN>`.
2. Cloudflare's edge applies WAF/rate-limit/bot rules, overwrites `CF-Connecting-IP` with the true client IP, and proxies the request down the tunnel to `cloudflared` on the laptop.
3. `cloudflared` forwards to `127.0.0.1:8723` with an injected `X-Nomusic-Tunnel` secret header.
4. The app validates origin/edge/rate/admission, enqueues a bounded job, downloads via yt-dlp, runs demucs inference (serialized on the GPU), and emits chunked opus.
5. The extension subscribes to `GET /events/{job_id}` (SSE) for progress, pulls `GET /chunk/.../audio` for playback, and `page-script.js` mutes the page video and keeps the vocals-only audio in sync.

### 1.3 Threat model and how this plan answers it

Audience is the entire public internet with **no client auth**. The 31 confirmed security findings (F1–F31, full detail in the security study) cluster into:

- **Availability / resource DoS** (F1, F2, F7–F10, F13): unbounded worker threads, a GPU lock that pins the whole pipeline, no disk cap, `/video` amplification, threadpool starvation, unbounded SSE, unbounded request bodies. → Phase 1 §2.5–§2.10 (admission caps, inference-only lock + deadlines, LRU disk evictor, height allowlist + gates, SSE caps, body/field limits) backed by Cloudflare edge rate-limiting (Phase 4 §5.5).
- **Auth / CORS / tenancy** (F3, F11, F14, F15, F19–F21, F29): unauthenticated destructive `/cache/clear`, any-website-drives-`/process`, IP-trust, derivable `job_id` oracle, info leaks, fingerprinting, dead `allow_private_network`. → Phase 1 admin token gate, locked CORS + server-side Origin gate, `CF-Connecting-IP`-only identity, sanitized errors, slimmed `/capabilities`.
- **SSRF / yt-dlp** (F4–F6, F12, F27): open download proxy, DNS-rebind TOCTOU, redirect SSRF, `/video` re-feed without re-validation. → Phase 1 host allowlist + `allowed_extractors` + shared `validate_public_url`, plus the **OS-level nftables egress filter** (Phase 4 §6.2) as the durable network-layer control.
- **Disk / abuse** (F7, F8): unbounded fill. → cache size cap + free-disk floor + duration/size ceilings.
- **Path / file serving** (F18, F22): unvalidated `job_id`. → `^[0-9a-f]{16}$` pattern on every route + cache-layer guard.
- **Supply chain** (F16, F17, F23–F26): floating deps, unpinned ffmpeg/JS runtime, live-fetched solver/weights. → hash-locked requirements, pre-staged weights, deno on Linux, token-out-of-subprocess-env, yt-dlp weekly cadence.
- **Defense-in-depth notes** (F28, F30, F31): clarifications and latent argv hygiene — addressed where cheap.

**One master switch.** `NOMUSIC_PUBLIC=1` turns on every hardening control. Unset, the server behaves exactly as today (loopback dev mode), so local use is unaffected. The bind stays `127.0.0.1:8723`; cloudflared is the only thing that reaches the socket.

---

## 2. Phase 1 — Backend hardening

Ordered so each step's dependencies land first. New files: `backend/netsec.py`, `backend/security.py`, `backend/ratelimit.py`, `backend/middleware.py`, `backend/requirements.lock`. Edited: `config.py`, `server.py`, `jobs.py`, `engines/mlx_engine.py`, `routes/jobs.py`, `routes/media.py`, `routes/system.py`, `pipeline/cache.py`, `pipeline/downloader.py`, `pipeline/processor.py`, `requirements.txt`, `install.sh`.

### 2.1 Config flags — `backend/config.py`

Add these fields to the frozen `Settings` dataclass (after the existing fields, ~line 139), each via the existing `_env*` helpers; add a small `_env_tuple(key, default)` for comma-separated lists. All read once at import. With `NOMUSIC_PUBLIC` unset, every gate below is a no-op.

| Env var | Setting | Default | Finding |
|---|---|---|---|
| `NOMUSIC_PUBLIC` | `public` | `False` | master toggle |
| `NOMUSIC_ADMIN_TOKEN` | `admin_token` | `""` | F3, F19 (empty ⇒ admin fails closed) |
| `NOMUSIC_TUNNEL_SECRET` | `tunnel_secret` | `""` | F14 (blocks LAN-direct origin hits) |
| `NOMUSIC_EXTENSION_ORIGIN` | `extension_origin` | `""` | F11 (`chrome-extension://<ID>` for CORS) |
| `NOMUSIC_MAX_INFLIGHT_JOBS` | `max_inflight_jobs` | `3` | F1 |
| `NOMUSIC_MAX_JOBS_PER_IP` | `max_jobs_per_ip` | `2` | F1 |
| `NOMUSIC_MAX_VIDEO_EXPORTS` | `max_video_exports` | `1` | F8, F9, F12 |
| `NOMUSIC_MAX_AUDIO_TRANSCODES` | `max_audio_transcodes` | `2` | F9 |
| `NOMUSIC_MAX_SSE_PER_JOB` | `max_sse_per_job` | `4` | F10 |
| `NOMUSIC_MAX_SSE_PER_IP` | `max_sse_per_ip` | `20` | F10 |
| `NOMUSIC_MAX_SSE_GLOBAL` | `max_sse_global` | `200` | F10 |
| `NOMUSIC_SSE_MAX_LIFETIME_SECONDS` | `sse_max_lifetime_seconds` | `1800` | F10 (slowloris) |
| `NOMUSIC_RATE_PROCESS_PER_MIN` | `rate_process_per_min` | `6` | F1, F11 |
| `NOMUSIC_RATE_VIDEO_PER_MIN` | `rate_video_per_min` | `4` | F8, F12 |
| `NOMUSIC_RATE_DEFAULT_PER_MIN` | `rate_default_per_min` | `120` | F15 (status/chunk/audio) |
| `NOMUSIC_JOB_DEADLINE_SECONDS` | `job_deadline_seconds` | `1800` | F2 |
| `NOMUSIC_DOWNLOAD_DEADLINE_SECONDS` | `download_deadline_seconds` | `900` | F2 |
| `NOMUSIC_MAX_DURATION_SECONDS` | `max_duration_seconds` | `5400` | F4, F7 |
| `NOMUSIC_MAX_SOURCE_BYTES` | `max_source_filesize` | `600_000_000` | F4, F7 |
| `NOMUSIC_MAX_VIDEO_BYTES` | `max_video_filesize` | `4_000_000_000` | F4, F7, F8 |
| `NOMUSIC_CACHE_MAX_BYTES` | `cache_max_bytes` | `40_000_000_000` | F7 (LRU evict) |
| `NOMUSIC_FREE_DISK_FLOOR_BYTES` | `free_disk_floor_bytes` | `5_000_000_000` | F7 |
| `NOMUSIC_MAX_REQUEST_BYTES` | `max_request_bytes` | `8192` | F13 |
| `NOMUSIC_MAX_URL_LENGTH` | `max_url_length` | `2048` | F13 |
| `NOMUSIC_MAX_KEEP_STEMS` | `max_keep_stems` | `8` | F13 |
| `NOMUSIC_ALLOWED_VIDEO_HEIGHTS` | `allowed_video_heights` | `(360,480,720,1080)` | F8 |
| `NOMUSIC_ALLOWED_URL_HOSTS` | `allowed_url_hosts` | `("youtube.com","youtu.be","m.youtube.com","music.youtube.com","www.facebook.com","facebook.com","m.facebook.com","fb.watch")` | F4, F6 — YouTube + Facebook (locked §0) |
| `NOMUSIC_ALLOWED_EXTRACTORS` | `allowed_extractors` | `("youtube","youtube:tab","facebook")` | F4, F6 (never `generic`) |

Keep `allow_origins` (line 73) but stop using it directly in public mode. Add a derived property (used by §2.10):

```python
@property
def cors_origins(self) -> list[str]:
    if not self.public:
        return list(self.allow_origins)              # '*' dev
    o = []
    if self.extension_origin:
        o.append(self.extension_origin)
    o += [f"https://{h}" for h in self.allowed_url_hosts]   # curated sites
    return o
```

Also resolve the cache dir to an absolute path at startup as cheap argv hygiene (F30): `cache_dir = cache_dir.resolve()`.

### 2.2 `backend/netsec.py` (NEW) — SSRF / URL policy (F4, F5, F6, F12, F27)

Move `_resolve_host_ip_candidates` (`routes/jobs.py:46-80`) and `_is_blocked_host_ip` (`routes/jobs.py:83-95`) here verbatim, add the positive host allowlist, and expose one entry point reused by `/process` **and** `/video`:

```python
# backend/netsec.py
from urllib.parse import urlsplit
from config import SETTINGS

class UrlNotAllowed(ValueError): ...

def _host_allowed(host: str) -> bool:
    h = host.lower().rstrip(".")
    return any(h == d or h.endswith("." + d) for d in SETTINGS.allowed_url_hosts)

def validate_public_url(url: str) -> str:
    """Scheme + positive host allowlist + internal-IP block-list. The single gate
    used at submit AND at /video, so on-disk meta.url is re-checked (F12/F27)."""
    v = url.strip()
    parts = urlsplit(v)
    if parts.scheme not in ("http", "https"):
        raise UrlNotAllowed("url must be an http(s) URL")
    host = parts.hostname
    if not host:
        raise UrlNotAllowed("url must include a host")
    lowered = host.lower()
    if lowered == "localhost" or lowered.endswith(".localhost"):
        raise UrlNotAllowed("url host is not allowed")
    if SETTINGS.public and not _host_allowed(host):
        raise UrlNotAllowed("url host is not on the allowlist")  # F4/F6
    for ip in _resolve_host_ip_candidates(host):                 # F5 first-hop
        if _is_blocked_host_ip(ip):
            raise UrlNotAllowed("url host is not allowed")
    return v
```

`routes/jobs.py` `ProcessRequest._validate_url` (103-125) becomes a one-liner delegating to `validate_public_url`.

**F5/F6 caveat — must be stated in the runbook.** The allowlist + `allowed_extractors` closes the open-proxy and most-redirect surface, but DNS-rebinding and redirect-to-internal are only **fully** closed at the network layer (yt-dlp re-resolves DNS per fetch and exposes no per-redirect host hook). The durable control is the **nftables egress filter** dropping RFC1918/loopback/link-local for the service user (Phase 4 §6.2).

### 2.3 `backend/security.py` (NEW) — identity, admin auth, edge trust, job_id (F3, F14, F18, F22)

```python
# backend/security.py
import hmac
from typing import Annotated
from fastapi import Header, HTTPException, Path, Request
from config import SETTINGS

JOB_ID_RE = r"^[0-9a-f]{16}$"
JobId   = Annotated[str, Path(pattern=JOB_ID_RE)]        # F18/F22
ChunkIdx = Annotated[int, Path(ge=0, le=100_000)]

def client_ip(request: Request) -> str:
    cf = request.headers.get("cf-connecting-ip")          # F14: trust ONLY this
    if cf:
        return cf.strip()
    return request.client.host if request.client else "unknown"

async def require_edge(request: Request) -> None:
    """In public mode, reject anything that didn't traverse the tunnel."""
    if not SETTINGS.public or not SETTINGS.tunnel_secret:
        return
    got = request.headers.get("x-nomusic-tunnel", "")
    if not hmac.compare_digest(got, SETTINGS.tunnel_secret):
        raise HTTPException(status_code=404)

async def require_admin(x_admin_token: Annotated[str | None, Header()] = None) -> None:
    token = SETTINGS.admin_token
    if not token:                                         # F3: fail closed
        raise HTTPException(status_code=404)
    if not x_admin_token or not hmac.compare_digest(x_admin_token, token):
        raise HTTPException(status_code=404)              # 404, not 401/403
```

- **Never** trust `X-Forwarded-For`; identity is `CF-Connecting-IP` only, trustworthy because the only path to the loopback socket is the tunnel (F14).
- Apply `JobId` to `chunk/audio/video/video_progress/status/events/prioritize` so `^[0-9a-f]{16}$` is enforced before any filesystem use. Add a belt-and-suspenders guard in `cache._key_dir` (cache.py:112): `if not re.fullmatch(JOB_ID_RE, key): raise KeyError`.
- Admin call (owner): `curl -X POST -H "X-Admin-Token: $TOKEN" https://nomusic.<DOMAIN>/cache/clear`, or against `127.0.0.1:8723` locally. 404 on missing/bad token so the endpoint looks absent.

### 2.4 `backend/ratelimit.py` (NEW) — per-IP limits + concurrency gates (F1, F8–F10)

Hand-rolled, not slowapi: slowapi does request-rate only (not the concurrency gates / per-IP job caps we also need) and keys on `request.client.host` by default — wrong here. The single-process model makes shared in-memory state correct.

```python
# backend/ratelimit.py
import threading, time
from collections import defaultdict, deque
from fastapi import HTTPException, Request
from config import SETTINGS
from security import client_ip

class _Window:
    def __init__(self, limit, window=60.0):
        self.limit, self.window = limit, window
        self._hits = defaultdict(deque); self._lock = threading.Lock()
    def check(self, key):
        now = time.monotonic()
        with self._lock:
            dq = self._hits[key]
            while dq and dq[0] <= now - self.window: dq.popleft()
            if len(dq) >= self.limit:
                return False, max(1, int(self.window - (now - dq[0])))
            dq.append(now); return True, 0

def rate_limit(window: _Window):
    def dep(request: Request):
        if not SETTINGS.public: return
        ok, retry = window.check(client_ip(request))
        if not ok:
            raise HTTPException(429, "rate limited", headers={"Retry-After": str(retry)})
    return dep

class Gate:
    """Non-blocking bounded concurrency; 503 + Retry-After when full."""
    def __init__(self, limit): self._sem = threading.BoundedSemaphore(limit)
    def __enter__(self):
        if not self._sem.acquire(blocking=False):
            raise HTTPException(503, "server busy", headers={"Retry-After": "5"})
        return self
    def __exit__(self, *a): self._sem.release()

process_rl = _Window(SETTINGS.rate_process_per_min)
video_rl   = _Window(SETTINGS.rate_video_per_min)
default_rl = _Window(SETTINGS.rate_default_per_min)
video_export_gate    = Gate(SETTINGS.max_video_exports)
audio_transcode_gate = Gate(SETTINGS.max_audio_transcodes)
```

Add a small `SseCounter` (per-job/per-IP/global dicts under a lock; `acquire(job, ip) -> bool`, `release(...)`) consumed by `events()` (§2.9).

### 2.5 Admission control + unbounded-thread fix — `backend/jobs.py` (F1)

The cap must be enforced **at thread-creation time in `submit()`** (queued threads never reach the idle check), counting active+queued keys. Bounding inflight bounds total daemon threads — the actual F1 fix.

`JobRegistry.__init__` (after line 174):
```python
self._inflight: set[str] = set()
self._ip_counts: dict[str, int] = collections.defaultdict(int)
self._ip_by_key: dict[str, str] = {}
```
New exception near `WorkerAbandoned`: `class JobRejected(Exception): ...`

`submit()` gains a `client_ip: str` parameter; inside the `with self._lock:` block (jobs.py:231), after the live-duplicate adopt and before spawning (248-260):
```python
is_new_work = existing_meta is None or not existing_meta.complete
if is_new_work:
    if len(self._inflight) >= SETTINGS.max_inflight_jobs:
        raise JobRejected("server at capacity")           # -> 429
    if self._ip_counts[client_ip] >= SETTINGS.max_jobs_per_ip:
        raise JobRejected("too many concurrent jobs")
    # F7 disk floor (see §2.7):
    import shutil as _sh
    if SETTINGS.public and _sh.disk_usage(self.cache.root).free < SETTINGS.free_disk_floor_bytes:
        raise JobRejected("insufficient disk")
...
if status.state == JobState.READY:
    return status
self._inflight.add(key)
self._ip_counts[client_ip] += 1
self._ip_by_key[key] = client_ip
t = threading.Thread(...); self._threads[key] = t; t.start()
```
In `_run`'s `finally` (484-508), when the key is still owned (`self._threads.get(key) is my_thread`), release admission:
```python
self._inflight.discard(key)
ip = self._ip_by_key.pop(key, None)
if ip is not None and self._ip_counts.get(ip):
    self._ip_counts[ip] -= 1
    if self._ip_counts[ip] <= 0: del self._ip_counts[ip]
```
`abandon_all()` (359-398) clears `_inflight`/`_ip_counts`/`_ip_by_key` too.

`routes/jobs.py` `process()` (144-165):
```python
@router.post("/process",
    dependencies=[Depends(require_edge), Depends(enforce_origin), Depends(rate_limit(process_rl))])
def process(req, request):
    ...
    try:
        status = registry.submit(req.url, model=model, keep_stems=keep_stems,
                                 client_ip=client_ip(request))
    except JobRejected as exc:
        raise HTTPException(429, "server busy; try again shortly",
                            headers={"Retry-After": "10"}) from exc
    except Exception as exc:
        log.exception("submit failed")
        raise HTTPException(400, "could not start job") from exc   # F20: generic
```

### 2.6 GPU-lock scope + hard deadlines — `jobs.py` + `engines/mlx_engine.py` (F2)

**Serialize inference only, not the whole pipeline.** Probe + download then run concurrently across the (capped) admitted jobs instead of blocking every other user.

`engines/mlx_engine.py`: add `self._infer_lock = threading.Lock()` in `__init__` (after line 70) and wrap the `apply_model` call in `infer_batch` (~152-156):
```python
with self._infer_lock:
    with torch.no_grad():
        estimates = apply_model(model, x, device=self._device, ...)
```

`jobs.py` `_run` (414-447): **remove** `with self._gpu_lock:` around `self.processor.run(...)`; delete the now-unused `self._gpu_lock` (174) or leave a comment.

`_raise_if_abandoned` (638-663): add an **absolute deadline that fires regardless of subscribers**, placed *before* the `if self._subscribers.get(key): return` early return (650):
```python
status = self._jobs.get(key)
if status and SETTINGS.job_deadline_seconds > 0 and \
   time.time() - status.created_at >= SETTINGS.job_deadline_seconds:
    self._abandoning.add(key)
    raise WorkerAbandoned                      # a held /events stream can't keep it alive
if self._subscribers.get(key):
    return
```

`pipeline/processor.py` `run()` (514-534): wire `abort_check` into the non-progressive `fetcher.download()` (528) and `download_source()` (533) paths (they currently pass only `_yt_hook`), and bound total download time — run the yt-dlp pull under a watchdog that calls `_ProgressiveSource.cancel()` / raises `DownloadCancelled` at `SETTINGS.download_deadline_seconds`, plus a min-throughput cancel in the progress hook. `downloader.py` keeps `socket_timeout` and adds `max_filesize` (§2.11) so a slow trickle is additionally bounded by size.

### 2.7 Disk-fill defense — `pipeline/cache.py` + admission (F7)

Add a size tally and LRU eviction; call from the sweeper and (free-disk floor) from admission (§2.5):
```python
def total_bytes(self) -> int:
    return sum(_dir_bytes(p) for p in self.root.iterdir()
               if p.is_dir() and p.name not in _RESERVED_DIRS) \
         + sum(_dir_bytes(p) for t in _RESERVED_DIRS for p in self._tree_entries(t))

def evict_to_fit(self, max_bytes: int) -> tuple[int, int]:
    """Delete least-recently-used entries until under cap."""
    entries = [(p, _dir_newest_mtime(p)) for p in self.root.iterdir()
               if p.is_dir() and p.name not in _RESERVED_DIRS]
    for t in _RESERVED_DIRS:
        entries += [(p, _dir_newest_mtime(p)) for p in self._tree_entries(t)]
    entries.sort(key=lambda e: e[1])             # oldest first
    removed = freed = 0; total = self.total_bytes()
    for p, _ in entries:
        if total <= max_bytes: break
        b = _dir_bytes(p); shutil.rmtree(p, ignore_errors=True)
        total -= b; freed += b; removed += 1
    return removed, freed
```
`server.py` `_start_cache_ttl_sweeper._loop` (118-132): after `sweep_older_than`, call `cache.evict_to_fit(SETTINGS.cache_max_bytes)` when `SETTINGS.public`.

**Duration ceiling (cheapest cut, rejects before download):** in `downloader.probe`/`SourceFetcher.extract` (162-167, 392-397), after computing `duration`: `if SETTINGS.public and duration > SETTINGS.max_duration_seconds: raise RuntimeError("media too long")`.

### 2.8 `/video` amplification + threadpool starvation — `routes/media.py` (F8, F9, F12)

- **`video()` (276-385):** replace the `max(144, min(4320, ...))` clamp (294-297) with the allowlist, snapping to nearest allowed height — this collapses ~4000 distinct cache keys to ≤4 (F8):
  ```python
  if max_height is not None and max_height not in SETTINGS.allowed_video_heights:
      max_height = min(SETTINGS.allowed_video_heights, key=lambda h: abs(h - max_height))
  ```
  Add deps `require_edge` + `rate_limit(video_rl)`; wrap download+mux in `with video_export_gate:` (F8/F9/F12). Re-validate the stored URL right after `cache.load_meta` (299):
  ```python
  try: validate_public_url(meta.url)            # F12/F27 re-check on the delayed fetch
  except UrlNotAllowed: raise HTTPException(404)
  ```
  Replace the leaky 502 detail (334) with a generic message (F20).
- **`audio()` mp3 branch (228-252):** wrap the transcode in `with audio_transcode_gate:`; generic ffmpeg error (F20). `/audio?format=opus` already streams in 64 KiB blocks (254-265) — no change, no OOM vector (F28).
- **`video_progress()` (388-400):** snap `max_height` identically so its progress key matches `video()`'s.

The gates are load-bearing: they cap how many threadpool threads a heavy op can ever hold, so `status`/`chunk`/`healthz` stay responsive (F9). Offloading `/video`+mp3 to a bounded background worker the client polls via `/video/{id}/progress` is the fuller fix and a follow-up; the gate ships first.

### 2.9 SSE caps — `routes/jobs.py` `events()` (F10)

Add `Depends(require_edge)` to the route. After `initial = registry.get(job_id)` and before `registry.subscribe`:
```python
if SETTINGS.public and not sse_counter.acquire(job_id, client_ip(request)):
    raise HTTPException(429, "too many streams", headers={"Retry-After": "5"})
```
Release in the `finally` (262) beside `unsubscribe`. Inside the `while True` loop (244), track `start = time.monotonic()` and `if time.monotonic() - start > SETTINGS.sse_max_lifetime_seconds: return` so held/slowloris streams self-close. Per-job pinning is already defused by the §2.6 absolute deadline. The existing `X-Accel-Buffering: no` + `Cache-Control: no-cache` + 15 s keepalive stay (critical for Cloudflare SSE — see Phase 4 §5.3).

### 2.10 CORS, headers, body size, fingerprint — `server.py` + `backend/middleware.py` (NEW) (F11, F13, F20, F21, F29)

`server.py` CORS block (214-221):
```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=SETTINGS.cors_origins,                   # F11: no '*' public
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],             # drop DELETE (unused)
    allow_headers=["Content-Type", "X-Admin-Token"],
    allow_private_network=not SETTINGS.public,            # F29: off public
)
```
**Server-side Origin gate (F11)** — CORS cannot stop side effects, so add an `enforce_origin` dependency on `POST /process` and `/prioritize` that 403s a present-but-disallowed `Origin` (allow missing Origin for non-browser/curl, already bounded by rate-limit + admission). Wired alongside `require_edge` in §2.5.

`backend/middleware.py` (NEW):
- `MaxBodySizeMiddleware` — reject `Content-Length > SETTINGS.max_request_bytes` for `POST` with 413 before Starlette buffers (F13).
- `SecurityHeadersMiddleware` — `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cross-Origin-Resource-Policy: same-site`, minimal CSP.

`server.py` `uvicorn.run` (both calls, 284-300): add `server_header=False, date_header=False` (F21). `routes/system.py` `get_capabilities` (23-45): in public mode drop `server_version` and `engine.device` — the extension consumes only models/stems/defaults (F21).

`ProcessRequest` (routes/jobs.py:98-101): `url = Field(..., min_length=1, max_length=SETTINGS.max_url_length)`; `keep_stems: Optional[list[str]] = Field(None, max_items=SETTINGS.max_keep_stems)`; dedup in `_validate_stems` (`return list(dict.fromkeys(v))`) (F13).

### 2.11 SSRF egress + extractor allowlist + size caps — `pipeline/downloader.py` (F4–F7)

`_common_opts()` (85-115): pin the extractor set in public mode so `generic` and yt-dlp's hundreds of other sites are off:
```python
if SETTINGS.public and SETTINGS.allowed_extractors:
    opts["allowed_extractors"] = list(SETTINGS.allowed_extractors)   # never 'generic'
```
`_source_download_opts()` (328-351) and `download_video` opts (292-301): add `"max_filesize": SETTINGS.max_source_filesize` / `SETTINGS.max_video_filesize` (F4/F7/F8). Keep `socket_timeout`; never set the `youtube-ejs` dev/repo/script_version extractor-args (F23/F26). ffmpeg continues to receive only local absolute paths — the user URL never reaches an ffmpeg argv (verified F31); no change there.

### 2.12 Admin-gate destructive/stats routes — `routes/system.py` (F3, F19)

Apply the admin dependency at router level so future settings-mutation routes inherit it:
```python
admin = APIRouter(dependencies=[Depends(require_admin)])

@admin.post("/cache/clear")
def cache_clear(request): ...                     # body unchanged (F3)

@admin.get("/cache")                              # F19: now admin-only
def cache_stats(request):
    cache = request.app.state.cache
    return cache.stats()                          # drop str(cache.root) (F19)
```
`server.py` includes `admin` alongside the others. The derivable-`job_id` oracle (F15) is **accepted** as residual (public content; per-user secrecy is impossible without client auth) and bounded by the per-IP rate limit on `/status`/`/video`.

### 2.13 Error sanitization — gate verbose output on `NOMUSIC_DEBUG` (F20)

- `jobs.py:479` — store `status.error = "processing failed"` (full traceback stays in `log.exception`) unless debug.
- `routes/jobs.py:164`, `routes/media.py:70/131/334` — static `detail` strings unless debug.
- Add a global `app.exception_handler(Exception)` in `server.py` returning a generic 500 so default tracebacks never leak.

### 2.14 Security-invariant comment — `server.py:202-213` (F3, F10, F29)

Replace the comment so it no longer asserts "any origin reaching it is already local." New text: the app binds loopback and is exposed to the public internet via a cloudflared **named tunnel**; therefore auth/rate-limit/CORS hardening is active under `NOMUSIC_PUBLIC=1`, identity is `CF-Connecting-IP`, destructive ops require `NOMUSIC_ADMIN_TOKEN`, and `allow_private_network` is enabled only in loopback-direct dev mode.

### 2.15 Supply chain (F16, F17, F23–F26)

- **`requirements.lock` (NEW)** via `pip-compile --generate-hashes` for the PyPI-resolved deps (`fastapi, uvicorn, pydantic, starlette, yt-dlp, soundfile, numpy, demucs, httpx, pytest`); pin `torch`/`torchaudio` to exact `==` (their wheels come from the PyTorch CPU/cuXXX index and cannot share one hash-lock). `install.sh:190` → `pip install --require-hashes -r backend/requirements.lock` (F16).
- **yt-dlp cadence (F25):** weekly CI/cron bumps the pin + hash and runs an extraction smoke test (Phase 4 §7.4).
- **Model weights (F24):** pre-stage `htdemucs`/`htdemucs_ft` into the torch hub cache at deploy time and make that dir read-only to the service user so `_ModelBundle` (`mlx_engine.py:289-292`) does no live network fetch/unpickle on the public path; keep `check_hash=True`.
- **JS runtime (F23):** install `deno` on Linux (not just macOS, `install.sh:69`) and keep the admin token **out of the subprocess env** — run under the systemd unit whose env is scrubbed before yt-dlp spawns the JS runtime, or read the token from a file. Never enable `youtube-ejs` dev args; pre-warm the EJS solver cache at provisioning (F26).
- **ffmpeg/distro (F17):** enable `unattended-upgrades` (or pin a known-good static ffmpeg) and record binary versions in the runbook; optionally sandbox ffmpeg/yt-dlp under the systemd unit (already done via §Phase 4 sandbox).

### 2.16 Implementation order (Phase 1)

1. `config.py` (settings + `_env_tuple` + `cors_origins`).
2. `netsec.py`, `security.py`, `ratelimit.py`, `middleware.py` (NEW).
3. `jobs.py` admission/deadline/lock-removal + `engines/mlx_engine.py` `_infer_lock` (**critical availability fixes F1/F2**).
4. `routes/jobs.py` (URL delegation, `process()` deps + `JobRejected`→429, SSE caps + lifetime, `JobId`).
5. `routes/media.py` (`JobId`/`ChunkIdx`, height allowlist, gates, `/video` re-validation, generic errors).
6. `routes/system.py` (admin router, drop `root`).
7. `server.py` (locked CORS, middlewares, `server_header=False`, slim `/capabilities`, exception handler, invariant comment).
8. `pipeline/cache.py` (`evict_to_fit`/`total_bytes` + `_key_dir` guard); `pipeline/downloader.py` (`allowed_extractors`, `max_filesize`, duration ceiling); `pipeline/processor.py` (abort_check + download watchdog).
9. `requirements.lock`, `install.sh` + supply-chain items.
10. Extension build (Phase 2) consumed last, including `NOMUSIC_EXTENSION_ORIGIN`.

---

## 3. Phase 2 — Extension changes (public default + Web Store)

All line numbers re-confirmed against the files; nothing modified yet. Key facts: `activeTab` is **dead** (zero uses of `activeTab`/`chrome.scripting`/`chrome.tabs`/`executeScript`); the backend default appears in 7 spots; the only mutating POSTs are `session.js:217` (`/process`), `session.js:498` (`/prioritize`), and `popup.js:210` (`/cache/clear`, to be removed). `extension/dist/` and `extension/*.zip` are already gitignored.

### 3.1 Centralize the backend default and switch http → https

Add one side-effect-free module `extension/config.js`:
```js
// config.js — single source of truth for shipped defaults. No side effects, so it is
// safe to import from popup, service worker, and content scripts.
// FLAG: owner fills in <DOMAIN> before upload.
export const DEFAULT_BACKEND = "https://nomusic.<DOMAIN>";
```
Rewire each site (every one becomes **https**):
- **`settings.js:5`** — delete the literal; re-export: `export { DEFAULT_BACKEND } from "./config.js";` (line 25 `backendUrl: DEFAULT_BACKEND` keeps working).
- **`background.js`** — make the SW a module: in `manifest.json` set `"background": { "service_worker": "background.js", "type": "module" }`; add `import { DEFAULT_BACKEND } from "./config.js";`; set line 11 → `backendUrl: DEFAULT_BACKEND` (line 33 fallback inherits it).
- **`popup.html:162`** — `<script type="module" src="popup.js"></script>`.
- **`popup.js`** — add `import { DEFAULT_BACKEND } from "./config.js";`; replace literals at lines 35 and 240.
- **`popup.html:140`** — remove the hard-coded value; let `load()` populate it: `<input id="backend" type="url" placeholder="https://nomusic.<DOMAIN>" />`.
- **Tests** — update `tests/settings.test.js:14` and `tests/background.test.js:39` to the https default (and import source); keep `tests/settings.test.js:22-27` as the user-override regression.

### 3.2 `manifest.json` host_permissions → first-party HTTPS + opt-in

Replace lines 7-9:
```json
"host_permissions": ["https://nomusic.<DOMAIN>/*"],
"optional_host_permissions": ["*://*/*"]
```
Drops the `http://127.0.0.1` mixed-content/dev-leftover flag and declares one concrete HTTPS host (reviewer-friendly). `optional_host_permissions` is not requested at install (no scary consent) and lets a self-hoster point the popup at their own backend. Wire it on the backend-URL change (`popup.js:267`), on the user gesture:
```js
const origin = new URL($("backend").value).origin + "/*";
await chrome.permissions.request({ origins: [origin] });
```
`chrome.permissions` needs no manifest permission entry.

### 3.3 Curate content_scripts + web_accessible_resources

**Default match list (locked §0 — YouTube + Facebook).** YouTube has first-party support (`page-script.js:75-78` hard-codes YouTube's `movie_player`/`getVideoData`; `content.js`/`main.js` rely on `yt-navigate-finish`); Facebook rides the generic `<video>` path (no first-party hooks, so behavior is best-effort there). Default:
```json
"matches": [
  "*://*.youtube.com/*",
  "*://*.youtube-nocookie.com/*",
  "*://*.facebook.com/*"
]
```
Apply to **both** content-script entries (`manifest.json:15`, `:22`) and `web_accessible_resources.matches` (`:58`). Keep `all_frames: true` (lines 18, 26) — YouTube embeds run in `youtube-nocookie.com` iframes and Facebook videos in nested frames. Add `"config.js"` to `web_accessible_resources.resources` (47-57) since `content.js` does `import(chrome.runtime.getURL("main.js"))` and the module graph now reaches `config.js`. The store description must name exactly YouTube + Facebook.

To add more sites later (e.g. Vimeo), extend both this match list and `NOMUSIC_ALLOWED_URL_HOSTS`/`allowed_extractors`, and update the store description — the smaller the list, the smoother the review.

**Optional "enable everywhere" (advanced, ship now or later):** add `"scripting"` to `permissions` (no host warning); add a popup control that on click calls `chrome.permissions.request({ origins:[...] })` then `chrome.scripting.registerContentScripts([...])`. Additive; the curated list stands alone.

### 3.4 Remove the Clear-Cache control AND the cache-stats panel

The panel now shows global, shared-server stats to every user, and `/cache/clear` is admin-only server-side.
- **`popup.html`** — delete the cache-row block (148-154, incl. `#clearCache .danger`); delete now-dead CSS `.cache-row` (108-123), `button.danger.armed` (124-128), `button.danger` (101-105), `button:disabled` (129-132). Keep `.hint`, `.err`, `.status`, `.field-label`, base `button`.
- **`popup.js`** — delete `fmtBytes()` (116-127), `refreshCacheSize()` (129-157), `refreshCacheTtl()` (159-176), the arm/confirm + `clearCache()` machinery (178-227, the only `/cache/clear` caller at 210), and the three trailing `DOMContentLoaded` lines (271-273). Keep `flashSaved()`, `load()`, `persist()`, and the model/stems/backend listeners (261-270). After this the popup makes only the read-only `/capabilities` probe (39).
- Optional: replace the panel with a static hint "Processed audio is cached on the server and auto-deleted after N days." — but do **not** call `GET /cache`.

### 3.5 Confirm remaining traffic + CORS reality

After §3.4, server calls are: core mutating `POST /process` (session.js:216) and `POST /process/{id}/prioritize` (session.js:498) — model/stems are per-request body params, not server settings; read-only `GET /capabilities`, `EventSource /events`, `GET /chunk`, `GET /audio`, `GET /video` + `/progress`. No `/cache`/`/cache/clear`/settings mutation. `host_permissions: ["https://nomusic.<DOMAIN>/*"]` grants cross-origin fetch **and** EventSource to that host; the backend's locked CORS + `allow_credentials=False` (no cookies sent) satisfies it.

### 3.6 Version + single-purpose description

- **Version:** bump `manifest.json:4` `0.1.0` → `0.2.0` (match `package.json:3`) or `1.0.0` for first public release; each CWS upload must strictly increase.
- **Description (`manifest.json:5`)** — drop "local" and "rest of the web"; use single-purpose phrasing:
  ```json
  "description": "Removes the background music from the video you're watching and plays a vocals-only version of its audio in sync with the video."
  ```

### 3.7 Add a pack step (`extension/package.json`)

```json
"scripts": {
  "test": "node --test --import ./tests/setup.js tests/*.test.js",
  "pack": "rm -rf dist && mkdir -p dist && zip -r dist/nomusic-$npm_package_version.zip . -x 'tests/*' 'dist/*' 'package.json' '.gitignore' '*.DS_Store'"
}
```
Run from `extension/` so `manifest.json` sits at the zip root. Output `extension/dist/nomusic-<version>.zip` is gitignored. **Include:** `manifest.json`, `config.js`, `settings.js`, `session.js`, `button.js`, `mute-controller.js`, `audio-scheduler.js`, `stretch.js`, `main.js`, `content.js`, `content.css`, `page-script.js`, `background.js`, `popup.html`, `popup.js`, `icons/*`, `third_party/soundtouch/*`. **Remote-code sanity check before upload:** `grep -rn "http://\|https://" extension --include='*.js' | grep -v config.js` should surface only data endpoints (no `<script src>`, `import('http…')`, `eval`, `new Function`); confirm `config.js` is the only place `nomusic.<DOMAIN>` appears.

### 3.8 Execution order (Phase 2)

1. Add `extension/config.js`. 2. Rewire the 7 default-URL sites (http→https). 3. `manifest.json`: drop `activeTab`, `host_permissions` + `optional_host_permissions`, curate matches + WAR, add `config.js` to WAR, bump version, rewrite description, `background.type:"module"`. 4. Remove Clear-Cache + cache panel. 5. Wire optional-host-permission request. 6. Update tests. 7. Add `pack`, produce the zip, run the remote-code grep.

---

## 4. Phase 3 — Chrome Web Store submission

Because the extension transmits page-derived URLs to a developer-operated server and uses MAIN-world injection, expect **in-depth review (1–2 weeks, up to ~3–4 for a new account)**. You get one appeal per violation — address every cited point precisely.

### 4.1 Single purpose (dashboard "single purpose" field, verbatim)

> nomusic removes the background music from the video on the page you are watching and plays a vocals-only version of its audio in sync with the video.

Description rules: lead with the one capability; no "and also" features; explicitly state that processing happens on a remote server you operate and that the page's video URL is sent there (reviewers cross-check listing vs. code vs. privacy disclosures); avoid copyright-flavored wording ("rip"/"download music"/"bypass") — frame as accessibility/listening preference; describe model/stem selectors and the backend-URL field as *configuration*, not separate features.

### 4.2 Permission justifications (paste-ready)

**`storage`**
> Stores the user's own preferences locally: the backend server URL, the selected separation model, and which stems to keep. Synced via chrome.storage so settings persist across the user's signed-in Chrome profiles. No browsing data, page content, or personal data is stored.

**host_permissions → `https://nomusic.<DOMAIN>/*`**
> This is the extension's own audio-processing backend. The extension sends the URL of the video on the current page to this server and streams back a vocals-only audio track and processing status (Server-Sent Events). Access is limited to this single first-party HTTPS host; the extension makes no requests to any other server.

**Content scripts (curated match list)**
> Content scripts run only on the specific streaming sites the extension supports (listed in the manifest match patterns). They read the current video's source URL, mute the page's original audio track, and keep the replacement vocals-only audio in sync with the page's video element. They do not read page text, form fields, cookies, or any personal data.

**`world: "MAIN"` content script (page-script.js)**
> A single small script runs in the page's main world to patch the HTMLMediaElement volume property and bridge to the site's own video player API (e.g. the YouTube IFrame API). This is required because those player objects and the media element's behavior are only reachable from the page's JavaScript context, not from an isolated content script. The script only mutes/controls the existing video element and reports playback time for synchronization; it injects no remote code and transmits nothing.

`activeTab` is dropped (dead permission; an unused permission is a direct rejection risk).

### 4.3 Remote-code compliance (MV3)

Compliant **as long as** the backend never serves executable JS and all `import()`s resolve to `chrome.runtime.getURL()` paths. Dynamically importing your own bundled modules via `getURL()` is allowed (not remote code). The backend returning opus/mp3/MP4/JSON/SSE is **data**, permitted. Verify: no `<script src="https://...">`, no `import('https://...')`, no `eval`/`new Function` on server responses; `GET /audio`/`/chunk`/`/events`/`/status` carry audio/JSON/`text/event-stream` content types, never `application/javascript`; grep the **built** bundle for `http://`/`https://`.

### 4.4 Privacy / data disclosure

A privacy policy is **mandatory** (the extension sends page URLs off-device = "personal and sensitive user data").

**Dashboard "Privacy practices" tab:** Data collected = **Website content** (the video URL). Purpose = **App functionality** only. Certify all three: not selling data; not using/transferring for unrelated purposes; not for creditworthiness/lending. Provide the privacy-policy URL. Include the Limited Use statement.

**In-product prominent disclosure + consent:** show a one-time banner/dialog in the popup or pill stating "video URLs from supported sites are sent to nomusic.<DOMAIN> for processing" with an explicit Accept before the first `POST /process`.

**Privacy policy draft (host at `https://nomusic.<DOMAIN>/privacy`):**

```markdown
# nomusic — Privacy Policy
Last updated: <DATE>

nomusic is a Chrome extension that removes the background music from the video on
the page you are watching and plays a vocals-only version of its audio. This
policy explains what data the extension handles.

## What we collect
To process a video, the extension sends the following to the nomusic backend
server (https://nomusic.<DOMAIN>), operated by the extension's developer:
- The URL of the video on the page you choose to process.
- The processing options you select (separation model and which stems to keep).

We do NOT collect: your name, email, account credentials, IP-based identity
profiles, browsing history, page text, form data, cookies, or any other personal
information. The extension has no user accounts and no login.

The extension reads the current page's video element and player only on the
specific streaming sites it supports, solely to mute the original audio and
synchronize the vocals-only audio. This page content is processed locally in your
browser and is not transmitted, except for the video URL described above.

## How we use it
The video URL and options are used only to download and process that video on the
server and stream the resulting vocals-only audio back to your browser. We do not
use this data for advertising, profiling, analytics, or any purpose unrelated to
this single function. We do not sell or rent data to anyone.

## Server-side caching and retention
To avoid reprocessing the same video, the server temporarily caches the downloaded
source and the generated audio, keyed to the video URL and options. Cached files
are automatically deleted after <N> day(s) by an automatic cleanup process. We do
not associate cached files with any user identity.

## Sharing
We do not share your data with third parties. The video URL is sent only to our
own backend. The backend reaches the third-party video site (e.g. YouTube) to
download the video you requested; your interaction with those sites is governed by
their own privacy policies. Traffic to our backend is proxied through Cloudflare,
which may process connection metadata as our infrastructure provider.

## Security
All communication between the extension and the backend uses HTTPS. The backend is
not publicly browsable and exposes only the processing endpoints.

## Analytics / tracking
None. The extension contains no analytics, tracking, or advertising code.

## Your choices
Processing only happens when you click the in-page control. You can change or
self-host the backend URL in the extension settings, or uninstall the extension at
any time.

## Limited Use
nomusic's use of information received from Google APIs and from users adheres to
the Chrome Web Store User Data Policy, including the Limited Use requirements.

## Contact
Questions: <CONTACT_EMAIL>
```
Set `<N>` to the deployed `NOMUSIC_CACHE_TTL_DAYS` (1 day in Phase 4) and keep "no analytics" true.

### 4.5 Other gates & listing assets

US$5 verified developer account + verified contact email + identity/publisher verification (faster review). Fill every permission-justification field and the single-purpose field. Surface the remote-server fact via description + "website content" declaration + in-product disclosure. Assets: ≥1 screenshot 1280×800 (or 640×400) of the actual extension, 128×128 store icon, manifest icons 16/32/48/128, category Accessibility or Entertainment, short summary ≤132 chars.

### 4.6 Pre-submission checklist (preempts common rejections)

- [ ] Version reconciled and increasing; no `http://127.0.0.1` defaults anywhere (settings.js:5, background.js:11, popup.html:140, popup.js:35/240).
- [ ] `host_permissions` = exactly `["https://nomusic.<DOMAIN>/*"]` (HTTPS only).
- [ ] `content_scripts.matches` and `web_accessible_resources.matches` = curated list, not `<all_urls>`.
- [ ] `activeTab` removed (grep-confirmed unused).
- [ ] Clear-Cache / cache panel removed; client never calls destructive endpoints.
- [ ] Default MV3 CSP unchanged; no `'unsafe-eval'`, no remote `script-src`.
- [ ] Every permission justified; no unused permissions; no leftover debug; built bundle grep clean.
- [ ] Privacy policy hosted + linked; Limited Use sentence present; dashboard privacy fields complete; in-product consent before first `POST /process`.
- [ ] Marketing framed as accessibility/listening preference, not ripping.

---

## 5. Phase 4 — Deployment runbook (Linux laptop + Cloudflare named tunnel)

> Phase 4 assumes **Phase 1 has landed**. If you deploy before the `NOMUSIC_PUBLIC` controls and `NOMUSIC_ADMIN_TOKEN` gate exist, `POST /cache/clear` and `GET /cache` are **wide open** and the Cloudflare WAF rule (§5.6) is your **primary** protection for destructive ops — not defense-in-depth. The env file sets `NOMUSIC_ADMIN_TOKEN`/`NOMUSIC_PUBLIC` forward-compatibly; unknown `NOMUSIC_*` vars are silently ignored, so they become live automatically once the code ships. SSE anti-buffering (`X-Accel-Buffering: no` + `Cache-Control: no-cache`) and the loopback bind are already in code; the SSRF guard on `/process` exists today and is hardened to an allowlist in Phase 1.

Placeholders to fill: `<DOMAIN>`, `<ADMIN_LAN_IP>`, `<GATEWAY_IP>`, `<DNS_IP>`, `<UUID>`, `<your-repo-url>`.

### 5.1 Laptop prep

```bash
# OS up to date
sudo apt-get update && sudo apt-get -y full-upgrade
sudo apt-get install -y git curl ca-certificates ufw nftables

# NVIDIA driver — install if missing, then VERIFY
nvidia-smi || { sudo ubuntu-drivers autoinstall && sudo reboot; }
nvidia-smi   # must print GPU, driver, CUDA version before continuing

# Unprivileged service user; writable state under /var/lib
sudo useradd --system --create-home --home-dir /var/lib/nomusic \
     --shell /usr/sbin/nologin --comment "nomusic backend" nomusic
sudo mkdir -p /var/lib/nomusic/cache /var/lib/nomusic/torch /var/lib/nomusic/.cache
sudo chown -R nomusic:nomusic /var/lib/nomusic
sudo chmod 750 /var/lib/nomusic

# NVIDIA device-node group (only if a group owns the nodes)
ls -l /dev/nvidia*
# e.g.: sudo usermod -aG video,render nomusic

# Clone to a root-owned tree and install (apt python/ffmpeg/git/nodejs+deno, venv, CUDA torch)
sudo git clone <your-repo-url> /opt/nomusic
# GTX 1050 Ti is Pascal (sm_61): pin the legacy CUDA build. torch 2.4.1 is the
# last wheel shipping sm_61 kernels; newer CUDA-12 wheels fall back to CPU.
sudo NOMUSIC_PYTHON=3.11 NOMUSIC_CUDA=cu118 NOMUSIC_TORCH=2.4.1 bash /opt/nomusic/install.sh
#   -> the install's device check MUST print "engine device: cuda" (not cpu).

# Code + venv readable but NOT writable by the service user
sudo chmod -R a+rX /opt/nomusic

# Independent GPU check AS the service user
sudo -u nomusic env HOME=/var/lib/nomusic TORCH_HOME=/var/lib/nomusic/torch \
  PYTHONPATH=/opt/nomusic/backend /opt/nomusic/backend/.venv/bin/python - <<'PY'
import torch
from engines.mlx_engine import _pick_device
print("torch", torch.__version__, "device:", _pick_device())
PY
```
The service user owns only `/var/lib/nomusic`; it cannot rewrite `/opt/nomusic` or the venv, so an RCE in a worker can't backdoor the binaries it runs next boot. **Two writable trees matter:** the cache and `TORCH_HOME` (demucs downloads ~80 MB weights there on first run). Pre-stage the weights and make `TORCH_HOME` read-only afterward to satisfy F24.

### 5.2 Admin token + environment file

```bash
openssl rand -hex 32        # 64 hex chars; paste into the env file below
```
`/etc/nomusic/nomusic.env`:
```bash
sudo mkdir -p /etc/nomusic
sudo tee /etc/nomusic/nomusic.env >/dev/null <<'EOF'
# Bind: loopback only (defense in depth; matches config.py default)
NOMUSIC_HOST=127.0.0.1
NOMUSIC_PORT=8723

# Writable state into the one ReadWritePaths tree
HOME=/var/lib/nomusic
TORCH_HOME=/var/lib/nomusic/torch
XDG_CACHE_HOME=/var/lib/nomusic/.cache
NOMUSIC_CACHE_DIR=/var/lib/nomusic/cache

# Hosted cache hygiene (implemented today; config.py)
NOMUSIC_CACHE_TTL_DAYS=1
NOMUSIC_CACHE_SWEEP_INTERVAL_SECONDS=900
NOMUSIC_KEEP_SOURCE_AFTER_COMPLETE=0

# Throttle yt-dlp egress so a /process flood can't saturate the home uplink
NOMUSIC_DOWNLOAD_RATELIMIT=4M

# JS runtime for yt-dlp's YouTube challenge — prefer deno (deny-all) on Linux (F23)
NOMUSIC_JS_RUNTIME=/usr/bin/deno

# Public hardening (Phase 1). Live once the code ships; harmless before.
NOMUSIC_PUBLIC=1
NOMUSIC_ADMIN_TOKEN=PASTE_THE_openssl_rand_hex_32_OUTPUT_HERE
NOMUSIC_TUNNEL_SECRET=PASTE_A_SECOND_openssl_rand_hex_32_HERE
NOMUSIC_EXTENSION_ORIGIN=chrome-extension://<PUBLISHED_EXT_ID>
EOF
sudo chmod 600 /etc/nomusic/nomusic.env
sudo chown root:root /etc/nomusic/nomusic.env
```
The `NOMUSIC_TUNNEL_SECRET` here must match the header `cloudflared` injects (§5.4). Keep the admin token out of the env inherited by the yt-dlp JS subprocess where practical (F23) — the hardened unit's scrubbed env plus deno's deny-all posture are the controls.

### 5.3 Hardened backend systemd unit

`/etc/systemd/system/nomusic.service`. Two directives are deliberately **off** because they break torch/CUDA — re-verify after any torch upgrade.

```ini
[Unit]
Description=nomusic backend (FastAPI source-separation, loopback only)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=nomusic
Group=nomusic
WorkingDirectory=/opt/nomusic/backend
EnvironmentFile=/etc/nomusic/nomusic.env
ExecStart=/opt/nomusic/backend/.venv/bin/python /opt/nomusic/backend/server.py
Restart=on-failure
RestartSec=5s
StartLimitIntervalSec=300
StartLimitBurst=10

# Force NVIDIA node creation before the sandbox starts (root via '+')
ExecStartPre=+/usr/bin/nvidia-smi

# Privilege / exec hardening
NoNewPrivileges=yes
LockPersonality=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
RemoveIPC=yes
ProtectHostname=yes
ProtectClock=yes

# Filesystem
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/var/lib/nomusic

# Kernel / namespace
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectProc=invisible
ProcSubset=pid
RestrictNamespaces=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM

# GPU access: PrivateDevices=yes WOULD REMOVE the GPU nodes. Whitelist instead.
PrivateDevices=no
DevicePolicy=closed
DeviceAllow=/dev/nvidia0 rw
DeviceAllow=/dev/nvidiactl rw
DeviceAllow=/dev/nvidia-uvm rw
DeviceAllow=/dev/nvidia-uvm-tools rw
DeviceAllow=/dev/nvidia-modeset rw
DeviceAllow=char-nvidia-caps rw
# add /dev/nvidia1 ... for extra GPUs

# MUST stay off: torch/CUDA/oneDNN JIT needs W+X mappings (pytorch#143651)
MemoryDenyWriteExecute=no

# DoS containment (tune to the laptop; CUDA VRAM is NOT counted by MemoryMax)
MemoryMax=12G
MemoryHigh=10G
CPUQuota=350%
TasksMax=512
LimitNOFILE=8192

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nomusic
journalctl -u nomusic -f         # wait for "Engine warmup complete"
sudo systemd-analyze security nomusic
curl -s http://127.0.0.1:8723/capabilities | python3 -m json.tool   # device == "cuda"
```
If CUDA init fails with `EPERM`/`EACCES`: temporarily add `SystemCallLog=@system-service` (audit-only) to find the denied group (commonly `@resources`/`@memlock`), or `strace -f .../python -c "import torch; torch.cuda.init()"` to find a missing `/dev/nvidia*` node, then add it to `DeviceAllow`. `MemoryDenyWriteExecute` and `PrivateDevices` must stay off on this unit.

### 5.4 Cloudflare named tunnel + cloudflared service

**Add the zone:** in the dashboard, Add a site for `<DOMAIN>` (Free plan), repoint registrar nameservers to Cloudflare's, wait for **Active**.

**Install cloudflared (apt repo = apt-updatable):**
```bash
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install -y cloudflared
cloudflared --version
```

**Login, create, route DNS (interactive, as admin user):**
```bash
cloudflared tunnel login                              # -> ~/.cloudflared/cert.pem
cloudflared tunnel create nomusic                     # prints <UUID>, writes <UUID>.json
cloudflared tunnel route dns nomusic nomusic.<DOMAIN> # proxied CNAME -> <UUID>.cfargotunnel.com
cloudflared tunnel list && cloudflared tunnel info nomusic
```

**Dedicated hardened cloudflared user + config:**
```bash
sudo useradd --system --no-create-home --home-dir /nonexistent \
     --shell /usr/sbin/nologin --comment "cloudflared tunnel" cloudflared
sudo mkdir -p /etc/cloudflared
sudo cp ~/.cloudflared/<UUID>.json /etc/cloudflared/
```
`/etc/cloudflared/config.yml`:
```yaml
tunnel: <UUID>
credentials-file: /etc/cloudflared/<UUID>.json
no-autoupdate: true            # apt is the single update source
protocol: quic                 # UDP/7844, auto-fallback to http2
ingress:
  - hostname: nomusic.<DOMAIN>
    service: http://127.0.0.1:8723
    originRequest:
      connectTimeout: 30s
      disableChunkedEncoding: false   # MUST stay false so SSE/chunked streams pass
      noTLSVerify: true               # origin is plain http on loopback; harmless
      httpHostHeader: nomusic.<DOMAIN>
      # F14: inject the shared secret so a LAN-direct hit to :8723 is rejected by the app
      setHostHeader: nomusic.<DOMAIN>
  - service: http_status:404         # catch-all REQUIRED + last
```
To inject `X-Nomusic-Tunnel`, configure the tunnel's origin request headers (dashboard "Public Hostname" → HTTP Settings → add request header `X-Nomusic-Tunnel: <NOMUSIC_TUNNEL_SECRET>`, matching §5.2), since `config.yml` ingress does not set arbitrary request headers directly. If header injection is not configured, leave `NOMUSIC_TUNNEL_SECRET` empty so `require_edge` is a no-op (loopback bind + no port-forward already prevents inbound abuse).
```bash
sudo chown -R cloudflared:cloudflared /etc/cloudflared
sudo chmod 600 /etc/cloudflared/<UUID>.json
cloudflared tunnel ingress validate
cloudflared tunnel ingress rule https://nomusic.<DOMAIN>/healthz
```
`/etc/systemd/system/cloudflared.service` (Go/no-JIT, so MDWE + PrivateDevices are safe here):
```ini
[Unit]
Description=cloudflared tunnel for nomusic
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
User=cloudflared
Group=cloudflared
ExecStart=/usr/bin/cloudflared --no-autoupdate --config /etc/cloudflared/config.yml tunnel run nomusic
Restart=on-failure
RestartSec=5s
NoNewPrivileges=yes
LockPersonality=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
RemoveIPC=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectClock=yes
ProtectHostname=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectProc=invisible
ProcSubset=pid
ReadOnlyPaths=/etc/cloudflared
RestrictNamespaces=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
MemoryDenyWriteExecute=yes
MemoryMax=512M
TasksMax=256
LimitNOFILE=4096

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cloudflared
journalctl -u cloudflared -f        # expect ~4 connections to >=2 datacenters
sudo systemd-analyze security cloudflared
```

### 5.5 SSE, long requests, body size through Cloudflare

- **SSE (`/events`):** Cloudflare can buffer `text/event-stream` and 524s after ~100 s idle. The existing 15 s keepalive + `X-Accel-Buffering: no` + `Cache-Control: no-cache` (already in code) are the right levers; keep `disableChunkedEncoding: false`; do not cache or compress this path. If a future edge change buffers SSE, the extension's fallback is to poll `GET /status/{job_id}` (which already reads on-disk `meta.json`).
- **Long requests (`/video`, `/audio?format=mp3`):** ~100 s **first-byte** limit before 524; after streaming starts, transfer continues while bytes flow. The Phase 1 background-worker offload + `/video/{id}/progress` polling is the durable fix; until then, stream early bytes. The work completes on the laptop regardless; the cached file is then retrievable.
- **Body size:** Free/Pro cap is 100 MB request body — a non-issue for the tiny `POST /process` JSON. There is no Cloudflare response-size cap (the constraint there is the §5.7 media ToS, not size).

### 5.6 Edge protections (dashboard, zone `<DOMAIN>`)

- **Rate-limiting rule (Free = 1)** on the GPU endpoint: expression `(http.request.method eq "POST" and starts_with(http.request.uri.path, "/process") and not http.request.uri.path contains "/prioritize")`, count by **IP**, threshold **5 req / 60 s**, action **Block** 60 s. Tune to GPU throughput.
- **Bot Fight Mode** (Security → Bots); after enabling, test `POST /process` from the extension and relax if challenged.
- **WAF managed/free ruleset + Security Level Medium/High**; **DDoS protection** is automatic.
- **⚠️ Custom rule blocking destructive paths (Free = 5 custom rules):** expression `(starts_with(http.request.uri.path, "/cache"))` → **Block**. Until the Phase 1 admin gate ships this is the **only** protection for the unauthenticated `POST /cache/clear` and `GET /cache`. You still reach those over loopback on the laptop (§5.8). When the server-side gate lands, keep this rule as defense-in-depth.
- Identity at the edge: Cloudflare overwrites `CF-Connecting-IP` (the value the app trusts, F14). **Leave Cloudflare Access OFF** — the audience is public by design; service tokens are the future privatization lever if you ever switch to a private audience.

### 5.7 Cloudflare media ToS caveat (read before publicizing)

The hostname is **proxied**, so all `GET /audio`/`/video` bytes transit Cloudflare's CDN. Cloudflare's Service-Specific Terms let them throttle/limit serving "video or a disproportionate percentage of … audio/large files" via the CDN **without** a paid product (Stream/R2/Images). **Pro/Business do NOT exempt you — only Enterprise or R2/Stream do.** Mitigation: keep volume low via the rate-limit rule + 1-day TTL, and have the **R2 artifact-offload** path ready (upload finished `/audio`+`/video` artifacts to R2, hand clients a direct R2 URL, keep only JSON/SSE control traffic on the tunnel) as the contingency if Cloudflare sends a notice. For a single home laptop with strict quotas, total egress is naturally bounded; start with quotas + low volume (acceptable risk) and switch to R2 the moment Cloudflare flags it.

### 5.8 Firewall + LAN isolation

No inbound port and **no router port-forward** — cloudflared dials out over 443/UDP-7844.

**ufw default-deny inbound:**
```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow in on lo                 # cloudflared -> 127.0.0.1:8723 rides loopback
# OPTIONAL admin SSH from ONE LAN host (omit if you use console/Tailscale):
sudo ufw allow from <ADMIN_LAN_IP> to any port 22 proto tcp
sudo ufw enable
sudo ufw status verbose
```

**Egress lateral-movement block (nftables) — the durable F5/F6 control.** `/etc/nftables.conf`:
```nft
table inet egress {
    chain output {
        type filter hook output priority 0; policy accept;
        oif "lo" accept
        ip daddr <DNS_IP> udp dport 53 accept
        ip daddr <DNS_IP> tcp dport 53 accept
        ip daddr <GATEWAY_IP> accept
        ip daddr 10.0.0.0/8      reject with icmp type admin-prohibited
        ip daddr 172.16.0.0/12   reject with icmp type admin-prohibited
        ip daddr 192.168.0.0/16  reject with icmp type admin-prohibited
        ip daddr 169.254.0.0/16  reject with icmp type admin-prohibited
        ip daddr 100.64.0.0/10   reject     # CGNAT — DELETE this line if you run Tailscale
        # public internet allowed by policy accept
    }
}
```
```bash
sudo nft -f /etc/nftables.conf
sudo systemctl enable --now nftables
```
This drops RFC1918/loopback/link-local egress for everything on the box, so a followed yt-dlp redirect or DNS-rebind to an internal host simply fails — closing the residual SSRF that the app-layer allowlist cannot fully cover. If you use Tailscale: drop the `100.64.0.0/10` line and add `oif "tailscale0" accept`.

**Strongest isolation (off-box):** put the laptop on its own guest SSID with client isolation or a separate VLAN whose ACL denies the nomusic subnet → RFC1918 (gateway + DNS + internet only). On-box rules can be undone by a root compromise; segmentation cannot.

**Kill LAN-discovery services:**
```bash
sudo systemctl disable --now avahi-daemon.socket avahi-daemon.service 2>/dev/null || true
sudo systemctl disable --now smbd nmbd cups cups-browsed 2>/dev/null || true
```

**Admin access:** never port-forward SSH. Prefer local console or Tailscale SSH (`tailscale up --ssh`); if you keep LAN SSH, harden it (keys only, `PermitRootLogin no`, `PasswordAuthentication no`, `AllowUsers <admin>`, `MaxAuthTries 3`) and add fail2ban.

### 5.9 Admin operations

**Clear the cache.** After the Phase 1 gate ships:
```bash
curl -fsS -X POST https://nomusic.<DOMAIN>/cache/clear -H "X-Admin-Token: $(sudo grep NOMUSIC_ADMIN_TOKEN /etc/nomusic/nomusic.env | cut -d= -f2)"
```
(temporarily disable the §5.6 WAF rule or carve an authenticated-header exception). Before the gate ships, call it over loopback only:
```bash
curl -fsS -X POST http://127.0.0.1:8723/cache/clear
```
Routine cleanup needs none of this — the 1-day TTL sweep + LRU evictor reclaim space automatically.

**Logs / disk / journal:**
```bash
journalctl -u nomusic -f
journalctl -u nomusic -p err --since today
journalctl -u cloudflared -f
df -h /var/lib/nomusic ; du -sh /var/lib/nomusic/cache
```
Hourly disk warning `/etc/cron.hourly/nomusic-disk` (`chmod +x`):
```bash
#!/bin/sh
USE=$(df --output=pcent /var/lib/nomusic | tail -1 | tr -dc 0-9)
[ "$USE" -gt 85 ] && logger -p user.warning "nomusic cache disk ${USE}% full"
```
Cap the journal so logs can't fill the disk:
```bash
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=500M\nMaxRetentionSec=2week\n' | sudo tee /etc/systemd/journald.conf.d/cap.conf
sudo systemctl restart systemd-journald
```
Consider a dedicated partition / XFS project quota for `/var/lib/nomusic` so a full cache can't take down the OS (the Phase 1 LRU cap + free-disk floor bound it in-app, this bounds it at the filesystem).

### 5.10 Updates (§F17, F25)

```bash
# yt-dlp breaks when YouTube changes its player — bump weekly (automate via cron/CI):
sudo /opt/nomusic/backend/.venv/bin/pip install -U yt-dlp && sudo systemctl restart nomusic
# cloudflared + OS security patches via apt:
sudo apt-get update && sudo apt-get -y upgrade
sudo apt-get install -y unattended-upgrades apt-listchanges
sudo dpkg-reconfigure -plow unattended-upgrades
```
Keep `no-autoupdate: true` in cloudflared's config so apt is the single update source. Record ffmpeg/deno/node versions in the deploy log.

### 5.11 Smoke tests

```bash
# Liveness + capabilities through the tunnel
curl -fsS https://nomusic.<DOMAIN>/healthz                              # {"ok":true}
curl -fsS https://nomusic.<DOMAIN>/capabilities | python3 -m json.tool  # engine ok

# One real job end-to-end
JOB=$(curl -fsS -X POST https://nomusic.<DOMAIN>/process \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["job_id"])')
echo "job=$JOB"
curl -fsS "https://nomusic.<DOMAIN>/status/$JOB" | python3 -m json.tool

# SSE streams incrementally (events + ': keep-alive' ~every 15s, NOT one buffered dump)
curl -N -fsS "https://nomusic.<DOMAIN>/events/$JOB"

# Fetch a finished chunk once ready
curl -fsS -o /tmp/test.opus "https://nomusic.<DOMAIN>/audio/$JOB?format=opus" && ls -l /tmp/test.opus

# Negative checks (after Phase 1):
curl -fsS -o /dev/null -w '%{http_code}\n' https://nomusic.<DOMAIN>/cache            # 404 (admin-gated/WAF)
curl -fsS -o /dev/null -w '%{http_code}\n' -X POST https://nomusic.<DOMAIN>/cache/clear  # 404
curl -fsS -o /dev/null -w '%{http_code}\n' "https://nomusic.<DOMAIN>/status/%2e%2e"  # 404 (job_id pattern)
```

### 5.12 Kill switch + rollback

```bash
sudo systemctl stop cloudflared        # INSTANT offline (reversible); app keeps running locally
sudo systemctl start cloudflared       # resume
sudo systemctl stop nomusic            # harder off — nothing listens on 8723
sudo systemctl disable --now cloudflared  # "disable public mode", keep loopback API
# Permanent teardown:
cloudflared tunnel cleanup nomusic
cloudflared tunnel delete nomusic      # refuses while connections are active
#   then delete the CNAME nomusic.<DOMAIN> in dashboard DNS -> Records
```
Fastest reversible kill = `systemctl stop cloudflared`. For a maintenance page, add a second ingress rule returning `http_status:503` while the real service is down.

---

## 6. Consolidated go-live checklist

**Backend (Phase 1)**
- [ ] `NOMUSIC_PUBLIC` master switch + all settings (§2.1) added; `_env_tuple` + `cors_origins` working.
- [ ] `netsec.py`/`security.py`/`ratelimit.py`/`middleware.py` shipped; `validate_public_url` shared by `/process` and `/video` (F4/F5/F6/F12/F27).
- [ ] Admission cap + per-IP cap + `JobRejected`→429 in `submit()` at thread creation (F1); GPU lock moved to inference only + absolute job/download deadlines + download watchdog (F2).
- [ ] Admin token gate (404, fail-closed) on `/cache/clear` and `/cache`, `root` dropped (F3/F19); `require_edge` on mutating + SSE routes (F14).
- [ ] Locked CORS + server-side Origin gate + `allow_private_network` off in public (F11/F29); body/url/stems limits + body middleware (F13); `JobId` pattern on all `{job_id}` routes + cache guard (F18/F22).
- [ ] Disk LRU evictor + free-disk floor + duration/size ceilings + `allowed_extractors` (F4/F7/F8); SSE caps + lifetime (F10); height allowlist + export/transcode gates (F8/F9/F12).
- [ ] Errors sanitized + `server_header=False` + slim `/capabilities` (F20/F21); invariant comment rewritten.
- [ ] `requirements.lock` (hashes) + `--require-hashes` install; weights pre-staged read-only; deno on Linux; yt-dlp weekly cadence documented (F16/F23/F24/F25/F26/F17).
- [ ] `pytest` green; loopback dev mode (switch unset) unchanged.

**Extension (Phase 2/3)**
- [ ] `config.js` single source; all 7 default sites on `https://nomusic.<DOMAIN>`; tests updated.
- [ ] `host_permissions` = `https://nomusic.<DOMAIN>/*` + `optional_host_permissions`; `activeTab` removed.
- [ ] Content scripts + WAR curated to the **owner-confirmed** site list; `config.js` web-accessible.
- [ ] Clear-Cache + cache panel removed; version reconciled/increasing; single-purpose description.
- [ ] `pack` script; built-bundle remote-code grep clean; privacy policy hosted + dashboard privacy fields + in-product consent before first `POST /process`.

**Deployment (Phase 4)**
- [ ] `nvidia-smi` works; `/capabilities` device `cuda` locally and through the tunnel.
- [ ] `nomusic.service` active; `systemd-analyze security` clean; `MemoryDenyWriteExecute=no` and `PrivateDevices=no` confirmed off.
- [ ] `/etc/nomusic/nomusic.env` `chmod 600` root-owned; `NOMUSIC_ADMIN_TOKEN` + `NOMUSIC_TUNNEL_SECRET` real random values; `NOMUSIC_EXTENSION_ORIGIN` set to the published id.
- [ ] `cloudflared.service` active; ~4 healthy connections; tunnel injects `X-Nomusic-Tunnel` (or secret left empty deliberately).
- [ ] Cloudflare WAF `/cache*` block enabled (mandatory interim) + `POST /process` rate-limit + Bot Fight Mode; `POST /process` still works from the extension.
- [ ] ufw default-deny inbound, only `lo` (+ optional pinned SSH); no router port-forward; nftables egress blocks RFC1918 except GW+DNS (or off-box VLAN).
- [ ] Smoke tests 5.11 pass incl. SSE incremental + negative checks; disk monitor + journald cap installed; TTL = 1 day.
- [ ] Kill switch tested (`systemctl stop/start cloudflared`); media-ToS R2 contingency documented.

---

## 7. Open questions for the owner

1. ~~**Domain.**~~ **Resolved (§0): keep `<DOMAIN>` placeholder** — owner substitutes the real domain (and `nomusic.<DOMAIN>`) at implementation time across `config.js`, `manifest.json`, the privacy policy, the env file, the tunnel DNS route, and the WAF rules.
2. ~~**Curated site allowlist.**~~ **Resolved (§0): YouTube + Facebook.** Backend `allowed_url_hosts`/`allowed_extractors` (§2.1) and the extension matches (§3.3) cover both; the store description names exactly those two.
3. **Release version.** `0.2.0` (match package.json) or `1.0.0` for first public release.
4. **Optional "enable everywhere."** Ship the `scripting`-based opt-in now, or defer for the leanest first submission.
5. ~~**Public vs. gated.**~~ **Resolved (§0): full-public, no client auth.** Cloudflare Access service tokens + an extension-default flip remain the documented privatization path if abuse appears — not enabled now.
6. ~~**Cloudflare plan / media ToS.**~~ **Resolved (§0): start on Free, no R2 yet.** Accept the §5.7 media-ToS risk under strict quotas + low volume; R2 artifact-offload stays the pre-built contingency to switch on if Cloudflare sends a notice.
7. ~~**Laptop GPU model + VRAM.**~~ **Resolved (§0): single GTX 1050 Ti (Pascal, sm_61, 4 GB), already validated.** `DeviceAllow` covers `/dev/nvidia0` + the control nodes; install pins `NOMUSIC_PYTHON=3.11 NOMUSIC_CUDA=cu118 NOMUSIC_TORCH=2.4.1` (§5.1); keep `gpu_batch` low (1–2) and tune `MemoryMax` to the box.
8. **Network placement.** Can the laptop go on a dedicated guest SSID / VLAN (strongest LAN isolation), or is on-box nftables the only available control? Confirm `<GATEWAY_IP>`, `<DNS_IP>`, and whether Tailscale is used (affects the CGNAT egress line).
9. **Admin/contact details.** The `<CONTACT_EMAIL>` for the privacy policy + verified CWS contact email; where the 64-char admin token is stored; the `NOMUSIC_CACHE_TTL_DAYS` value to put in the privacy policy (default 1 here).
10. **Tunnel secret header.** Whether to configure `X-Nomusic-Tunnel` injection at the Cloudflare tunnel (enables `require_edge`) or leave `NOMUSIC_TUNNEL_SECRET` empty and rely solely on the loopback bind + no-port-forward invariant.
