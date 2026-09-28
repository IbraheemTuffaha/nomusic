# Installation and development

This guide describes the pinned local dependency profile. The initial source
scope is public YouTube videos with a fixed duration, using `htdemucs` and the
vocals stem. Other extractors remain experimental. The backend binds to
loopback by default and does not yet authenticate users; remote deployment is
outside this installation profile.

For the complete first-run sequence, use the [local workflow checklist](local-workflow.md).

## Platform profile

| Platform | Installation | Processing device | Validation status |
| --- | --- | --- | --- |
| Linux x86_64, glibc 2.28+ | Locked CPU profile; Debian/Ubuntu system-package helper | CPU | Fresh installation, real CPU processing, actual extension playback and MP3/MP4 exports passed |
| Apple Silicon, macOS 14+ | Same lock, native macOS PyTorch wheel; Homebrew prerequisites | MPS when available, otherwise CPU | Wheels checked and installation path retained; no Mac or GPU execution in the Linux validation environment |
| Linux NVIDIA | Locked `cu126` profile, selected automatically for a compatible device/driver | CUDA if its wheel and driver support the GPU | Build installation checked separately; actual GPU inference still needs hardware acceptance |

Intel Macs, Linux ARM, Alpine/musl and native Windows are not covered by this
lock. A wheel being available is not a claim about inference performance.
CPU throughput, GPU throughput, browser compatibility and audible separation
quality need their own measurements.

## Standard installation

Run commands from the repository root. On macOS, install
[Homebrew](https://brew.sh/) first. On Linux, provide either
[Node.js 22 or newer](https://nodejs.org/en/download) or
[Deno 2.3 or newer](https://docs.deno.com/runtime/getting_started/installation/).
Distro `nodejs` packages can be too old, so the installer checks the actual
runtime version.

```sh
./install.sh
backend/.venv/bin/nomusic doctor
backend/.venv/bin/nomusic serve
```

The installer:

1. Checks the platform and installs missing supported system prerequisites.
   macOS uses Homebrew for FFmpeg, Deno and bootstrap Python as needed.
   Debian/Ubuntu uses apt for missing FFmpeg and Python/venv prerequisites.
2. Uses **uv 0.12.19**, either an existing exact-version executable or an
   isolated bootstrap in `backend/.installer-venv`. It does not upgrade the
   user's global Python packages.
3. Obtains Python **3.12.14** and installs `uv.lock` into `backend/.venv`
   using `uv sync --locked --no-editable --reinstall-package nomusic` with
   the selected `cpu` or `cu126` extra. It
   rebuilds the application from the current checkout on each run.
   Development dependencies are omitted by default.
4. Runs `nomusic check-runtime` to verify FFmpeg, ffprobe and the JavaScript
   runtime, and report the installed yt-dlp/EJS versions.
5. Downloads and verifies the pinned default model with `nomusic models fetch`.

The runtime check verifies prerequisites. Model fetching verifies artifact
identity. Neither command runs inference or establishes service readiness.
Successful processing is a separate check.

### Installer options

| Option | Purpose |
| --- | --- |
| `--profile auto\|cpu\|cu126` | Choose the locked PyTorch profile; default `auto` probes NVIDIA driver/device information on Linux |
| `--dev` | Include the locked development dependency group |
| `--skip-system-packages` | Use prerequisites already installed; do not call apt or Homebrew |
| `--skip-model-download` | Defer model download; a later fetch or first inference still needs the weights |
| `NOMUSIC_UV=/absolute/path/to/uv` | Use an explicit uv executable; its version must be 0.12.19 |
| `NOMUSIC_VENV=/absolute/path/to/environment` | Install into an explicit environment instead of `backend/.venv` |

For example, with FFmpeg and a supported JS runtime already available:

```sh
./install.sh --dev --skip-system-packages
```

`UV_PROJECT_ENVIRONMENT` is also accepted as the environment path when
`NOMUSIC_VENV` is unset. If using a custom environment, replace
`backend/.venv` in the remaining commands with that path.

The installer accepts `NOMUSIC_PYTHON` only when it equals the exact pinned
version in `.python-version`. The former `NOMUSIC_TORCH` override is rejected because the lock pins torch.
`NOMUSIC_CUDA=cu126` remains an alias for the locked CUDA profile; other CUDA
versions are rejected with instructions to select a supported profile.

## Start, inspect and stop

```sh
backend/.venv/bin/nomusic --help
backend/.venv/bin/nomusic check-runtime
backend/.venv/bin/nomusic doctor
backend/.venv/bin/nomusic serve
```

The listener defaults to `127.0.0.1:8723`. Keep the process running and stop it
with Control+C. `/healthz` answers whether the API responds, and
`/capabilities` reports the engine and configuration. `/readyz` returns HTTP
503 during initialization or after a startup check fails, and HTTP 200 only
after runtime, writable storage and default-model loading checks pass. The
“Uvicorn running” log line means only that the HTTP server is listening.

### Local diagnostics and readiness

`nomusic doctor` works without a running API or an accessible source website.
It reports runtime/package versions, checks cache and temporary storage with
real write/read/rename/delete operations, identifies the selected device/model,
verifies the pinned cached model files, and separates two seconds of generated
audio. It validates duration, all stem lengths, finite samples and at least one
non-silent stem. This is an execution check, not a music-removal quality or
sustained performance benchmark.

```sh
backend/.venv/bin/nomusic doctor --json
backend/.venv/bin/nomusic doctor --skip-inference
NOMUSIC_DEVICE=cpu backend/.venv/bin/nomusic doctor
backend/.venv/bin/nomusic doctor --model htdemucs_ft
curl --fail http://127.0.0.1:8723/readyz
```

Doctor uses local model files only and never downloads weights. If the model
is missing, follow its `nomusic models fetch --model MODEL` instruction, then
run doctor again. It may create the configured cache directory but preserves
existing data and removes its temporary probes. Run it as the same OS user,
with the same environment as the service. Normal doctor runs load a model and
consume inference memory; avoid running them alongside a busy service on a
memory-constrained machine. `--skip-inference` checks prerequisites only and
explicitly reports that inference was not verified. Both modes exit nonzero
when a check fails. JSON reports include `ok`, `inference_verified` and named
checks with errors/remedies; file paths and runtime details are local diagnostics.

Readiness is a cheap snapshot of **startup** checks; it performs no I/O or
inference per request. States are `not_started`, `starting`, `warming` (with a
check name), `failed` (with a check name), `ready`, and `stopping`. Readiness
responses cannot be cached and omit raw errors and private paths. Full failure
details stay in server logs and doctor. A startup failure remains unready until
the cause is fixed and the service is restarted, even if a later job happens
to load the model successfully. Shutdown immediately withdraws readiness.

Readiness verifies the default model can load; doctor separately proves a tiny
inference. Neither proves remote-source access, optional-model readiness,
available queue capacity, long-session reliability or safe public deployment.
The existing extension uses liveness/capabilities and does not wait for readyz;
early jobs can still wait for lazy model loading. An unavailable YouTube source
can therefore fail even when doctor and readiness succeed.

The old launch forms remain available for existing workflows:

```sh
backend/.venv/bin/python backend/server.py
```

From the `backend` directory, `.venv/bin/python -m tools.cli URL` remains a
compatibility launcher for the processing CLI. New code should import from
the `nomusic` package. The installed `nomusic` command works from any working
directory when invoked by its absolute path or through an activated environment.

### Service lifecycle

Each application lifespan creates its own engine, cache, job registry and
background threads. Importing the server or constructing an app starts no
model loading, downloads or maintenance work. Restarting creates fresh
in-memory services and preserves the on-disk caches.

Control+C or SIGTERM first stops new job admission and closes progress
streams quietly. Active processing and HTTP requests, including exports,
have one total grace period of 60 seconds. Configure it with
`NOMUSIC_SHUTDOWN_GRACE_SECONDS`. The first signal explains the wait;
Control+C again exits immediately. A model preload alone is not awaited.

Ordinary shutdown joins owned work before releasing services and logs
**Service shutdown complete**. If work cannot finish within the grace period,
the process exits with a **Forced shutdown** message and code 124; a second
interrupt exits with code 130. Restart and retry unfinished work. On startup,
cleanup removes abandoned nomusic scratch files whose leases are no longer
held, while retaining complete cached media and shared model-download
partials. `/readyz` keeps a startup failure visible until restart, while
`/healthz` continues to report liveness.

Use `nomusic serve`, `python -m nomusic serve`, or the compatibility launcher
above. They use a small Uvicorn adapter for quiet progress-stream closure and
bounded process shutdown. A generic `uvicorn nomusic.server:app` command
bypasses these guarantees. Separate supervised model workers remain future
work; this adapter exits the whole server if an in-process worker is stuck.

### Device selection

`NOMUSIC_DEVICE` accepts `auto` (the default), `cpu`, `mps`, or `cuda`.
Automatic selection prefers usable MPS, then usable CUDA, then CPU. An
explicit unavailable device fails with an explanation instead of silently
changing devices.

```sh
NOMUSIC_DEVICE=cpu backend/.venv/bin/nomusic serve
```

On Linux, `--profile auto` selects CUDA 12.6 when NVIDIA device/driver queries
succeed and the reported CUDA support is at least 12.6, otherwise CPU. GeForce
RTX 50-series and devices named Blackwell select CPU, including mixed-device
systems: the locked CUDA 12.6 build does not support them. Blackwell GPU
acceleration needs a newer wheel ([PyTorch guidance](https://pytorch.org/blog/pytorch-2-12-release-blog/#deprecation-of-the-cuda-128-wheel)),
which is not currently a locked installer profile. An
installed CUDA build does not guarantee GPU architecture compatibility; the
installer reports the build and selected device. Use `--profile cpu` to force
CPU packages. On Apple Silicon the `cpu` extra installs the native PyTorch
wheel, which also provides MPS. `NOMUSIC_DEVICE` chooses the runtime device;
it cannot add CUDA support to CPU packages.

## Browser setup

Load `extension/` through Chrome's `chrome://extensions` page with Developer
mode enabled. Keep the default backend URL, `http://127.0.0.1:8723`, when the
browser and helper run on the same machine.

The current extension makes processing requests from the video page's
content-script context. Chrome may therefore ask the **site** for permission
to access the local network. Allow that permission on the video site so it
can reach the helper. If denied, use the control beside the address bar to
open site settings, allow **Local network access**, and reload. Permission
denial can appear as “backend unreachable” even when the server is running;
extension host permission does not replace this check.

Start a short public finite YouTube video, click nomusic, wait for processed
playback, then test an MP3 and an MP4 export. This exercises the installed
extension, acquisition, inference, chunk serving, Web Audio and existing
export flow. Keep the tab open while preparing an export. A short successful
example does not establish long-session reliability or support for every
source video.

The popup's **backend up** label means the API is reachable; it does not check
model readiness or YouTube access. Settings save automatically. After changing
the model or stems, toggle nomusic off and on to start a new session. See the
[local workflow checklist](local-workflow.md) for readiness, restart and
source-access troubleshooting.

## Dependencies and external tools

[`pyproject.toml`](../pyproject.toml) declares direct dependencies, the installed
entry points and the development group. [`uv.lock`](../uv.lock) records the
resolved graph and artifact hashes. [`.python-version`](../.python-version)
pins the interpreter used by installation.

| Component | Reference version or requirement |
| --- | --- |
| Python | 3.12.14; package accepts the 3.12 series |
| uv installer | 0.12.19 |
| PyTorch | 2.14.0; locked `+cpu` or `+cu126` wheels on Linux, native PyPI wheel on macOS |
| Demucs | 4.1.0 |
| NumPy / soundfile | 2.2.6 / 0.14.0 |
| FastAPI / Starlette | 0.141.1 / 1.7.0 |
| Uvicorn / Pydantic | 0.54.0 / 2.13.5 |
| yt-dlp / packaged EJS | 2026.8.19 / 0.8.0 |
| JS runtime | Node.js 22+ or Deno 2.3+ |
| FFmpeg | `ffmpeg` and `ffprobe` on PATH; `libopus`, `libmp3lame`, `aac` and `libx264` encoders |

Python dependencies and model files are pinned. System FFmpeg and JS-runtime
packages follow the host's package manager; record their exact versions when
reporting a reproduction. `check-runtime` prints versions and locations but
does not exhaustively validate every codec. Actual media processing and
export tests provide that evidence.

Starlette's `allow_private_network` option first appeared in **0.51.0**; the
old lower bound allowed incompatible versions. Demucs 4.1.0's inference
dependencies no longer include torchaudio, so the reference install omits it.
The earlier requirement that every torch/torchaudio version number match is
also outdated for TorchAudio 2.11. These changes preserve the same
Demucs inference approach. [Starlette release notes](https://starlette.dev/release-notes/),
[Demucs 4.1.0 dependencies](https://github.com/adefossez/demucs/blob/v4.1.0/pyproject.toml),
[TorchAudio compatibility](https://docs.pytorch.org/audio/stable/installation.html).

yt-dlp uses the installed EJS package; nomusic no longer opts into fetching
solver scripts from GitHub at runtime. `NOMUSIC_JS_RUNTIME` can select a
supported executable by path. The runtime is identified from its version
output, including a binary named `nodejs`. Bun is outside this profile.
[Upstream EJS setup](https://github.com/yt-dlp/yt-dlp/wiki/EJS).

## Model identity and storage

The engine still uses Demucs and the same two model names. It now downloads
the author's safetensors exports at fixed repository commits and checks the
SHA-256 of every model/configuration file before loading it. Failed downloads
or checksum mismatches fail visibly; loading does not fall back to a moving
revision or the legacy pickle downloads.

| Name | Upstream repository | Pinned revision | Weight size |
| --- | --- | --- | --- |
| `htdemucs` (default) | [adefossez/HTDemucs](https://huggingface.co/adefossez/HTDemucs) | `cbc8a9b1a87023b7fd74e7b3412e6321c0eab003` | 84,025,440 bytes |
| `htdemucs_ft` | [adefossez/HTDemucs-ft](https://huggingface.co/adefossez/HTDemucs-ft) | `d74ac89c3a1e874fc78f152555cf4d8533f06cd4` | Four files of 84,025,440 bytes each |

The complete filenames, hashes and member order are maintained in
[`model_store.py`](../backend/nomusic/engines/model_store.py). The default
file, `955717e8.safetensors`, has SHA-256
`d9fa14133cfcc034a6758923bb3a8ca9f8dfd0b582134643bbf83f72c17576dd`.
The default pin identifies the same weights used by the preceding local proof;
pinning does not select a different separation model. The fine-tuned bag keeps
its four models and original per-stem weights.

Fetch either model without starting the server or selecting a GPU:

```sh
backend/.venv/bin/nomusic models fetch
backend/.venv/bin/nomusic models fetch --model htdemucs_ft
```

Model files use the standard Hugging Face cache, normally
`~/.cache/huggingface/hub`, honoring `HF_HOME` or `HF_HUB_CACHE` when set.
Processed media uses `~/.cache/nomusic`, configurable with
`NOMUSIC_CACHE_DIR`. These are separate caches. Installation does not clear
either one. Existing safetensors for the pinned revisions can be reused;
an installation with only legacy model files needs the safetensors download.

## Developer workflow and validation

The [verification guide](verification.md) provides one repeatable command for
the suites and controlled real extension/CPU playback/export check, including
browser setup, offline model use, local evidence and manual acceptance.

Install development dependencies:

```sh
./install.sh --dev --skip-system-packages
backend/.venv/bin/python -m pytest backend/tests -v
npm --prefix extension test
```

The backend suite contains isolated tests with stubbed acquisition/inference;
it is not a substitute for real processing. Any GPU-specific skip must be
reported as such. Extension Node tests do not replace loading the unpacked
extension in Chrome. Lifecycle tests also start real server subprocesses to
exercise SIGTERM, open progress streams, active HTTP request drain and reload.

The standard installer uses a non-editable package. Re-run it after changing
backend code, or use an editable development installation from the same lock
with uv 0.12.19:

```sh
UV_PROJECT_ENVIRONMENT=backend/.venv uv sync --locked --extra cpu --python 3.12.14
```

Here and below, `uv` means an installed uv 0.12.19 executable; the bootstrap
copy is `backend/.installer-venv/bin/uv` when the installer created one.

For automatic restart after editing Python files in an editable installation:

```sh
NOMUSIC_RELOAD=1 backend/.venv/bin/nomusic serve
```

The reloader's parent process watches files without starting model warmup or
maintenance. On a change, the serving child shuts down before its replacement
starts fresh services. A slow graceful stop can therefore delay a reload.

### Run with fresh media and model caches

This command uses a new temporary directory and explicitly selects CPU.
Substitute a short public finite YouTube URL for `PUBLIC_VIDEO_URL`:

```sh
proof_dir="$(mktemp -d "${TMPDIR:-/tmp}/nomusic-cold.XXXXXX")"
HF_HOME="$proof_dir/models" \
HF_HUB_CACHE="$proof_dir/models/hub" \
HF_HUB_OFFLINE=0 \
NOMUSIC_CACHE_DIR="$proof_dir/media" \
NOMUSIC_DEVICE=cpu \
backend/.venv/bin/nomusic process 'PUBLIC_VIDEO_URL' --model htdemucs --stems vocals
```

Keep the directory until inspecting the output and recording the result;
remove it afterward when no process is using it. This exercises acquisition,
new model download, inference and chunk writing. It does not exercise the
browser or export routes. Record the source, platform, CPU/thread settings,
binary versions and elapsed time. A small smoke run is not a sustained
throughput or subjective listening benchmark.

### Code organization

| Location | Responsibility |
| --- | --- |
| `backend/nomusic/__main__.py` | Installed command routing |
| `backend/nomusic/server.py` and `routes/` | API construction, lifespan and HTTP endpoints |
| `backend/nomusic/services.py` | One lifespan's engine, registry and background-thread ownership |
| `backend/nomusic/serving.py` | Uvicorn launch/reload and shutdown announcement before HTTP drain |
| `backend/nomusic/config.py` and `runtime.py` | Configuration and external-tool checks |
| `backend/nomusic/diagnostics.py` | Offline doctor, storage checks and real inference smoke |
| `backend/nomusic/jobs.py` | Current in-process job registry and worker ownership |
| `backend/nomusic/engines/` | Engine contract, Demucs inference and pinned models |
| `backend/nomusic/pipeline/` | Source download, chunk processing, cache and exports |
| `backend/tests/` | Backend tests |
| `extension/` | Browser UI, session networking and audio scheduling |

Keep installation checks, model fetching and CLI help independent of server
startup. Keep thread cleanup with the service or processor that creates the
work. Readiness belongs to the lifespan's service owner; supervision of
separate model worker processes remains future work beyond this bounded
server lifecycle.

### Useful configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `NOMUSIC_DEVICE` | `auto` | `auto`, `cpu`, `mps`, or `cuda` |
| `NOMUSIC_HOST` / `NOMUSIC_PORT` | `127.0.0.1` / `8723` | API listener |
| `NOMUSIC_SHUTDOWN_GRACE_SECONDS` | `60` | Total shutdown grace period for active processing and requests; a second Control+C forces exit |
| `NOMUSIC_ENGINE` | `mlx` | Existing engine name; `demucs` is an alias for the same PyTorch implementation |
| `NOMUSIC_CACHE_DIR` | `~/.cache/nomusic` | Processed-media cache |
| `NOMUSIC_CACHE_TTL_DAYS` | `7` | Retention age; `0` disables the sweep's age limit |
| `NOMUSIC_JS_RUNTIME` | Auto-detected | Explicit Node/Deno executable |
| `NOMUSIC_CHUNK_SECONDS` | `10` | Processing chunk duration |
| `NOMUSIC_CHUNK_OVERLAP_SECONDS` | `0.5` | Separator context overlap |
| `NOMUSIC_GPU_BATCH` | `2` | Maximum chunks per inference batch; larger values need measurement |
| `NOMUSIC_PROGRESSIVE` | `true` | Process decodable source sections while downloading |
| `NOMUSIC_DEBUG` | `false` | Additional backend logs |
| `NOMUSIC_RELOAD` | `false` | Development reload; use an editable install for source changes |

See [`config.py`](../backend/nomusic/config.py) for retention intervals,
idle-work behavior and other advanced settings. Their current behavior should
not be treated as public-service resource guarantees.

## Upgrading and rollback

Stop the helper before replacing its environment. Retain the previous
checkout or downloaded project folder until the replacement has passed the
same local playback/export checks.

If the default `backend/.venv` uses a different Python version, the installer
moves it to `backend/.venv.bak` after checking prerequisites, then creates the
pinned environment. It never overwrites an existing backup. If installation
fails, it preserves the backup and prints the exact restore command.

Incomplete directories, symlinks and incompatible explicitly selected custom
environments are refused without deletion. Python environments are not
generally relocatable; restore a backup to its original path before reuse.
After accepting the upgrade, remove the backup when no longer needed.

For ordinary upgrades, obtain the new checkout and run `./install.sh` there.
The installer keeps Python packages in sync with the committed lock and
verifies the pinned weights. Media and model caches are preserved. The old
launch wrappers remain, but `nomusic serve` is the canonical command.

After extension files change, open `chrome://extensions`, reload nomusic,
and reload your video page. If the new checkout is in a different folder,
remove the old unpacked extension and load the new `extension` folder; check
its backend URL, model and stems again. Repeat the
[local workflow checklist](local-workflow.md) before retiring the old setup.

To roll back, stop the new helper and return to the previous checkout and
environment at their original paths, or reinstall that checkout's own
dependency profile. Do not combine an old source tree with an unrelated new
lock or overwrite the known-good environment before validating its replacement.

### Updating dependency and model pins

Edit direct pins in `pyproject.toml`, then regenerate and review the lock:

```sh
uv lock
uv lock --check
UV_PROJECT_ENVIRONMENT=backend/.venv uv sync --locked --extra cpu --python 3.12.14
backend/.venv/bin/python -m pytest backend/tests -v
npm --prefix extension test
```

Also run a fresh-cache real CPU processing test and the actual Chrome
playback/export smoke before treating the new profile as verified. The lock
requires Linux x86_64 and macOS arm64 wheels, but that resolver check does not
run macOS inference.

Update `.python-version`, `requires-python` and the documented profile together
when changing the reference Python version. Keep the installer uv pin and documented version
consistent. Model changes require reviewed repository revisions and file hashes
in `model_store.py`; a Python lock update alone cannot pin remote model data.

## NVIDIA installation

CPU and CUDA 12.6 packages are separate, mutually exclusive extras in the same
lock. Choose a profile explicitly to install without relying on auto-detection:

```sh
./install.sh --profile cu126 --skip-system-packages
NOMUSIC_DEVICE=cuda backend/.venv/bin/nomusic serve
```

An explicit CUDA request fails if the installed build, GPU architecture or
NVIDIA driver cannot run it. Installing the CUDA wheels on a CPU-only machine
can verify dependency resolution and build identity, but cannot test inference.
The native Mac profile does not use a Linux CUDA package index.

To switch back, stop the helper and run `./install.sh --profile cpu`.
For editable CUDA development use `uv sync --locked --extra cu126` with the
same `UV_PROJECT_ENVIRONMENT` and Python pin as above. Always select one extra
when running uv directly; unqualified `uv sync` resolves the default upstream
PyTorch build instead of choosing a nomusic accelerator profile.

See [uv's optional accelerator profiles](https://docs.astral.sh/uv/guides/integration/pytorch/#configuring-accelerators-with-optional-dependencies).
