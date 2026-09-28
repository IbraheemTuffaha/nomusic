# Installation and development

This guide describes the pinned local dependency profile. The initial source
scope is public YouTube videos with a fixed duration, using `htdemucs` and the
vocals stem. Other extractors remain experimental. The backend binds to
loopback by default and does not yet authenticate users; remote deployment is
outside this installation profile.

## Platform profile

| Platform | Installation | Processing device | Validation status |
| --- | --- | --- | --- |
| Linux x86_64, glibc 2.28+ | Locked CPU PyTorch wheels; Debian/Ubuntu system-package helper | CPU | Fresh installation, real CPU processing, actual extension playback and MP3/MP4 exports passed |
| Apple Silicon, macOS 14+ | Same lock, native macOS PyTorch wheel; Homebrew prerequisites | MPS when available, otherwise CPU | Wheels checked and installation path retained; no Mac or GPU execution in the Linux validation environment |
| Linux NVIDIA | Separate experimental environment described below | CUDA if its wheel and driver support the GPU | Not part of the locked or tested reference profile |

Intel Macs, Linux ARM, Alpine/musl and native Windows are not covered by this
lock. A wheel being available is not a claim about inference performance.
CPU throughput, GPU throughput, browser compatibility and audible separation
quality need their own measurements.

The 2026-09-28 Linux checks used Python 3.12.14, FFmpeg 9.0.1 and Node
24.19.0. They included an empty dependency cache, a fresh interpreter download,
an empty model cache, installed-package imports outside the checkout, and
controlled-media plus live YouTube browser runs. The controlled case replaces
only source acquisition with a fixture; the live case uses real acquisition.
The backend suite passed 99 tests with one GPU-only skip, and the extension
Node suite passed 38 tests. Long-session behavior, subjective listening
quality, Mac execution and GPU inference are separate checks.

## Standard installation

Run commands from the repository root. On macOS, install
[Homebrew](https://brew.sh/) first. On Linux, provide either
[Node.js 22 or newer](https://nodejs.org/en/download) or
[Deno 2.3 or newer](https://docs.deno.com/runtime/getting_started/installation/).
Distro `nodejs` packages can be too old, so the installer checks the actual
runtime version.

```sh
./install.sh
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
   using `uv sync --locked --no-editable --reinstall-package nomusic`. It
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
version in `.python-version`. The former `NOMUSIC_TORCH` and `NOMUSIC_CUDA`
installer overrides are rejected: changing those values would no longer
install the recorded lock. Use a separate experimental profile when needed.

## Start, inspect and stop

```sh
backend/.venv/bin/nomusic --help
backend/.venv/bin/nomusic check-runtime
backend/.venv/bin/nomusic serve
```

The listener defaults to `127.0.0.1:8723`. Keep the process running and stop it
with Control+C. `/healthz` answers whether the API responds, and
`/capabilities` reports the engine and configuration. They do not currently
prove model readiness or successful inference. The “Uvicorn running” log line
also means only that the HTTP server is listening.

The old launch forms remain available for existing workflows:

```sh
backend/.venv/bin/python backend/server.py
```

From the `backend` directory, `.venv/bin/python -m tools.cli URL` remains a
compatibility launcher for the processing CLI. New code should import from
the `nomusic` package. The installed `nomusic` command works from any working
directory when invoked by its absolute path or through an activated environment.

### Device selection

`NOMUSIC_DEVICE` accepts `auto` (the default), `cpu`, `mps`, or `cuda`.
Automatic selection prefers usable MPS, then usable CUDA, then CPU. An
explicit unavailable device fails with an explanation instead of silently
changing devices.

```sh
NOMUSIC_DEVICE=cpu backend/.venv/bin/nomusic serve
```

The Linux reference installation contains a CPU PyTorch build, even on a
machine with an NVIDIA GPU. Setting `NOMUSIC_DEVICE=cuda` cannot add CUDA
support to that build. On Apple Silicon the native PyTorch wheel provides the
MPS path, subject to the host's support.

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

## Dependencies and external tools

[`pyproject.toml`](../pyproject.toml) declares direct dependencies, the installed
entry points and the development group. [`uv.lock`](../uv.lock) records the
resolved graph and artifact hashes. [`.python-version`](../.python-version)
pins the interpreter used by installation.

| Component | Reference version or requirement |
| --- | --- |
| Python | 3.12.14; package accepts the 3.12 series |
| uv installer | 0.12.19 |
| PyTorch | 2.14.0; official `+cpu` wheel on Linux, native PyPI wheel on macOS |
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

Install development dependencies:

```sh
./install.sh --dev --skip-system-packages
backend/.venv/bin/python -m pytest backend/tests -v
npm --prefix extension test
```

The backend suite contains isolated tests with stubbed acquisition/inference;
it is not a substitute for real processing. Any GPU-specific skip must be
reported as such. Extension Node tests do not replace loading the unpacked
extension in Chrome.

The standard installer uses a non-editable package. Re-run it after changing
backend code, or use an editable development installation from the same lock
with uv 0.12.19:

```sh
UV_PROJECT_ENVIRONMENT=backend/.venv uv sync --locked --python 3.12.14
```

Here and below, `uv` means an installed uv 0.12.19 executable; the bootstrap
copy is `backend/.installer-venv/bin/uv` when the installer created one.

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
| `backend/nomusic/server.py` and `routes/` | API construction and HTTP endpoints |
| `backend/nomusic/config.py` and `runtime.py` | Configuration and external-tool checks |
| `backend/nomusic/jobs.py` | Current in-process job registry and worker ownership |
| `backend/nomusic/engines/` | Engine contract, Demucs inference and pinned models |
| `backend/nomusic/pipeline/` | Source download, chunk processing, cache and exports |
| `backend/tests/` | Backend tests |
| `extension/` | Browser UI, session networking and audio scheduling |

Keep installation checks, model fetching and CLI help independent of server
startup. Full lifecycle ownership and meaningful model readiness are follow-up
work; packaging alone does not establish those guarantees.

### Useful configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `NOMUSIC_DEVICE` | `auto` | `auto`, `cpu`, `mps`, or `cuda` |
| `NOMUSIC_HOST` / `NOMUSIC_PORT` | `127.0.0.1` / `8723` | API listener |
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

If the old `backend/.venv` uses a different Python version, is incomplete, or
is not a virtual environment, the installer stops without deleting it.
Install to a new absolute path using `NOMUSIC_VENV`, or rename the old
environment yourself after stopping its processes. Python environments are
not generally relocatable; a renamed backup should be restored to its
original path before reuse.

For ordinary upgrades, obtain the new checkout and run `./install.sh` there.
The installer keeps Python packages in sync with the committed lock and
verifies the pinned weights. Media and model caches are preserved. The old
launch wrappers remain, but `nomusic serve` is the canonical command.

To roll back, stop the new helper and return to the previous checkout and
environment at their original paths, or reinstall that checkout's own
dependency profile. Do not combine an old source tree with an unrelated new
lock or overwrite the known-good environment before validating its replacement.

### Updating dependency and model pins

Edit direct pins in `pyproject.toml`, then regenerate and review the lock:

```sh
uv lock
uv lock --check
UV_PROJECT_ENVIRONMENT=backend/.venv uv sync --locked --python 3.12.14
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

## Experimental NVIDIA installation

Use a separate environment for GPU evaluation. This path deliberately does
not use the CPU reference lock and has not been validated here. It needs
compatible NVIDIA hardware and drivers, and it can fail if the pinned torch
version does not publish a wheel usable by that driver or GPU.

From the repository root, with uv 0.12.19 and external prerequisites present:

```sh
uv venv --python 3.12.14 backend/.venv-cuda
uv pip install --python backend/.venv-cuda/bin/python . --torch-backend=auto
backend/.venv-cuda/bin/nomusic check-runtime
backend/.venv-cuda/bin/nomusic models fetch
NOMUSIC_DEVICE=cuda backend/.venv-cuda/bin/nomusic serve
```

uv chooses a PyTorch index based on detected hardware and driver support;
the explicit `cuda` device request then prevents an unnoticed CPU fallback.
The package's direct pins still apply, but transitive dependencies in this
experimental environment are resolved separately from `uv.lock`. Record the
resulting environment and real inference measurements before proposing a
supported CUDA profile. Older GPUs may require a separately maintained
dependency profile. [uv's PyTorch installation documentation](https://docs.astral.sh/uv/guides/integration/pytorch/#automatic-backend-selection).
