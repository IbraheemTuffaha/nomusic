// ---------------------------------------------------------------------------
// StretchClient: pitch-preserving time-stretch via the vendored SoundTouch
// library (WSOLA). Pure JavaScript, run synchronously on the main thread — no
// Worker/worklet/WASM/CSP concerns. Each stretch() runs the WSOLA
// feed/process/flush loop over one chunk and returns a buffer ~1/rate its
// length with the pitch preserved. See third_party/soundtouch/.
//
// Loaded as an ES module (dynamically imported by content.js the first time a
// session needs it), so the library only loads when pitch-preserve is actually
// used. The static import below pulls in only Stretch.js + its three buffer
// helpers — not the package index, which would also drag in the rate
// transposer's interpolation-strategy dependency we don't use.
// ---------------------------------------------------------------------------
import Stretch from "./third_party/soundtouch/Stretch.js";

// WSOLA time-stretch tuning for speech/vocals. sequence/seek/overlap in ms.
//
// SoundTouch's auto/default tuning (sequence ~90 ms, overlap 8 ms at 2x) copies
// a long *verbatim* middle per window and then jumps — on speech that jump is
// an audible stutter ("two voices / chopping"). We instead use a short window
// with ~50% overlap (overlap ≈ sequence/2 → no verbatim middle, every output
// sample is a crossfade), which is how ffmpeg's `atempo` WSOLA is shaped. This
// was validated against atempo and rubberband on real demucs-vocal chunks: a
// spectral-similarity-to-atempo metric rose from ~0.91 (old params, audibly
// stuttering) to ~0.95 (these params, matching the clean references), and the
// per-chunk seam discontinuity dropped from ~55x to ~5x the median sample step.
//
// quickSeek MUST stay true: the exhaustive (quickSeek=false) seek measured
// ~45 ms vs ~950 ms per chunk here — synchronous on the main thread, the slow
// path froze the page, and it did not improve quality.
const WSOLA_SEQUENCE_MS = 40;
const WSOLA_SEEK_MS = 15;
const WSOLA_OVERLAP_MS = 20;
const WSOLA_QUICK_SEEK = true;

export class StretchClient {
  // Kept for API symmetry with the resample fallback path: callers check
  // ``stretcher?.available`` before using it. The module only exists once its
  // import resolved, so by the time anyone holds an instance it is available;
  // a per-call failure is surfaced by stretch() rejecting instead.
  get available() {
    return true;
  }

  /** Stretch one chunk to ~1/rate its length, pitch preserved. ``channels``
   *  is one Float32Array per channel (1 or 2). Resolves ``{channels}``.
   *  Rejects on an internal error, so the caller can fall back to resampling. */
  async stretch(channels, rate, sampleRate) {
    const inFrames = channels[0].length;
    const stereo = channels.length >= 2;
    const L = channels[0];
    const R = stereo ? channels[1] : channels[0];

    // SoundTouch's WSOLA stage operates on interleaved stereo frames.
    const inter = new Float32Array(inFrames * 2);
    for (let i = 0; i < inFrames; i++) {
      inter[2 * i] = L[i];
      inter[2 * i + 1] = R[i];
    }

    const st = new Stretch({ createBuffers: true });
    // Short window + ~50% overlap (no verbatim middle) — see the constants above.
    st.setParameters(sampleRate, WSOLA_SEQUENCE_MS, WSOLA_SEEK_MS, WSOLA_OVERLAP_MS);
    st.tempo = rate; // tempo > 1 shortens (faster); pitch is preserved
    try {
      st.quickSeek = WSOLA_QUICK_SEEK;
    } catch (err) {
      // Older SoundTouch builds may not expose this setter; the default is fine.
      console.debug("[nomusic] quickSeek unsupported by this SoundTouch build", err);
    }

    const target = Math.max(1, Math.ceil(inFrames / rate));
    const outL = new Float32Array(target);
    const outR = stereo ? new Float32Array(target) : null;
    let outFrames = 0;
    const TMP = 8192;
    const tmp = new Float32Array(TMP * 2);
    const drain = () => {
      while (st.outputBuffer.frameCount > 0 && outFrames < target) {
        const want = Math.min(TMP, st.outputBuffer.frameCount, target - outFrames);
        st.outputBuffer.receiveSamples(tmp, want);
        for (let i = 0; i < want; i++) {
          outL[outFrames + i] = tmp[2 * i];
          if (outR) outR[outFrames + i] = tmp[2 * i + 1];
        }
        outFrames += want;
      }
    };

    const BLK = 4096;
    for (let off = 0; off < inFrames && outFrames < target; off += BLK) {
      const n = Math.min(BLK, inFrames - off);
      st.inputBuffer.putSamples(inter.subarray(off * 2, (off + n) * 2), 0, n);
      st.process();
      drain();
    }
    // Flush: push trailing zeros so the final WSOLA window is emitted, until
    // we've collected the full target length (WSOLA leaves a tail otherwise,
    // which would show up as a small gap at each chunk boundary).
    const flush = new Float32Array(BLK * 2);
    let guard = 0;
    while (outFrames < target && guard++ < 64) {
      st.inputBuffer.putSamples(flush, 0, BLK);
      st.process();
      drain();
    }

    const out = outR ? [outL, outR] : [outL];
    return { channels: out };
  }

  dispose() {
    // Nothing persistent: the import is browser-cached and Stretch instances
    // are per-call and garbage-collected.
  }
}
