// nomusic content script.
//
// What it does on any page:
//   1. Watches the DOM for <video> elements (handles SPA navigation).
//   2. Attaches a small floating button to each discovered <video>.
//   3. On click: tells the local backend to process the page URL, mutes the
//      <video>, fetches separated audio chunks as they become ready, and
//      schedules them through Web Audio so they play in sync.
//
// Why Web Audio (not <audio>): the backend serves WAV chunks. WAV doesn't fit
// MSE's container model. AudioBufferSourceNode lets us schedule chunks at exact
// sample offsets so the seam is gapless and the result tracks the video clock.
//
// Sync strategy: every wall-clock event (play/pause/seek/ratechange) tears
// down the active source nodes and reschedules from the current video.time.
// Drift between the audio clock and the video clock is corrected in a 250 ms
// monitor loop: if they diverge by more than 80 ms we restart the active
// chunk's source node at the corrected offset.

(() => {
  if (window.__nomusicLoaded) return;
  window.__nomusicLoaded = true;

  const DEFAULT_BACKEND = "http://127.0.0.1:8723";
  const SYNC_TOLERANCE_S = 0.08;
  const SYNC_CHECK_MS = 250;

  // Pitch-preserving playback at non-1x speeds. When true, each chunk is
  // time-stretched (pitch preserved) by the vendored SoundTouch library
  // (third_party/soundtouch/) and scheduled at srcRate 1, matching the native
  // player. When false — or if the library fails to load — playback falls back
  // to resampling, which keeps sync but shifts pitch.
  const PITCH_PRESERVE = true;
  // Debug logging for seek/buffer/prioritize state transitions. Off in
  // shipped builds; flip to ``true`` locally to trace state in DevTools
  // (every line is prefixed with [nomusic]).
  const DEBUG = false;
  const dlog = DEBUG
    ? (...args) => console.log("[nomusic]", ...args)
    : () => {};

  // ---------------------------------------------------------------------------
  // settings (cached in-memory; chrome.storage drives the popup)
  // ---------------------------------------------------------------------------
  const settings = {
    backendUrl: DEFAULT_BACKEND,
    model: null,
    keepStems: null,
  };

  async function loadSettings() {
    try {
      const stored = await chrome.storage.sync.get([
        "backendUrl",
        "model",
        "keepStems",
      ]);
      if (stored.backendUrl) settings.backendUrl = stored.backendUrl;
      if (stored.model !== undefined) settings.model = stored.model;
      if (stored.keepStems !== undefined) settings.keepStems = stored.keepStems;
    } catch (_err) {
      // Storage permission missing? Fall back to defaults.
    }
  }

  chrome.storage?.onChanged?.addListener?.((changes) => {
    if (changes.backendUrl) settings.backendUrl = changes.backendUrl.newValue;
    if (changes.model) settings.model = changes.model.newValue;
    if (changes.keepStems) settings.keepStems = changes.keepStems.newValue;
  });

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
      this.muteAsserter = null;
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
      this.originalMuted = video.muted;
      this.originalVolume = video.volume;
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
        volumechange: () => this._onHostVolumeChange(),
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

      this.muteVideo();
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

    muteVideo() {
      this.originalMuted = this.video.muted;
      this.originalVolume = this.video.volume;

      // What we track across host events:
      //   _userVolume — last slider position the user set (0..1). Survives
      //                 mute toggles so unmuting restores the right level.
      //   _lastMuted  — last muted state we observed. Lets the volumechange
      //                 handler detect mute/unmute clicks (which can fire
      //                 without a volume change).
      this._userVolume =
        this.originalVolume > 0 ? this.originalVolume : 1.0;
      this._lastMuted = this.originalMuted;

      // We override only ``volume`` and pin it to 0 via the prototype
      // setter + rAF re-assertion. ``muted`` is left alone: audio is
      // already silent because volume is 0, and reading video.muted gives
      // us the user's real mute intent (needed by the volumechange path).
      const proto = HTMLMediaElement.prototype;
      const volumeDesc = Object.getOwnPropertyDescriptor(proto, "volume");
      this._realSetVolume = (v) => volumeDesc.set.call(this.video, v);
      this._realGetVolume = () => volumeDesc.get.call(this.video);

      // Seed our processed-audio gain to whatever the user was listening
      // at, so clicking the button doesn't jump the loudness.
      if (this.gain) {
        this.gain.gain.value = this.originalMuted
          ? 0
          : Math.max(0, Math.min(1, this.originalVolume));
      }

      // Hook the main-world page-script setter patch. Each volume write
      // the host page performs lands here as a CustomEvent before the
      // audio renderer can pick up a non-zero value, so there is no
      // bleed window. The patched setter pins the underlying volume to 0
      // for us.
      this._volIntentHandler = (e) => {
        if (!e || !e.detail) return;
        const { volume, muted } = e.detail;
        if (typeof volume === "number" && volume > 0) {
          this._userVolume = volume;
        }
        if (typeof muted === "boolean") this._lastMuted = muted;
        this._applyEffectiveVolume();
      };
      this.video.addEventListener(
        "nomusic:vol-intent",
        this._volIntentHandler,
      );

      // Flag this element so page-script.js knows to intercept its
      // volume writes. dataset writes propagate to the main world via
      // the shared DOM.
      this.video.dataset.nomusicVolBlock = "1";

      // Initial pin (also exercises the page-script path).
      this._realSetVolume(0);

      // Fallback rAF re-assertion. If page-script.js failed to load
      // (e.g. a browser that disables MAIN-world content scripts), we
      // still keep underlying volume pinned. Cheap: one comparison +
      // at most one setter call per frame.
      const tick = () => {
        if (this.disposed) return;
        try {
          if (this._realGetVolume() !== 0) this._realSetVolume(0);
        } catch (_err) {
          /* element detached */
        }
        this.muteAsserter = requestAnimationFrame(tick);
      };
      this.muteAsserter = requestAnimationFrame(tick);

      // YouTube's player updates volume via its own state machine and
      // sometimes doesn't round-trip through video.volume at all, so the
      // volumechange listener never sees the change. They do persist the
      // setting to localStorage on every interaction; we poll that as a
      // redundant signal.
      this._startYouTubeVolumePoll();
    }

    _startYouTubeVolumePoll() {
      if (!/(?:^|\.)youtube\.com$/.test(location.hostname)) return;

      const readYT = () => {
        try {
          const raw = localStorage.getItem("yt-player-volume");
          if (!raw) return null;
          const wrapper = JSON.parse(raw);
          const data =
            typeof wrapper.data === "string"
              ? JSON.parse(wrapper.data)
              : wrapper.data;
          return {
            volume: Math.max(0, Math.min(1, (data.volume ?? 100) / 100)),
            muted: !!data.muted,
          };
        } catch (_err) {
          return null;
        }
      };

      let last = null;
      const tick = () => {
        if (this.disposed) return;
        this._ytVolTimer = setTimeout(tick, 200);
        const yt = readYT();
        if (!yt) return;
        if (last && last.volume === yt.volume && last.muted === yt.muted) {
          return;
        }
        last = yt;
        if (yt.volume > 0) this._userVolume = yt.volume;
        this._lastMuted = yt.muted;
        this._applyEffectiveVolume();
      };
      tick();
    }

    /** Push the current user intent (volume + mute) into the processed-
     *  audio gain. Short setTargetAtTime ramp avoids zipper noise on fast
     *  slider drags. */
    _applyEffectiveVolume() {
      if (!this.gain || !this.audioCtx) return;
      const effective = this._lastMuted ? 0 : this._userVolume;
      try {
        this.gain.gain.setTargetAtTime(
          effective,
          this.audioCtx.currentTime,
          0.005,
        );
      } catch (_err) {
        this.gain.gain.value = effective;
      }
    }

    /**
     * Fires whenever the host page changes volume or muted on the <video>.
     * We update our intent state (_userVolume from non-zero volume reads,
     * _lastMuted from any mute change) and push the effective volume to
     * our gain. Then we re-silence the host's volume so the original audio
     * stays off. The 'muted' attribute is left alone — the page can flip
     * it freely; volume=0 is what actually keeps the host silent.
     */
    _onHostVolumeChange() {
      if (this.disposed || !this._realGetVolume || !this.gain) return;
      const realVol = this._realGetVolume();
      const muted = this.video.muted;

      let changed = false;
      if (realVol > 0) {
        // Page set a real volume — that's the user's intent. (When the
        // page reads back 0 it's our pin, so it doesn't tell us anything.)
        this._userVolume = realVol;
        changed = true;
      }
      if (muted !== this._lastMuted) {
        this._lastMuted = muted;
        changed = true;
      }

      if (changed) this._applyEffectiveVolume();

      if (realVol !== 0) {
        try {
          this._realSetVolume(0);
        } catch (_err) {
          /* element gone */
        }
      }
    }

    unmuteVideo() {
      if (this.muteAsserter) cancelAnimationFrame(this.muteAsserter);
      this.muteAsserter = null;
      if (this._ytVolTimer) clearTimeout(this._ytVolTimer);
      this._ytVolTimer = null;

      if (this._volIntentHandler) {
        this.video.removeEventListener(
          "nomusic:vol-intent",
          this._volIntentHandler,
        );
        this._volIntentHandler = null;
      }

      // Releasing the data attribute makes the main-world setter patch a
      // no-op for this element again. Future volume writes flow through
      // normally.
      delete this.video.dataset.nomusicVolBlock;

      if (this._realSetVolume) this._realSetVolume(this.originalVolume);
    }

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
      this.unmuteVideo();
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

  // Strip characters that are illegal in filenames across Windows/macOS/Linux
  // (plus control chars), collapse whitespace, and bound the length so a very
  // long video title can't produce an unwieldy filename.
  function sanitizeFilename(name) {
    return (name || "")
      .replace(/[/\\:*?"<>|\x00-\x1f]/g, " ")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, 120);
  }

  // ---------------------------------------------------------------------------
  // Button + per-video attachment
  // ---------------------------------------------------------------------------
  class Button {
    constructor(video) {
      this.video = video;
      this.session = null;
      // Set once the user dismisses (×): a re-anchor must never bring a
      // dismissed pill back into view.
      this._dismissed = false;
      // Error state is intentionally transient — see _scheduleErrorRevert.
      this._errorRevertTimer = null;
      this.el = document.createElement("button");
      this.el.className = "nomusic-btn";
      this.el.type = "button";
      this.el.title = "Strip music (nomusic)";
      // Progress fill lives inside a clip wrapper so it's clipped to the
      // pill's rounded shape (the dismiss × stays outside the wrapper, so it
      // isn't clipped).
      this.fillClip = document.createElement("span");
      this.fillClip.className = "nomusic-btn__clip";
      this.fill = document.createElement("span");
      this.fill.className = "nomusic-btn__fill";
      this.fillClip.appendChild(this.fill);
      // Brand icon replaces the old colored dot as the leading visual.
      // It picks up the same pulse animation while the backend is working.
      this.icon = document.createElement("img");
      this.icon.className = "nomusic-btn__icon";
      this.icon.src = chrome.runtime.getURL("icons/button.png");
      this.icon.alt = "";
      // Lock the icon's box with inline !important. Some hosts (Telegram Web)
      // force-size every <img> in their message UI to fill its container with a
      // high-specificity !important rule; an inline !important declaration
      // outranks any stylesheet rule, so this stops the 1008x510 wordmark from
      // ballooning across the video. 28x14 keeps its ~2:1 ratio.
      for (const [k, v] of Object.entries({
        width: "28px", height: "14px",
        "max-width": "28px", "max-height": "14px",
        "min-width": "0", "min-height": "0",
      })) {
        this.icon.style.setProperty(k, v, "important");
      }
      this.label = document.createElement("span");
      this.label.className = "nomusic-btn__label";
      this.label.textContent = "nomusic";
      this.pct = document.createElement("span");
      this.pct.className = "nomusic-btn__pct";
      // Latest video title from the backend status stream; used to name the
      // downloaded file. Populated in showStatus().
      this.title = "";
      // Download control: a chevron that opens a dropdown menu (MP3 + MP4 at
      // several resolutions). Only visible once the job is ready (CSS keys it
      // off [data-state="active"]). A <span role="button"> avoids nested-button
      // HTML inside the pill; the menu itself lives on document.body (see
      // _buildMenu) so its <button> items aren't nested in the pill <button>.
      this.dlBtn = document.createElement("span");
      this.dlBtn.className = "nomusic-btn__dl";
      this.dlBtn.setAttribute("role", "button");
      this.dlBtn.setAttribute("aria-haspopup", "menu");
      this.dlBtn.setAttribute("aria-label", "Download…");
      this.dlBtn.title = "Download…";
      this.dlBtn.textContent = "⤵"; // ⤵ download-ish chevron
      this.dlBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        e.preventDefault();
        this.toggleMenu();
      });
      this._menuOpen = false;
      this._downloading = false;
      // A download requested before the track finished processing: {format,
      // height}. Held until the job reaches "ready", then saved automatically.
      this._pendingDownload = null;
      // Latest backend readiness, tracked from showStatus so download() knows
      // whether it can fetch now or must queue and wait.
      this._ready = false;
      this.menu = this._buildMenu();
      // Close the menu on an outside click / scroll / resize. Capture phase so
      // we see the event even if the host page stops propagation.
      this._onDocClick = (e) => {
        if (this._menuOpen && !this.menu.contains(e.target) && e.target !== this.dlBtn)
          this.closeMenu();
      };
      this._onReposition = () => this.closeMenu();
      // <span role="button"> rather than nested <button> — nested buttons
      // are invalid HTML and some browsers (older Safari) misroute clicks
      // when they appear.
      this.dismissBtn = document.createElement("span");
      this.dismissBtn.className = "nomusic-btn__dismiss";
      this.dismissBtn.setAttribute("role", "button");
      this.dismissBtn.setAttribute("aria-label", "Hide nomusic on this video");
      this.dismissBtn.title = "Hide nomusic";
      // The X is drawn geometrically via ::before/::after in content.css
      // because the "×" glyph's font metrics render off-center in the
      // dismiss bubble.
      this.dismissBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        e.preventDefault();
        this.dismiss();
      });
      this.el.append(
        this.fillClip,
        this.icon,
        this.label,
        this.pct,
        this.dlBtn,
        this.dismissBtn,
      );
      this.el.addEventListener("click", (e) => {
        e.stopPropagation();
        e.preventDefault();
        this.toggle();
      });
      this.setIdle();
    }

    /** Hide the button entirely. If nomusic is active, tear the session
     *  down first so the host video is restored. The button stays
     *  attached to the DOM but with display:none, so we don't re-create
     *  it via the MutationObserver. A new <video> on SPA navigation gets
     *  its own button. */
    dismiss() {
      if (this.session && !this.session.disposed) {
        this.session.dispose();
        this.session = null;
      }
      this.closeMenu();
      this.menu.remove(); // it lives on document.body, so clean it up
      this._dismissed = true;
      this._pendingDownload = null; // user is leaving — drop any queued download
      this.el.style.display = "none";
    }

    setIdle() {
      this._clearErrorRevert();
      this.el.dataset.state = "idle";
      this.label.textContent = "nomusic";
      this.pct.textContent = "";
      this.fill.style.width = "0%";
    }

    /** ``status`` is the raw JobStatus from the backend. */
    showStatus(status) {
      // Remember the title for the download filename; it arrives on every
      // snapshot but isn't otherwise displayed.
      if (status.title) this.title = status.title;
      // While a file fetch is in flight the export progress owns the pill
      // (_showExportProgress); ignore late status snapshots so they don't fight.
      if (this._downloading) return;

      const state = status.state;
      this._ready = state === "ready";
      if (state === "ready") {
        // A download queued mid-processing fires the instant the track is done.
        if (this._pendingDownload) {
          const pd = this._pendingDownload;
          this._pendingDownload = null;
          this._startDownload(pd.format, pd.height);
          return;
        }
        this._clearErrorRevert();
        this.el.dataset.state = "active";
        this.label.textContent = "nomusic on";
        this.pct.textContent = "";
        this.fill.style.width = "0%";
        return;
      }
      if (state === "error") {
        this._pendingDownload = null; // can't deliver a file from a failed job
        this.setError(status.phase_label || "Error");
        return;
      }
      this._clearErrorRevert();
      this.el.dataset.state = "working";
      // Relabel the processing phase while a download is queued so it's clear
      // we're finishing the track for the file (and it keeps going on pause).
      this.label.textContent = this._pendingDownload
        ? "Preparing"
        : status.phase_label || "Working";
      const p = status.phase_progress;
      if (typeof p === "number" && isFinite(p)) {
        const pct = Math.max(0, Math.min(100, Math.round(p * 100)));
        this.pct.textContent = `${pct}%`;
        this.fill.style.width = `${pct}%`;
      } else {
        this.pct.textContent = "";
        this.fill.style.width = "0%";
      }
    }

    setBuffering() {
      // An in-flight export owns the pill (Preparing/Encoding N%); a playback
      // buffer event must not clobber it. _showExportProgress has the inverse
      // guard, so the export display is fully isolated while _downloading.
      if (this._downloading) return;
      this._clearErrorRevert();
      this.el.dataset.state = "working";
      this.label.textContent = "Buffering";
      this.pct.textContent = "";
      this.fill.style.width = "0%";
    }

    /** User paused while the backend was still working. Distinct from
     *  "Buffering" (which auto-resumes): this means we've let the worker go
     *  idle. Keeps the current % + fill so the frozen progress is visible,
     *  and the non-"working" state stops the icon pulse. */
    setPaused() {
      if (this._downloading) return; // export owns the pill — see setBuffering
      this._clearErrorRevert();
      this.el.dataset.state = "paused";
      this.label.textContent = "Paused";
    }

    setError(label) {
      this.el.dataset.state = "error";
      this.label.textContent = label || "Error";
      this.pct.textContent = "";
      this.fill.style.width = "0%";
      this._scheduleErrorRevert();
    }

    // Error is transient feedback, not a sticky mode. After a brief moment
    // the pill returns to its idle shape so the user can click again
    // cleanly instead of staring at a red bar.
    _scheduleErrorRevert() {
      this._clearErrorRevert();
      this._errorRevertTimer = setTimeout(() => {
        this._errorRevertTimer = null;
        if (this.el.dataset.state === "error") this.setIdle();
      }, 2500);
    }

    _clearErrorRevert() {
      if (this._errorRevertTimer) {
        clearTimeout(this._errorRevertTimer);
        this._errorRevertTimer = null;
      }
    }

    async toggle() {
      if (this.session && !this.session.disposed) {
        this.session.dispose();
        this.session = null;
        return;
      }
      this.session = new Session(this.video, this);
      await this.session.start();
    }

    dispose() {
      // Drop the body-level menu node and its open-state listeners. Without
      // this, a button torn down on SPA navigation (video emptied) leaves its
      // hidden menu orphaned on document.body, one per navigation. openMenu()
      // re-appends it if this same button is later reused.
      this.closeMenu();
      this.menu.remove();
      this.setIdle();
    }

    // The download menu. MP3 (audio only) plus MP4 at a few resolution caps.
    // Resolution is a ceiling — the backend grabs the best stream up to it and
    // falls back when a video doesn't offer that height.
    static MENU_ITEMS = [
      { section: "Audio" },
      { label: "MP3 — audio only", format: "mp3", height: 0 },
      { section: "Video (MP4)" },
      { label: "Best available", format: "mp4", height: 0 },
      { label: "2160p · 4K", format: "mp4", height: 2160 },
      { label: "1440p", format: "mp4", height: 1440 },
      { label: "1080p", format: "mp4", height: 1080 },
      { label: "720p", format: "mp4", height: 720 },
      { label: "480p", format: "mp4", height: 480 },
    ];

    _buildMenu() {
      const menu = document.createElement("div");
      menu.className = "nomusic-menu";
      menu.setAttribute("role", "menu");
      menu.hidden = true;
      for (const it of Button.MENU_ITEMS) {
        if (it.section) {
          const h = document.createElement("div");
          h.className = "nomusic-menu__section";
          h.textContent = it.section;
          menu.appendChild(h);
          continue;
        }
        const b = document.createElement("button");
        b.type = "button";
        b.className = "nomusic-menu__item";
        b.textContent = it.label;
        b.addEventListener("click", (e) => {
          e.stopPropagation();
          e.preventDefault();
          this.closeMenu();
          this.download(it.format, it.height);
        });
        menu.appendChild(b);
      }
      // Lives on body (not inside the pill <button>) to avoid nested buttons
      // and host-page overflow clipping; positioned on open via openMenu().
      document.body.appendChild(menu);
      return menu;
    }

    toggleMenu() {
      this._menuOpen ? this.closeMenu() : this.openMenu();
    }

    openMenu() {
      if (this._downloading) return; // no menu while a download is in flight
      // The menu lives on document.body and is removed on dispose() (so a
      // torn-down button doesn't orphan it). The button itself is reusable
      // after dispose() → setIdle(), so re-attach before measuring/positioning.
      if (!this.menu.isConnected) document.body.appendChild(this.menu);
      const r = this.el.getBoundingClientRect();
      this.menu.hidden = false; // unhide first so offsetWidth/Height are real
      const mw = this.menu.offsetWidth;
      const mh = this.menu.offsetHeight;
      let left = Math.max(8, r.right - mw); // right-align to the pill
      let top = r.bottom + 6;
      if (top + mh > window.innerHeight - 8) top = r.top - 6 - mh; // flip up
      this.menu.style.left = `${Math.round(left)}px`;
      this.menu.style.top = `${Math.round(Math.max(8, top))}px`;
      this._menuOpen = true;
      document.addEventListener("click", this._onDocClick, true);
      window.addEventListener("scroll", this._onReposition, true);
      window.addEventListener("resize", this._onReposition, true);
    }

    closeMenu() {
      if (!this._menuOpen) return;
      this.menu.hidden = true;
      this._menuOpen = false;
      document.removeEventListener("click", this._onDocClick, true);
      window.removeEventListener("scroll", this._onReposition, true);
      window.removeEventListener("resize", this._onReposition, true);
    }

    /** Entry point for a download menu pick. If the track is fully processed we
     *  fetch + save immediately; otherwise we queue it and keep the worker alive
     *  to completion (the user can pause / stop watching), saving automatically
     *  when it's ready. ``format`` is "mp3" or "mp4"; ``height`` caps MP4 res. */
    download(format, height = 0) {
      if (!this.session?.jobId) return;
      if (this._downloading) return; // a fetch is already in flight
      if (this._ready) {
        this._startDownload(format, height);
        return;
      }
      // Not done yet: queue it and pin the worker so processing runs to the end
      // regardless of play/pause, then save when it reaches "ready".
      this._pendingDownload = { format, height };
      this.closeMenu();
      this.el.dataset.state = "working";
      this.label.textContent = "Preparing";
      this.session.ensureLiveForDownload();
    }

    /** Fetch the finished export from the backend and save it to disk. We fetch
     *  the bytes and save via a blob: URL because a direct cross-origin
     *  <a download> to the backend would have its filename ignored. */
    async _startDownload(format, height = 0) {
      const jobId = this.session?.jobId;
      if (!jobId) return;
      if (this._downloading) return; // ignore double-clicks mid-download
      this._downloading = true;

      const ext = format === "mp4" ? "mp4" : "mp3";
      const q = height ? `?max_height=${height}` : "";
      const url =
        format === "mp4"
          ? `${settings.backendUrl}/video/${jobId}${q}`
          : `${settings.backendUrl}/audio/${jobId}?format=mp3`;

      // Busy feedback — freeze the pill while preparing.
      this._clearErrorRevert();
      this.el.dataset.state = "working";
      this.label.textContent = format === "mp4" ? "Preparing…" : "Saving…";
      this.pct.textContent = "";
      this.fill.style.width = "0%";

      // MP4 prep can take a while (download + mux/re-encode); poll the backend
      // so the pill shows real "Fetching N%" / "Encoding N%" progress.
      let pollTimer = null;
      if (format === "mp4") {
        const progUrl = `${settings.backendUrl}/video/${jobId}/progress${q}`;
        const poll = async () => {
          try {
            const r = await fetch(progUrl, { cache: "no-store" });
            if (r.ok) this._showExportProgress(await r.json());
          } catch (_e) {
            /* transient; keep polling */
          }
        };
        pollTimer = setInterval(poll, 600);
        poll();
      }

      let objUrl = null;
      try {
        // no-store: never reuse a cached response. Older backends served raw
        // Opus at the ?format=mp3 URL with a 24h cache header, which the
        // browser would otherwise keep handing back instead of the real MP3.
        const resp = await fetch(url, { cache: "no-store" });
        if (!resp.ok) {
          throw new Error(
            resp.status === 425 ? "not ready" : `HTTP ${resp.status}`,
          );
        }
        const blob = await resp.blob();
        // Stop progress polling the moment the bytes arrive, before we restore
        // the label, so a late poll can't overwrite it.
        if (pollTimer) {
          clearInterval(pollTimer);
          pollTimer = null;
        }
        objUrl = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = objUrl;
        a.download = `${sanitizeFilename(this.title) || "nomusic"}.${ext}`;
        a.rel = "noopener";
        document.body.appendChild(a);
        a.click();
        a.remove();
        this._restoreAfterDownload();
      } catch (err) {
        console.warn("[nomusic] download failed", err);
        this._flashDownloadError();
      } finally {
        if (pollTimer) clearInterval(pollTimer);
        this._downloading = false;
        // Revoke after the click-initiated download has had time to start;
        // revoking immediately can cancel it in some browsers.
        if (objUrl) setTimeout(() => URL.revokeObjectURL(objUrl), 10000);
      }
    }

    /** Render a polled export-progress snapshot onto the pill. */
    _showExportProgress(p) {
      if (!this._downloading || !p) return;
      if (p.phase === "idle" || p.phase === "done") return;
      const label = p.phase === "downloading" ? "Fetching" : "Encoding";
      const pct = Math.max(0, Math.min(100, Math.round(p.percent || 0)));
      this.el.dataset.state = "working";
      this.label.textContent = label;
      this.pct.textContent = `${pct}%`;
      this.fill.style.width = `${pct}%`;
    }

    /** Return the pill to its post-download resting visual: "nomusic on" if the
     *  session is still live, otherwise idle. */
    _restoreAfterDownload() {
      if (this.session && !this.session.disposed) {
        this.el.dataset.state = "active";
        this.label.textContent = "nomusic on";
        this.pct.textContent = "";
        this.fill.style.width = "0%";
      } else {
        this.setIdle();
      }
    }

    _flashDownloadError() {
      this.el.dataset.state = "error";
      this.label.textContent = "Download failed";
      this.pct.textContent = "";
      this.fill.style.width = "0%";
      this._clearErrorRevert();
      this._errorRevertTimer = setTimeout(() => {
        this._errorRevertTimer = null;
        if (this.el.dataset.state === "error") this._restoreAfterDownload();
      }, 2500);
    }

    position(host) {
      // Anchor inside the nearest positioned ancestor of the video. The host
      // page often wraps the <video> in a player container; we live inside it.
      // Top-right keeps us clear of the bottom control bar / scrubber, which
      // otherwise overlaps the pill (badly at the end of a video when the
      // progress bar is full).
      host.appendChild(this.el);
      this.el.style.right = "12px";
      this.el.style.top = "12px";
    }
  }

  // ---------------------------------------------------------------------------
  // Discovery: attach to every <video>, including ones added later.
  // ---------------------------------------------------------------------------
  const attached = new WeakMap();
  // Enumerable view of live buttons so layout-change handlers (fullscreen /
  // resize) can re-anchor them — the WeakMap above isn't iterable.
  const liveButtons = new Set();

  function anchorButton(btn) {
    const host = pickHost(btn.video);
    if (!host) {
      // No visible player to anchor to (the video's player was torn down /
      // hidden, e.g. after navigating to the YouTube home feed). Hide the pill
      // rather than let it strand in a window-sized fallback host; it re-appears
      // on the next re-anchor once a real player is back.
      btn.el.style.display = "none";
      return;
    }
    // Our position:absolute needs a positioned host, re-asserted every time: the
    // host can be re-created or have its inline position reset by the page around
    // fullscreen / SPA navigation, which otherwise lets the button's offset
    // parent climb to a window-sized ancestor (the "stuck in the corner" bug).
    if (getComputedStyle(host).position === "static") {
      host.style.position = "relative";
    }
    btn.position(host);
    if (!btn._dismissed) btn.el.style.display = "";
  }

  function attachToVideo(video) {
    if (attached.has(video)) return;
    // Skip tiny/decorative videos (autoplay ads, etc.).
    if (video.clientWidth > 0 && video.clientWidth < 200) return;
    if (!pickHost(video)) return;

    const btn = new Button(video);
    anchorButton(btn);
    attached.set(video, btn);
    liveButtons.add(btn);
  }

  // Re-anchor every live button to its video's current host. Called on layout
  // shifts that can re-parent the player (fullscreen toggle, window resize).
  function reanchorButtons() {
    for (const btn of liveButtons) {
      if (!btn.video || !btn.video.isConnected) {
        btn.el.style.display = "none"; // its video is gone — don't leave it stranded
        liveButtons.delete(btn);
        continue;
      }
      anchorButton(btn);
    }
  }

  function pickHost(video) {
    // Only anchor to an ancestor that wraps the *rendered* video. If the video
    // has no visible box — a torn-down/hidden watch player after navigating to
    // the home feed, a background tab, etc. — there's no good host; return null
    // so the caller hides the button instead of pinning it to a window-sized
    // fallback (the "button stuck in the masthead corner on the home page" bug).
    const vr = video.getBoundingClientRect();
    if (vr.width < 200 || vr.height < 100) return null;
    // Prefer the closest visible block ancestor; YouTube wraps its <video> in
    // .html5-video-container nested inside .html5-video-player.
    let node = video.parentElement;
    while (node && node !== document.body) {
      const rect = node.getBoundingClientRect();
      if (rect.width >= 240 && rect.height >= 135) return node;
      node = node.parentElement;
    }
    return null;
  }

  function scan(root) {
    const videos = root.querySelectorAll?.("video");
    if (!videos) return;
    videos.forEach(attachToVideo);
  }

  function init() {
    scan(document);
    const observer = new MutationObserver((mutations) => {
      for (const m of mutations) {
        for (const node of m.addedNodes) {
          if (node.nodeType === 1) {
            if (node.tagName === "VIDEO") attachToVideo(node);
            else scan(node);
          }
        }
      }
    });
    observer.observe(document.documentElement, {
      childList: true,
      subtree: true,
    });

    // Re-anchor on layout shifts that re-parent, resize, or remove the player:
    //  - fullscreen toggle: exiting it (then navigating without a reload) could
    //    leave the button pinned to the window corner instead of the new video.
    //  - yt-navigate-finish: YouTube's SPA route change (watch <-> home <-> next
    //    video) without a reload — re-anchors to the new player, or hides the
    //    pill on pages with no player (the home feed).
    //  - resize: a debounced generic safety net for other hosts.
    document.addEventListener("fullscreenchange", reanchorButtons, true);
    document.addEventListener("webkitfullscreenchange", reanchorButtons, true);
    document.addEventListener("yt-navigate-finish", reanchorButtons, true);
    let resizeTimer = null;
    window.addEventListener("resize", () => {
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(reanchorButtons, 250);
    });
  }

  loadSettings().finally(init);
})();
