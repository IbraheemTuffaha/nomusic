// Session: coordinates playback for one <video>. Owns the job lifecycle (SSE
// status, chunk fetch+decode), buffer pause/resume, and the video-event glue —
// and delegates the audio graph + scheduling to AudioScheduler and host muting
// to MuteController. Split out of the former monolithic content.js.
import { settings, dlog, SYNC_CHECK_MS } from "./settings.js";
import { MuteController } from "./mute-controller.js";
import { AudioScheduler } from "./audio-scheduler.js";

// ---------------------------------------------------------------------------
// Session: drives one <video> at a time. Reused if user toggles off and on.
// ---------------------------------------------------------------------------
class Session {
  constructor(video, button) {
    this.video = video;
    this.button = button;
    // Web Audio graph + chunk scheduling + sync monitor (created in start()).
    this.scheduler = null;
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
    // idx -> { buffer: AudioBuffer, playStart: number }. Written here as chunks
    // decode; read by the scheduler (shared reference).
    this.chunks = new Map();
    this.fetchedIdx = new Set();
    // SSE stream of backend status (replaces /status polling). Opened in
    // start(); closed in dispose() and when a terminal state arrives.
    this.eventSource = null;
    // True when we closed the stream because the user paused (not a buffer
    // pause). While closed, the backend sees no subscriber and starts its
    // idle-abandon clock; we re-establish the worker + stream on play.
    this._streamPausedClosed = false;
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
        this.scheduler?.reschedule();
        this._onUserPlay();
      },
      pause: () => {
        this.scheduler?.stopAll();
        this._onUserPause();
      },
      seeking: () => this.scheduler?.stopAll(),
      seeked: () => {
        dlog("seeked", {
          currentTime: this.video.currentTime,
          chunk: this._chunkIdxForTime(this.video.currentTime),
          buffered: this._isBuffered(this.video.currentTime),
          pausedByUs: this._pausedByUs,
          videoPaused: this.video.paused,
        });
        this.scheduler?.reschedule();
        this._reconcileBufferState();
        this._sendPrioritizeHint();
      },
      ratechange: () => this.scheduler?.reschedule(),
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
    } catch (err) {
      // Backend unreachable on resume; reopening the stream below will
      // surface the failure (204/CLOSED) without crashing playback.
      dlog("resume requestJob failed", err?.name || err);
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
      // The session can be disposed mid-fetch (toggle off, SPA navigation,
      // <video> emptied) — the scheduler is then null. Bail quietly rather
      // than throwing on a dead session's audio graph.
      if (this.disposed || !this.scheduler) return;
      const decoded = await this.scheduler.decode(buf);
      if (this.disposed || !this.scheduler) return;
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
        this.scheduler?.scheduleChunk(idx, entry);
      }
    } catch (err) {
      console.warn(`[nomusic] chunk ${idx} fetch/decode failed`, err);
      this.fetchedIdx.delete(idx);
    }
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
    } catch (err) {
      dlog("buffer pause: video element gone", err?.name || err);
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
    } catch (err) {
      dlog("resume: video element gone", err?.name || err);
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
      } catch (err) {
        dlog("dispose: resume video element gone", err?.name || err);
      }
    }
    this.chunks.clear();
    this.fetchedIdx.clear();
    // Error paths set the button to "error" and rely on its own
    // auto-revert timer for the visual transition. Calling button.dispose
    // here would clobber that.
    if (!preserveButtonState) this.button.dispose();
  }
}

export { Session };
