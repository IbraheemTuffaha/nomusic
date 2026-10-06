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
    this._starting = null;
    this._reconnectTimer = null;
    this._streamTimer = null;
    this._reconnectAttempts = 0;
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
    this.failed = false;
    this.loader = new ChunkLoader({
      chunks: this.chunks,
      getTime: () => this.video.currentTime,
      getStride: () => this.chunkSeconds - this.chunkOverlapSeconds,
      getTotalChunks: () => this.totalChunks,
      getChunkUrl: (idx) => `${this.config.backendUrl}/chunk/${this.jobId}/${idx}`,
      decode: (encoded) => this.scheduler.decode(encoded),
      onChunk: (idx, entry) => this._chunkArrived(idx, entry),
      onError: (message) => this.fail(message),
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
    if (this.disposed || this.failed) return;
    if (this._starting) return this._starting;
    this.sourceUrl ||= resolveSourceUrl();
    // Suppression and the hold belong to the selection, including while the
    // first request is pending or when setup fails. Retry keeps these owners.
    this.playback.hold();
    this.attachVideoListeners();
    this.button.setStarting();
    const signal = this._requests.signal;
    const attempt = this._startProcessing(signal);
    this._starting = attempt;
    try {
      await attempt;
    } catch (err) {
      if (!signal.aborted && !this.disposed) {
        dlog("playback setup failed", err?.name || err);
        this.fail("Could not start playback. Check the backend and site permissions.");
      }
    } finally {
      if (this._starting === attempt) this._starting = null;
    }
  }

  async _startProcessing(signal) {
    if (!this.muteController) {
      this.muteController = new MuteController(this.video, (level, immediate) =>
        this.scheduler?.setVolume(level, immediate));
      this.muteController.mute();
    }
    const info = await this.requestJob();
    if (signal.aborted || this.disposed) return;
    this._adoptJob(info);
    if (info.state === "error") { this.fail(info.phase_label || "Processing failed"); return; }
    this.button.showStatus(info);
    try {
      const caps = await this.fetchCapabilities();
      if (signal.aborted || this.disposed) return;
      this.chunkSeconds = caps?.defaults?.chunk_seconds ?? this.chunkSeconds;
      this.chunkOverlapSeconds = caps?.defaults?.chunk_overlap_seconds ?? this.chunkOverlapSeconds;
    } catch (err) {
      dlog("capabilities fetch failed; using defaults", err?.name || err);
    }
    if (signal.aborted || this.disposed) return;
    this.scheduler = new AudioScheduler(this.video, {
      chunks: this.chunks,
      getStride: () => this.chunkSeconds - this.chunkOverlapSeconds,
      getTotalChunks: () => this.totalChunks,
    });
    await this.scheduler.init();
    if (signal.aborted || this.disposed) return;
    this.muteController.refresh();
    this._sendPrioritizeHint();
    this._openEventStream();
    this.startBufferMonitor();
  }

  async _fetchJSON(path, options, timeoutMs) {
    const parent = this._requests.signal;
    const controller = new AbortController();
    const abort = () => controller.abort();
    parent.addEventListener("abort", abort, { once: true });
    if (parent.aborted) abort();
    const timer = setTimeout(abort, timeoutMs);
    try {
      const response = await fetch(`${this.config.backendUrl}${path}`, {
        ...options, signal: controller.signal,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return await response.json();
    } finally {
      clearTimeout(timer);
      parent.removeEventListener("abort", abort);
    }
  }

  requestJob() {
    const body = { url: this.sourceUrl || window.location.href };
    if (this.config.model) body.model = this.config.model;
    if (this.config.keepStems) body.keep_stems = this.config.keepStems;
    return this._fetchJSON("/process", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }, 30_000);
  }

  fetchCapabilities() {
    return this._fetchJSON("/capabilities", {}, 5_000);
  }

  _closeEventStream() {
    clearTimeout(this._streamTimer);
    this._streamTimer = null;
    this.eventSource?.close();
    this.eventSource = null;
  }

  /** Own reconnection so CONNECTING and CLOSED failures both have a budget.
   *  Re-POST before reopening: a restarted backend may need to respawn work. */
  _openEventStream() {
    if (this.disposed || this.failed) return;
    this._closeEventStream();
    let stream;
    try {
      stream = new EventSource(`${this.config.backendUrl}/events/${this.jobId}`);
    } catch {
      this._recoverStream();
      return;
    }
    this.eventSource = stream;
    // The backend sends an initial snapshot immediately. Once it arrives,
    // inference may take much longer; SSE keepalives maintain the connection.
    this._streamTimer = setTimeout(() => this._recoverStream(), 15_000);
    stream.onmessage = (event) => {
      if (this.disposed || this.failed || this.eventSource !== stream) return;
      let status;
      try { status = JSON.parse(event.data); } catch { return; }
      if (!status || status.job_id !== this.jobId ||
          !["queued", "probing", "downloading", "processing", "ready", "error"].includes(status.state)) return;
      clearTimeout(this._streamTimer);
      this._streamTimer = null;
      this._reconnectAttempts = 0;
      this.handleStatus(status);
    };
    stream.onerror = () => {
      if (this.eventSource === stream) this._recoverStream();
    };
  }

  _recoverStream() {
    this._closeEventStream();
    if (this.disposed || this.failed || this._streamEnded || this._streamPausedClosed) return;
    if (this._reconnectTimer) return;
    if (this._reconnectAttempts === 3) {
      this.fail("Connection lost. Check the backend and retry.");
      return;
    }
    const delay = 500 * 2 ** this._reconnectAttempts++;
    this._reconnectTimer = setTimeout(() => {
      this._reconnectTimer = null;
      this._resumeProcessing();
    }, delay);
  }

  _onPlaybackIntent(wantsPlay) {
    if (this.disposed) return;
    if (this.failed) { this.playback.hold(); return; }
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
    this._closeEventStream();
    clearTimeout(this._reconnectTimer);
    this._reconnectTimer = null;
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
    if (this.disposed || this.failed) return;
    if (this._resuming) return this._resuming;
    clearTimeout(this._reconnectTimer);
    this._reconnectTimer = null;
    const attempt = this._resumeJob();
    this._resuming = attempt;
    try {
      await attempt;
    } finally {
      if (this._resuming === attempt) this._resuming = null;
    }
  }

  _adoptJob(info) {
    if (this.jobId && info.job_id !== this.jobId) {
      this._requests.abort();
      this._requests = new AbortController();
      this._closeEventStream();
      this.scheduler?.reset();
      this.loader.reset();
      this.failed = false;
      this._streamEnded = false;
    }
    this.jobId = info.job_id;
    this.totalChunks = info.total_chunks || this.totalChunks || 1;
    this.duration = info.duration_seconds ?? this.duration;
  }

  async _resumeJob() {
    const signal = this._requests.signal;
    let info;
    try {
      info = await this.requestJob();
    } catch (err) {
      if (!signal.aborted) this._recoverStream();
      return;
    }
    if (signal.aborted || this.disposed || this.failed) return;
    // A user can pause again while /process is in flight. That latest choice
    // still owns the stream unless a queued export needs processing to finish.
    if (this._streamPausedClosed && !this.button._pendingDownload) return;
    if (info?.job_id) this._adoptJob(info);
    if (info?.state === "error") { this.fail(info.phase_label || "Processing failed"); return; }
    if (!this.eventSource) this._openEventStream();
    // Re-point the worker at where the user actually is, in case it was
    // abandoned and respawned with a from-scratch chunk order.
    this._sendPrioritizeHint();
  }

  /** Apply one status snapshot (initial or pushed). Mirrors what the old
   *  poll loop did per tick: repaint the label, fetch any newly-ready
   *  chunks, and tear down on a terminal state. */
  handleStatus(status) {
    if (this.disposed || this.failed) return;
    this.totalChunks = status.total_chunks || this.totalChunks;
    if (status.state === "error") {
      this.fail(status.phase_label || "Processing failed");
      return;
    }
    this.button.showStatus(status);

    this.loader.updateAvailable(Array.isArray(status.ready_chunks)
      ? status.ready_chunks : []);

    if (status.state === "ready") {
      this._streamEnded = true;
      this._closeEventStream();
    }
  }

  _chunkArrived(idx, entry) {
    if (this.disposed || this.failed) return;
    if (this.playback.held && this._isBuffered(this.video.currentTime)) {
      this._resumeAfterBuffer();
    }
    if (!this.video.paused) this.scheduler.scheduleChunk(idx, entry);
  }

  /** Failure stops processing/audio work, but selection still owns silence. */
  fail(message) {
    if (this.disposed || this.failed) return;
    this.failed = true;
    this.playback.hold();
    this._stopProcessing();
    this.button.setError(message);
  }

  async retry() {
    if (this.disposed || !this.failed) return;
    this.failed = false;
    this._requests = new AbortController();
    this._starting = this._resuming = null;
    this._reconnectAttempts = 0;
    this._streamEnded = this._streamPausedClosed = false;
    this.jobId = null;
    await this.start();
  }

  _stopProcessing() {
    this._requests.abort();
    this._closeEventStream();
    for (const key of ["bufferTimer", "_prioritizeTimer", "_reconnectTimer"]) {
      clearTimeout(this[key]);
      this[key] = null;
    }
    this.scheduler?.dispose();
    this.scheduler = null;
    // Keep unabortable decodes charged to this loader even across Retry.
    this.loader.reset();
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
    if (this.disposed || this.failed || this._streamEnded || !this.jobId) return;
    if (this._prioritizeTimer) clearTimeout(this._prioritizeTimer);
    this._prioritizeTimer = setTimeout(() => {
      this._prioritizeTimer = null;
      if (this.disposed || this.failed || this._streamEnded || !this.jobId) return;
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
    if (this.disposed) return;
    if (this.failed) { this.playback.hold(); return; }
    if (!this.scheduler) return;
    this.loader.reconcile();
    if (this.failed) return;
    if (this._isBuffered(this.video.currentTime)) this._resumeAfterBuffer();
    else this._pauseForBuffer();
  }

  /** Maintain the chunk window and reconcile buffering every SYNC_CHECK_MS. */
  startBufferMonitor() {
    const tick = () => {
      if (this.disposed || this.failed) return;
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

  dispose({ restore = true } = {}) {
    if (this.disposed) return;
    this.disposed = true;
    this._stopProcessing();
    this.loader.dispose();
    this.detachVideoListeners();
    this.muteController?.dispose();
    this.muteController = null;
    this.playback.dispose({ restore });
    this.button.dispose();
  }
}
