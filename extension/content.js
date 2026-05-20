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
      this._lastStatus = null;
      this._boundHandlers = {
        play: () => this.reschedule(),
        pause: () => this.stopAll(),
        seeking: () => this.stopAll(),
        seeked: () => {
          this.reschedule();
          this._maybeBufferPause();
        },
        ratechange: () => this.reschedule(),
        emptied: () => this.dispose(),
        volumechange: () => this._onHostVolumeChange(),
      };
    }

    async start() {
      this.button.setLocal("Starting");

      let info;
      try {
        info = await this.requestJob();
      } catch (err) {
        console.warn("[nomusic] /process failed", err);
        this.button.setError("backend unreachable");
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
      this._pauseForBuffer();
      this.attachVideoListeners();
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
        this._lastStatus = status;
        // While we're buffer-paused we own the label; let the buffer
        // monitor refresh it once buffering ends. Status updates still
        // record into _lastStatus for later restore.
        if (!this._pausedByUs) this.button.showStatus(status);

        if (status.state === "error") return;

        // Fetch any newly-ready chunk in parallel; ordering doesn't matter,
        // each chunk knows its own play_start.
        const fetches = [];
        for (let i = 0; i < status.chunks_ready; i++) {
          if (!this.fetchedIdx.has(i)) {
            this.fetchedIdx.add(i);
            fetches.push(this.fetchAndQueueChunk(i));
          }
        }
        await Promise.all(fetches);

        if (status.state === "ready") return; // stop polling
      } catch (err) {
        console.warn("[nomusic] poll failed", err);
      }
      this.statusTimer = setTimeout(() => this.pollLoop(), STATUS_POLL_MS);
    }

    async fetchAndQueueChunk(idx) {
      try {
        const resp = await fetch(
          `${settings.backendUrl}/chunk/${this.jobId}/${idx}`,
          { cache: "force-cache" },
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

    _pauseForBuffer() {
      if (this._pausedByUs || this.disposed) return;
      this._pausedByUs = true;
      this.button.setLocal("Buffering");
      try {
        this.video.pause();
      } catch (_err) {
        /* element gone */
      }
    }

    _resumeAfterBuffer() {
      if (!this._pausedByUs || this.disposed) return;
      this._pausedByUs = false;
      if (this._lastStatus) this.button.showStatus(this._lastStatus);
      try {
        const p = this.video.play();
        if (p && typeof p.catch === "function") p.catch(() => {});
      } catch (_err) {
        /* element gone */
      }
    }

    _maybeBufferPause() {
      if (this.disposed || this.video.paused || this._pausedByUs) return;
      if (!this._isBuffered(this.video.currentTime)) this._pauseForBuffer();
    }

    /** rAF-rate check: as currentTime crosses into an unfetched chunk while
     *  playing, pause until it lands. Cheap (one branch + a Map.has per
     *  tick) so we can run it at SYNC_CHECK_MS. */
    startBufferMonitor() {
      const tick = () => {
        if (this.disposed) return;
        this.bufferTimer = setTimeout(tick, SYNC_CHECK_MS);
        this._maybeBufferPause();
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

      // YouTube et al. re-assert volume / muted state every frame from their
      // own player code, so a single `video.muted = true` is undone almost
      // immediately. We layer two mutes:
      //
      //   1. Override the instance's `muted` and `volume` properties so any
      //      direct setter the host page calls is a no-op. The underlying
      //      state still has to be set via the original setter from
      //      HTMLMediaElement.prototype — instance overrides only shadow
      //      reads/writes that come through the instance.
      //   2. requestAnimationFrame loop that re-applies the underlying state
      //      via the original setter, in case the host bypasses the instance
      //      and writes through the prototype.
      //
      // We deliberately do NOT call createMediaElementSource on the host
      // <video>: attaching one is permanent (no detach API exists), it gets
      // silenced anyway for cross-origin media like YouTube, and once the
      // AudioContext is closed on dispose the video element ends up waiting
      // forever on audio that will never flow — the player shows an
      // infinite loading spinner you can't dismiss.
      const proto = HTMLMediaElement.prototype;
      const mutedDesc = Object.getOwnPropertyDescriptor(proto, "muted");
      const volumeDesc = Object.getOwnPropertyDescriptor(proto, "volume");

      const realSetMuted = (v) => mutedDesc.set.call(this.video, v);
      const realSetVolume = (v) => volumeDesc.set.call(this.video, v);
      const realGetMuted = () => mutedDesc.get.call(this.video);
      const realGetVolume = () => volumeDesc.get.call(this.video);
      this._realSetMuted = realSetMuted;
      this._realSetVolume = realSetVolume;
      this._realGetMuted = realGetMuted;
      this._realGetVolume = realGetVolume;

      // Start our gain at the user's current effective volume so toggling
      // nomusic on doesn't jump the loudness around. Whatever they set
      // before clicking the button is what they'll hear from the processed
      // audio.
      if (this.gain) {
        this.gain.gain.value = this.originalMuted
          ? 0
          : Math.max(0, Math.min(1, this.originalVolume));
      }

      realSetMuted(true);
      realSetVolume(0);

      try {
        Object.defineProperty(this.video, "muted", {
          configurable: true,
          get: () => true,
          set: () => {
            /* swallow */
          },
        });
        Object.defineProperty(this.video, "volume", {
          configurable: true,
          get: () => 0,
          set: () => {
            /* swallow */
          },
        });
      } catch (err) {
        console.warn("[nomusic] property override failed", err);
      }

      const tick = () => {
        if (this.disposed) return;
        try {
          if (!realGetMuted()) realSetMuted(true);
          if (realGetVolume() !== 0) realSetVolume(0);
        } catch (_err) {
          /* element detached */
        }
        this.muteAsserter = requestAnimationFrame(tick);
      };
      this.muteAsserter = requestAnimationFrame(tick);
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

      // Remove the instance overrides so the prototype's behavior comes back.
      try {
        delete this.video.muted;
        delete this.video.volume;
      } catch (_err) {
        /* noop */
      }

      // Restore underlying state via the original setters.
      if (this._realSetMuted) this._realSetMuted(this.originalMuted);
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

    dispose() {
      if (this.disposed) return;
      this.disposed = true;
      this.detachVideoListeners();
      this.stopAll();
      if (this.statusTimer) clearTimeout(this.statusTimer);
      if (this.syncTimer) clearTimeout(this.syncTimer);
      if (this.bufferTimer) clearTimeout(this.bufferTimer);
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
      this.button.dispose();
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
      this.el = document.createElement("button");
      this.el.className = "nomusic-btn";
      this.el.type = "button";
      this.el.title = "Strip music (nomusic)";
      this.fill = document.createElement("span");
      this.fill.className = "nomusic-btn__fill";
      this.dot = document.createElement("span");
      this.dot.className = "nomusic-btn__dot";
      this.label = document.createElement("span");
      this.label.className = "nomusic-btn__label";
      this.label.textContent = "nomusic";
      this.pct = document.createElement("span");
      this.pct.className = "nomusic-btn__pct";
      this.el.append(this.fill, this.dot, this.label, this.pct);
      this.el.addEventListener("click", (e) => {
        e.stopPropagation();
        e.preventDefault();
        this.toggle();
      });
      this.setIdle();
    }

    setIdle() {
      this.el.dataset.state = "idle";
      this.label.textContent = "nomusic";
      this.pct.textContent = "";
      this.fill.style.width = "0%";
    }

    /** ``status`` is the raw JobStatus from the backend. */
    showStatus(status) {
      const state = status.state;
      if (state === "ready") {
        this.el.dataset.state = "active";
        this.label.textContent = "nomusic on";
        this.pct.textContent = "";
        this.fill.style.width = "0%";
        return;
      }
      if (state === "error") {
        this.el.dataset.state = "error";
        this.label.textContent = "error";
        this.pct.textContent = "";
        this.fill.style.width = "0%";
        return;
      }
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

    /** Lightweight transitions for cases without a full status object. */
    setLocal(label) {
      this.el.dataset.state = "working";
      this.label.textContent = label;
      this.pct.textContent = "";
      this.fill.style.width = "0%";
    }

    setError(label) {
      this.el.dataset.state = "error";
      this.label.textContent = label || "error";
      this.pct.textContent = "";
      this.fill.style.width = "0%";
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
