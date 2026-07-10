# nomusic — Security Study (Public Deployment)

## 1. Executive Summary & Risk Verdict

You have decided to take a tool that was designed, commented, and code-reviewed around a single hard assumption — *"the only thing that can reach this server is the machine it runs on"* — and put it on the public internet. That assumption is written directly into the code as a load-bearing comment:

> **SECURITY INVARIANT:** `allow_origins='*'` with no auth is only safe because the server binds to `127.0.0.1` ... any origin reaching it is already local. (`backend/server.py:202-206`)

A Cloudflare named tunnel keeps the *bind* on `127.0.0.1:8723` but makes the *audience* the entire internet. The premise of that invariant is now false. Every design decision that was justified by "it's loopback-only" — no auth, `allow_origins='*'`, unbounded worker threads, no rate limit, no size caps, verbose error bodies, an unauthenticated destructive `/cache/clear` — is now an exposed, anonymously-reachable behavior.

**Risk verdict (current code, as-is, behind a tunnel): DO NOT SHIP.** In its current state the backend is trivially destroyable and abusable by a single unauthenticated HTTP client. Three of the findings are unauthenticated total-availability kills (F1, F2, F3), one of which (`POST /cache/clear`) wipes every user's data and can be fired by any web page a victim visits, with no token, in a loop. The box is also an open media-download proxy that launders arbitrary downloads through your home IP (F4). None of this requires sophistication — `curl` and a `for` loop are sufficient.

**Risk verdict after the planned hardening (the `NOMUSIC_PUBLIC=1` control set + Cloudflare edge + Linux host hardening): ACCEPTABLE for a low-traffic hobby deployment, with eyes open.** The in-process admission cap, concurrency gates, hard deadlines, admin-token gate, locked CORS, SSRF allowlist, disk caps, and host sandboxing collectively convert this from "trivially destroyable" to "abuse-resistant within the limits of one GPU and one residential uplink." What hardening *cannot* fix are the structural realities of choosing a public, no-client-auth model: the legal/ToS exposure of fetching third-party media for anonymous strangers, the Cloudflare CDN-media-terms risk, and the residual fact that anyone can spend your GPU and bandwidth up to the rate limits. Those are accepted risks, not solved problems — see §2.

**Bottom line recommendation (expanded in §6):** ship public *only after* the F1/F2/F3/F4 fixes plus the full control checklist in §5 are in place, and keep the "privatization switch" (Cloudflare Access + extension default flip) pre-wired so you can go invite-only the day abuse or a Cloudflare/abuse-desk notice arrives — without re-architecting anything.

---

## 2. Reality Check — What Going Fully Public Actually Means

You have already decided to go public, so this is not an argument against it. It is the list of things you are signing up for, each with its mitigation and its "tighten it later" escape hatch. Read all five before launch.

### (a) No client secret is possible — ever

Your audience is *"anyone who installs the published extension,"* and the extension ships with `backendUrl` defaulting to `https://nomusic.<DOMAIN>` (`extension/settings.js:5`, `background.js:11`, `popup.js:35,240`). Any token, API key, or HMAC secret you bake into that published bundle is readable by anyone who unzips the `.crx`. **There is no such thing as a per-user secret in this model.** All protection must come from (1) server-side admission/rate/quota controls keyed on `CF-Connecting-IP`, (2) the Cloudflare edge, and (3) the host/network layer. The only secret that exists is `NOMUSIC_ADMIN_TOKEN`, which **you** hold and which never ships in the extension.

- **Mitigation:** Treat every request as hostile and anonymous. The hardening plan's identity model is "the real client IP from `CF-Connecting-IP`, trusted only because the loopback+tunnel topology means nothing else can reach the socket" (F14). Rate-limit and cap on that, accept that it's the best you get.
- **Tighten-it-later escape hatch:** Cloudflare Access (Zero Trust) with **service tokens** turns the hostname private behind an edge auth gate. The moment you decide to restrict to known users, you put an Access app over `nomusic.<DOMAIN>`, issue service tokens, and flip the extension's default backend URL to a self-hosted/invite value. This is a config change, not a re-architecture. **Do not enable it now** — it would force a login wall on every extension user, which is incompatible with "anyone can install and it just works."

### (b) Abuse & cost: you are running an open GPU + open download proxy

With no auth, the unauthenticated surface is, by design:
- **A free GPU-separation service.** Any web page can drive `POST /process` cross-origin (F11) — `allow_origins='*'` (`config.py:73`) waves the preflight through and CORS by design does not stop the side effect. A page with traffic becomes a free, anonymous GPU drain on your laptop.
- **An open media-download proxy.** The URL validator only blocks internal IPs; it has **no positive domain allowlist** and yt-dlp's generic extractor stays enabled (F4, `routes/jobs.py:103-125`, `pipeline/downloader.py:85-115`). An attacker can POST `https://attacker.example/movie-50GB.mkv`, then pull it back at up to 4320p via `GET /video/{job_id}?max_height=4320` (F8). **The owner's residential IP — not the attacker's — appears fetching the content.** That is download laundering through your home connection: a real abuse-desk and legal exposure, distinct from the bandwidth/disk cost.
- **Bandwidth, disk, and GPU burned by strangers.** No total cache cap, no max duration, only a 7-day TTL sweep (F7, `cache.py:293-331`, `config.py:80`). A stream of distinct long URLs fills the laptop's disk between hourly sweeps; `/video` amplifies a few request bytes into multi-GB downloads + hours-long 8K re-encodes (F8).

- **Mitigation:** The load-bearing controls are **in-process** — `NOMUSIC_MAX_INFLIGHT_JOBS` admission cap (F1), per-IP job cap, concurrency gates on `/video` and mp3 transcode (F8/F9), a positive host allowlist + `allowed_extractors` excluding `generic` (F4), `max_filesize`/`max_duration` caps (F4/F7), and an LRU disk-evictor + free-space floor (F7). The single free Cloudflare rate-limit rule on `POST /process` is **defense-in-depth, not the primary control** — it can't reliably stop a modest-rate distinct-URL flood, and you only get one rule on the free plan.
- **Tighten-it-later escape hatch:** Lower the env caps (`NOMUSIC_MAX_INFLIGHT_JOBS`, `NOMUSIC_RATE_*`) toward 1, narrow `NOMUSIC_ALLOWED_URL_HOSTS` to YouTube only, or flip to Access (a).

### (c) Cloudflare CDN media-terms risk

The entire purpose of this service is to return opus/mp3 audio and muxed mp4 video through a Cloudflare-proxied (orange-cloud) tunnel. Cloudflare's Service-Specific Terms (the old "Section 2.8," now in the CDN section) state that non-Enterprise customers **must** use paid products (Stream/R2/Images) to serve video or a *"disproportionate percentage"* of audio/large files via the CDN, and reserve the right to throttle or disable access otherwise. **Pro/Business plans do NOT exempt you** — only Enterprise or moving media to R2/Stream does.

- **Mitigation:** Keep media volume low (strict quotas from (b) directly bound egress and keep you out of "disproportionate" territory). The compliant pattern Cloudflare blesses is serving finished `audio`/`video` artifacts from **R2** (S3-compatible, free tier, free egress) and handing clients a direct R2 URL, keeping only the tiny JSON/SSE control traffic on the tunnel.
- **Tighten-it-later escape hatch:** Have the **R2-offload path pre-designed** so that the day Cloudflare sends a "disproportionate non-HTML" notice (they commit to "reasonable efforts to provide notice" before action), you switch artifact delivery to R2 in an afternoon. This is the documented contingency; don't build it now, but know exactly how you'd build it.

### (d) Legal / ToS exposure of a public third-party-media service

This is the single most material *non-technical* risk and the one hardening cannot touch. A personal tool that strips music from videos *you* watch is one thing. A **public service that downloads YouTube (and other) media on behalf of anonymous users** is materially higher liability:
- It plausibly violates the source platforms' Terms of Service (YouTube's ToS prohibits downloading/circumvention) at scale and on behalf of third parties.
- It creates copyright/DMCA exposure for *you* as the operator, since the fetch and the transformation happen on your hardware and egress from your IP.
- It shifts you from "end user" to "service operator," with the duty-of-care and notice-and-takedown expectations that come with that.

- **Mitigation:** Narrow `NOMUSIC_ALLOWED_URL_HOSTS` to the minimum set you actually intend to support; keep volume low; do not advertise the service as a general downloader; be ready to respond to takedown/abuse notices. Understand that no amount of code hardening changes the operator-liability posture.
- **Tighten-it-later escape hatch:** This is the clearest case for going **invite-only via Cloudflare Access (a)** or for keeping the service entirely personal. If the legal posture worries you, the lowest-risk configuration is "private, just me / a handful of trusted users," which the extension's `backendUrl` setting already supports — the public default is the only thing creating the exposure.

### (e) Home-network blast radius if the box is popped

The backend runs a large attacker-influenced surface in-process: yt-dlp executes live third-party YouTube player JS in a runtime that inherits the server environment (F23), ffmpeg parses attacker-chosen media containers (F17), torch/demucs unpickle network-fetched weights (F24), and the whole dependency tree floats on `>=` with no hash-pinning (F16). If any of these is compromised, the attacker is executing code on a laptop **on your home LAN**, holding `NOMUSIC_ADMIN_TOKEN`, one hop from your router admin UI, NAS, and IoT devices.

- **Mitigation:** The Linux host-hardening guide is the answer: a dedicated unprivileged service user with a read-only code tree; a heavily-sandboxed systemd unit (`ProtectSystem=strict`, `ProtectKernel*`, `RestrictNamespaces`, `SystemCallFilter=@system-service`, `DevicePolicy=closed`+`DeviceAllow` for the NVIDIA nodes); and — most importantly — **off-box network segmentation** (a guest VLAN/SSID with client isolation) plus an on-box `nftables` egress filter that drops RFC1918/loopback/link-local so a popped box cannot pivot into the LAN. Keep the admin token **out of the subprocess environment** that yt-dlp hands to the JS runtime (read it from a file/secret).
- **Tighten-it-later escape hatch:** The strongest control (segmentation) is off-box, so a root-level compromise can't undo it. If you can't segment today, the `nftables` egress block + systemd sandbox are the interim; treat segmentation as the thing you add before traffic grows.

> **Two directives that MUST stay OFF on the backend unit** because they break torch/CUDA (verified): `MemoryDenyWriteExecute` (torch oneDNN/CUDA JIT needs W+X mappings, [pytorch#143651](https://github.com/pytorch/pytorch/issues/143651)) and `PrivateDevices=yes` (removes the GPU device nodes). Use `DevicePolicy=closed` + explicit `DeviceAllow` for the NVIDIA nodes instead. cloudflared (plain Go, no JIT, no GPU) can and should run with both ON.

---

## 3. Threat Model

### Trust boundaries & topology

```
  Internet (anonymous, no client auth possible)
        │  HTTPS
        ▼
  Cloudflare edge  ──(WAF / rate-limit rule / Bot Fight / DDoS)──┐
        │  QUIC/UDP 7844 (outbound from laptop only)             │ sets CF-Connecting-IP
        ▼                                                        │
  cloudflared  (separate unprivileged user, hardened unit)       │
        │  http://127.0.0.1:8723   (loopback ONLY)               │
        ▼                                                        │
  FastAPI/uvicorn  (nomusic user, sandboxed systemd unit)  ◄─────┘  the public attack surface = the HTTP API
        │  spawns: yt-dlp (+JS runtime), ffmpeg, torch/demucs (GPU)
        ▼
  Home LAN  ◄── blast-radius boundary (segmentation + egress filter)
```

**Key structural fact:** the app stays bound to `127.0.0.1:8723` (`config.py:38`, `server.py:294-300`). The only process that can reach the socket is the local `cloudflared`, and `cloudflared` makes **outbound-only** connections — **no inbound router port is opened, no port-forward, no DMZ.** That is the genuine security win of the named-tunnel choice and it is preserved by every part of this plan. It also means the origin's socket peer is always `127.0.0.1`, so the **only** trustworthy client identity is the `CF-Connecting-IP` header — trustworthy *precisely because* nothing but cloudflared can reach loopback (F14). Never trust `X-Forwarded-For`; never enable uvicorn `--proxy-headers`/`forwarded-allow-ips`.

### Assets

| Asset | Why it matters |
|---|---|
| GPU compute (one NVIDIA GPU, serialized by a global inference lock) | The scarce resource; the whole DoS surface targets it |
| Home uplink bandwidth (residential) | Burned by download-proxy abuse; also the laundering vector |
| Local disk (`~/.cache/nomusic`, demucs weights) | Filled by unbounded jobs/`/video` pulls |
| `NOMUSIC_ADMIN_TOKEN` | The only real secret; gates destructive ops; sits in the box's env |
| The shared on-disk cache (all users' processed jobs) | Wiped by `/cache/clear`; readable via derivable job_ids |
| The home LAN (router, NAS, IoT) | Blast radius if the box is popped |
| The owner's IP reputation / legal standing | Download laundering, ToS/copyright exposure |

### Attackers

1. **Anonymous internet script-kiddie** — `curl`/`for`-loop. Already sufficient for F1, F2, F3, F4, F7, F8, F13, F18.
2. **Malicious web page** — drives the victim's browser cross-origin into `POST /process` / `/cache/clear` (F3, F11) with no preflight-bypass tricks needed; an ordinary `application/json` fetch suffices because `allow_origins='*'`.
3. **Abuse operator** — uses the box as an anonymizing downloader / third-party DoS reflector (F4, F11), rotating IPs to defeat naive per-IP limits (F14).
4. **Supply-chain / upstream compromise** — backdoored dep, hijacked yt-dlp/torch/demucs release, MITM'd player JS (F16, F23, F24, F25, F26). Low probability, high impact (RCE on a LAN-resident box holding the admin token).

### Out of scope / structurally mitigated

- **Direct inbound to the laptop** — impossible without a deliberate router port-forward (named tunnel is outbound-only). Don't create one.
- **`CF-Connecting-IP` spoofing by an internet client** — overwritten at Cloudflare's edge; only spoofable by a process *already on the laptop/LAN* reaching loopback, which the host hardening addresses.
- **Cloud metadata SSRF (169.254.169.254)** — largely moot; this is a home laptop, not a cloud VM with an IMDS (F6 note). The live SSRF risk is internal-LAN read, not metadata theft.

---

## 4. Findings

### 4.1 Summary table (severity-sorted)

| ID | Sev | Title | Primary location | Dimension |
|---|---|---|---|---|
| F1 | **CRITICAL** | `POST /process` has no admission control — unbounded worker threads + in-memory state | `jobs.py:253-261`, `routes/jobs.py:144-165` | DoS / resource |
| F2 | **CRITICAL** | One slow/stalling source URL pins the global GPU lock for the whole service | `jobs.py:419`, `downloader.py:343-345`, `jobs.py:638-663` | DoS / resource |
| F3 | **CRITICAL** | Destructive `POST /cache/clear` is unauthenticated — drive-by wipe of all data; loopback invariant broken | `routes/system.py:54-66`, `server.py:202-206` | auth / CORS |
| F4 | HIGH | No domain allowlist — backend is an open media-download proxy (laundering + bandwidth/disk abuse) | `routes/jobs.py:103-125`, `downloader.py:85-115` | SSRF / yt-dlp |
| F5 | HIGH | DNS-rebinding TOCTOU — validated IP never pinned; yt-dlp re-resolves at fetch time | `routes/jobs.py:46-80,122-124`; fetch sites in `downloader.py` | SSRF / yt-dlp |
| F6 | HIGH | HTTP redirect-following bypasses the front-door SSRF host check | `downloader.py:85-115` (no redirect/host policy) | SSRF / yt-dlp |
| F7 | HIGH | Unbounded disk fill — no size cap, no max-duration, only a 7-day TTL sweep | `cache.py:293-331`, `config.py:80-82`, `downloader.py:162-167` | DoS / resource |
| F8 | HIGH | `GET /video` amplification — distinct `max_height` ⇒ unbounded full downloads + 8K re-encodes | `routes/media.py:276-385,294-297,347` | DoS / resource |
| F9 | HIGH | Sync endpoints share the ~40-thread anyio pool — slow `/video` & mp3 starve the whole API | `routes/media.py:197,277`, `server.py:197-248` | DoS / resource |
| F10 | HIGH | Unbounded concurrent SSE subscriptions per job — fd exhaustion, fan-out, job-pinning | `routes/jobs.py:191-266`, `jobs.py:183-192,709-724` | DoS / resource |
| F11 | HIGH | `allow_origins='*'` + no Origin enforcement — any site drives `POST /process` | `server.py:214-221`, `config.py:73`, `routes/jobs.py:144-165` | auth / CORS |
| F12 | MED | `GET /video` re-invokes yt-dlp on stored URL with no re-validation, no concurrency cap | `routes/media.py:276-334` | SSRF / yt-dlp |
| F13 | MED | No request-body / URL / `keep_stems` size limit on `POST /process` | `routes/jobs.py:98-101`, `server.py:214-221` | DoS / resource |
| F14 | MED | Rate-limit IP trust — must key on `CF-Connecting-IP`, never `X-Forwarded-For` | `server.py:294-300`, `config.py` (no trusted-proxy) | auth / CORS |
| F15 | MED | Derivable `job_id` ⇒ cross-user reads + watch-history oracle on shared cache | `cache.py:68-93`; consumers in `routes/*` | auth / tenancy |
| F16 | MED | No lockfile / hash-pinning — every backend dep floats on `>=` | `requirements.txt:1-28`, `install.sh:179-190` | deps / supply-chain |
| F17 | MED | ffmpeg & JS runtime are unversioned system binaries; ffmpeg parses attacker media | `install.sh:45,69`, `downloader.py:485-515`, `mlx_engine.py:107-109` | deps / supply-chain |
| F18 | LOW | `job_id` path param unvalidated — single-level path traversal (router holds the line) | `cache.py:107-112`; reached from `routes/media.py`, `routes/jobs.py` | path / file-serving |
| F19 | LOW | `GET /cache` leaks global stats + absolute cache path (username / FS layout) | `routes/system.py:48-51`, `cache.py:238-264` | auth / tenancy |
| F20 | LOW | Internal tracebacks, ffmpeg stderr, yt-dlp errors leak to anonymous clients | `jobs.py:473-480`, `routes/jobs.py:160-165`, `routes/media.py:67-70,127-131,330-334` | auth / tenancy |
| F21 | LOW | Server/version fingerprinting — uvicorn `Server` header & `/capabilities` version/device | `server.py:294-300`, `routes/system.py:23-45` | auth / tenancy |
| F22 | LOW | `job_id` unvalidated ⇒ constrained traversal into cache + ffmpeg inputs | `routes/media.py:171-178,...`; `cache.py:107-112,...` | subprocess / injection |
| F23 | LOW | yt-dlp runs live third-party player JS in a runtime inheriting the server env; Linux uses weaker node sandbox | `downloader.py:95-115`, `install.sh:45 vs 69` | deps / supply-chain |
| F24 | LOW | demucs downloads weights over network, loads via `torch.load` (pickle / arbitrary code) | `mlx_engine.py:289-292` → demucs internals | deps / supply-chain |
| F25 | LOW | yt-dlp version floor ~17 months stale; no enforced update cadence | `requirements.txt:8` | deps / supply-chain |
| F26 | LOW | Runtime fetch+exec of EJS solver JS from GitHub on the request path | `downloader.py:101`; yt-dlp `ejs.py:291-305` | deps / supply-chain |
| F27 | INFO | `/video` re-feeds on-disk `meta.url` without re-validation (latent LFR/SSRF chain) | `routes/media.py:324-329`, `downloader.py:308-309` | path / file-serving |
| F28 | INFO | Clarification — `/audio?format=opus` streams in 64 KiB blocks (no OOM); mp3 is the cost | `routes/media.py:254-273,228-252` | DoS / resource |
| F29 | INFO | `allow_private_network=True` is pointless & abuse-broadening on a public HTTPS origin | `server.py:208-220` | auth / CORS |
| F30 | INFO | ffmpeg/yt-dlp argv lacks `--` option terminator before path positionals (latent) | `downloader.py:499-500,514`; `export.py`, `processor.py` | subprocess / injection |
| F31 | INFO | Verification — subprocess/command-injection surface is otherwise clean (negative result) | pipeline + routes | subprocess / injection |

No claims were refuted during verification; all 31 stand as scoped above.

---

### 4.2 Critical findings (must fix before any public exposure)

#### F1 — `POST /process` has no admission control (unbounded threads)

**Impact.** `JobRegistry.submit()` spawns one daemon `threading.Thread` per distinct cache key with **no cap** on concurrent jobs, queue depth, or per-IP/per-client count (`jobs.py:210-261`, thread spawn at `:253-261`). The cache key is `sha256(url, model, stems, chunk params)` (`cache.py:68-80`), so every distinct query string (`youtube.com/watch?v=VALID&n=1,2,3,…`) is a distinct key, a distinct SSRF-passing public URL, and a distinct thread. The single global `_gpu_lock` (`jobs.py:419`) serializes only the inference *run*; queued threads block on `_gpu_lock.acquire()` **before** ever reaching the idle-abandon check (`_raise_if_abandoned`, `jobs.py:638`), so they pile up alive. `memory_gc` won't reap a key while its thread exists, and won't drop disk state for up to 7 days. `/process` returns immediately with no probe (`routes/jobs.py:161-165`), so each request is cheap for the attacker. An unauthenticated POST flood of distinct public URLs creates unbounded daemon threads + `_jobs`/`_threads`/`_subscribers` state until the host OOMs or thread creation fails — **total, trivial, unauthenticated availability kill.**

**Fix (per backend design §4/§5).** Enforce the cap **at thread-creation time inside `submit()` under `_lock`**, counting active+queued keys (queued threads never reach the idle check). Add `NOMUSIC_MAX_INFLIGHT_JOBS` (default 3) and `NOMUSIC_MAX_JOBS_PER_IP` (default 2, keyed on `CF-Connecting-IP`); reject overflow with `JobRejected` → HTTP 429 + `Retry-After` instead of spawning. Release admission in `_run`'s `finally` and in `abandon_all()`. Add a per-IP rate window (`NOMUSIC_RATE_PROCESS_PER_MIN`, default 6) and the Cloudflare rate-limit rule on `POST /process` as edge defense-in-depth. The **in-process cap is the load-bearing control**; the edge rule alone will not stop a modest-rate distinct-URL flood.

#### F2 — One slow source URL pins the global GPU lock for the whole service

**Impact.** The worker holds `_gpu_lock` across the *entire* pipeline — probe (yt-dlp `extract_info`), source download, **and** inference (`jobs.py:419` wraps `processor.run`). yt-dlp's download sets only `socket_timeout=30`/`retries=3` (`downloader.py:343-345`) — **no wall-clock cap**, so a server trickling bytes slower than every 30 s resets the per-read timeout forever. Idle-abandon doesn't save you: `_raise_if_abandoned` returns early whenever a subscriber is connected (`jobs.py:650-651`), and the extract/full-download paths wire **no** `abort_check` at all (`processor.py:528,533`), so an extract-stall hangs under the lock even with no subscriber. A public direct-media URL (passes the SSRF allowlist, generic extractor accepts it) dripping ~1 byte/25 s, with an `/events` stream held open, pins `_gpu_lock` indefinitely — and because the lock also gates probe+download, **every other user's job stalls behind it.** One request from one client takes the whole single-GPU service down.

**Fix (backend design §6).** Move serialization off the whole pipeline onto inference only: add `self._infer_lock` in `mlx_engine.py` around the `apply_model` call (`~:152-156`) and **remove** `with self._gpu_lock:` from `jobs.py:_run` (`:414-447`). Add an **absolute per-job deadline** in `_raise_if_abandoned` placed *before* the subscriber early-return (`NOMUSIC_JOB_DEADLINE_SECONDS`, default 1800) so a held `/events` stream can't keep a job alive past the cap. Add a download watchdog (`NOMUSIC_DOWNLOAD_DEADLINE_SECONDS`, default 900) that cancels via `_ProgressiveSource.cancel()`, wire `abort_check` into the extract/full-download paths, add a min-throughput floor, and set yt-dlp `max_filesize` so a trickle is also size-bounded.

#### F3 — Destructive `POST /cache/clear` is unauthenticated

**Impact.** `cache_clear()` takes only `request`, runs `registry.abandon_all()` then `cache.clear_all()` (`routes/system.py:54-66`), which `shutil.rmtree`s every child of the cache root — **all jobs, `sources/`, `videos/`** (`cache.py:266-291`) — and tears down every in-flight worker. **Zero auth.** The SECURITY INVARIANT at `server.py:202-206` justified no-auth on the loopback bind; the tunnel breaks that premise, so this destructive endpoint is anonymously reachable by the whole internet. Two attack shapes: (1) any web page a victim visits runs `fetch('https://nomusic.<DOMAIN>/cache/clear',{method:'POST'})` — a preflight-free **simple request** (no body, no custom headers), so the browser fires it cross-origin with no preflight; the attacker never needs to read the (CORS-blocked) response because the side effect — wiping the cache and killing every job — executes server-side regardless. Loop it for persistent DoS forcing every video re-separated from scratch on the GPU. (2) `curl -X POST https://nomusic.<DOMAIN>/cache/clear` from anywhere, no token.

**Fix (backend design §12).** Remove the Clear-Cache control from the published extension (`extension/popup.html:153`, `popup.js:194-227`). Gate `cache_clear` server-side behind `NOMUSIC_ADMIN_TOKEN`: a reusable `require_admin` Depends that compares an `X-Admin-Token` header with `hmac.compare_digest`, **fails closed** when the token is unset, and returns **404** (not 401/403) on missing/bad token so the endpoint is indistinguishable from absent. Apply the dependency **at router level** (split an admin router) so future settings-mutation routes inherit it. Routine cleanup relies on the existing TTL sweep + the new LRU evictor — no operational loss (`clear_all` preserves the root dir).

---

### 4.3 High findings

#### F4 — No domain allowlist: open media-download proxy

**Impact.** `_validate_url` only rejects internal-resolving hosts; there is **no positive host allowlist** and no `allowed_extractors`, so yt-dlp's generic + all bundled extractors stay live (`routes/jobs.py:103-125`, `downloader.py:85-115`). Any public http(s) URL is accepted: `POST /process {url:"https://attacker.example/movie-50GB.mkv"}` then `GET /video/{id}?max_height=4320`. Combined with no auth, no rate limit, no concurrent-job cap, and no size/duration ceiling, the box is an **unauthenticated download proxy** that saturates the home uplink, fills disk inside the TTL window, and **launders pirated/abusive downloads through the owner's residential IP** — a real legal/abuse exposure.

**Fix (backend design §2, §11).** Add a positive hostname allowlist in `validate_public_url` (`NOMUSIC_ALLOWED_URL_HOSTS`, default YouTube/Vimeo family); set yt-dlp `allowed_extractors` (`NOMUSIC_ALLOWED_EXTRACTORS`) excluding `generic`; add a max-duration check at probe and `max_filesize` (F7); per-IP rate limit + concurrent-job + disk caps; Cloudflare WAF rate rules as edge backstop.

#### F5 — DNS-rebinding TOCTOU (validated IP never pinned)

**Impact.** `_validate_url` resolves the host and rejects internal IPs but **discards the resolved address** — only the original hostname string is stored (`routes/jobs.py:122-125`) and later handed to yt-dlp, which re-resolves DNS itself at each fetch (`downloader.py:154,222,386,309`), seconds-to-hours later. A low-TTL A record answers public at validation time, then flips to `127.0.0.1` / `192.168.1.1` (router admin UI) / `169.254.169.254`; yt-dlp fetches the internal target and the bytes come back via `/audio` and `/video` — **full SSRF read of loopback/LAN, exfiltrated as "audio."** Re-resolution at `/video` (F12) widens the window. Blind SSRF into loopback/LAN is unconditional; body exfil depends on yt-dlp's generic extractor parsing the internal response as media.

**Fix.** Pin the validated IP and force yt-dlp to connect to exactly that address, OR run yt-dlp egress inside a network namespace / forward proxy dropping RFC1918/loopback/link-local/ULA. A re-resolve in the validator cannot close the window. The durable control is the **OS-layer egress filter** (host-hardening §6b `nftables`) — flag it in the runbook as the load-bearing F5/F6 fix. Pin per-fetch (probe, source, video) since each re-resolves independently.

#### F6 — HTTP redirect-following bypasses the front-door SSRF check

**Impact.** `_validate_url` inspects only the *submitted* URL's host. yt-dlp follows 3xx redirects by default and `_common_opts` sets no redirect limit or per-hop host validation; the generic extractor also fetches secondary URLs (M3U8/MPD segments, `og:video`) that never pass the validator. So `https://attacker.example/x` (public, passes hop 1) returns `302 → http://192.168.1.1/` (or serves an HLS manifest with internal segment URLs); yt-dlp follows it and the body returns via `/audio`/`/video`. **No DNS control required — a plain attacker web server defeats the IP block-list.** On this home laptop the live risk is internal-LAN read (router/NAS/IoT), not cloud IMDS.

**Fix.** yt-dlp exposes no per-redirect host hook, so the only complete control is **OS/firewall egress filtering on the laptop** blocking RFC1918/loopback/link-local (host-hardening §6b), backed by the `allowed_extractors`+domain allowlist (F4). A redirect cap alone does not close it.

#### F7 — Unbounded disk fill (only a 7-day TTL sweep)

**Impact.** The only reclamation is the hourly age-based TTL sweep (`cache.py:293-331`, `config.py:80`). No total-size ceiling, no per-job byte cap, no max video duration — `probe()` rejects only `duration is None`, happily accepting a 10-hour VOD (`downloader.py:162-167`). Each job writes `sources/<hash>/*` + chunks; each `/video` writes multi-GB `videos/<hash>/*`. An attacker submitting distinct long URLs fills the disk in minutes-to-hours; the once-hourly sweep can't keep up because nothing is older than 7 days. Subsequent writes fail, jobs error, and other host services degrade.

**Fix (backend design §7, §11).** `NOMUSIC_CACHE_MAX_BYTES` + an LRU `evict_to_fit()` called from the sweeper; reject probed `duration > NOMUSIC_MAX_DURATION_SECONDS` before download (cheapest cut); yt-dlp `max_filesize` for source and `/video`; refuse new jobs below `NOMUSIC_FREE_DISK_FLOOR_BYTES`. Back with the host-level XFS project quota / dedicated partition (host-hardening §10) so a full cache disk can't take down the OS/journald/ssh.

#### F8 — `GET /video` amplification (per-`max_height` cache miss + 8K re-encode)

**Impact.** `/video` pulls the full original stream (no GPU lock, no concurrency cap) and re-encodes VP9/AV1→H.264 on CPU when not copyable (`media.py:347`), up to the 3600 s ffmpeg ceiling. The video cache is keyed by the **requested** `max_height` tag, not the actual resolution (`cache.py:139`), so `max_height=200,201,…,4320` each create a distinct dir and a fresh full download+encode even for the same source stream (`media.py:294-297` exposes ~4177 values). One completed audio job amplifies into unlimited expensive video work — saturating CPU, bandwidth, and disk, and starving the sync threadpool (F9).

**Fix (backend design §8).** Replace the clamp with a small server-side allowlist (`NOMUSIC_ALLOWED_VIDEO_HEIGHTS`, default `{360,480,720,1080}`), snapping to nearest — collapses ~4000 keys to ≤4. Key the video cache on actual downloaded resolution. Gate `/video` behind a global semaphore (`NOMUSIC_MAX_VIDEO_EXPORTS`, default 1) + per-IP rate limit. Refuse CPU re-encode above a resolution ceiling.

#### F9 — Sync endpoints share the ~40-thread anyio pool

**Impact.** Every endpoint except `/events` is a sync `def`, so Starlette runs each in the default anyio threadpool (capacity 40). `/video` blocks a thread for the entire download+encode (up to 3600 s); `/audio?format=mp3` blocks one for a full-track ffmpeg transcode (`media.py:228-252`). No semaphore separates these from fast routes. **~40 concurrent slow requests occupy every thread**, after which all sync routes — `/healthz`, `/status`, `/chunk`, `/process` — stop responding (only async `/events` survives). A single completed `job_id` can be hammered with unlimited concurrent `/video` requests (the mux re-runs each time). ~40 requests, not thousands, hangs the whole API.

**Fix (backend design §8).** Bound heavy ops with **dedicated semaphores** (`video_export_gate`, `audio_transcode_gate`) separate from the request pool — raising the anyio limiter alone just moves the saturation point and is **not** a fix. Follow-up: offload `/video`+mp3 to a bounded background worker and poll the existing `/video/{id}/progress`.

#### F10 — Unbounded concurrent SSE subscriptions per job

**Impact.** `/events` creates an unbounded `asyncio.Queue` subscriber per connection for any live job with **no cap** per job/IP/global (`routes/jobs.py:221`, `jobs.py:188`). Each stream holds an fd + coroutine and can be held slowloris-style. Every status `_update` does a `call_soon_threadsafe` per subscriber queue (`jobs.py:709-723`), so N subscribers multiply per-update work. A connected subscriber resets the idle-abandon clock (`jobs.py:650-651`), so holding one blocks early GPU-release of an in-flight job. Thousands of held `/events/{job_id}` connections exhaust fds/connection capacity and amplify every update.

**Fix (backend design §9).** Per-job / per-IP / global SSE caps returning 429 (`NOMUSIC_MAX_SSE_*`); a max stream lifetime (`NOMUSIC_SSE_MAX_LIFETIME_SECONDS`, default 1800) so held streams self-close; the absolute job deadline (F2) decouples idle-abandon from subscriber presence. (Note: a held subscriber blocks *early* abandonment of an in-flight job but does not pin a completed job forever — the worker still exits at ready/error.)

#### F11 — `allow_origins='*'` + no server-side Origin enforcement

**Impact.** CORS reflects any Origin (`server.py:214-221`, `config.py:73`). CORS only governs whether the browser exposes the *response* to attacker JS — it does **nothing** to stop the request or its side effects. A malicious page's ordinary `application/json` `fetch` to `POST /process` is approved by the `'*'` config (verified via TestClient against the installed FastAPI 0.136.1 / Starlette 1.0.0 / Pydantic 2.13.4: preflight returns `ACAO:*` and the POST executes the handler) — every visitor's browser silently enqueues a separation job on the owner's GPU and uses the owner's home IP as a yt-dlp fetcher. `/process/{id}/prioritize` and unauth `GET /video` are the same primitive.

> One correction to the original finding text: the "`Content-Type: text/plain` simple-request, no preflight" mechanism does **not** work here — text/plain JSON bodies 422 on this FastAPI version. The working attack is the normal preflighted `application/json` request, which `'*'` waves through. Outcome identical.

**Fix (backend design §10).** Set `allow_origins` to the exact published `chrome-extension://<ID>` origin + curated site list (never `'*'`) in public mode. Because CORS can't block side effects, **also** add a server-side `enforce_origin` Depends on `POST /process` and `/prioritize` that 403s a present-but-disallowed Origin before work begins (allow missing Origin for non-browser clients, which admission + rate-limit already bound). Keep `allow_credentials=False`. Pair with per-IP rate limit + concurrent-job cap.

---

### 4.4 Medium findings

- **F12 — `/video` re-feeds `meta.url` with no re-validation, no concurrency cap** (`routes/media.py:276-334`). The SSRF validator is a Pydantic field-validator on `ProcessRequest.url`, run only at `/process`; `/video` reads `meta.url` from disk and passes it straight to `download_video` with no re-check, unbounded parallelism, and per-`(url,height)` caching forcing fresh full pulls. **Fix:** re-run `validate_public_url(meta.url)` at the top of `video()`; bound with the `video_export_gate` semaphore + per-IP cap (backend design §8). The `max_height` clamp already exists (`:294-297`); the real lever is the unbounded count of distinct cached downloads.

- **F13 — No body/URL/`keep_stems` size limit** (`routes/jobs.py:98-101`, `server.py:214-221`). `url` has `min_length=1` but no max; `keep_stems` has no `max_items`; Starlette buffers the whole body before Pydantic parses. A multi-hundred-MB body or a million-element `keep_stems` balloons memory per request. **Fix:** `max_length=2048` on `url`, `max_items` + dedup on `keep_stems`, a `MaxBodySizeMiddleware` rejecting `Content-Length` over a few KB on POST routes (backend design §10). Cloudflare's 100 MB body cap bounds a single body but not the rate.

- **F14 — Rate-limit IP trust (partial / forward-looking)** (`server.py:294-300`). There is no rate limiting today; when added, key strictly on `CF-Connecting-IP` and **never** trust `X-Forwarded-For`/`Forwarded` or enable uvicorn `--proxy-headers`/`forwarded-allow-ips=*` (Cloudflare *appends* to XFF; a client can pre-seed it). Reject requests lacking `CF-Connecting-IP` as not-from-edge. Optionally inject a tunnel shared-secret header (`require_edge`, backend design §3) so a LAN-local process can't reach the loopback API unauthenticated. Keep the secret **out of the subprocess env** (F23).

- **F15 — Derivable `job_id` ⇒ cross-user reads + watch-history oracle** (`cache.py:68-93`). `job_id` is a deterministic hash of public/guessable inputs, not a capability. On the shared public cache, anyone can compute another user's `job_id` from a candidate URL + default settings and probe `GET /status/{job_id}` for presence/title/completion — a watch-history oracle — and trigger expensive `/video`. The chunk/audio bytes are public YouTube content (not a confidentiality breach). **Fix:** accept as residual (per-user secrecy is impossible without client auth); bound it with per-IP rate limits on `/status`/`/chunk`/`/audio`/`/video`. **Do not** "fix" by lengthening the hash — derivability, not collision, is the issue.

- **F16 — No lockfile / hash-pinning** (`requirements.txt:1-28`, `install.sh:179-190`). All 12 deps float on `>=`; no lockfile, no `--require-hashes`. A re-run of `install.sh` resolves the newest PyPI release + full torch/demucs transitive closure; a hijacked/typosquatted release is full in-process RCE on a box holding the admin token. (Severity is medium, not high — the tunnel doesn't amplify it; the compromise is realized at install time from the owner's own machine.) **Fix:** `pip-compile --generate-hashes` for PyPI-resolved deps + exact `==` for torch/torchaudio (their wheels come from the PyTorch CPU/cuXXX index and can't share one hash-lock); `install.sh` → `pip install --require-hashes -r requirements.lock`; a written review policy for the yt-dlp/torch/demucs trio (backend design §15).

- **F17 — Unversioned ffmpeg / JS runtime, no update story** (`install.sh:45,69`; `downloader.py:485-515`; `mlx_engine.py:107-109`). ffmpeg parses attacker-chosen media on every job (direct slice + demucs `AudioFile` decode); a stale-distro ffmpeg demuxer CVE is reachable with attacker-influenced input. **Fix:** enable `unattended-upgrades` (or pin a known-good static ffmpeg) on the box; record binary versions in the runbook; sandbox ffmpeg/yt-dlp under the systemd unit (host-hardening §12).

---

### 4.5 Low findings (defense-in-depth; fix before/soon after launch)

- **F18 / F22 — `job_id` path param unvalidated (constrained traversal).** Raw `{job_id}` flows into `cache._key_dir` → `self.root / key` with no format check (`cache.py:107-112`); the cache layer is fully traversable in isolation. The **only** thing holding the line is Starlette's `[^/]+` single-segment convertor — verified: `GET /status/..` (or `%2e%2e`) reaches the handler as `job_id='..'`, reading a fixed-named file (`meta.json`/`chunk_NNN.opus`) exactly one dir above the cache root + a 200/404 existence oracle; `%2f`/multi-level payloads 404. Near-zero practical disclosure, but real missing validation on a now-public endpoint. **Fix:** annotate every `{job_id}` route with `Path(..., pattern=r'^[0-9a-f]{16}$')` (`JobId` type, backend design §3) and add a belt-and-suspenders reject in `cache._key_dir`.

- **F19 — `GET /cache` leaks absolute cache path + global stats** (`routes/system.py:48-51`). Returns `str(cache.root)` (default `/home/<username>/.cache/nomusic`, leaking the OS username + home layout) plus byte/count totals to anonymous clients. **Fix:** gate behind `require_admin` like `/cache/clear`; at minimum stop serializing `str(cache.root)`; remove the cache-stats panel from the published extension (`popup.js:129-157`).

- **F20 — Internal tracebacks / ffmpeg stderr / yt-dlp errors leak.** `status.error` stores a 3-frame `traceback.format_exc` returned verbatim by `/status` + SSE (`jobs.py:473-480`); `/process` returns `str(exc)` in a 400 (`routes/jobs.py:164`); media routes return raw ffmpeg stderr and yt-dlp text in 500/502 detail (`routes/media.py:67-70,127-131,330-334`). Discloses `/home/<user>/...` paths, dep versions, and module structure. Full traces are already logged server-side, so sanitizing loses nothing. **Fix:** store/return generic messages; gate verbose output behind `NOMUSIC_DEBUG`; add a global exception handler (backend design §13).

- **F21 — Server/version fingerprinting** (`server.py:294-300`, `routes/system.py:23-45`). Default `Server: uvicorn` header + `/capabilities` exposing version `0.2.0` and resolved device (cuda/mps/cpu), flagging a GPU-backed target. **Fix:** `server_header=False, date_header=False` on `uvicorn.run`; drop `server_version`/`device` from public `/capabilities` (the extension consumes only models/stems/defaults).

- **F23 — yt-dlp runs live player JS in a runtime inheriting the server env; Linux uses node.** `remote_components=["ejs:github"]` + auto-select deno>node>bun (`downloader.py:95-115`); `install.sh` installs deno on macOS but `nodejs` on Linux. node's `--permission` blocks fs/child_process but **not** `process.env` and **not** network, and Popen sets no `env=`, so a JS-level compromise (MITM/0-day/sandbox escape — all external preconditions) could read `process.env.NOMUSIC_ADMIN_TOKEN` and exfil it. Not directly exploitable today (hash-verified solver), but a real blast-radius item. **Fix:** keep the **admin token out of the subprocess env** (read from file/secret); prefer deno on Linux + jitless extractor-arg; never set `youtube-ejs` dev args (backend design §15).

- **F24 — demucs weights via `torch.load` (pickle).** First run fetches weights from `dl.fbaipublicfiles.com` and unpickles with `weights_only=False` (`mlx_engine.py:289-292` → demucs internals); plain MITM is blocked by `check_hash=True`, so the realistic path is a backdoored (unpinned) demucs wheel. **Fix:** dependency pinning (F16) subsumes most of it; pre-stage `htdemucs` weights into a read-only torch hub cache at deploy time so the public path does no live fetch/unpickle.

- **F25 — yt-dlp floor ~17 months stale, no cadence** (`requirements.txt:8`). The installed version is current (2026.03.17), so this is forward-looking hygiene, not a live bug. **Fix:** exact pin/hash in the lockfile + a weekly reviewed bump with an extraction smoke test. yt-dlp is the one dep where this cadence genuinely matters (it parses untrusted remote content on the server).

- **F26 — Runtime fetch+exec of EJS solver from GitHub** (`downloader.py:101`). On a cold cache, yt-dlp fetches `yt.solver.lib.min.js` from GitHub releases and executes it; integrity is enforced by the wheel-baked sha3_512 hash table, so code-injection is neutralized — residual is a soft github.com availability dependency on first use. **Fix:** pre-warm the yt-dlp cache (or install `yt-dlp-ejs` pinned/hashed) at provisioning; keep yt-dlp pinned; never set the dev args.

---

### 4.6 Informational (latent / negative results — no action required for launch)

- **F27** — `/video` re-feeds on-disk `meta.url` to yt-dlp without re-validating scheme/SSRF. Not reachable today (the only `meta.json` writer stores submit-validated URLs; the traversal in F18 is read-only). Becomes a real arbitrary-local-file-read the moment any write primitive influences a `meta.url`. The shared `validate_public_url` re-check from F12 closes it cheaply.
- **F28** — Clarification: `/audio?format=opus` streams in 64 KiB blocks (`media.py:254-273`), so it is **not** an in-memory-concat OOM vector. The real cost is the mp3 up-front transcode (F9). Redirect effort accordingly.
- **F29** — `allow_private_network=True` is inert dead config on a public HTTPS origin (PNA preflights only target private/loopback IPs). Set it `False` in public mode and fix the stale invariant comment; do not double-count with F11.
- **F30** — ffmpeg/yt-dlp argv lack a `--` option terminator, but every positional is an absolute path and the attacker URL never reaches an ffmpeg argv, so no injection today. Cheapest hardening: `SETTINGS.cache_dir = cache_dir.resolve()` at startup + `--` before output positionals.
- **F31** — Negative result: the subprocess/command-injection surface is otherwise clean (no `shell=True`/`os.system`/`shlex`; url goes only to yt-dlp's Python API; model/stems/format/max_height all validated; temp files via `mkdtemp`). Maintain these invariants.

---

## 5. Required Hardening Controls Checklist

Organized by layer. The **master switch** is `NOMUSIC_PUBLIC=1`, which activates every app-layer control below; with it unset the server behaves exactly as today (loopback dev mode). Implement in the order of backend design §17 (config → helper modules → jobs/engine → routes → server → pipeline → supply-chain → extension).

### App layer (in-process — the load-bearing controls)

- [ ] **Admission cap** in `jobs.submit()` at thread-creation: `NOMUSIC_MAX_INFLIGHT_JOBS` (3) + `NOMUSIC_MAX_JOBS_PER_IP` (2), `JobRejected`→429. *(F1)*
- [ ] **GPU lock → inference only** (`_infer_lock` in `mlx_engine.py`); remove the broad `_gpu_lock` from `jobs._run`. *(F2)*
- [ ] **Absolute job deadline** in `_raise_if_abandoned`, before the subscriber early-return; **download watchdog** + min-throughput + `abort_check` on extract/full-download. *(F2)*
- [ ] **Admin-token gate** (`require_admin`, fail-closed, 404, constant-time) at **router level** on `/cache/clear` and `/cache`. *(F3, F19)*
- [ ] **SSRF: positive host allowlist + `allowed_extractors`** (exclude `generic`) in shared `validate_public_url`; re-validate `meta.url` in `/video`. *(F4, F6, F12, F27)*
- [ ] **`max_filesize` + `max_duration` ceilings** at probe and in download opts. *(F4, F7, F8)*
- [ ] **Disk caps:** `evict_to_fit` LRU in the sweeper + `NOMUSIC_FREE_DISK_FLOOR_BYTES` admission refusal. *(F7)*
- [ ] **`/video` height allowlist** (snap to `{360,480,720,1080}`) + cache key on actual resolution + `video_export_gate` semaphore + per-IP rate. *(F8, F12)*
- [ ] **Concurrency gates** (`video_export_gate`, `audio_transcode_gate`) separate from the anyio pool. *(F9)*
- [ ] **SSE caps** (per-job/IP/global) + max lifetime. *(F10)*
- [ ] **Locked CORS** (published extension origin + curated sites, never `'*'`) + **server-side `enforce_origin`** Depends on mutating routes + `allow_private_network=False` in public mode. *(F11, F29)*
- [ ] **Per-IP rate windows** keyed on `CF-Connecting-IP` (`process`/`video`/`default`). *(F1, F8, F11, F14, F15)*
- [ ] **`client_ip` from `CF-Connecting-IP` only**; never `X-Forwarded-For`; no uvicorn proxy-headers; optional `require_edge` tunnel-secret header. *(F14)*
- [ ] **Body/field size limits:** `MaxBodySizeMiddleware` + `url max_length=2048` + `keep_stems max_items`/dedup. *(F13)*
- [ ] **`JobId` pattern `^[0-9a-f]{16}$`** on all `{job_id}` routes + `cache._key_dir` guard. *(F18, F22)*
- [ ] **Error sanitization** (generic messages, full traces to logs, `NOMUSIC_DEBUG` gate, global exception handler). *(F20)*
- [ ] **Fingerprint suppression:** `server_header=False`, `date_header=False`, slim `/capabilities`. *(F21)*
- [ ] **SSE edge-compat:** add `X-Accel-Buffering: no` + `Cache-Control: no-cache` to `/events`; keep the 15 s keepalive (under Cloudflare's ~100 s idle/524). Keep the `/status` polling fallback in the extension.

### Cloudflare edge (defense-in-depth, not relied upon)

- [ ] Spend the **1 free rate-limit rule** on `POST /process` (≈5/60s per `CF-Connecting-IP`, block). Pro adds rules for `/video`, `/prioritize`, `/audio`.
- [ ] **Bot Fight Mode** + free **WAF managed ruleset**; **DDoS protection** is automatic.
- [ ] **5 free custom WAF rules:** block `/cache/clear` at the edge (belt-and-suspenders on the server-side gate); optional geo/method narrowing.
- [ ] `config.yml`: `protocol: quic`, generous `connectTimeout`, `disableChunkedEncoding: false` (don't break SSE).
- [ ] **Plan for the CDN media-terms risk:** strict quotas now, **R2 artifact-offload pre-designed** as the contingency. Pro does **not** exempt you.
- [ ] Leave **Cloudflare Access OFF** (public by design); keep service-tokens noted as the privatization switch.

### Linux host

- [ ] **Loopback bind stays `127.0.0.1:8723`**; cloudflared is outbound-only; **no router port-forward.**
- [ ] **Dedicated unprivileged service users** (`nomusic`, `cloudflared`), **read-only code tree** (`/opt/nomusic` root-owned), writable state confined to `/var/lib/nomusic` (+ `TORCH_HOME`).
- [ ] **Hardened systemd unit:** `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `ProtectKernel*`, `ProtectControlGroups`, `RestrictNamespaces`, `RestrictAddressFamilies`, `SystemCallFilter=@system-service`+`SystemCallErrorNumber=EPERM`, `MemoryMax`/`CPUQuota`/`TasksMax`/`LimitNOFILE`.
- [ ] **NVIDIA:** `PrivateDevices=no` + `DevicePolicy=closed` + explicit `DeviceAllow` for `/dev/nvidia*` (incl. `nvidia-uvm`, `char-nvidia-caps`); `ExecStartPre=+/usr/bin/nvidia-smi`. **Keep `MemoryDenyWriteExecute=no` and `PrivateDevices=no`** (both break torch/CUDA — re-verify after any torch upgrade).
- [ ] cloudflared unit hardened with `MemoryDenyWriteExecute=yes` + `PrivateDevices=yes` (safe — Go, no JIT/GPU); `no-autoupdate: true` (apt manages it).
- [ ] **Disk safety:** dedicated partition / **XFS project quota** on `/var/lib/nomusic`; shorten TTL (`NOMUSIC_CACHE_TTL_DAYS=1`) + sweep interval for hosted mode.
- [ ] **`unattended-upgrades`** (security origin + quiet-hour auto-reboot); **weekly yt-dlp bump** + service restart; bound journald size.
- [ ] **Pre-stage demucs weights** read-only; **keep `NOMUSIC_ADMIN_TOKEN` out of the subprocess env**; install **deno on Linux**. *(F23, F24, F26)*
- [ ] **Crash/disk/GPU monitoring** (`OnFailure=` notify unit, hourly disk threshold check, `nvidia-smi` VRAM watch).

### LAN isolation (blast radius)

- [ ] **Off-box segmentation (strongest):** dedicated guest VLAN/SSID with **client isolation**, or physical isolation behind a travel router. A root compromise cannot undo off-box rules.
- [ ] **On-box `nftables` egress filter** (defense-in-depth + the durable F5/F6 control): allow loopback + gateway + DNS + public internet; **reject RFC1918 / link-local / (CGNAT if no Tailscale)**.
- [ ] **`ufw` default-deny inbound**, allow only `lo` (+ optional admin SSH from one LAN IP).
- [ ] **No public SSH** (named tunnel doesn't expose it — don't port-forward it). Prefer local console or **Tailscale SSH**; if LAN SSH, keys-only + `fail2ban`.
- [ ] **Disable LAN-discovery/sharing** (avahi, smbd/nmbd, cups).

### Extension (Chrome Web Store)

- [ ] **Remove** the Clear-Cache control (`popup.html:153`, `popup.js:194-227`) and the cache-stats panel (`popup.js:129-157`) from the published build. *(F3, F19)*
- [ ] Set default `backendUrl` to `https://nomusic.<DOMAIN>` consistently (`settings.js:5`, `background.js:11`, `popup.html:140`, `popup.js:35,240`).
- [ ] **Reconcile version** (`manifest.json` `0.1.0` vs `package.json` `0.2.0`).
- [ ] **Minimal permissions** + **curated `content_scripts`/`host_permissions` allowlist** (not `<all_urls>`); ship the published `chrome-extension://<ID>` into `cors_origins`.

---

## 6. Residual Risks & Final Recommendation

### Residual risks after all fixes

Even with every control in §5 in place, the following remain by the nature of the public, no-client-auth choice:

1. **Bounded-but-real GPU/bandwidth spend by strangers.** Rate limits and admission caps bound the *rate*, not the *fact*. Within the quotas, anonymous users will spend your GPU and uplink. Accepted; tunable down toward 1.
2. **The watch-history oracle (F15) cannot be closed** without client auth — `job_id` is derivable from public inputs. Bounded by per-IP rate limits; accepted as residual for public content.
3. **DNS-rebind / redirect SSRF into the LAN (F5/F6)** is only *fully* closed by the OS egress filter, which a **root-level box compromise could disable.** The off-box segmentation is the real backstop; without it, residual LAN-read risk persists.
4. **Cloudflare CDN media-terms enforcement (§2c)** is a policy risk no code change removes. You are relying on low volume + the R2 contingency + Cloudflare's "reasonable notice."
5. **Operator legal/ToS exposure (§2d)** is unchanged by hardening. Fetching third-party media for anonymous users is a higher-liability posture than a personal tool, full stop.
6. **Supply-chain RCE (F16/F23/F24)** is reduced (hash-pinning, weights pre-stage, token-out-of-env, sandbox) but not eliminated; a hijacked upstream release plus an `install.sh` re-run is still a path to code execution on a LAN-resident box.
7. **Single-box availability.** One GPU, one disk, one residential uplink, one laptop. Even fully abuse-hardened, this is not a resilient service — it is a hobby deployment that will degrade under legitimate popularity, never mind attack.

### Final recommendation

1. **Do not expose the current code.** F1, F2, F3, and F4 are unauthenticated, trivially-exploitable, and individually sufficient to destroy or abuse the service. The `NOMUSIC_PUBLIC=1` control set plus the host/edge/LAN hardening in §5 is the **minimum** bar for public exposure.

2. **Strongly reconsider "fully public" vs "lightly-gated."** The technical DoS/abuse surface is closeable with engineering effort. The structural risks that hardening cannot touch — operator legal/ToS liability for anonymous third-party media fetching (§2d), Cloudflare CDN-media enforcement (§2c), and your residential IP's reputation/legal standing as the laundering egress (§2b) — all scale with *how public and how popular* the service is, and none of them have a technical fix. For a hobby project on a home laptop, the risk-adjusted sweet spot is **lightly-gated, not wide-open:**
   - Narrow `NOMUSIC_ALLOWED_URL_HOSTS` to the single platform you actually intend to support.
   - Keep quotas tight enough that total media egress stays unambiguously *proportionate* (Cloudflare's trigger word).
   - **Pre-wire the privatization switch** (Cloudflare Access + service tokens + the extension default-URL flip) so the day an abuse-desk notice, a Cloudflare media-terms notice, or unmanageable abuse arrives, you flip from public to invite-only in an afternoon — **without re-architecting**. That switch is the single most valuable piece of insurance for this deployment, and it costs nothing to keep ready.

3. **If the legal posture in §2d gives you any pause, default to private.** The extension's `backendUrl` setting already supports a private/self-hosted audience; the public default is the *only* thing creating the operator-liability exposure. Going public is a one-line default; staying private is the same one-line default pointed elsewhere. Make that choice deliberately, not by inertia.

4. **Sequencing for launch:** ship the F1/F2/F3 criticals and F4 first (they are the "destroyable today" set), then the remaining highs, then the lows/supply-chain, in the order of backend design §17. Stand up the Linux host hardening and LAN segmentation *before* the tunnel goes live, not after. Verify `systemd-analyze security nomusic` scores low (GPU nodes will cost a little), confirm `MemoryDenyWriteExecute=no`/`PrivateDevices=no` survive every torch upgrade, and smoke-test SSE through the edge (`X-Accel-Buffering: no` + 15 s keepalive) and the `POST /process` rate-limit rule before announcing.
