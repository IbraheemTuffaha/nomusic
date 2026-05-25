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
  const STATUS_POLL_MS = 500;
  const SYNC_TOLERANCE_S = 0.08;
  const SYNC_CHECK_MS = 250;

  // Debug logging for seek/buffer/prioritize state transitions. Flip to
  // ``false`` (or remove the calls) once the seek-backwards investigation
  // is done. Browser DevTools' console filter is the easiest way to scan;
  // every line is prefixed with [nomusic].
  const DEBUG = true;
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
      this.chunkSeconds = 30;
      this.chunkOverlapSeconds = 1;
      this.duration = 0;
      // idx -> { buffer: AudioBuffer, playStart: number }
      this.chunks = new Map();
      this.activeSources = new Set();
      this.fetchedIdx = new Set();
      this.pollAbort = null;
      this.statusTimer = null;
      this.syncTimer = null;
      this.bufferTimer = null;
      this.disposed = false;
      this.originalMuted = video.muted;
      this.originalVolume = video.volume;
      // When true, we (not the user) called video.pause() because the
      // chunk for the current timecode isn't on disk yet. We track this so
      // a manual pause stays paused but a buffering pause auto-resumes.
      this._pausedByUs = false;
      // Flipped true once pollLoop exits (state == ready/error). Tells
      // _resumeAfterBuffer that no future poll will repaint the label,
      // so it has to restore "nomusic on" itself.
      this._pollingEnded = false;
      // Debounce timer for the /prioritize POST on seek so scrubbing a
      // timeline doesn't fire one request per intermediate frame.
      this._prioritizeTimer = null;
      this._boundHandlers = {
        play: () => this.reschedule(),
        pause: () => this.stopAll(),
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

      // AudioContext needs a user gesture; the click that triggered .start()
      // satisfies that requirement on every Chromium-derived browser.
      this.audioCtx = new (window.AudioContext || window.webkitAudioContext)({
        latencyHint: "playback",
      });
      this.gain = this.audioCtx.createGain();
      this.gain.gain.value = 1.0;
      this.gain.connect(this.audioCtx.destination);

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
      this.pollLoop();
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

    async pollLoop() {
      if (this.disposed) return;
      try {
        const resp = await fetch(
          `${settings.backendUrl}/status/${this.jobId}`,
          { cache: "no-store" },
        );
        if (!resp.ok) throw new Error(`status ${resp.status}`);
        const status = await resp.json();
        this.totalChunks = status.total_chunks || this.totalChunks;
        // Always reflect the backend phase in the label, even while we're
        // paused for buffering — the pulsing icon + paused video already
        // convey "waiting", and the phase label is more useful content.
        this.button.showStatus(status);

        if (status.state === "error") {
          this._pollingEnded = true;
          this.dispose({ preserveButtonState: true });
          return;
        }

        // Fetch any newly-ready chunk in parallel. With seek-driven
        // reordering, ready chunks are no longer contiguous from 0, so
        // we iterate the explicit ``ready_chunks`` set the backend sends.
        // Each chunk knows its own play_start so order doesn't matter.
        const readyChunks = Array.isArray(status.ready_chunks)
          ? status.ready_chunks
          : [];
        const fetches = [];
        for (const i of readyChunks) {
          if (!this.fetchedIdx.has(i)) {
            this.fetchedIdx.add(i);
            fetches.push(this.fetchAndQueueChunk(i));
          }
        }
        await Promise.all(fetches);

        if (status.state === "ready") {
          this._pollingEnded = true;
          return; // stop polling
        }
      } catch (err) {
        console.warn("[nomusic] poll failed", err);
      }
      this.statusTimer = setTimeout(() => this.pollLoop(), STATUS_POLL_MS);
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
          this.fetchedIdx.delete(idx); // allow retry on next poll
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

    scheduleChunk(idx, entry) {
      if (this.disposed || !this.audioCtx) return;
      const now = this.audioCtx.currentTime;
      const videoTime = this.video.currentTime;
      const rate = this.video.playbackRate || 1;
      const chunkStart = entry.playStart;
      const chunkEnd = chunkStart + entry.buffer.duration;

      // Where in the chunk should playback begin?
      let offset = (videoTime - chunkStart) * rate;
      let when = now;
      if (offset < 0) {
        // Chunk is in the future on the video timeline. Schedule its onset.
        when = now + (chunkStart - videoTime) / rate;
        offset = 0;
      } else if (offset >= entry.buffer.duration) {
        return; // chunk already behind us
      }

      const src = this.audioCtx.createBufferSource();
      src.buffer = entry.buffer;
      src.playbackRate.value = rate;
      src.connect(this.gain);
      // Tag with idx so the sync monitor can find the currently-active source.
      src._nomusicIdx = idx;
      src._nomusicChunkStart = chunkStart;
      src._nomusicChunkEnd = chunkEnd;
      src.onended = () => this.activeSources.delete(src);
      try {
        src.start(when, offset);
      } catch (err) {
        console.warn("[nomusic] scheduling failed", err);
        return;
      }
      this.activeSources.add(src);
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
      // If polling is still active, the next status tick will overwrite the
      // "Buffering" label naturally. If polling has ended (state was ready
      // or error) we have to restore the active label ourselves.
      if (this._pollingEnded && this.button.el.dataset.state === "working") {
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
     *  scrub doesn't generate dozens of POSTs. No-op once polling has
     *  ended because the worker is already done. */
    _sendPrioritizeHint() {
      if (this.disposed || this._pollingEnded || !this.jobId) return;
      if (this._prioritizeTimer) clearTimeout(this._prioritizeTimer);
      this._prioritizeTimer = setTimeout(() => {
        this._prioritizeTimer = null;
        if (this.disposed || this._pollingEnded || !this.jobId) return;
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
        const actualOffset =
          (this.audioCtx.currentTime - active._nomusicStartedAt) *
            (this.video.playbackRate || 1) +
          (active._nomusicOffsetAtStart || 0);
        if (
          Number.isFinite(actualOffset) &&
          Math.abs(actualOffset - expectedOffset) > SYNC_TOLERANCE_S
        ) {
          // Drift exceeded tolerance: restart the active source at the right
          // offset. This is cheap because we already hold the decoded buffer.
          const idx = active._nomusicIdx;
          const entry = this.chunks.get(idx);
          try {
            active.stop();
          } catch (_err) {
            /* noop */
          }
          this.activeSources.delete(active);
          if (entry) this.scheduleChunk(idx, entry);
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
      if (this.statusTimer) clearTimeout(this.statusTimer);
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
      // Error paths set the button to "error" and rely on its own
      // auto-revert timer for the visual transition. Calling button.dispose
      // here would clobber that.
      if (!preserveButtonState) this.button.dispose();
    }
  }

  // Track BufferSource start time + offset for the sync monitor.
  // We monkey-patch start() once on the prototype to capture these.
  (function instrumentBufferSource() {
    if (!window.AudioBufferSourceNode) return;
    if (window.AudioBufferSourceNode.prototype._nomusicInstrumented) return;
    const proto = window.AudioBufferSourceNode.prototype;
    const origStart = proto.start;
    proto.start = function (when, offset) {
      this._nomusicStartedAt = when ?? (this.context?.currentTime ?? 0);
      this._nomusicOffsetAtStart = offset ?? 0;
      return origStart.apply(this, arguments);
    };
    proto._nomusicInstrumented = true;
  })();

  // ---------------------------------------------------------------------------
  // Button + per-video attachment
  // ---------------------------------------------------------------------------
  class Button {
    constructor(video) {
      this.video = video;
      this.session = null;
      // Error state is intentionally transient — see _scheduleErrorRevert.
      this._errorRevertTimer = null;
      this.el = document.createElement("button");
      this.el.className = "nomusic-btn";
      this.el.type = "button";
      this.el.title = "Strip music (nomusic)";
      this.fill = document.createElement("span");
      this.fill.className = "nomusic-btn__fill";
      // Brand icon replaces the old colored dot as the leading visual.
      // It picks up the same pulse animation while the backend is working.
      this.icon = document.createElement("img");
      this.icon.className = "nomusic-btn__icon";
      this.icon.src = chrome.runtime.getURL("icons/button.png");
      this.icon.alt = "";
      this.label = document.createElement("span");
      this.label.className = "nomusic-btn__label";
      this.label.textContent = "nomusic";
      this.pct = document.createElement("span");
      this.pct.className = "nomusic-btn__pct";
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
        this.fill,
        this.icon,
        this.label,
        this.pct,
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
      const state = status.state;
      if (state === "ready") {
        this._clearErrorRevert();
        this.el.dataset.state = "active";
        this.label.textContent = "nomusic on";
        this.pct.textContent = "";
        this.fill.style.width = "0%";
        return;
      }
      if (state === "error") {
        this.setError(status.phase_label || "Error");
        return;
      }
      this._clearErrorRevert();
      this.el.dataset.state = "working";
      this.label.textContent = status.phase_label || "Working";
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
      this._clearErrorRevert();
      this.el.dataset.state = "working";
      this.label.textContent = "Buffering";
      this.pct.textContent = "";
      this.fill.style.width = "0%";
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
      this.setIdle();
    }

    position(host) {
      // Anchor inside the nearest positioned ancestor of the video. The host
      // page often wraps the <video> in a player container; we live inside it.
      host.appendChild(this.el);
      this.el.style.right = "12px";
      this.el.style.bottom = "60px";
    }
  }

  // ---------------------------------------------------------------------------
  // Discovery: attach to every <video>, including ones added later.
  // ---------------------------------------------------------------------------
  const attached = new WeakMap();

  function attachToVideo(video) {
    if (attached.has(video)) return;
    // Skip tiny/decorative videos (autoplay ads, etc.).
    if (video.clientWidth > 0 && video.clientWidth < 200) return;

    const host = pickHost(video);
    if (!host) return;

    // Make sure the host is positioned so our `position: absolute` makes sense.
    const hostPos = getComputedStyle(host).position;
    if (hostPos === "static") {
      host.style.position = "relative";
    }
    const btn = new Button(video);
    btn.position(host);
    attached.set(video, btn);
  }

  function pickHost(video) {
    // Prefer the closest visible block ancestor; YouTube wraps its <video> in
    // .html5-video-container nested inside .html5-video-player.
    let node = video.parentElement;
    while (node && node !== document.body) {
      const rect = node.getBoundingClientRect();
      if (rect.width >= 240 && rect.height >= 135) return node;
      node = node.parentElement;
    }
    return video.parentElement || document.body;
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
  }

  loadSettings().finally(init);
})();
