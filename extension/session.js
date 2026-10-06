// Session coordinates one video's job, status stream and playback intent.
// ChunkLoader owns acquisition/retention, AudioScheduler owns the audio graph
// and scheduling, and MuteController owns host-video suppression.
import { settings, dlog, SYNC_CHECK_MS } from "./settings.js";
import { MuteController } from "./mute-controller.js";
import { AudioScheduler } from "./audio-scheduler.js";
import { ChunkLoader } from "./chunk-loader.js";
import { PlaybackIntent } from "./playback-intent.js";

/** Clean a raw URL down to https://www.youtube.com/watch?v=ID, or null if it
 *  isn't a watch URL — so the caller can fall back to the page URL. Stripping
 *  the time/playlist params keeps the same video to one backend cache key. */
export function normalizeWatchUrl(raw) {
  if (!raw || !/[?&]v=/.test(raw)) return null;
  try {
    const id = new URL(raw, location.href).searchParams.get("v");
    return id ? `https://www.youtube.com/watch?v=${id}` : null;
  } catch {
    return null;
  }
}

/** Best-effort canonical URL of the video that's actually playing.
 *
 *  Normally window.location.href IS the video's URL, but a decoupled player
 *  breaks that: YouTube's miniplayer keeps a video playing in the corner while
 *  you browse the homepage, so location.href is the page you're on (e.g.
 *  /feed/history) and posting that to /process makes yt-dlp try to download the
 *  page (no duration -> error). Ask the MAIN-world bridge (page-script.js) for
 *  the URL via YouTube's player API; it answers synchronously by setting a
 *  documentElement attribute. Falls back to the page URL when there's no answer
 *  (all non-YouTube sites), so behaviour elsewhere is unchanged. */
export function resolveSourceUrl() {
  try {
    const root = document.documentElement;
    root.removeAttribute("data-nomusic-source-url");
    document.dispatchEvent(new CustomEvent("nomusic:resolve-source-url"));
    const url = normalizeWatchUrl(root.getAttribute("data-nomusic-source-url"));
    if (url) return url;
  } catch (err) {
    dlog("resolveSourceUrl failed; using page URL", err?.name || err);
  }
  return location.href;
}

// ---------------------------------------------------------------------------
// Session: drives one <video> with one captured configuration until disposed.
// ---------------------------------------------------------------------------
export class Session {
  constructor(video, button) {
    this.video = video;
    this.button = button;
    // Settings changes apply to the next session, never half of this artifact.
    this.config = {
      backendUrl: settings.backendUrl,
      model: settings.model,
      keepStems: settings.keepStems?.slice() ?? null,
    };
    this._requests = new AbortController();
    this._resuming = null;
    // Web Audio graph + chunk scheduling + sync monitor (created in start()).
    this.scheduler = null;
    // Owns host-video muting + volume mirroring; created in start().
    this.muteController = null;
    this.jobId = null;
    // The page URL captured when this session starts. The job's cache key is
    // derived from it, so resumes must POST the SAME url — reading the live
    // window.location.href on an SPA (a YouTube miniplayer, a Facebook URL
    // rewrite) would target a different video and strand this session.
    this.sourceUrl = null;
    this.totalChunks = 0;
    // Must mirror the backend defaults (config.py: chunk_seconds=10,
    // chunk_overlap_seconds=0.5). fetchCapabilities() overwrites these, but
    // it is best-effort — if it fails these stay in force, and a wrong value
    // throws stride/playStart ~3x off and desyncs every chunk after the first.
    this.chunkSeconds = 10;
    this.chunkOverlapSeconds = 0.5;
    this.duration = 0;
    // idx -> { buffer: AudioBuffer, playStart: number }. The loader writes
    // and evicts entries; the scheduler reads this shared window.
    this.chunks = new Map();
    this._chunkError = false;
    this.loader = new ChunkLoader({
      chunks: this.chunks,
      getTime: () => this.video.currentTime,
      getStride: () => this.chunkSeconds - this.chunkOverlapSeconds,
      getTotalChunks: () => this.totalChunks,
      getChunkUrl: (idx) => `${this.config.backendUrl}/chunk/${this.jobId}/${idx}`,
      decode: (encoded) => this.scheduler.decode(encoded),
      onChunk: (idx, entry) => this._chunkArrived(idx, entry),
      onError: (message) => this._failChunk(message),
      onWindowChange: () => this.scheduler?.pruneBuffers(),
    });
    // SSE stream of backend status (replaces /status polling). Opened in
    // start(); closed in dispose() and when a terminal state arrives.
    this.eventSource = null;
    // True when we closed the stream because the user paused (not a buffer
    // pause). While closed, the backend sees no subscriber and starts its
    // idle-abandon clock; we re-establish the worker + stream on play.
    this._streamPausedClosed = false;
    this.bufferTimer = null;
    this.disposed = false;
    // Flipped true once the SSE stream ends (state == ready/error, or the
    // server closed it). Tells _resumeAfterBuffer that no future status
    // event will repaint the label, so it has to restore "nomusic on".
    this._streamEnded = false;
    // Debounce timer for the /prioritize POST on seek so scrubbing a
    // timeline doesn't fire one request per intermediate frame.
    this._prioritizeTimer = null;
    this._boundHandlers = {
      play: () => {
        this._reconcileBufferState();
        this.scheduler?.reschedule();
      },
      pause: () => {
        this.scheduler?.stopAll();
      },
      seeking: () => this.scheduler?.stopAll(),
      seeked: () => {
        dlog("seeked", {
          currentTime: this.video.currentTime,
          chunk: this._chunkIdxForTime(this.video.currentTime),
          buffered: this._isBuffered(this.video.currentTime),
          held: this.playback.held,
          videoPaused: this.video.paused,
        });
        this._reconcileBufferState();
        this.scheduler?.reschedule();
        this._sendPrioritizeHint();
      },
      ratechange: () => this.scheduler?.reschedule(),
      emptied: () => this.dispose(),
      volumechange: () => this.muteController?.handleHostVolumeChange(),
    };
    this.playback = new PlaybackIntent(video, (wantsPlay) =>
      this._onPlaybackIntent(wantsPlay));
  }

  async start() {
    // Pin the video's URL up front so every later /process (resume after a
    // pause/idle-abandon) targets this same video, even if the SPA has since
    // changed window.location.href. Resolve it from the player rather than the
    // address bar so starting from the YouTube miniplayer captures the playing
    // video, not the homepage the user is browsing.
    this.sourceUrl = resolveSourceUrl();
    this.video.addEventListener("emptied", this._boundHandlers.emptied);
    let info;
    try {
      info = await this.requestJob();
    } catch (err) {
      if (this.disposed) return;
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

    this._adoptJob(info);
    this.button.showStatus(info);

    try {
      const caps = await this.fetchCapabilities();
      this.chunkSeconds = caps?.defaults?.chunk_seconds ?? this.chunkSeconds;
      this.chunkOverlapSeconds =
        caps?.defaults?.chunk_overlap_seconds ?? this.chunkOverlapSeconds;
    } catch (err) {
      // capabilities is best-effort; defaults are reasonable.
      dlog("capabilities fetch failed; using defaults", err?.name || err);
    }
    if (this.disposed) return; // re-check after the second await (see above).

    // The scheduler owns the audio graph, chunk scheduling, the time-stretcher,
    // and the sync monitor. It reads the shared chunk map and a couple of live
    // getters (stride/total chunks). init() creates the AudioContext + loads
    // stretch.js (one more await — re-check disposed after it).
    this.scheduler = new AudioScheduler(this.video, {
      chunks: this.chunks,
      getStride: () => this.chunkSeconds - this.chunkOverlapSeconds,
      getTotalChunks: () => this.totalChunks,
    });
    await this.scheduler.init();
    if (this.disposed) return;

    // Mute the host video and mirror its volume onto our audio output. The
    // callback hands the effective level to the scheduler's gain.
    this.muteController = new MuteController(this.video, (level, immediate) =>
      this.scheduler?.setVolume(level, immediate),
    );
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
    this.startBufferMonitor();
  }

  async requestJob() {
    const body = { url: this.sourceUrl || window.location.href };
    if (this.config.model) body.model = this.config.model;
    if (this.config.keepStems) body.keep_stems = this.config.keepStems;
    const resp = await fetch(`${this.config.backendUrl}/process`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: this._requests.signal,
    });
    if (!resp.ok) {
      const detail = await resp.text().catch(() => "");
      throw new Error(`${resp.status}: ${detail}`);
    }
    return resp.json();
  }

  async fetchCapabilities() {
    const resp = await fetch(`${this.config.backendUrl}/capabilities`, {
      signal: this._requests.signal,
    });
    if (!resp.ok) return null;
    return resp.json();
  }

  /** Subscribe to the backend's SSE status stream. EventSource handles
   *  reconnection on transient network drops on its own; the backend
   *  returns 204 for an unknown job, which drives readyState to CLOSED and
   *  stops the reconnect loop. */
  _openEventStream() {
    if (this.disposed) return;
    const stream = new EventSource(
      `${this.config.backendUrl}/events/${this.jobId}`,
    );
    this.eventSource = stream;
    stream.onmessage = (e) => {
      if (this.disposed || this.eventSource !== stream) return;
      let payload;
      try {
        payload = JSON.parse(e.data);
      } catch (err) {
        console.warn("[nomusic] bad SSE payload", err);
        return;
      }
      this.handleStatus(payload);
    };
    stream.onerror = () => {
      if (this.disposed || this.eventSource !== stream) return;
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

  _onPlaybackIntent(wantsPlay) {
    if (this.disposed) return;
    if (this._chunkError) { this.playback.hold(); return; }
    if (!this.scheduler) return; // Startup will use the latest captured intent.
    if (wantsPlay) {
      this._onUserPlay();
      this._reconcileBufferState();
    } else {
      this.scheduler.stopAll();
      this._onUserPause();
    }
  }

  /** User paused the video (not a buffer pause). Close the status stream so
   *  the backend sees no subscriber and starts its idle-abandon countdown —
   *  if the user stays away, the worker releases the GPU. Re-established on
   *  play. No-op during a buffer pause: we still need chunk-ready events to
   *  know when to resume, and a fully-processed job has no worker to idle. */
  _onUserPause() {
    if (this.disposed || this._streamEnded) return;
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
    if (this.disposed || this._chunkError) return;
    if (this._resuming) return this._resuming;
    this._resuming = this._resumeJob();
    try {
      await this._resuming;
    } finally {
      this._resuming = null;
    }
  }

  _adoptJob(info) {
    if (this.jobId && info.job_id !== this.jobId) {
      this._requests.abort();
      this._requests = new AbortController();
      this.eventSource?.close();
      this.eventSource = null;
      this.scheduler?.reset();
      this.loader.reset();
      this._chunkError = false;
      this._streamEnded = false;
    }
    this.jobId = info.job_id;
    this.totalChunks = info.total_chunks || this.totalChunks || 1;
    this.duration = info.duration_seconds ?? this.duration;
  }

  async _resumeJob() {
    let info;
    try {
      info = await this.requestJob();
    } catch (err) {
      // Backend unreachable on resume; reopening the stream below will
      // surface the failure (204/CLOSED) without crashing playback.
      dlog("resume requestJob failed", err?.name || err);
    }
    if (this.disposed || this._chunkError) return;
    // A user can pause again while /process is in flight. That latest choice
    // still owns the stream unless a queued export needs processing to finish.
    if (this._streamPausedClosed && !this.button._pendingDownload) return;
    if (info?.job_id) this._adoptJob(info);
    if (!this.eventSource) this._openEventStream();
    // Re-point the worker at where the user actually is, in case it was
    // abandoned and respawned with a from-scratch chunk order.
    this._sendPrioritizeHint();
  }

  /** Apply one status snapshot (initial or pushed). Mirrors what the old
   *  poll loop did per tick: repaint the label, fetch any newly-ready
   *  chunks, and tear down on a terminal state. */
  handleStatus(status) {
    if (this.disposed || this._chunkError) return;
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

    this.loader.updateAvailable(Array.isArray(status.ready_chunks)
      ? status.ready_chunks : []);

    if (status.state === "ready") {
      this._streamEnded = true;
      if (this.eventSource) this.eventSource.close();
    }
  }

  _chunkArrived(idx, entry) {
    if (this.disposed || this._chunkError) return;
    if (this.playback.held && this._isBuffered(this.video.currentTime)) {
      this._resumeAfterBuffer();
    }
    if (!this.video.paused) this.scheduler.scheduleChunk(idx, entry);
  }

  // T4 supplies the shared recovery UI. Retain suppression while a terminal
  // acquisition error is visible instead of dispose() restoring original audio.
  _failChunk(message) {
    if (this.disposed || this._chunkError) return;
    this._chunkError = true;
    this._streamEnded = true;
    this._requests.abort();
    this.eventSource?.close();
    this.eventSource = null;
    clearTimeout(this.bufferTimer);
    clearTimeout(this._prioritizeTimer);
    this.scheduler?.stopAll();
    this.playback.hold();
    this.button.setError(message);
    this.button._clearErrorRevert?.();
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
    if (this.playback.held || this.disposed) return;
    if (showBufferingLabel && this.playback.wantsPlay) this.button.setBuffering();
    this.playback.hold();
  }

  _resumeAfterBuffer() {
    if (!this.playback.held || this.disposed) return;
    if (this._streamEnded && this.button.el.dataset.state === "working") {
      this.button.showStatus({ state: "ready" });
    }
    this.playback.release();
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
      fetch(`${this.config.backendUrl}/process/${this.jobId}/prioritize`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ from_chunk: fromChunk }),
        signal: this._requests.signal,
      })
        .then((r) => dlog("prioritize response", r.status))
        .catch((err) => dlog("prioritize POST failed", err));
    }, 250);
  }

  /** Buffering controls a temporary hold; it never changes user intent. */
  _reconcileBufferState() {
    if (this.disposed || !this.scheduler) return;
    if (this._chunkError) { this.playback.hold(); return; }
    this.loader.reconcile();
    if (this._chunkError) return;
    if (this._isBuffered(this.video.currentTime)) this._resumeAfterBuffer();
    else this._pauseForBuffer();
  }

  /** Maintain the chunk window and reconcile buffering every SYNC_CHECK_MS. */
  startBufferMonitor() {
    const tick = () => {
      if (this.disposed) return;
      this.bufferTimer = setTimeout(tick, SYNC_CHECK_MS);
      this._reconcileBufferState();
    };
    tick();
  }


  // -- video glue -----------------------------------------------------------

  attachVideoListeners() {
    for (const [name, handler] of Object.entries(this._boundHandlers)) {
      this.video.addEventListener(name, handler);
    }
    // If the video is already playing, schedule immediately.
    if (!this.video.paused) this.scheduler?.reschedule();
  }

  detachVideoListeners() {
    for (const [name, handler] of Object.entries(this._boundHandlers)) {
      this.video.removeEventListener(name, handler);
    }
  }

  dispose({ preserveButtonState = false } = {}) {
    if (this.disposed) return;
    this.disposed = true;
    this._requests.abort();
    this.loader.dispose();
    this.detachVideoListeners();
    // Tears down the audio graph: stops sources, clears the sync monitor,
    // closes the AudioContext, disposes the stretcher + caches.
    this.scheduler?.dispose();
    this.scheduler = null;
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
    }
    if (this.bufferTimer) clearTimeout(this.bufferTimer);
    if (this._prioritizeTimer) clearTimeout(this._prioritizeTimer);
    this.muteController?.dispose();
    this.muteController = null;
    this.playback.dispose({ restore: true });
    this.chunks.clear();
    // Error paths set the button to "error" and rely on its own
    // auto-revert timer for the visual transition. Calling button.dispose
    // here would clobber that.
    if (!preserveButtonState) this.button.dispose();
  }
}
