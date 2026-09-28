# Verification

The reference check runs the backend and extension suites, real FFmpeg
regressions, and a controlled browser smoke using the installed backend,
unmodified Manifest V3 extension and real CPU Demucs model. It is the same
command locally and in [CI](../.github/workflows/verify.yml).

## Prepare once, refresh after backend changes

The reference environment is Linux x86_64 with Python 3.12.14, uv 0.12.19,
Node 24.19.0, and FFmpeg. See [installation](installation.md) for the locked
Python profile. Use Node 22 or newer locally; Deno alone cannot run the
extension tests. FFmpeg must include `libopus`, `libmp3lame`, `aac`, `libx264`
and `libvpx-vp9` (the last is used by an existing export regression).

From the repository root:

```sh
./install.sh --profile cpu --dev --skip-system-packages
npm ci --prefix tests/e2e
tests/e2e/node_modules/.bin/playwright install --with-deps chromium
backend/.venv/bin/python scripts/verify.py
```

The Playwright installation command may require sudo to install Linux system
libraries. On a machine with those libraries already present, use
`playwright install chromium` instead. The committed npm lock pins Playwright
and its matching bundled Chromium; do not substitute a system Chrome build.
Playwright uses full headless Chromium with an unpacked extension and a new
persistent profile. Browser output stays muted throughout the check.

The installer fetches and verifies the pinned public model. The verification
runner uses that cache **offline**, including when `HF_HOME` or `HF_HUB_CACHE`
selects a separate model directory. It never fetches weights during a test.
If missing, run `backend/.venv/bin/nomusic models fetch` with the same cache
settings before retrying.

The check requires a non-editable installation whose Python source matches
the checkout. Re-run the installer after backend edits; a stale installation
fails early instead of testing older code. Pytest runs with `--import-mode=importlib`
and checks loaded `nomusic` modules inside the test process against that installed
directory, including modules imported during test execution. A checkout that
shadows the installation fails verification. The installed path is recorded in
`summary.json`; tests, browser helpers and the extension come from the checkout.
Ordinary pytest runs outside this verification command may use an editable
development installation.

## Commands and evidence

```sh
# Complete baseline (default)
backend/.venv/bin/python scripts/verify.py
# Smaller checks during development
backend/.venv/bin/python scripts/verify.py --suite unit
backend/.venv/bin/python scripts/verify.py --suite smoke
# Use another loopback port if the normal helper is already running
backend/.venv/bin/python scripts/verify.py --suite smoke --port 18723
```

Each run creates a new directory under ignored `mds/verification/` and prints
its location. `summary.json` records the result, environment, duration and
backend skips. Step logs, pytest XML, a browser report, screenshots, trace,
fresh media cache, exports and disposable browser profile stay there for
local investigation. Remove a run directory when the runner has stopped and
you no longer need its evidence. Do not publish profiles, traces or raw local
logs without reviewing them for private paths or data.

Any failed check returns a nonzero exit status. A missing FFmpeg codec fails
before pytest can silently skip the associated regression. The only allowed
backend skip is the existing MPS/CUDA batch-equivalence test when no GPU is
available; CPU inference is verified by the smoke instead. Hardware runs
should record their GPU coverage separately.

Tests use fresh media caches and browser profiles, CPU inference with two
model threads, bounded waits, and test-owned subprocess groups. Existing
`NOMUSIC_*` tuning is cleared so it cannot select a private cache or change
the chunk profile. Port conflicts fail without touching the other process.
On success, failure or interruption, the runner stops its children. Normal
backend shutdown must confirm that owned threads drained. A hung process
can be killed by the harness; that cleanup does not count as a successful
normal shutdown. `--timeout-seconds` changes each suite deadline (600 by
default, maximum 1800), with up to 120 seconds allowed for backend cleanup.

## What the smoke establishes

`tests/e2e/fixture.py` synthesizes 12 seconds of stereo harmonic pulses and a
video test pattern. It uses no downloaded or licensed recording. Encoded
bytes may differ across FFmpeg versions; a manifest records their actual
hashes. `tests/e2e/backend.py` replaces **only source acquisition**, accepting
one exact fake YouTube URL and rejecting every other source. This adapter
lives outside the installed application and is never a deployment launcher.

The scenario checks:

- Actual extension settings, service-worker communication and page bridge.
- Ready CPU backend, a fresh processing job, real inference, encoded chunks
  and extension retrieval of those chunks.
- Nonzero processed Web Audio while video advances, backward/forward seeks,
  pause/resume, and playback across the first chunk boundary.
- MP3 and MP4 exports through the extension UI, followed by complete FFmpeg
  decoding, finite/nonzero audio and expected durations.
- Toggle-off disposal and restoration of original video volume, followed by
  normal backend shutdown with no owned threads remaining.

A parallel analyser observes the extension's real output gain in its isolated
world. It preserves the existing audio connections and scheduling methods.
This measures signal before Chromium's output mute; it is **not listening
validation**. Generated media cannot establish speech intelligibility,
separation quality, sustained performance, long-session memory behavior,
upstream YouTube acquisition, or Mac/GPU compatibility. The test page is served
from loopback; it does not exercise a public website requesting Chrome local-network
permission. Permission denial and recovery remain separate browser acceptance
work, planned with playback recovery.

## CI and manual acceptance

CI runs on ordinary pull requests (including stacked PR targets), pushes to
`main`, and manual dispatch. Actions use commit pins and read-only repository
permissions. Both jobs exercise `install.sh --profile cpu` with the committed
Python lock, fetch the pinned model explicitly, and run the real
`nomusic doctor --json` CPU inference check. Linux installs the locked npm browser dependencies
and runs the complete baseline. The `macos-15` Apple Silicon job runs the backend
and extension unit suites, without a browser or MPS inference. No account cookies,
credentials or live-source requests are required. CI does not upload browser profiles,
traces, logs or generated media as artifacts.
On failure, it prints bounded tails of the disposable CI step logs so the
failed check remains diagnosable. Local runs keep their raw logs local.

The Ubuntu/macOS runner images and apt/Homebrew FFmpeg packages receive updates.
Recorded versions, architecture assertions and codec checks make failures
diagnosable; the OS is not a frozen, bit-for-bit build environment. Mac CPU CI
cannot establish MPS acceleration, a native Chrome session, or installer behavior
on a user's existing machine. Those remain hardware acceptance checks.

For a PR changing code or a user flow, automated green checks are followed by
a fresh agent-led manual application session before opening the draft:
inspect settings and readiness, start processing, observe playback and seeks,
use the affected controls, and inspect exports and shutdown. Keep output
muted when testing on a shared or forwarded environment. Record the exact
candidate revision, actions, evidence and limitations locally. A scripted
scenario alone does not satisfy this manual gate.

Live YouTube acquisition and playback remain a separate acceptance check.
When upstream verification or throttling blocks it, record the blocker and
the last successful scope explicitly. A controlled smoke is never reported
as a live-source pass.
