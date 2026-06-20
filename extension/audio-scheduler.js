// AudioScheduler: owns the Web Audio graph (AudioContext + gain), schedules the
// decoded chunks against the video clock (anchor-based, gapless), runs the
// pitch-preserving time-stretch (stretch.js), and corrects audio<->video drift
// in a periodic monitor. Decoupled from Session: it reads the shared chunk map
// and a couple of getters, and never calls back into Session.
//
// Extracted from the former Session god-object.
import {
  dlog,
  PITCH_PRESERVE,
  SYNC_TOLERANCE_S,
  SYNC_CHECK_MS,
} from "./settings.js";

export class AudioScheduler {
  /** @param video  the host media element (clock + playbackRate).
   *  @param opts   { chunks: Map (shared, read), getStride: ()=>number (video
   *                  seconds per chunk), getTotalChunks: ()=>number }. */
  constructor(video, opts) {
    this.video = video;
    this.chunks = opts.chunks;
    this._getStride = opts.getStride;
    this._getTotalChunks = opts.getTotalChunks;
    this.audioCtx = null;
    this.gain = null;
    this.activeSources = new Set();
    this._srcByIdx = new Map();
    this._anchorAudio = null;
    this._anchorVideo = null;
    this.stretcher = null;
    this.stretchCache = new Map();
    this._stretchInflight = new Set();
    this._stretchDisabled = false;
    this.syncTimer = null;
    this.disposed = false;
  }

  /** Create the audio graph, load the time-stretcher, start the sync monitor.
   *  AudioContext starts suspended; reschedule() resumes it on play. */
  async init() {
    this.audioCtx = new (window.AudioContext || window.webkitAudioContext)({
      latencyHint: "playback",
    });
    this.gain = this.audioCtx.createGain();
    this.gain.gain.value = 1.0;
    this.gain.connect(this.audioCtx.destination);

    // Load the SoundTouch time-stretcher (stretch.js). If it fails, non-1x
    // playback falls back to resampling (pitch shifts) — stretcher stays null
    // and scheduleChunk's ``stretcher?.available`` check routes to resample.
    if (PITCH_PRESERVE) {
      try {
        const mod = await import(chrome.runtime.getURL("stretch.js"));
        if (this.disposed) return;
        this.stretcher = new mod.StretchClient();
      } catch (err) {
        console.warn(
          "[nomusic] stretch module failed to load; using resample fallback",
          err,
        );
      }
    }
    this.startSyncMonitor();
  }

  /** Decode an encoded chunk into an AudioBuffer using our context. */
  async decode(arrayBuffer) {
    return this.audioCtx.decodeAudioData(arrayBuffer);
  }

  /** Push the effective volume to the output gain (immediate skips the ramp). */
  setVolume(level, immediate) {
    if (!this.gain || !this.audioCtx) return;
    if (immediate) {
      this.gain.gain.value = level;
      return;
    }
    try {
      this.gain.gain.setTargetAtTime(level, this.audioCtx.currentTime, 0.005);
    } catch (_err) {
      this.gain.gain.value = level;
    }
  }

  // -- scheduling -----------------------------------------------------------

  /** Capture the audio<->video clock anchor if not already set. Cleared by
   *  stopAll() so each playback run (after play/seek/rate change) re-anchors
   *  to the current position. */
  _ensureAnchor() {
    if (this._anchorAudio == null) {
      this._anchorAudio = this.audioCtx.currentTime;
      this._anchorVideo = this.video.currentTime;
    }
  }

  scheduleChunk(idx, entry) {
    if (this.disposed || !this.audioCtx) return;
    // Already playing/scheduled this chunk? Don't stack a second copy. Stale
    // sources are cleared by stopAll() (on every seek/pause/rate change), so a
    // present entry here is always the correct, current one.
    if (this._srcByIdx.has(idx)) return;
    const now = this.audioCtx.currentTime;
    const rate = this.video.playbackRate || 1;
    const chunkStart = entry.playStart;
    // The chunk's span on the VIDEO timeline is always the original decoded
    // duration, regardless of how it's stored for playback.
    const origDuration = entry.buffer.duration;
    // The chunk's EXCLUSIVE span on the video timeline: its stride, except the
    // last chunk (no successor) which owns its full decoded duration. The sync
    // monitor uses [chunkStart, chunkEnd) to find the one source covering a
    // given time; using the exclusive span (not the overlapping full duration)
    // keeps exactly one source matching each instant.
    const exclusiveSpan =
      idx < this._getTotalChunks() - 1
        ? this._getStride()
        : origDuration;
    const chunkEnd = chunkStart + exclusiveSpan;

    // Pick the buffer + source rate:
    //  * rate == 1: original buffer at srcRate 1.
    //  * rate != 1 with the stretcher available: a pitch-preserved buffer,
    //    already time-compressed by `rate`, played at srcRate 1.
    //  * rate != 1 without it: original at srcRate = rate, which resamples
    //    (shifts pitch) — the fallback.
    let playBuf = entry.buffer;
    let srcRate = rate;
    if (
      rate !== 1 &&
      PITCH_PRESERVE &&
      !this._stretchDisabled &&
      this.stretcher?.available
    ) {
      const stretched = this._requestStretched(idx, entry, rate);
      if (!stretched) return; // still preparing; rescheduled when it lands
      playBuf = stretched;
      srcRate = 1;
    }

    // start()'s offset is in playBuf's own media time. playBuf covers the same
    // video span [chunkStart, chunkEnd] but may be compressed, so map video
    // seconds -> buffer seconds with spanRatio (1 for the original buffer,
    // == rate for a stretched one).
    const spanRatio = origDuration / playBuf.duration;
    // Anchor-based gapless scheduling. We map video-time -> audio-clock through
    // ONE (audioAnchor, videoAnchor) reference captured per playback run, so
    // every chunk's onset is consistent no matter when — or in what order —
    // it's scheduled. (Recomputing each onset from a freshly-sampled
    // video.currentTime jitters adjacent chunks by tens of ms, producing comb
    // overlaps / gaps at every seam — the stutter.) The drift monitor
    // re-anchors if the audio and video hardware clocks slowly diverge.
    this._ensureAnchor();
    const onsetIdeal =
      this._anchorAudio + (chunkStart - this._anchorVideo) / rate;
    let when;
    let offset;
    if (onsetIdeal >= now) {
      when = onsetIdeal; // future chunk: schedule its onset
      offset = 0;
    } else {
      // Current chunk, already in progress: start now, mid-buffer. The buffer
      // advances at srcRate buffer-seconds per wall second (srcRate 1 for a
      // stretched buffer, == rate for the resample fallback), so the buffer
      // position now is (now - onsetIdeal) * srcRate.
      when = now;
      offset = (now - onsetIdeal) * srcRate;
      if (offset >= playBuf.duration) return; // fully in the past
    }

    // Each chunk's buffer carries chunkOverlapSeconds of the NEXT chunk's
    // audio (the backend overlaps chunks for continuity), so the same span is
    // present in two adjacent buffers. Playing both copies double-plays that
    // tail: in the resample path the copies are sample-identical and aligned
    // (a harmless +6 dB blip), but in the stretch path each chunk is WSOLA'd
    // independently, so the two copies are phase-decorrelated and comb-filter
    // — the "two audios out of sync" stutter. Cap playback to this chunk's
    // exclusive stride so the shared tail plays exactly once. The last chunk
    // has no successor, so it plays its full buffer (its tail is real content,
    // not an overlap).
    const stride = this._getStride();
    let playDur = playBuf.duration - offset; // default: rest of the buffer
    if (idx < this._getTotalChunks() - 1) {
      // stride is in video seconds; spanRatio converts to this buffer's secs.
      playDur = Math.min(playDur, stride / spanRatio - offset);
    }
    if (playDur <= 0) return; // exclusive region already behind us

    const src = this.audioCtx.createBufferSource();
    src.buffer = playBuf;
    src.playbackRate.value = srcRate;
    src.connect(this.gain);
    // Tag with idx so the sync monitor can find the currently-active source,
    // plus the conversion factors it needs to compare against the video clock.
    src._nomusicIdx = idx;
    src._nomusicChunkStart = chunkStart;
    src._nomusicChunkEnd = chunkEnd;
    src._nomusicSrcRate = srcRate;
    src._nomusicSpanRatio = spanRatio;
    // Wall-clock instant this source starts, and how long it actually sounds
    // (offset already consumed; playDur is its trimmed buffer span). The
    // sync/diag monitors use these to tell which source is audible right now.
    src._nomusicStartedAt = when;
    src._nomusicOffsetAtStart = offset;
    src._nomusicPlayDur = playDur;
    src.onended = () => {
      this.activeSources.delete(src);
      if (this._srcByIdx.get(idx) === src) this._srcByIdx.delete(idx);
    };
    try {
      src.start(when, offset, playDur);
    } catch (err) {
      console.warn("[nomusic] scheduling failed", err);
      return;
    }
    this.activeSources.add(src);
    this._srcByIdx.set(idx, src);
    dlog("schedule", {
      idx,
      rate,
      srcRate,
      stretched: srcRate === 1 && rate !== 1,
      delayMs: ((when - now) * 1000).toFixed(0),
      offset: offset.toFixed(3),
      bufDur: playBuf.duration.toFixed(3),
      active: this.activeSources.size,
    });
  }

  /** Return the cached pitch-preserved buffer for (idx, rate), or null while
   *  it is being prepared. NOT a pure getter: on the first request it kicks off
   *  the async stretch and, when the result lands, caches it and reschedules so
   *  it starts playing — hence the imperative ``request`` name. */
  _requestStretched(idx, entry, rate) {
    const key = `${idx}@${rate}`;
    const cached = this.stretchCache.get(key);
    if (cached) return cached;
    if (this._stretchInflight.has(key)) return null;
    this._stretchInflight.add(key);

    const srcBuf = entry.buffer;
    const sr = srcBuf.sampleRate;
    const numCh = srcBuf.numberOfChannels;
    const chunkFrames = srcBuf.length;
    // Stretch each chunk with a short pad of the neighbouring chunks so the
    // WSOLA stretcher has continuity at both edges. Stretched in isolation, a
    // chunk's first ~0.25s is mistimed (the stretcher has no history) and its
    // tail is flushed with silence — a glitch at every chunk boundary. We
    // prepend the previous chunk's tail (warm-up) and append the next chunk's
    // head (real continuation), stretch the whole thing, then discard both
    // pads. Pads are skipped when a neighbour isn't loaded yet (live edge); a
    // re-watch from cache gets the fully-padded, seamless version.
    const padFrames = Math.round(0.5 * sr);
    const prev = this.chunks.get(idx - 1);
    const next = this.chunks.get(idx + 1);
    const leadIn = prev ? Math.min(padFrames, prev.buffer.length) : 0;
    const leadOut = next ? Math.min(padFrames, next.buffer.length) : 0;
    const channels = [];
    for (let c = 0; c < numCh; c++) {
      const mid = srcBuf.getChannelData(c);
      if (!leadIn && !leadOut) {
        channels.push(mid.slice());
        continue;
      }
      const combined = new Float32Array(leadIn + mid.length + leadOut);
      if (leadIn) {
        const pd = prev.buffer.getChannelData(
          Math.min(c, prev.buffer.numberOfChannels - 1),
        );
        combined.set(pd.subarray(pd.length - leadIn), 0);
      }
      combined.set(mid, leadIn);
      if (leadOut) {
        const nd = next.buffer.getChannelData(
          Math.min(c, next.buffer.numberOfChannels - 1),
        );
        combined.set(nd.subarray(0, leadOut), leadIn + mid.length);
      }
      channels.push(combined);
    }

    this.stretcher
      .stretch(channels, rate, sr)
      .then(({ channels: out }) => {
        this._stretchInflight.delete(key);
        if (this.disposed) return;
        // Drop the stretched lead-in / lead-out pads; keep just this chunk's
        // span (~chunkFrames/rate), which now has warmed-up, continuous edges.
        const discardFront = Math.round(leadIn / rate);
        const keepLen = Math.max(1, Math.ceil(chunkFrames / rate));
        const buf = this.audioCtx.createBuffer(out.length, keepLen, sr);
        for (let c = 0; c < out.length; c++) {
          const o = out[c];
          const avail = Math.max(0, Math.min(keepLen, o.length - discardFront));
          if (avail > 0) {
            buf
              .getChannelData(c)
              .set(o.subarray(discardFront, discardFront + avail));
          }
        }
        this.stretchCache.set(key, buf);

        // Play it now if it's still wanted (same rate, still rolling).
        if (!this.video.paused && (this.video.playbackRate || 1) === rate) {
          this.scheduleChunk(idx, entry);
        }
      })
      .catch((err) => {
        this._stretchInflight.delete(key);
        // One failure → stop trying; fall back to resample for this session.
        this._stretchDisabled = true;
        console.warn(
          "[nomusic] stretch failed; falling back to resample (pitch shifts)",
          err,
        );
        if (!this.video.paused) this.reschedule();
      });

    return null;
  }

  reschedule() {
    if (this.disposed) return;
    this.stopAll();
    if (this.video.paused) return;
    // AudioContext starts suspended in some browsers; resume to be safe.
    if (this.audioCtx?.state === "suspended") {
      this.audioCtx.resume();
    }
    for (const [idx, entry] of this.chunks) {
      const t = this.video.currentTime;
      if (entry.playStart + entry.buffer.duration <= t) continue;
      // Schedule chunks within a 30 s look-ahead window; later chunks get
      // scheduled when the look-ahead catches up to them.
      if (entry.playStart - t > 30) continue;
      this.scheduleChunk(idx, entry);
    }
  }

  stopAll() {
    for (const src of this.activeSources) {
      try {
        src.stop();
      } catch (err) {
        dlog("stopAll: source already stopped", err?.name || err);
      }
    }
    this.activeSources.clear();
    this._srcByIdx.clear();
    // Drop the clock anchor so the next playback run re-captures it at the
    // current position (post seek/pause/rate change).
    this._anchorAudio = null;
  }

  startSyncMonitor() {
    const tick = () => {
      if (this.disposed) return;
      this.syncTimer = setTimeout(tick, SYNC_CHECK_MS);
      if (this.video.paused || !this.audioCtx) return;

      // Find the chunk currently covering the video clock.
      const t = this.video.currentTime;
      let active = null;
      for (const src of this.activeSources) {
        if (t >= src._nomusicChunkStart && t < src._nomusicChunkEnd) {
          active = src;
          break;
        }
      }
      if (!active) {
        // No active source for the current time — the chunk may have just
        // become available, or the user scrubbed into uncovered territory.
        this.reschedule();
        return;
      }
      const expectedOffset = t - active._nomusicChunkStart;
      // Express the audio's actual position in video-timeline seconds. The
      // source advances at _nomusicSrcRate buffer-sec per wall second;
      // _nomusicSpanRatio converts buffer-sec back to video-sec (1 for an
      // un-stretched buffer, == rate for a pitch-preserved one).
      const srcRate = active._nomusicSrcRate ?? (this.video.playbackRate || 1);
      const spanRatio = active._nomusicSpanRatio ?? 1;
      const bufferPos =
        (this.audioCtx.currentTime - active._nomusicStartedAt) * srcRate +
        (active._nomusicOffsetAtStart || 0);
      const actualOffset = bufferPos * spanRatio;
      if (
        Number.isFinite(actualOffset) &&
        Math.abs(actualOffset - expectedOffset) > SYNC_TOLERANCE_S
      ) {
        // Drift exceeded tolerance (audio and video clocks have diverged).
        // Re-anchor and reschedule the whole window from the current position
        // rather than restarting one chunk against the now-stale anchor — that
        // keeps every chunk gapless after the correction.
        dlog("sync RESTART (re-anchor)", {
          idx: active._nomusicIdx,
          drift: (actualOffset - expectedOffset).toFixed(3),
          rate: this.video.playbackRate || 1,
        });
        this.reschedule();
        return;
      }
    };
    tick();
  }

  dispose() {
    this.disposed = true;
    this.stopAll();
    if (this.syncTimer) clearTimeout(this.syncTimer);
    this.syncTimer = null;
    try {
      this.audioCtx?.close();
    } catch (err) {
      dlog("dispose: audioCtx already closed", err?.name || err);
    }
    this.audioCtx = null;
    this.stretcher?.dispose();
    this.stretcher = null;
    this.stretchCache.clear();
    this._stretchInflight.clear();
  }
}
