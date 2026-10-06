// Owns encoded fetches, decoding and the rolling playback cache. Server-ready
// indices are metadata; they do not imply that audio has been fetched/decoded.
export const BACKWARD_SECONDS = 20;
export const FORWARD_SECONDS = 45;
const CONCURRENCY = 3;
const NETWORK_TIMEOUT_MS = 15_000;
const DECODE_TIMEOUT_MS = 15_000;
const MAX_ATTEMPTS = 4;
const RETRY_DELAY_MS = 500;

export class ChunkLoader {
  constructor({ chunks, getTime, getStride, getTotalChunks, getChunkUrl,
    decode, onChunk, onError, onWindowChange }) {
    Object.assign(this, { chunks, getTime, getStride, getTotalChunks,
      getChunkUrl, decode, onChunk, onError, onWindowChange });
    this.available = new Set();
    this.active = new Map();
    this.retries = new Map();
    this.wanted = new Set();
    this.generation = 0;
    this.nextRequest = 0;
    this.retryTimer = null;
    this.failed = false;
    this.disposed = false;
  }

  updateAvailable(indices) {
    if (this.disposed || this.failed) return;
    for (const idx of indices) {
      if (Number.isInteger(idx) && idx >= 0 && idx < this.getTotalChunks()) {
        this.available.add(idx);
      }
    }
    this.reconcile();
  }

  reconcile() {
    if (this.disposed || this.failed) return;
    clearTimeout(this.retryTimer);
    this.retryTimer = null;
    const time = this.getTime();
    const stride = this.getStride();
    const current = Math.floor(Math.max(0, time) / stride);
    const first = Math.floor(Math.max(0, time - BACKWARD_SECONDS) / stride);
    const last = Math.min(this.getTotalChunks() - 1,
      Math.floor((time + FORWARD_SECONDS) / stride));
    const order = [];
    for (let idx = current; idx <= last; idx++) order.push(idx);
    for (let idx = Math.min(current - 1, last); idx >= first; idx--) order.push(idx);
    this.wanted = new Set(order);

    for (const idx of this.chunks.keys()) {
      if (!this.wanted.has(idx)) this.chunks.delete(idx);
    }
    for (const idx of this.retries.keys()) {
      if (!this.wanted.has(idx)) this.retries.delete(idx);
    }
    for (const request of this.active.values()) {
      if (!this.wanted.has(request.idx)) this._cancel(request);
    }
    this.onWindowChange();

    const currentRequests = [...this.active.values()].filter((request) => this._current(request));
    const queueBlocked = this.active.size >= CONCURRENCY && order.some((idx) =>
      this.available.has(idx) && !this.chunks.has(idx) &&
      !currentRequests.some((request) => request.idx === idx));
    if ([...this.active.values()].some((request) => request.decodeExpired &&
      (this._current(request) || queueBlocked))) {
      this._fail("Audio decoding timed out; retry playback");
      return;
    }

    const now = Date.now();
    let nextRetry = Infinity;
    for (const idx of order) {
      if (!this.available.has(idx) || this.chunks.has(idx)) continue;
      if ([...this.active.values()].some((request) =>
        request.idx === idx && this._current(request))) continue;
      const retry = this.retries.get(idx);
      if (retry?.attempts >= MAX_ATTEMPTS) {
        if (idx === current) {
          this._fail(`Audio chunk ${idx + 1} failed after ${MAX_ATTEMPTS} attempts`);
          return;
        }
        continue;
      }
      if (retry?.deadline > now) {
        nextRetry = Math.min(nextRetry, retry.deadline);
        continue;
      }
      if (this.active.size < CONCURRENCY) this._start(idx);
    }
    if (Number.isFinite(nextRetry)) {
      this.retryTimer = setTimeout(() => this.reconcile(), nextRetry - now);
    }
  }

  _current(request) {
    return !this.disposed && !this.failed && !request.cancelled &&
      request.generation === this.generation && this.wanted.has(request.idx);
  }

  _cancel(request) {
    request.cancelled = true;
    clearTimeout(request.timeout);
    request.controller.abort();
    if (this.disposed) clearTimeout(request.decodeTimeout);
    // Fetch/body abort normally settles promptly. Audio decoding cannot be
    // aborted, so every slot stays charged until its operation actually ends.
  }

  _fail(message) {
    this.failed = true;
    for (const request of this.active.values()) {
      this._cancel(request);
      clearTimeout(request.decodeTimeout);
    }
    this.onError(message);
  }

  _start(idx) {
    const retry = this.retries.get(idx) ?? { attempts: 0, deadline: 0 };
    retry.attempts++;
    this.retries.set(idx, retry);
    const id = this.nextRequest++;
    const request = { idx, generation: this.generation,
      controller: new AbortController(), cancelled: false,
      timeout: null, decodeTimeout: null, decodeExpired: false };
    this.active.set(id, request);
    // Capture the URL before any await so a later session transition cannot
    // combine the old chunk index with a replacement artifact's identity.
    const url = this.getChunkUrl(idx);
    this._run(id, request, retry, url);
  }

  async _run(id, request, retry, url) {
    request.timeout = setTimeout(() => request.controller.abort(), NETWORK_TIMEOUT_MS);
    try {
      // Honor HTTP cache headers: force-cache could permanently retain a 425.
      const response = await fetch(url, {
        cache: "default", signal: request.controller.signal,
      });
      if (!this._current(request)) return;
      if (request.controller.signal.aborted) throw new Error("Audio request timed out");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const encoded = await response.arrayBuffer();
      if (!this._current(request)) return;
      if (request.controller.signal.aborted) throw new Error("Audio request timed out");
      clearTimeout(request.timeout);
      request.timeout = null;
      // decodeAudioData cannot be canceled. Surface a stuck decode without
      // releasing its slot or pretending a retry stopped the original work.
      request.decodeTimeout = setTimeout(() => {
        request.decodeExpired = true;
        this.reconcile();
      }, DECODE_TIMEOUT_MS);
      const buffer = await this.decode(encoded);
      if (!this._current(request)) return;
      const entry = { buffer, playStart: request.idx * this.getStride() };
      this.chunks.set(request.idx, entry);
      this.retries.delete(request.idx);
      this.onChunk(request.idx, entry);
    } catch {
      if (this._current(request)) {
        retry.deadline = Date.now() + RETRY_DELAY_MS * 2 ** (retry.attempts - 1);
      }
    } finally {
      clearTimeout(request.timeout);
      clearTimeout(request.decodeTimeout);
      this.active.delete(id);
      this.reconcile();
    }
  }

  reset() {
    this.generation++;
    clearTimeout(this.retryTimer);
    this.retryTimer = null;
    for (const request of this.active.values()) this._cancel(request);
    this.available.clear();
    this.retries.clear();
    this.wanted.clear();
    this.chunks.clear();
    this.failed = false;
    this.onWindowChange();
  }

  dispose() {
    this.disposed = true;
    this.reset();
  }
}
