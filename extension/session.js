// Session: drives one <video> at a time. Owns the job lifecycle (SSE), chunk
// fetch+decode, Web Audio scheduling, sync/buffer monitors, and mute/volume.
// Split out of the former monolithic content.js.
import {
  settings,
  dlog,
  SYNC_TOLERANCE_S,
  SYNC_CHECK_MS,
  PITCH_PRESERVE,
} from "./settings.js";
import { MuteController } from "./mute-controller.js";

// StretchClient (pitch-preserving WSOLA time-stretch) lives in stretch.js and
// is dynamically imported by start() the first time a session needs it.

// ---------------------------------------------------------------------------
// Session: drives one <video> at a time. Reused if user toggles off and on.
// ---------------------------------------------------------------------------
class Session {
  constructor(video, button) {
    this.video = video;
    this.button = button;
    this.audioCtx = null;
    this.gain = null;
    // Owns host-video muting + volume mirroring; created in start().
    this.muteController = null;
    this.jobId = null;
    this.totalChunks = 0;
    // Must mirror the backend defaults (config.py: chunk_seconds=10,
    // chunk_overlap_seconds=0.5). fetchCapabilities() overwrites these, but
    // it is best-effort — if it fails these stay in force, and a wrong value
    // throws stride/playStart ~3x off and desyncs every chunk after the first.
    this.chunkSeconds = 10;
    this.chunkOverlapSeconds = 0.5;
    this.duration = 0;
    // idx -> { buffer: AudioBuffer, playStart: number }
    this.chunks = new Map();
    // Pitch-preserve: time-stretched buffers keyed "idx@rate", the SoundTouch
    // client, an in-flight guard, and a kill-switch that trips to the resample
    // fallback if a stretch ever errors. See PITCH_PRESERVE.
    this.stretcher = null;
    this.stretchCache = new Map();
    this._stretchInflight = new Set();
    this._stretchDisabled = false;
    this.activeSources = new Set();
    // At most one live source per chunk index. Guards against the same chunk
    // being scheduled from more than one path (chunk-arrival, reschedule,
    // stretch-completion), which would play two copies offset in time
    // (comb-filter "stutter").
    this._srcByIdx = new Map();
    // Anchor mapping audio-clock <-> video-clock for gapless scheduling.
    // Captured on the first schedule after each stopAll(); null = recapture.
    this._anchorAudio = null;
    this._anchorVideo = null;
    this.fetchedIdx = new Set();
    // SSE stream of backend status (replaces /status polling). Opened in
    // start(); closed in dispose() and when a terminal state arrives.
    this.eventSource = null;
    // True when we closed the stream because the user paused (not a buffer
    // pause). While closed, the backend sees no subscriber and starts its
    // idle-abandon clock; we re-establish the worker + stream on play.
    this._streamPausedClosed = false;
    this.syncTimer = null;
    this.bufferTimer = null;
    this.disposed = false;
    // When true, we (not the user) called video.pause() because the
    // chunk for the current timecode isn't on disk yet. We track this so
    // a manual pause stays paused but a buffering pause auto-resumes.
    this._pausedByUs = false;
    // Flipped true once the SSE stream ends (state == ready/error, or the
    // server closed it). Tells _resumeAfterBuffer that no future status
    // event will repaint the label, so it has to restore "nomusic on".
    this._streamEnded = false;
    // Debounce timer for the /prioritize POST on seek so scrubbing a
    // timeline doesn't fire one request per intermediate frame.
    this._prioritizeTimer = null;
    this._boundHandlers = {
      play: () => {
        this.reschedule();
        this._onUserPlay();
      },
      pause: () => {
        this.stopAll();
        this._onUserPause();
      },
      seeking: () => this.stopAll(),
      seeked: () => {
        dlog("seeked", {
          currentTime: this.video.currentTime,
          chunk: this._chunkIdxForTime(this.video.currentTime),
          buffered: this._isBuffered(this.video.currentTime),
          pausedByUs: this._pausedByUs,
          videoPaused: this.video.paused,
        });
        this.reschedule();
        this._reconcileBufferState();
        this._sendPrioritizeHint();
      },
      ratechange: () => this.reschedule(),
      emptied: () => this.dispose(),
      volumechange: () => this.muteController?.handleHostVolumeChange(),
    };
  }

  async start() {
    let info;
    try {
      info = await this.requestJob();
    } catch (err) {
      console.warn("[nomusic] /process failed", err);
      this.button.setError("backend unreachable");
      // Mark the session terminal but leave the error visual alone so
      // the auto-revert timer can do its 2.5s display. Without this the
      // first post-error click would just dispose this dead session
      // instead of starting a fresh one.
      this.dispose({ preserveButtonState: true });
      return;
    }
    // start() awaits above (the multi-second probe). If the session was
    // disposed meanwhile (a second click / toggle started a new session),
    // abort — otherwise this disposed session would go on to create its own
    // AudioContext and play in parallel with the live one, two music-removed
    // streams slightly offset = comb-filter "stutter".
    if (this.disposed) return;

    this.jobId = info.job_id;
    this.totalChunks = info.total_chunks || 1;
    this.duration = info.duration_seconds || 0;
    this.button.showStatus(info);

    try {
      const caps = await this.fetchCapabilities();
      this.chunkSeconds = caps?.defaults?.chunk_seconds ?? this.chunkSeconds;
      this.chunkOverlapSeconds =
        caps?.defaults?.chunk_overlap_seconds ?? this.chunkOverlapSeconds;
    } catch (_err) {
      // capabilities is best-effort; defaults are reasonable.
    }
    if (this.disposed) return; // re-check after the second await (see above).

    // AudioContext needs a user gesture; the click that triggered .start()
    // satisfies that requirement on every Chromium-derived browser.
    this.audioCtx = new (window.AudioContext || window.webkitAudioContext)({
      latencyHint: "playback",
    });
    this.gain = this.audioCtx.createGain();
    this.gain.gain.value = 1.0;
    this.gain.connect(this.audioCtx.destination);

    // Set up the SoundTouch time-stretcher (stretch.js, dynamically imported
    // so the library only loads when pitch-preserve is on). If the import
    // ever fails, non-1x playback falls back to resampling (pitch shifts)
    // without breaking — the stretcher stays null and scheduleChunk's
    // ``stretcher?.available`` check routes to the resample path.
    if (PITCH_PRESERVE && !this.stretcher) {
      try {
        const mod = await import(chrome.runtime.getURL("stretch.js"));
        if (this.disposed) return; // re-check after the import await
        this.stretcher = new mod.StretchClient();
      } catch (err) {
        console.warn(
          "[nomusic] stretch module failed to load; using resample fallback",
          err,
        );
      }
    }

    // Mute the host video and mirror its volume onto our gain. The callback is
    // how MuteController pushes the effective level to our audio output.
    this.muteController = new MuteController(this.video, (level, immediate) => {
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
    });
    this.muteController.mute();
    // Pause the host video until chunk 0 is on disk; resume from the
    // chunk-fetch handler. Better than playing silent: the user doesn't
    // miss any seconds of content while the first chunk is being made.
    // This is the initial pause — we deliberately don't relabel the
    // button to "Buffering" here because the live phase label
    // (Downloading / Removing music %) is more informative.
    this._pauseForBuffer({ showBufferingLabel: false });
    this.attachVideoListeners();
    // Tell the backend to start where the user actually is, not at
    // chunk 0 — handles YouTube's "resume from history", &t=NNN URL
    // params, and any pre-scrub before the user clicked nomusic. The
    // hint is debounced 250 ms, which still lands well before the
    // probe + download phases finish on the backend.
    const startChunk = this._chunkIdxForTime(this.video.currentTime);
    dlog("session start", {
      currentTime: this.video.currentTime,
      chunk: startChunk,
      totalChunks: this.totalChunks,
      chunkSeconds: this.chunkSeconds,
    });
    this._sendPrioritizeHint();
    // Open the status stream now — after audioCtx + capabilities exist, so
    // the first event (especially a cached replay's immediate terminal
    // event) can decode chunks with the right stride. This is still within
    // a few hundred ms of /process, far inside the backend's idle window.
    this._openEventStream();
    this.startSyncMonitor();
    this.startBufferMonitor();
  }

  async requestJob() {
    const body = { url: window.location.href };
    if (settings.model) body.model = settings.model;
    if (settings.keepStems) body.keep_stems = settings.keepStems;
    const resp = await fetch(`${settings.backendUrl}/process`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const detail = await resp.text().catch(() => "");
      throw new Error(`${resp.status}: ${detail}`);
    }
    return resp.json();
  }

  async fetchCapabilities() {
    const resp = await fetch(`${settings.backendUrl}/capabilities`);
    if (!resp.ok) return null;
    return resp.json();
  }

  /** Subscribe to the backend's SSE status stream. EventSource handles
   *  reconnection on transient network drops on its own; the backend
   *  returns 204 for an unknown job, which drives readyState to CLOSED and
   *  stops the reconnect loop. */
  _openEventStream() {
    if (this.disposed) return;
    this.eventSource = new EventSource(
      `${settings.backendUrl}/events/${this.jobId}`,
    );
    this.eventSource.onmessage = (e) => {
      let payload;
      try {
        payload = JSON.parse(e.data);
      } catch (err) {
        console.warn("[nomusic] bad SSE payload", err);
        return;
      }
      this.handleStatus(payload);
    };
    this.eventSource.onerror = () => {
      // A 204 (unknown job) or our own .close() on a terminal state puts
      // readyState at CLOSED — there's no more stream to wait on. A
      // transient drop instead sits in CONNECTING while EventSource retries,
      // so we leave _streamEnded alone in that case.
      if (
        this.eventSource &&
        this.eventSource.readyState === EventSource.CLOSED
      ) {
        this._streamEnded = true;
      }
    };
  }

  /** User paused the video (not a buffer pause). Close the status stream so
   *  the backend sees no subscriber and starts its idle-abandon countdown —
   *  if the user stays away, the worker releases the GPU. Re-established on
   *  play. No-op during a buffer pause: we still need chunk-ready events to
   *  know when to resume, and a fully-processed job has no worker to idle. */
  _onUserPause() {
    if (this.disposed || this._pausedByUs || this._streamEnded) return;
    // A queued download pins the worker: keep the stream open on pause so the
    // track finishes processing and the file is delivered even though the user
    // stopped watching. The pill keeps showing "Preparing N%".
    if (this.button && this.button._pendingDownload) return;
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
    }
    this._streamPausedClosed = true;
    // Replace the frozen live label (e.g. "Removing music 41%") with a
    // "Paused" signal so it's clear we've stopped, not stalled.
    this.button.setPaused();
  }

  /** A download was requested before the track finished. Make sure the worker
   *  is running and the stream is open so processing continues to completion,
   *  even if the user had already paused (which would normally let it idle). */
  ensureLiveForDownload() {
    if (this.disposed || this._streamEnded) return;
    if (!this.eventSource) {
      this._streamPausedClosed = false;
      this._resumeProcessing(); // respawn the worker + reopen the stream
    }
  }

  /** User resumed after a pause that closed the stream. Re-ensure a worker
   *  exists (it may have been abandoned while paused; /process respawns it
   *  from disk-cached progress) and reopen the status stream. */
  _onUserPlay() {
    if (this.disposed || this._streamEnded || !this._streamPausedClosed) return;
    this._streamPausedClosed = false;
    this._resumeProcessing();
  }

  async _resumeProcessing() {
    try {
      await this.requestJob();
    } catch (_err) {
      // Backend unreachable on resume; reopening the stream below will
      // surface the failure (204/CLOSED) without crashing playback.
    }
    if (this.disposed) return;
    if (!this.eventSource) this._openEventStream();
    // Re-point the worker at where the user actually is, in case it was
    // abandoned and respawned with a from-scratch chunk order.
    this._sendPrioritizeHint();
  }

  /** Apply one status snapshot (initial or pushed). Mirrors what the old
   *  poll loop did per tick: repaint the label, fetch any newly-ready
   *  chunks, and tear down on a terminal state. */
  handleStatus(status) {
    if (this.disposed) return;
    this.totalChunks = status.total_chunks || this.totalChunks;
    // Always reflect the backend phase in the label, even while we're
    // paused for buffering — the pulsing icon + paused video already
    // convey "waiting", and the phase label is more useful content.
    this.button.showStatus(status);

    if (status.state === "error") {
      this._streamEnded = true;
      if (this.eventSource) this.eventSource.close();
      this.dispose({ preserveButtonState: true });
      return;
    }

    // Fetch any newly-ready chunk. With seek-driven reordering, ready
    // chunks are no longer contiguous from 0, so we iterate the explicit
    // ``ready_chunks`` set the backend sends. Each chunk knows its own
    // play_start, so order doesn't matter. fetchedIdx dedups across the
    // repeated snapshots SSE delivers.
    const readyChunks = Array.isArray(status.ready_chunks)
      ? status.ready_chunks
      : [];
    for (const i of readyChunks) {
      if (!this.fetchedIdx.has(i)) {
        this.fetchedIdx.add(i);
        this.fetchAndQueueChunk(i);
      }
    }

    if (status.state === "ready") {
      this._streamEnded = true;
      if (this.eventSource) this.eventSource.close();
    }
  }

  async fetchAndQueueChunk(idx) {
    try {
      // ``cache: "default"`` lets the browser honor the backend's
      // Cache-Control header (max-age=86400 for 200, no-store for 425).
      // ``force-cache`` is wrong here because it returns ANY cached
      // response unconditionally — so a 425 from "chunk not ready yet"
      // gets remembered forever and poisons every retry, even after the
      // chunk lands on disk. That manifested as seek-backwards getting
      // stuck on chunks the backend had finished long ago.
      const resp = await fetch(
        `${settings.backendUrl}/chunk/${this.jobId}/${idx}`,
        { cache: "default" },
      );
      if (!resp.ok) {
        // Drop the dedup mark so the next SSE snapshot that re-lists this
        // chunk in ready_chunks re-attempts the fetch.
        this.fetchedIdx.delete(idx);
        return;
      }
      const buf = await resp.arrayBuffer();
      const decoded = await this.audioCtx.decodeAudioData(buf);
      const stride = this.chunkSeconds - this.chunkOverlapSeconds;
      const entry = {
        buffer: decoded,
        playStart: idx * stride,
      };
      this.chunks.set(idx, entry);
      dlog("chunk arrived", {
        idx,
        currentTime: this.video.currentTime,
        currentChunk: this._chunkIdxForTime(this.video.currentTime),
        pausedByUs: this._pausedByUs,
        videoPaused: this.video.paused,
      });
      // If we paused because this chunk wasn't ready, resume now.
      if (this._pausedByUs && this._isBuffered(this.video.currentTime)) {
        this._resumeAfterBuffer();
      }
      if (!this.video.paused && !this.disposed) {
        this.scheduleChunk(idx, entry);
      }
    } catch (err) {
      console.warn(`[nomusic] chunk ${idx} fetch/decode failed`, err);
      this.fetchedIdx.delete(idx);
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
      idx < this.totalChunks - 1
        ? this.chunkSeconds - this.chunkOverlapSeconds
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
      const stretched = this._getStretched(idx, entry, rate);
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
    const stride = this.chunkSeconds - this.chunkOverlapSeconds;
    let playDur = playBuf.duration - offset; // default: rest of the buffer
    if (idx < this.totalChunks - 1) {
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

  /** Return the pitch-preserved buffer for (idx, rate), or null while it is
   *  being prepared. On first request it kicks off the async stretch and,
   *  when the result lands, caches it and reschedules so it starts playing. */
  _getStretched(idx, entry, rate) {
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
      } catch (_err) {
        /* already stopped */
      }
    }
    this.activeSources.clear();
    this._srcByIdx.clear();
    // Drop the clock anchor so the next playback run re-captures it at the
    // current position (post seek/pause/rate change).
    this._anchorAudio = null;
  }

  // -- buffer pause/resume -------------------------------------------------

  _chunkIdxForTime(t) {
    const stride = this.chunkSeconds - this.chunkOverlapSeconds;
    return Math.max(0, Math.floor(t / stride));
  }

  _isBuffered(t) {
    return this.chunks.has(this._chunkIdxForTime(t));
  }

  _pauseForBuffer({ showBufferingLabel = true } = {}) {
    if (this._pausedByUs || this.disposed) return;
    this._pausedByUs = true;
    // Initial pause at session start (showBufferingLabel:false) leaves the
    // live phase label alone — the user wants to see Downloading /
    // Removing music %. A mid-watch buffer pause (the default) overrides
    // with "Buffering" since at that point the user has been watching
    // happily and needs to know why playback stopped.
    if (showBufferingLabel) this.button.setBuffering();
    dlog("pauseForBuffer", {
      currentTime: this.video.currentTime,
      chunk: this._chunkIdxForTime(this.video.currentTime),
      showBufferingLabel,
    });
    try {
      this.video.pause();
    } catch (_err) {
      /* element gone */
    }
  }

  _resumeAfterBuffer() {
    if (!this._pausedByUs || this.disposed) return;
    this._pausedByUs = false;
    dlog("resumeAfterBuffer", {
      currentTime: this.video.currentTime,
      chunk: this._chunkIdxForTime(this.video.currentTime),
    });
    // While the SSE stream is live, the next status event overwrites the
    // "Buffering" label naturally. Once the stream has ended (state was
    // ready or error) we have to restore the active label ourselves.
    if (this._streamEnded && this.button.el.dataset.state === "working") {
      this.button.showStatus({ state: "ready" });
    }
    try {
      const p = this.video.play();
      if (p && typeof p.catch === "function") {
        p.catch((err) => dlog("video.play() rejected", err?.name || err));
      }
    } catch (_err) {
      /* element gone */
    }
  }

  /** After the user seeks, ask the backend to process the chunk at the
   *  new position next (then onward, then loop back). Debounced so a
   *  scrub doesn't generate dozens of POSTs. No-op once the stream has
   *  ended because the worker is already done. */
  _sendPrioritizeHint() {
    if (this.disposed || this._streamEnded || !this.jobId) return;
    if (this._prioritizeTimer) clearTimeout(this._prioritizeTimer);
    this._prioritizeTimer = setTimeout(() => {
      this._prioritizeTimer = null;
      if (this.disposed || this._streamEnded || !this.jobId) return;
      const fromChunk = this._chunkIdxForTime(this.video.currentTime);
      dlog("prioritize POST", { fromChunk, currentTime: this.video.currentTime });
      fetch(`${settings.backendUrl}/process/${this.jobId}/prioritize`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ from_chunk: fromChunk }),
      })
        .then((r) => dlog("prioritize response", r.status))
        .catch((err) => dlog("prioritize POST failed", err));
    }, 250);
  }

  /** Bidirectional buffer-state reconciliation. Called every
   *  SYNC_CHECK_MS by the buffer monitor and synchronously from the
   *  ``seeked`` handler for immediate response.
   *
   *  Three jobs:
   *    1. Heal a stale ``_pausedByUs`` flag — if the user un-paused the
   *       video via the host site's controls, our flag is now lying
   *       about who owns the pause.
   *    2. Resume when we paused for buffer and the current chunk has
   *       since landed in ``this.chunks`` (covers the case where a seek
   *       arrives into already-buffered territory while we still hold a
   *       prior pause).
   *    3. Pause when we're playing and the current chunk isn't buffered. */
  _reconcileBufferState() {
    if (this.disposed) return;
    if (this._pausedByUs && !this.video.paused) {
      // User overrode us. Don't keep claiming ownership of the pause.
      dlog("reconcile: healing stale _pausedByUs (video resumed externally)");
      this._pausedByUs = false;
    }
    const buffered = this._isBuffered(this.video.currentTime);
    if (this._pausedByUs && buffered) {
      dlog("reconcile: chunk arrived under our pause -> resume");
      this._resumeAfterBuffer();
      return;
    }
    if (this.video.paused || this._pausedByUs) return;
    if (!buffered) this._pauseForBuffer();
  }

  /** rAF-rate check: drives ``_reconcileBufferState`` every
   *  SYNC_CHECK_MS so playback recovers from any state desync within
   *  one tick. Cheap (one branch + a Map.has per tick). */
  startBufferMonitor() {
    const tick = () => {
      if (this.disposed) return;
      this.bufferTimer = setTimeout(tick, SYNC_CHECK_MS);
      this._reconcileBufferState();
    };
    tick();
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

  // -- video glue -----------------------------------------------------------

  attachVideoListeners() {
    for (const [name, handler] of Object.entries(this._boundHandlers)) {
      this.video.addEventListener(name, handler);
    }
    // If the video is already playing, schedule immediately.
    if (!this.video.paused) this.reschedule();
  }

  detachVideoListeners() {
    for (const [name, handler] of Object.entries(this._boundHandlers)) {
      this.video.removeEventListener(name, handler);
    }
  }

  dispose({ preserveButtonState = false } = {}) {
    if (this.disposed) return;
    this.disposed = true;
    this.detachVideoListeners();
    this.stopAll();
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
    }
    if (this.syncTimer) clearTimeout(this.syncTimer);
    if (this.bufferTimer) clearTimeout(this.bufferTimer);
    if (this._prioritizeTimer) clearTimeout(this._prioritizeTimer);
    // If we paused for buffering, let the video resume now that we're
    // letting go of it — otherwise it would stay paused with no audio
    // override and the user would have to hit play themselves.
    const resumeOnExit = this._pausedByUs;
    this._pausedByUs = false;
    this.muteController?.dispose();
    this.muteController = null;
    if (resumeOnExit) {
      try {
        const p = this.video.play();
        if (p && typeof p.catch === "function") p.catch(() => {});
      } catch (_err) {
        /* noop */
      }
    }
    try {
      this.audioCtx?.close();
    } catch (_err) {
      /* already closed */
    }
    this.audioCtx = null;
    this.chunks.clear();
    this.fetchedIdx.clear();
    this.stretcher?.dispose();
    this.stretcher = null;
    this.stretchCache.clear();
    this._stretchInflight.clear();
    // Error paths set the button to "error" and rely on its own
    // auto-revert timer for the visual transition. Calling button.dispose
    // here would clobber that.
    if (!preserveButtonState) this.button.dispose();
  }
}

export { Session };
