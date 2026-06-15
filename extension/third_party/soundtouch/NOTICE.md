# SoundTouchJS (vendored)

WSOLA time-stretch library used by nomusic to keep audio at the correct pitch
when the video is played at non-1x speed.

- **Upstream:** <https://github.com/cutterbl/SoundTouchJS>
- **Source:** npm package `@soundtouchjs/core@2.0.4`
- **License:** MPL-2.0 — see [`LICENSE`](./LICENSE). Full text: <https://mozilla.org/MPL/2.0/>.

## Files

| File | Origin |
| --- | --- |
| `Stretch.js` | Verbatim from the package's `.dist/`. The WSOLA time-stretch stage. |
| `AbstractSamplePipe.js` | Verbatim. Base class Stretch extends. |
| `CircularSampleBuffer.js` | Verbatim. Sample buffer used by Stretch. |
| `FifoSampleBuffer.js` | Verbatim. Sample buffer used by Stretch. |
| `README.md` / `LICENSE` | Upstream, verbatim. |

All files are unmodified.

## Why only these four files

We import `Stretch.js` **directly** rather than the package's `index.js`. We
only need the WSOLA *time-stretch* stage (change tempo, preserve pitch); we do
not use the package's `RateTransposer` / pitch-shift path. `index.js` eagerly
pulls in an interpolation-strategy plugin (`@soundtouchjs/interpolation-strategy-lanczos`)
that the rate transposer needs, which `Stretch.js` does not. Importing the
stage directly keeps the vendored surface to these four self-contained files
with no further dependencies.

## Updating

Replace the four files from a newer `@soundtouchjs/core` release's `.dist/`
(keep `Stretch.js` + its transitive `./*.js` imports), and update the version
above. If a future release changes `Stretch.js`'s relative imports, vendor the
new closure accordingly.
