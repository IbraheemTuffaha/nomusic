// Session identity, asynchronous-result isolation, and chunk-index tests.
// Production session/scheduler methods run with stubbed media and network I/O.
import { test } from "node:test";
import assert from "node:assert/strict";

import { Session, resolveSourceUrl, normalizeWatchUrl } from "../session.js";
import { AudioScheduler } from "../audio-scheduler.js";
import { settings } from "../settings.js";

// Run `fn` with a mocked MAIN-world bridge: dispatching the resolve event makes
// `document` answer with `bridgeUrl` on the documentElement attribute, exactly
// as page-script.js does in the browser. Restores globals afterwards.
function withBridge(bridgeUrl, fn) {
  const root = {
    a: {},
    setAttribute(k, v) {
      this.a[k] = v;
    },
    getAttribute(k) {
      return k in this.a ? this.a[k] : null;
    },
    removeAttribute(k) {
      delete this.a[k];
    },
  };
  const prevDoc = globalThis.document;
  const prevCE = globalThis.CustomEvent;
  globalThis.CustomEvent = class {
    constructor(type) {
      this.type = type;
    }
  };
  globalThis.document = {
    documentElement: root,
    dispatchEvent() {
      root.setAttribute("data-nomusic-source-url", bridgeUrl);
    },
  };
  try {
    return fn();
  } finally {
    globalThis.document = prevDoc;
    globalThis.CustomEvent = prevCE;
  }
}

class Media extends EventTarget {
  constructor(paused) {
    super();
    Object.assign(this, { paused, currentTime: 0, playbackRate: 1,
      dataset: {}, ended: false, playCalls: 0 });
  }
  pause() {
    if (this.paused) return;
    this.paused = true;
    queueMicrotask(() => this.dispatchEvent(new Event("pause")));
  }
  play() {
    this.playCalls++;
    if (!this.paused) return Promise.resolve();
    this.paused = false;
    queueMicrotask(() => this.dispatchEvent(new Event("play")));
    return Promise.resolve();
  }
}

function makeSession(paused = true) {
  const s = new Session(
    new Media(paused),
    { el: { dataset: { state: "working" } }, showStatus() {}, setError() {},
      setPaused() {}, setBuffering() {}, dispose() {} },
  );
  // Mirror the backend defaults the session starts with (config.py).
  s.chunkSeconds = 10;
  s.chunkOverlapSeconds = 0.5; // stride = 9.5s
  return s;
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

const settle = () => new Promise(setImmediate);

function attachScheduler(s, decode = async (buffer) => buffer) {
  s.scheduler = new AudioScheduler(s.video, {
    chunks: s.chunks,
    getStride: () => s.chunkSeconds - s.chunkOverlapSeconds,
    getTotalChunks: () => s.totalChunks,
  });
  s.scheduler.audioCtx = { decodeAudioData: decode, close() {} };
  return s.scheduler;
}

for (const pause of ["initial", "during buffering"]) {
  test(`a pause ${pause} survives chunk arrival and disabling nomusic`, async (t) => {
    const s = makeSession(pause === "initial");
    t.after(() => s.dispose());
    s.totalChunks = 2;
    attachScheduler(s);
    const schedule = t.mock.method(s.scheduler, "scheduleChunk", () => {});
    s._pauseForBuffer();
    await settle();
    if (pause === "during buffering") {
      // MAIN-world bridge reports this even when pause() is a native no-op.
      s.video.dispatchEvent(new CustomEvent("nomusic:playback-intent", {
        detail: { playing: false },
      }));
    }
    const entry = { buffer: { duration: 10 }, playStart: 0 };
    s.chunks.set(0, entry);
    s._chunkArrived(0, entry);
    await settle();
    assert.equal(s.video.paused, true);
    assert.equal(s.video.playCalls, 0);
    assert.equal(schedule.mock.callCount(), 0);
    s.dispose();
    assert.equal(s.video.paused, true);
    assert.equal(s.video.playCalls, 0);
  });
}

test("a buffering hold resumes wanted playback after audio arrives", async (t) => {
  const s = makeSession(false);
  t.after(() => s.dispose());
  s.totalChunks = 2;
  attachScheduler(s);
  t.mock.method(s.scheduler, "scheduleChunk", () => {});
  s._pauseForBuffer();
  await settle();
  assert.equal(s.video.paused, true);
  assert.equal(s.playback.wantsPlay, true);
  const entry = { buffer: { duration: 10 }, playStart: 0 };
  s.chunks.set(0, entry);
  s._chunkArrived(0, entry);
  await settle();
  assert.equal(s.video.paused, false);
  assert.equal(s.video.playCalls, 1);
});

test("pausing during a pending resume keeps the worker stream closed", async (t) => {
  const s = makeSession(false);
  t.after(() => s.dispose());
  s._adoptJob({ job_id: "JOB", total_chunks: 2 });
  attachScheduler(s);
  const pending = deferred();
  s.requestJob = () => pending.promise;
  const open = t.mock.method(s, "_openEventStream", () => {});
  const resume = s._resumeProcessing();
  s.video.pause();
  await settle();
  assert.equal(s._streamPausedClosed, true);
  pending.resolve({ job_id: "JOB", total_chunks: 2 });
  await resume;
  assert.equal(open.mock.callCount(), 0);
});

test("_chunkIdxForTime maps a time to its chunk via the stride", () => {
  const s = makeSession();
  assert.equal(s._chunkIdxForTime(0), 0);
  assert.equal(s._chunkIdxForTime(9.4), 0);
  assert.equal(s._chunkIdxForTime(9.5), 1); // first instant of chunk 1
  assert.equal(s._chunkIdxForTime(19.0), 2);
});

test("_chunkIdxForTime never returns a negative index", () => {
  const s = makeSession();
  assert.equal(s._chunkIdxForTime(-5), 0);
});

test("_isBuffered reflects whether the covering chunk is decoded", () => {
  const s = makeSession();
  assert.equal(s._isBuffered(9.5), false);
  s.chunks.set(1, { buffer: {}, playStart: 9.5 });
  assert.equal(s._isBuffered(9.5), true); // time 9.5 -> chunk 1
  assert.equal(s._isBuffered(0), false); // time 0 -> chunk 0, not buffered
});

test("a different stride shifts the chunk boundaries", () => {
  const s = makeSession();
  s.chunkSeconds = 30;
  s.chunkOverlapSeconds = 1; // stride = 29s
  assert.equal(s._chunkIdxForTime(28.9), 0);
  assert.equal(s._chunkIdxForTime(29.0), 1);
});

test("requestJob posts the captured sourceUrl, not the live page URL", async () => {
  const s = makeSession();
  s.sourceUrl = "https://orig.example/watch?v=A"; // captured at start()
  const prevFetch = globalThis.fetch;
  let capturedBody = null;
  globalThis.fetch = async (_url, opts) => {
    capturedBody = JSON.parse(opts.body);
    return { ok: true, json: async () => ({ job_id: "J", total_chunks: 3 }) };
  };
  try {
    const info = await s.requestJob();
    assert.equal(capturedBody.url, "https://orig.example/watch?v=A");
    assert.equal(info.job_id, "J");
  } finally {
    globalThis.fetch = prevFetch;
  }
});

test("_resumeProcessing adopts a changed job_id and refetches chunks", async () => {
  const s = makeSession();
  s.jobId = "OLD";
  s.loader.available = new Set([0, 1, 2]);
  s.chunks.set(0, { buffer: { label: "OLD" }, playStart: 0 });
  const scheduler = attachScheduler(s);
  scheduler.stretchCache.set("0@2", { label: "OLD stretched" });
  let stopped = 0;
  const source = { stop() { stopped++; } };
  scheduler.activeSources.add(source);
  scheduler._srcByIdx.set(0, source);
  let closed = false;
  s.eventSource = {
    close() {
      closed = true;
    },
  };
  s.requestJob = async () => ({ job_id: "NEW", total_chunks: 5 });
  let opened = 0;
  s._openEventStream = () => {
    opened++;
  };
  s._sendPrioritizeHint = () => {};

  await s._resumeProcessing();

  assert.equal(s.jobId, "NEW");
  assert.equal(closed, true); // old stream closed
  assert.equal(s.loader.available.size, 0); // old artifact readiness is invalid
  assert.equal(s.totalChunks, 5);
  assert.equal(opened, 1); // stream reopened on the new id
  assert.equal(s.chunks.size, 0);
  assert.equal(scheduler.stretchCache.size, 0);
  assert.equal(stopped, 1);
  assert.equal(scheduler.activeSources.size, 0);
});

test("_resumeProcessing keeps the same job_id when the url is unchanged", async () => {
  const s = makeSession();
  s.jobId = "SAME";
  s.loader.available = new Set([0, 1]);
  const entry = { buffer: { label: "SAME" }, playStart: 0 };
  s.chunks.set(0, entry);
  const scheduler = attachScheduler(s);
  const stretched = { label: "SAME stretched" };
  scheduler.stretchCache.set("0@2", stretched);
  let stopped = 0;
  scheduler.activeSources.add({ stop() { stopped++; } });
  s.eventSource = null;
  s.requestJob = async () => ({ job_id: "SAME", total_chunks: 4 });
  let opened = 0;
  s._openEventStream = () => {
    opened++;
  };
  s._sendPrioritizeHint = () => {};

  await s._resumeProcessing();

  assert.equal(s.jobId, "SAME");
  assert.equal(s.loader.available.size, 2); // retained for the same artifact
  assert.equal(opened, 1); // reopened the (closed) stream
  assert.equal(s.chunks.get(0), entry);
  assert.equal(scheduler.stretchCache.get("0@2"), stretched);
  assert.equal(stopped, 0);
});

test("active requests retain their backend, model and copied stems after settings change", async (t) => {
  const previous = { ...settings };
  t.after(() => Object.assign(settings, previous));
  Object.assign(settings, {
    backendUrl: "https://old.example",
    model: "old-model",
    keepStems: ["vocals"],
  });
  const s = makeSession();
  s.sourceUrl = "https://source.example/video";
  attachScheduler(s);
  settings.keepStems.push("drums");
  settings.backendUrl = "https://new.example";
  settings.model = "new-model";
  const requests = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    requests.push({ url, options });
    return {
      ok: true,
      json: async () => ({ job_id: "JOB", total_chunks: 1 }),
      arrayBuffer: async () => ({ label: "audio" }),
    };
  });

  s._adoptJob(await s.requestJob());
  await s.fetchCapabilities();
  s.loader.updateAvailable([0]);
  await settle();

  assert.deepEqual(requests.map(({ url }) => url), [
    "https://old.example/process",
    "https://old.example/capabilities",
    "https://old.example/chunk/JOB/0",
  ]);
  assert.deepEqual(JSON.parse(requests[0].options.body), {
    url: "https://source.example/video",
    model: "old-model",
    keep_stems: ["vocals"],
  });
  assert.deepEqual(makeSession().config, {
    backendUrl: "https://new.example",
    model: "new-model",
    keepStems: ["vocals", "drums"],
  });
});

for (const stage of ["fetch", "body", "decode"]) {
  test(`an OLD ${stage} completion cannot overwrite NEW decoded audio`, async (t) => {
    const s = makeSession();
    s._adoptJob({ job_id: "OLD", total_chunks: 1 });
    const waiting = deferred();
    const reached = deferred();
    const oldAudio = { label: "OLD" };
    const newAudio = { label: "NEW" };
    attachScheduler(s, (data) => {
      if (data === oldAudio && stage === "decode") {
        reached.resolve();
        return waiting.promise;
      }
      return Promise.resolve(data);
    });
    let oldSignal;
    let newRequests = 0;
    t.mock.method(globalThis, "fetch", async (url, { signal }) => {
      if (url.includes("/NEW/")) {
        newRequests++;
        return { ok: true, arrayBuffer: async () => newAudio };
      }
      oldSignal = signal;
      if (stage === "fetch") {
        reached.resolve();
        return waiting.promise;
      }
      return {
        ok: true,
        arrayBuffer: () => {
          if (stage === "body") {
            reached.resolve();
            return waiting.promise;
          }
          return Promise.resolve(oldAudio);
        },
      };
    });

    t.after(() => s.dispose());
    s.loader.updateAvailable([0]);
    await reached.promise;
    s._adoptJob({ job_id: "NEW", total_chunks: 1 });
    assert.equal(oldSignal.aborted, true);
    s.loader.updateAvailable([0]);
    await settle();
    const current = s.chunks.get(0);
    assert.equal(current.buffer, newAudio);

    // Deliberately let the fake transport ignore abort: stale-result checks
    // must also cover body reads and decoding that cannot be canceled.
    waiting.resolve(stage === "fetch"
      ? { ok: true, arrayBuffer: async () => oldAudio }
      : oldAudio);
    await settle();
    assert.equal(s.chunks.get(0), current);
    s.loader.updateAvailable([0]);
    await settle();
    assert.equal(newRequests, 1, "old completion cannot force a duplicate NEW fetch");
  });
}

test("a rejected OLD request cannot force the NEW chunk to refetch", async (t) => {
  const s = makeSession();
  t.after(() => s.dispose());
  s._adoptJob({ job_id: "OLD", total_chunks: 1 });
  attachScheduler(s);
  const old = deferred();
  const audio = { label: "NEW" };
  let newRequests = 0;
  t.mock.method(globalThis, "fetch", (url) => {
    if (url.includes("/OLD/")) return old.promise;
    newRequests++;
    return Promise.resolve({ ok: true, arrayBuffer: async () => audio });
  });
  const warning = t.mock.method(console, "warn", () => {});
  s.loader.updateAvailable([0]);
  s._adoptJob({ job_id: "NEW", total_chunks: 1 });
  s.loader.updateAvailable([0]);
  await settle();
  old.reject(new Error("old connection failed"));
  await settle();
  s.loader.updateAvailable([0]);
  await settle();
  assert.equal(newRequests, 1);
  assert.equal(s.chunks.get(0).buffer, audio);
  assert.equal(warning.mock.callCount(), 0);
});

test("disposing a session aborts its fetch and ignores a late response", async (t) => {
  const s = makeSession();
  s._adoptJob({ job_id: "JOB", total_chunks: 1 });
  let decoded = 0;
  attachScheduler(s, async () => { decoded++; });
  const waiting = deferred();
  let signal;
  t.mock.method(globalThis, "fetch", (_url, options) => {
    signal = options.signal;
    return waiting.promise;
  });
  s.loader.updateAvailable([0]);
  assert.equal(signal.aborted, false);
  s.dispose();
  assert.equal(signal.aborted, true);
  waiting.resolve({ ok: true, arrayBuffer: async () => new ArrayBuffer(0) });
  await settle();
  assert.equal(decoded, 0);
  assert.equal(s.chunks.size, 0);
});

test("a failed final chunk retries after ready closes the status stream", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"] });
  const s = makeSession();
  t.after(() => s.dispose());
  s._adoptJob({ job_id: "JOB", total_chunks: 1 });
  attachScheduler(s);
  let closed = false;
  s.eventSource = { close() { closed = true; } };
  let attempts = 0;
  const audio = { label: "final audio" };
  t.mock.method(globalThis, "fetch", async () => ++attempts === 1
    ? { ok: false, status: 503 }
    : { ok: true, arrayBuffer: async () => audio });
  s.handleStatus({ state: "ready", total_chunks: 1, ready_chunks: [0] });
  await settle();
  assert.equal(closed, true);
  assert.equal(s._streamEnded, true);
  assert.equal(s.chunks.size, 0);
  t.mock.timers.tick(500);
  await settle();
  assert.equal(attempts, 2);
  assert.equal(s.chunks.get(0).buffer, audio);
});

test("terminal chunk failure pauses while retaining original-audio suppression", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"] });
  const s = makeSession();
  t.after(() => s.dispose());
  s._adoptJob({ job_id: "JOB", total_chunks: 1 });
  attachScheduler(s);
  let restored = 0;
  s.muteController = { dispose() { restored++; } };
  s.video.paused = false;
  const error = t.mock.method(s.button, "setError", () => {});
  t.mock.method(globalThis, "fetch", async () => ({ ok: false, status: 503 }));
  s.handleStatus({ state: "ready", total_chunks: 1, ready_chunks: [0] });
  await settle();
  for (const delay of [500, 1000, 2000]) {
    t.mock.timers.tick(delay);
    await settle();
  }
  assert.equal(s.video.paused, true);
  assert.equal(s.disposed, false);
  assert.equal(restored, 0);
  assert.equal(error.mock.callCount(), 1);
  s.video.paused = false;
  s._boundHandlers.play();
  assert.equal(s.video.paused, true);
});

test("overlapping resumes share one process request and reopen once", async (t) => {
  const s = makeSession();
  s.sourceUrl = "https://source.example/video";
  s._adoptJob({ job_id: "JOB", total_chunks: 1 });
  const waiting = deferred();
  const fetch = t.mock.method(globalThis, "fetch", () => waiting.promise);
  const open = t.mock.method(s, "_openEventStream", () => {});
  t.mock.method(s, "_sendPrioritizeHint", () => {});
  const first = s._resumeProcessing();
  const second = s._resumeProcessing();
  assert.equal(fetch.mock.callCount(), 1);
  waiting.resolve({ ok: true, json: async () => ({ job_id: "JOB", total_chunks: 1 }) });
  await Promise.all([first, second]);
  assert.equal(open.mock.callCount(), 1);
  assert.equal(fetch.mock.calls[0].arguments[1].method, "POST");
});

test("closed OLD event callbacks cannot change the replacement job", (t) => {
  const previous = globalThis.EventSource;
  t.after(() => { globalThis.EventSource = previous; });
  globalThis.EventSource = class {
    static CLOSED = 2;
    constructor(url) { this.url = url; this.readyState = 1; }
    close() { this.readyState = 2; }
  };
  const s = makeSession();
  s._adoptJob({ job_id: "OLD", total_chunks: 1 });
  s._openEventStream();
  const old = s.eventSource;
  s._adoptJob({ job_id: "NEW", total_chunks: 3 });
  s._openEventStream();
  const current = s.eventSource;
  old.onmessage({ data: JSON.stringify({ state: "error", total_chunks: 99 }) });
  old.onerror();
  assert.equal(s.disposed, false);
  assert.equal(s.totalChunks, 3);
  assert.equal(s._streamEnded, false);
  assert.equal(s.eventSource, current);
  assert.equal(current.readyState, 1);
  s.dispose();
});

test("normalizeWatchUrl extracts a clean watch URL and strips extra params", () => {
  assert.equal(
    normalizeWatchUrl("https://www.youtube.com/watch?v=ABC123&t=42s&list=PLx"),
    "https://www.youtube.com/watch?v=ABC123",
  );
});

test("normalizeWatchUrl returns null for non-watch / empty inputs", () => {
  assert.equal(normalizeWatchUrl("https://www.youtube.com/feed/history"), null);
  assert.equal(normalizeWatchUrl(""), null);
  assert.equal(normalizeWatchUrl(null), null);
});

test("resolveSourceUrl uses the bridge's playing-video URL (miniplayer case)", () => {
  // location.href is the page being browsed; the bridge reports the real video.
  withBridge("https://www.youtube.com/watch?v=MINI42&t=10s", () => {
    assert.equal(resolveSourceUrl(), "https://www.youtube.com/watch?v=MINI42");
  });
});

test("resolveSourceUrl falls back to the page URL when the bridge has no answer", () => {
  // Non-YouTube page (no player): bridge answers empty -> use location.href.
  withBridge("", () => {
    assert.equal(resolveSourceUrl(), location.href);
  });
});
