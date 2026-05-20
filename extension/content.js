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
      // Web Audio routing of the host video. Once a <video> is fed into a
      // MediaElementAudioSourceNode, its audio bypasses the default output
      // and only the Web Audio graph hears it — so setting hostGain to 0 is
      // a hard mute YouTube's player can't override. We connect to
      // destination at gain 0; on dispose we set the gain back to 1.
      this.hostSource = null;
      this.hostGain = null;
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
      this.disposed = false;
      this.originalMuted = video.muted;
      this.originalVolume = video.volume;
      this._boundHandlers = {
        play: () => this.reschedule(),
        pause: () => this.stopAll(),
        seeking: () => this.stopAll(),
        seeked: () => this.reschedule(),
        ratechange: () => this.reschedule(),
        emptied: () => this.dispose(),
      };
    }

    async start() {
      this.button.setState("working", "starting");

      let info;
      try {
        info = await this.requestJob();
      } catch (err) {
        console.warn("[nomusic] /process failed", err);
        this.button.setState("error", "backend unreachable");
        return;
      }

      this.jobId = info.job_id;
      this.totalChunks = info.total_chunks || 1;
      this.duration = info.duration_seconds || 0;

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
      this.attachVideoListeners();
      this.pollLoop();
      this.startSyncMonitor();

      this.button.setState("working", "0/" + this.totalChunks);
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
        this.button.setState(
          status.state === "ready" ? "active" : "working",
          `${status.chunks_ready}/${status.total_chunks || "?"}`,
        );

        if (status.state === "error") {
          this.button.setState("error", "engine error");
          return;
        }

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

        if (status.state === "ready") {
          this.button.setState("active", `${this.totalChunks}/${this.totalChunks}`);
          return; // stop polling
        }
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
      // immediately. We layer three mutes:
      //
      //   1. Override the instance's `muted` and `volume` properties so any
      //      direct setter the host page calls is a no-op. The internal
      //      state still has to be set via the original setter from
      //      HTMLMediaElement.prototype — instance overrides only shadow
      //      reads/writes that come through the instance.
      //   2. requestAnimationFrame loop that re-applies the underlying state
      //      via the original setter, in case the host bypasses the instance
      //      and writes through the prototype (e.g. .__proto__.muted setter).
      //   3. Web Audio routing of the host element. Often silenced for
      //      cross-origin media (so it won't single-handedly mute YouTube),
      //      but for same-origin players it's the cleanest mute.
      const proto = HTMLMediaElement.prototype;
      const mutedDesc = Object.getOwnPropertyDescriptor(proto, "muted");
      const volumeDesc = Object.getOwnPropertyDescriptor(proto, "volume");
      this._origDescriptors = { muted: mutedDesc, volume: volumeDesc };

      const realSetMuted = (v) => mutedDesc.set.call(this.video, v);
      const realSetVolume = (v) => volumeDesc.set.call(this.video, v);
      const realGetMuted = () => mutedDesc.get.call(this.video);
      const realGetVolume = () => volumeDesc.get.call(this.video);
      this._realSetMuted = realSetMuted;
      this._realSetVolume = realSetVolume;

      realSetMuted(true);
      realSetVolume(0);

      // Layer 1: instance overrides. The getter reports our intended values
      // so any host-page logic that checks `video.muted` keeps believing it
      // is muted, but the underlying state is the source of truth.
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

      // Layer 2: rAF re-assertion. Cheap (~one branch per frame).
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

      // Layer 3: Web Audio routing. May be silenced for cross-origin media.
      try {
        this.hostSource = this.audioCtx.createMediaElementSource(this.video);
        this.hostGain = this.audioCtx.createGain();
        this.hostGain.gain.value = 0;
        this.hostSource.connect(this.hostGain).connect(this.audioCtx.destination);
      } catch (err) {
        // Already routed by someone else, or cross-origin tainted. The
        // property-level mutes above cover this case.
        console.debug("[nomusic] MediaElementSource not used:", err?.message);
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

      if (this.hostGain) {
        // We can't fully un-route a MediaElementSource once created, so we
        // leave the graph in place at unity gain to restore audibility.
        try {
          this.hostGain.gain.value = 1.0;
        } catch (_err) {
          /* noop */
        }
      }
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
      this.unmuteVideo();
      try {
        this.audioCtx?.close();
      } catch (_err) {
        /* already closed */
      }
      this.audioCtx = null;
      this.chunks.clear();
      this.fetchedIdx.clear();
      this.button.setState("idle", "");
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
      this.dot = document.createElement("span");
      this.dot.className = "nomusic-btn__dot";
      this.label = document.createElement("span");
      this.label.textContent = "nomusic";
      this.progress = document.createElement("span");
      this.progress.className = "nomusic-btn__progress";
      this.el.append(this.dot, this.label, this.progress);
      this.el.addEventListener("click", (e) => {
        e.stopPropagation();
        e.preventDefault();
        this.toggle();
      });
      this.setState("idle", "");
    }

    setState(state, progress) {
      this.el.dataset.state = state;
      this.progress.textContent = progress || "";
      this.label.textContent =
        state === "active" ? "nomusic on" : state === "error" ? "nomusic" : "nomusic";
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
