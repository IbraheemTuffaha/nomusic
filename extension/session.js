// Session coordinates one video's job, status stream and playback intent.
// ChunkLoader owns acquisition/retention, AudioScheduler owns the audio graph
// and scheduling, and MuteController owns host-video suppression.
import { settings, dlog, SYNC_CHECK_MS } from "./settings.js";
import { MuteController } from "./mute-controller.js";
import { AudioScheduler } from "./audio-scheduler.js";
import { ChunkLoader } from "./chunk-loader.js";
import { PlaybackIntent } from "./playback-intent.js";

// The source metadata can round down by almost one processing chunk while the
// native player reports the container's full duration. Keep the host muted
// through that bounded tail instead of failing after the last processed chunk;
// larger mismatches still fail visibly.
const MAX_END_TAIL_SECONDS = 10;

const FALLBACK_CLIENT_LEASE_SECONDS = 30;
const FALLBACK_CLIENT_HEARTBEAT_SECONDS = 10;

function newClientId() {
  try {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  } catch {
    // Some older extension contexts expose crypto only after startup.
  }
  return `nomusic-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

/** Clean a raw URL down to https://www.youtube.com/watch?v=ID, or null if it
 *  isn't a watch URL — so the caller can fall back to the page URL. Stripping
 *  the time/playlist params keeps the same video to one backend cache key. */
export function normalizeWatchUrl(raw) {
  if (!raw) return null;
  try {
    const url = new URL(raw, location.href);
    const host = url.hostname.toLowerCase();
    const isYouTube = host === "youtu.be" || host === "youtube.com" ||
      host.endsWith(".youtube.com");
    if (!isYouTube) return null;
    const id = url.searchParams.get("v") ||
      (host === "youtu.be" ? url.pathname.slice(1) : null);
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
    // One session owns one interest lease. It survives an SSE reconnect and
    // the ordinary pause retention window, so transport changes never cancel
    // another tab's work.
    this.clientId = newClientId();
    this._interestSupported = false;
    this._interestHeartbeatTimer = null;
    this._interestLeaseSeconds = FALLBACK_CLIENT_LEASE_SECONDS;
    this._interestHeartbeatSeconds = FALLBACK_CLIENT_HEARTBEAT_SECONDS;
    // The page URL captured when this session starts. The job's cache key is
    // derived from it, so resumes must POST the SAME url — reading the live
    // window.location.href on an SPA (a YouTube miniplayer, a Facebook URL
    // rewrite) would target a different video and strand this session.
    this.sourceUrl = null;
    this.totalChunks = 0;
    // These values are only a temporary placeholder until the mandatory
    // /capabilities response establishes the backend's actual geometry.
    this.chunkSeconds = 10;
    this.chunkOverlapSeconds = 0.5;
    this.duration = 0;
    // idx -> { buffer: AudioBuffer, playStart: number }. The loader writes
    // and evicts entries; the scheduler reads this shared window.
    this.chunks = new Map();
    this.failed = false;
    this.loader = this._createLoader();
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
    this._statusState = null;
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
      emptied: () => this.dispose({ restore: false }),
      loadstart: () => this.checkSource(),
      loadedmetadata: () => this.checkSource(),
      volumechange: () => this.muteController?.handleHostVolumeChange(),
    };
    this.playback = new PlaybackIntent(video, (wantsPlay) =>
      this._onPlaybackIntent(wantsPlay));
  }

  _createLoader() {
    return new ChunkLoader({
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
  }

  async start() {
    if (this.disposed || this.failed) return;
    if (this._starting) return this._starting;
    this.sourceUrl ||= resolveSourceUrl();
    this.mediaSource ||= { src: this.video.src || "", current: this.video.currentSrc || "" };
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
    this._configureInterest(info);
    this._statusState = info.state;
    if (info.state === "error") { this.fail(info.phase_label || "Processing failed"); return; }
    if (this._streamPausedClosed && !this.button._pendingDownload) this.button.setPaused();
    else this.button.showStatus(info);
    // Chunk geometry is part of the backend contract. Falling back to local
    // constants after a failed or malformed capabilities response can make
    // playStart disagree with the server's chunk layout and shift every chunk
    // after the first. Treat the response as mandatory so Retry can recover a
    // transient failure without silently desynchronizing playback.
    await this._loadCapabilities(signal);
    if (signal.aborted || this.disposed) return;
    this.scheduler = new AudioScheduler(this.video, {
      chunks: this.chunks,
      getStride: () => this.chunkSeconds - this.chunkOverlapSeconds,
      getTotalChunks: () => this.totalChunks,
    });
    await this.scheduler.init();
    if (signal.aborted || this.disposed) return;
    this.muteController.refresh();
    if (!this._streamPausedClosed || this.button._pendingDownload) {
      this._sendPrioritizeHint();
      this._openEventStream();
    }
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
    const body = {
      url: this.sourceUrl || window.location.href,
      client_id: this.clientId,
    };
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

  async _loadCapabilities(signal = this._requests.signal) {
    const caps = await this.fetchCapabilities();
    if (signal.aborted || this.disposed) return false;
    const chunkSeconds = Number(caps?.defaults?.chunk_seconds);
    const chunkOverlapSeconds = Number(caps?.defaults?.chunk_overlap_seconds);
    if (!Number.isFinite(chunkSeconds) || !Number.isFinite(chunkOverlapSeconds) ||
        chunkSeconds <= 0 || chunkOverlapSeconds < 0 ||
        chunkSeconds <= chunkOverlapSeconds) {
      throw new Error("Backend returned invalid chunk geometry");
    }
    this.chunkSeconds = chunkSeconds;
    this.chunkOverlapSeconds = chunkOverlapSeconds;
    return true;
  }

  _closeEventStream() {
    clearTimeout(this._streamTimer);
    this._streamTimer = null;
    this.eventSource?.close();
    this.eventSource = null;
  }

  _configureInterest(info) {
    const interest = info?.interest;
    if (!interest?.supported || !this.jobId) {
      this._interestSupported = false;
      this._stopInterestHeartbeat();
      return;
    }
    this._interestSupported = true;
    this._interestLeaseSeconds = Number(interest.lease_seconds) > 0
      ? Number(interest.lease_seconds) : FALLBACK_CLIENT_LEASE_SECONDS;
    this._interestHeartbeatSeconds = Number(interest.heartbeat_seconds) > 0
      ? Number(interest.heartbeat_seconds)
      : Math.min(FALLBACK_CLIENT_HEARTBEAT_SECONDS, this._interestLeaseSeconds / 2);
    this._startInterestHeartbeat();
  }

  _startInterestHeartbeat() {
    this._stopInterestHeartbeat();
    if (!this._interestSupported || !this.jobId || this.disposed) return;
    const period = Math.max(1, this._interestHeartbeatSeconds) * 1000;
    this._interestHeartbeatTimer = setInterval(() => {
      this._renewInterest();
    }, period);
  }

  _stopInterestHeartbeat() {
    if (this._interestHeartbeatTimer) clearInterval(this._interestHeartbeatTimer);
    this._interestHeartbeatTimer = null;
  }

  async _renewInterest() {
    if (!this._interestSupported || !this.jobId || this.disposed) return;
    try {
      const response = await fetch(
        `${this.config.backendUrl}/process/${this.jobId}/interest`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ client_id: this.clientId }),
          signal: this._requests.signal,
        },
      );
      if (response.status === 404) {
        // A restarted helper may have lost in-memory interest. The next
        // bounded /process reconnect will reacquire it from cached progress.
        this._interestSupported = false;
        this._stopInterestHeartbeat();
      } else if (!response.ok) {
        dlog("interest heartbeat failed", response.status);
      }
    } catch (err) {
      if (!this._requests.signal.aborted && !this.disposed) {
        dlog("interest heartbeat failed", err?.name || err);
      }
    }
  }

  _releaseInterest(jobId = this.jobId) {
    if (!this._interestSupported || !jobId || !this.clientId) return;
    // Do not use the session abort signal: dispose() aborts all work before
    // this best-effort release is sent. ``keepalive`` lets tab retirement
    // finish the small request while the page is being torn down.
    const query = new URLSearchParams({ client_id: this.clientId });
    fetch(`${this.config.backendUrl}/process/${jobId}/interest?${query}`, {
      method: "DELETE",
      keepalive: true,
    }).catch((err) => dlog("interest release failed", err?.name || err));
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
    if (wantsPlay) {
      this._onUserPlay();
      this._reconcileBufferState();
    } else {
      this.scheduler?.stopAll();
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
    this._stopInterestHeartbeat();
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
      if (!this._starting) this._resumeProcessing(); // Startup will open its own stream.
    }
  }

  /** User resumed after a pause that closed the stream. Re-ensure a worker
   *  exists (it may have been abandoned while paused; /process respawns it
   *  from disk-cached progress) and reopen the status stream. */
  _onUserPlay() {
    if (this.disposed || this._streamEnded || !this._streamPausedClosed) return;
    this._streamPausedClosed = false;
    if (this._starting) return;
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
      const previousJobId = this.jobId;
      this._releaseInterest(previousJobId);
      this._stopInterestHeartbeat();
      this._interestSupported = false;
      this._requests.abort();
      this._requests = new AbortController();
      this._closeEventStream();
      this.scheduler?.reset();
      this.loader.dispose();
      this.loader = this._createLoader();
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
    const previousJobId = this.jobId;
    if (info?.job_id) this._adoptJob(info);
    this._configureInterest(info);
    this._statusState = info?.state ?? this._statusState;
    if (info?.state === "error") { this.fail(info.phase_label || "Processing failed"); return; }
    // A same-job resume retains the geometry established at startup. A
    // changed job can come from a backend with different chunk settings, so
    // refresh the contract before reopening its stream.
    if (info?.job_id && previousJobId && info.job_id !== previousJobId) {
      const currentSignal = this._requests.signal;
      try {
        if (!await this._loadCapabilities(currentSignal)) return;
      } catch (err) {
        if (!currentSignal.aborted) {
          this.fail("Backend capabilities unavailable. Retry when the helper is ready.");
        }
        return;
      }
    }
    if (this._streamPausedClosed && !this.button._pendingDownload) return;
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
    this._statusState = status.state;
    if (status.state === "error") {
      this.fail(status.phase_label || "Processing failed");
      return;
    }
    this.button.showStatus(status);

    this.loader.updateAvailable(Array.isArray(status.ready_chunks)
      ? status.ready_chunks : []);

    if (status.state === "ready") {
      this._streamEnded = true;
      this._stopInterestHeartbeat();
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
    // A decode timeout cannot be cancelled. Retire the old loader so its
    // charged slots cannot make Retry fail immediately; late decode results
    // remain fenced by the disposed loader's generation/ownership checks.
    this.loader.dispose();
    this.loader = this._createLoader();
    this._requests = new AbortController();
    this._starting = this._resuming = null;
    this._reconnectAttempts = 0;
    this._streamEnded = this._streamPausedClosed = false;
    this.jobId = null;
    await this.start();
  }

  _stopProcessing() {
    this._requests.abort();
    this._stopInterestHeartbeat();
    this._closeEventStream();
    for (const key of ["bufferTimer", "_prioritizeTimer", "_reconnectTimer"]) {
      clearTimeout(this[key]);
      this[key] = null;
    }
    this.scheduler?.dispose();
    this.scheduler = null;
    // Keep unabortable decodes fenced to this loader until they settle.
    this.loader.reset();
  }

  // -- buffer pause/resume -------------------------------------------------

  _chunkIdxForTime(t) {
    const stride = this.chunkSeconds - this.chunkOverlapSeconds;
    return Math.max(0, Math.floor(t / stride));
  }

  _isBuffered(t) {
    const lastIdx = this.totalChunks - 1;
    const idx = Math.min(this._chunkIdxForTime(t), lastIdx);
    const entry = this.chunks.get(idx);
    if (!entry) return false;
    if (idx !== lastIdx) return true;
    const audioEnd = entry.playStart + entry.buffer.duration;
    if (t < audioEnd) return true;
    // Keep original audio suppressed while the native player crosses a tiny
    // final tail. Larger/unknown gaps are not covered by a decoded buffer.
    return this._streamEnded && Number.isFinite(this.video.duration) &&
      t <= this.video.duration &&
      this.video.duration - audioEnd <= MAX_END_TAIL_SECONDS;
  }

  _pauseForBuffer({ showBufferingLabel = true } = {}) {
    if (this.disposed) return;
    const mayShowBuffering = this._statusState === "ready" || this._streamEnded;
    if (this.playback.held) {
      if (showBufferingLabel && mayShowBuffering && this.playback.wantsPlay) {
        this.button.setBuffering();
      }
      return;
    }
    if (showBufferingLabel && mayShowBuffering && this.playback.wantsPlay) {
      this.button.setBuffering();
    }
    this.playback.hold();
  }

  _resumeAfterBuffer() {
    if (!this.playback.held || this.disposed || this.video.ended) return;
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
      const final = this.totalChunks - 1;
      if (final < 0) return;
      const fromChunk = Math.min(final, this._chunkIdxForTime(this.video.currentTime));
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
    if (this.disposed || this.video.ended) return;
    if (this.failed) { this.playback.hold(); return; }
    if (!this.scheduler) return;
    this.loader.reconcile();
    if (this.failed) return;
    const time = this.video.currentTime;
    if (this._isBuffered(time)) {
      this._resumeAfterBuffer();
    } else {
      const last = this.chunks.get(this.totalChunks - 1);
      if (this._streamEnded && last && time >= last.playStart + last.buffer.duration) {
        this.fail("Processed audio ended before the video. Retry or return to original.");
      } else {
        this._pauseForBuffer();
      }
    }
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

  /** Navigation can reuse a video without emitting emptied. Compare the
   *  playing YouTube identity, not its miniplayer's surrounding page URL. */
  checkSource() {
    if (this.disposed || !this.sourceUrl || !this.mediaSource) return;
    const playingRaw = resolveSourceUrl();
    const playing = normalizeWatchUrl(playingRaw) || this._rawSourceIdentity(playingRaw);
    const original = normalizeWatchUrl(this.sourceUrl) || this._rawSourceIdentity(this.sourceUrl);
    const current = this.video.currentSrc || "";
    if ((playing && original && playing !== original) ||
        (this.video.src || "") !== this.mediaSource.src ||
        (current && this.mediaSource.current && current !== this.mediaSource.current)) {
      this.dispose({ restore: false });
      return;
    }
    // First metadata can arrive after the initial request; remember it once
    // without confusing resource initialization with a replacement.
    if (!this.mediaSource.current && current) this.mediaSource.current = current;
  }

  _rawSourceIdentity(raw) {
    if (!raw) return "";
    try {
      return new URL(raw, location.href).href;
    } catch {
      return String(raw);
    }
  }

  dispose({ restore = true } = {}) {
    if (this.disposed) return;
    this._releaseInterest();
    this.disposed = true;
    this._stopProcessing();
    this.loader.dispose();
    this.detachVideoListeners();
    // Source replacement and detached-video retirement must not restore the
    // native track while the element is still playing. Pause under the
    // playback-intent owner before releasing the volume pin.
    if (!restore) this.playback.hold();
    this.muteController?.dispose();
    this.muteController = null;
    this.playback.dispose({ restore });
    this.button.dispose();
  }
}
