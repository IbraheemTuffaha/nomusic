# Installation and development

For a first installation, follow the [README walkthrough](../README.md).
Run commands below from the project root, with the helper stopped before
installing or changing its environment.

## Platforms and profiles

| Platform | Installed profile | Runtime device |
| --- | --- | --- |
| Linux x86_64, glibc 2.28+ | Locked `cpu` or `cu126`; Debian/Ubuntu system-package helper | CPU or compatible NVIDIA GPU |
| Apple Silicon, macOS 14+ | Native macOS PyTorch wheel; Homebrew prerequisites | MPS when available, otherwise CPU |

macOS 14 is the minimum for the current pinned dependencies. Intel Macs,
Linux ARM, Alpine/musl and native Windows are outside this lock. Linux CPU is
the reference environment. Native Mac/MPS and NVIDIA inference require hardware
acceptance; installing wheels or running Mac CPU CI does not establish GPU behavior.

`./install.sh` defaults to `--profile auto`. On Linux it selects CUDA 12.6 when
`nvidia-smi` reports a device and driver advertising CUDA 12.6 or newer. GeForce
RTX 50-series and explicitly named Blackwell GPUs select CPU because this CUDA
12.6 build does not support them. Missing or incompatible device/driver detection
also selects CPU. The installed engine checks GPU architecture support as well.
You can choose explicitly:

```sh
./install.sh --profile cpu
./install.sh --profile cu126
```

CPU and CUDA are mutually exclusive extras in the same lock. The CUDA option
is Linux-only. On Mac the `cpu` extra selects the native wheel, which also
supports MPS. Package selection and runtime device selection are separate:

```sh
NOMUSIC_DEVICE=cpu backend/.venv/bin/nomusic doctor
NOMUSIC_DEVICE=mps backend/.venv/bin/nomusic doctor
NOMUSIC_DEVICE=cuda backend/.venv/bin/nomusic doctor
```

Run only the command appropriate to your machine. An explicit unavailable
device fails visibly. To switch Linux packages, stop the helper and rerun
the installer with the desired profile. A CPU wheel cannot enable CUDA merely
by setting `NOMUSIC_DEVICE`.

## Prerequisites and installer options

On Mac, install [Homebrew](https://brew.sh/). On Linux, provide
[Node.js 22+](https://nodejs.org/en/download) or
[Deno 2.3+](https://docs.deno.com/runtime/getting_started/installation/).
The current runtime check requires one for all source downloads; Bun is not
accepted. macOS installation supplies Deno when a supported runtime is missing.

The installer obtains Python **3.12.14**, uses **uv 0.12.19**, and installs
the committed [`uv.lock`](../uv.lock) into `backend/.venv`. It rebuilds the
application as a non-editable package, checks FFmpeg/ffprobe and the JavaScript
runtime, then fetches the pinned default model. Missing system packages use
Homebrew on Mac or apt on Debian/Ubuntu. Other Linux distributions should
provide FFmpeg, a JS runtime and either uv or Python with venv support themselves.

| Option | Purpose |
| --- | --- |
| `--profile auto\|cpu\|cu126` | Select the locked PyTorch profile |
| `--dev` | Include test/development dependencies |
| `--skip-system-packages` | Check prerequisites without calling apt/Homebrew |
| `--skip-model-download` | Defer weights until `models fetch` or first use |
| `NOMUSIC_UV=/absolute/path/to/uv` | Use an exact-version uv executable |
| `NOMUSIC_VENV=/absolute/path` | Use a dedicated custom environment |

`UV_PROJECT_ENVIRONMENT` supplies the environment path when `NOMUSIC_VENV` is
unset. Custom paths must be absolute; substitute them for `backend/.venv` in
commands. The installer can bootstrap uv in `backend/.installer-venv` without
changing global Python packages.

`NOMUSIC_PYTHON` must match `.python-version`; `NOMUSIC_TORCH` overrides are
rejected. `NOMUSIC_CUDA=cu126` is an alias for that profile; other CUDA tags are
not in the lock. Direct versions and entry points live in
[`pyproject.toml`](../pyproject.toml). FFmpeg and JS runtime versions follow the
host package manager. FFmpeg needs `libopus`, `libmp3lame`, `aac` and `libx264`
for normal processing/exports; verification additionally checks `libvpx-vp9`.

## Diagnostics and model storage

```sh
backend/.venv/bin/nomusic models fetch
backend/.venv/bin/nomusic doctor
backend/.venv/bin/nomusic doctor --json
backend/.venv/bin/nomusic doctor --skip-inference
```

Doctor uses cached weights only. It checks binaries, package versions, writable
storage, the selected device and model hashes, then runs two seconds of real
inference on generated audio. It plays no sound and contacts no video site.
`--skip-inference` omits that final check. Failures return a nonzero exit status
and a remedy; use the same OS user and environment as the service. Stop a busy
helper before running inference diagnostics on a memory-constrained machine.

Models are `htdemucs` (default, about 84 MB) and `htdemucs_ft` (four models,
about 336 MB). Fetch the latter with `nomusic models fetch --model htdemucs_ft`.
Use `nomusic doctor --model htdemucs_ft` to check that optional model afterward.
The author's [HTDemucs](https://huggingface.co/adefossez/HTDemucs) and
[HTDemucs-ft](https://huggingface.co/adefossez/HTDemucs-ft) safetensors exports use
fixed revisions and SHA-256 hashes in
[`model_store.py`](../backend/nomusic/engines/model_store.py). Downloads and
loads verify those hashes; legacy pickle weights do not replace these files.

Model files use `~/.cache/huggingface/hub`, honoring `HF_HOME` or `HF_HUB_CACHE`.
Processed media uses `~/.cache/nomusic`, overridden by `NOMUSIC_CACHE_DIR`.
Installation preserves both caches; the extension's Clear control removes
processed media only.

## Upgrading and rollback

Stop the helper and keep the previous checkout until the replacement works.
Update the project files, run `./install.sh`, then run doctor and the
[local workflow](local-workflow.md). Reload the extension and video tabs.

If the default `backend/.venv` uses another Python version, the installer moves
it to **`backend/.venv.bak`** after checking prerequisites and creates the pinned
environment. It never overwrites an existing backup. On failure it preserves
the backup and prints the exact restore command. Move an older backup elsewhere
before another migration; delete it only when no longer needed.

Incomplete directories, symlinks and incompatible explicitly selected custom
environments are refused without deletion. Choose a new custom path or move
the old one aside yourself. Python environments are not generally relocatable:
restore backups to their original paths before reuse.

To roll back, stop the new helper and restore the previous checkout and its
environment, or reinstall that checkout's own dependency profile. Do not mix
an old source tree with a new lock. Media and model caches survive upgrades.

## Development

The installed package is rebuilt by `./install.sh --dev --skip-system-packages`.
For editing with live reload, use an editable installation from the same lock:

```sh
UV_PROJECT_ENVIRONMENT="$PWD/backend/.venv" uv sync --locked --extra cpu --python 3.12.14 --group dev
NOMUSIC_RELOAD=1 backend/.venv/bin/nomusic serve
```

Use uv 0.12.19 (`backend/.installer-venv/bin/uv` if bootstrapped locally).
For CUDA development replace `--extra cpu` with `--extra cu126`; always select
one profile. Reload watches the imported package directory, so a non-editable
installation watches its installed copy. Every reload also reloads the model.

Run `backend/.venv/bin/python -m pytest backend/tests` and
`npm --prefix extension test` for development checks. Before release verification,
rerun the installer to restore a non-editable package, then follow
[verification](verification.md), which checks installed-code provenance and
the actual extension/CPU pipeline.

The supported service launcher is `nomusic serve`, also available as
`python -m nomusic serve` or `python backend/server.py` in the environment.
A generic `uvicorn nomusic.server:app` bypasses the shutdown adapter.
`nomusic process URL --model htdemucs --stems vocals` runs processing without
the API; `nomusic-process` and `python -m tools.cli` from `backend/` remain aliases.

When updating dependency pins, regenerate with `uv lock` and check `uv lock --check`.
Keep `.python-version`, package compatibility and installer/documented pins
consistent. Model revisions/hashes are maintained separately from the Python lock.
