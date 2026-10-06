// Production recovery with real mute/intent owners and controlled browser I/O.
import { test } from "node:test";
import assert from "node:assert/strict";
import { Session } from "../session.js";
import { AudioScheduler } from "../audio-scheduler.js";
import { mediaFixture } from "./media-fixture.js";

const settle = () => new Promise(setImmediate);
const json = (value) => ({ ok: true, json: async () => value });
const job = (job_id = "JOB") => ({ job_id, state: "processing", total_chunks: 10,
  duration_seconds: 90, ready_chunks: [] });
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function untilAborted(signal) {
  return new Promise((_resolve, reject) => {
    const abort = () => reject(new DOMException("Aborted", "AbortError"));
    if (signal.aborted) abort();
    else signal.addEventListener("abort", abort, { once: true });
  });
}
function fixture(t, { fetch: override, init, paused = false, volume = 0.6 } = {}) {
  const restores = [];
  const { MediaElement } = mediaFixture({ after: (fn) => restores.push(fn) }, { bridge: true });
  t.mock.timers.enable({ apis: ["setTimeout", "Date"] });
  location.href = "https://source.example/watch?v=video";
  const video = new MediaElement({ paused, volume });
  Object.assign(video, { currentTime: 0, playbackRate: 1, seeking: false });
  const errors = [];
  const button = {
    el: { dataset: { state: "idle" } },
    setStarting() { this.el.dataset.state = "working"; },
    setError(message) { errors.push(message); this.el.dataset.state = "error"; },
    showStatus(status) { this.el.dataset.state = status.state; },
    setPaused() { this.el.dataset.state = "paused"; },
    setBuffering() { this.el.dataset.state = "working"; },
    dispose() {},
  };
  const streams = [];
  const previous = Object.getOwnPropertyDescriptor(globalThis, "EventSource");
  globalThis.EventSource = class {
    static CONNECTING = 0; static OPEN = 1; static CLOSED = 2;
    constructor(url) { this.url = url; this.readyState = 0; streams.push(this); }
    close() { this.readyState = 2; }
    open() { this.readyState = 1; this.onopen?.({}); }
    error(state = 0) { this.readyState = state; this.onerror?.({}); }
    message(value) { this.onmessage?.({ data: JSON.stringify(value) }); }
  };
  const graphs = [];
  t.mock.method(AudioScheduler.prototype, "init", async function () {
    const graph = { scheduler: this, closed: false };
    graphs.push(graph);
    this.audioCtx = { currentTime: 0, state: "running", close() { graph.closed = true; } };
    this.gain = { gain: { value: 1, setTargetAtTime(value) { this.value = value; } } };
    if (init) await init(this, graphs.length);
  });
  const requests = [];
  t.mock.method(globalThis, "fetch", async (url, options = {}) => {
    const request = { path: new URL(url).pathname, ...options };
    requests.push(request);
    const response = await override?.(request, requests);
    if (response !== undefined) return response;
    if (request.path === "/process") return json(job());
    if (request.path === "/capabilities") return json({ defaults: { chunk_seconds: 10, chunk_overlap_seconds: 0.5 } });
    return json({});
  });
  const session = new Session(video, button);
  t.after(() => {
    session.dispose({ restore: false });
    t.mock.timers.reset();
    if (previous) Object.defineProperty(globalThis, "EventSource", previous);
    else delete globalThis.EventSource;
    for (const restore of restores.reverse()) restore();
  });
  return { session, video, errors, streams, graphs, requests,
    posts: () => requests.filter(({ path }) => path === "/process") };
}

test("selection suppresses before a pending POST and setup failure stays silent", async (t) => {
  const pending = deferred();
  const f = fixture(t, { fetch: ({ path }) => path === "/process" ? pending.promise : undefined });
  const starting = f.session.start();
  assert.equal(f.video.volume, 0);
  assert.equal(f.video.paused, true);
  assert.equal(f.session.playback.wantsPlay, true);
  pending.reject(new TypeError("network denied"));
  await starting;
  await settle();
  assert.equal(f.session.failed, true);
  assert.equal(f.session.disposed, false);
  assert.equal(f.video.volume, 0);
  assert.equal(f.video.paused, true);
  assert.equal(f.video.playCalls, 0);
  assert.equal(f.errors.length, 1);
  assert.equal(f.streams.length, 0);
});

test("audio initialization failure closes the graph and preserves suppression", async (t) => {
  const f = fixture(t, { init: async () => { throw new Error("audio unavailable"); } });
  await f.session.start();
  assert.equal(f.session.failed, true);
  assert.equal(f.graphs[0].closed, true);
  assert.equal(f.session.scheduler, null);
  assert.equal(f.video.volume, 0);
  assert.equal(f.video.paused, true);
  assert.equal(f.streams.length, 0);
});

test("a backend error aborts active chunks and timers without restoring original audio", async (t) => {
  const f = fixture(t, { fetch: ({ path, signal }) =>
    path.startsWith("/chunk/") ? untilAborted(signal) : undefined });
  await f.session.start();
  const stream = f.streams[0];
  stream.message({ ...job(), ready_chunks: [0, 1, 2] });
  await settle();
  const chunks = f.requests.filter(({ path }) => path.startsWith("/chunk/"));
  assert.equal(chunks.length, 3);

  stream.message({ ...job(), state: "error", phase_label: "Source unavailable" });
  await settle();
  assert.equal(f.session.failed, true);
  assert.equal(stream.readyState, 2);
  assert.ok(chunks.every(({ signal }) => signal.aborted));
  assert.ok(f.graphs.every(({ closed }) => closed));
  assert.equal(f.video.volume, 0);
  assert.equal(f.video.paused, true);
  const count = f.requests.length;
  t.mock.timers.tick(60_000);
  await settle();
  assert.equal(f.requests.length, count);
  assert.deepEqual(f.errors, ["Source unavailable"]);
});

test("Retry retains mute/intent owners and latest volume instead of capturing zero", async (t) => {
  const f = fixture(t);
  await f.session.start();
  const mute = f.session.muteController;
  const playback = f.session.playback;
  f.session.fail("temporary failure");
  f.video.volume = 0.7;
  await settle();
  assert.equal(f.video.volume, 0);
  await f.session.retry();
  assert.equal(f.session.failed, false);
  assert.equal(f.session.muteController, mute);
  assert.equal(f.session.playback, playback);
  assert.equal(f.graphs[1].scheduler.gain.gain.value, 0.7);
  assert.equal(f.video.volume, 0);
  assert.equal(f.video.paused, true);
  assert.equal(f.posts().length, 2);
  assert.deepEqual(JSON.parse(f.posts()[0].body), JSON.parse(f.posts()[1].body));
  f.session.dispose();
  await settle();
  assert.equal(f.video.volume, 0.7);
  assert.equal(f.video.paused, false);
});

for (const stage of ["process", "capabilities", "audio init"]) {
  test(`an OLD ${stage} completion cannot replace a successful Retry`, async (t) => {
    const pending = deferred(), reached = deferred();
    let posts = 0, capabilities = 0;
    const f = fixture(t, {
      fetch: ({ path }) => {
        if (path === "/process") {
          posts++;
          if (posts === 1 && stage === "process") { reached.resolve(); return pending.promise; }
          return json(job(posts === 1 ? "OLD" : "NEW"));
        }
        if (path === "/capabilities" && ++capabilities === 1 && stage === "capabilities") {
          reached.resolve(); return pending.promise;
        }
      },
      init: async (_scheduler, count) => {
        if (count === 1 && stage === "audio init") { reached.resolve(); await pending.promise; }
      },
    });
    const starting = f.session.start();
    await reached.promise;
    f.session.fail("replace old attempt");
    await f.session.retry();
    const graph = f.session.scheduler, stream = f.session.eventSource;
    pending.resolve(stage === "process" ? json(job("OLD"))
      : stage === "capabilities" ? json({ defaults: { chunk_seconds: 99 } }) : undefined);
    await starting;
    await settle();
    assert.equal(f.session.failed, false);
    assert.equal(f.session.jobId, "NEW");
    assert.equal(f.session.chunkSeconds, 10);
    assert.equal(f.session.scheduler, graph);
    assert.equal(f.session.eventSource, stream);
    assert.equal(f.streams.length, 1);
    assert.equal(f.graphs.at(-1).closed, false);
  });
}

for (const state of [0, 2]) {
  test(`SSE state ${state} gets three retries; opening alone does not reset the budget`, async (t) => {
    const f = fixture(t);
    await f.session.start();
    for (const delay of [500, 1000, 2000]) {
      const stream = f.streams.at(-1);
      stream.open(); stream.error(state);
      assert.equal(stream.readyState, 2);
      const count = f.posts().length;
      t.mock.timers.tick(delay - 1);
      await settle();
      assert.equal(f.posts().length, count);
      t.mock.timers.tick(1);
      await settle();
      assert.equal(f.posts().length, count + 1);
      assert.equal(f.streams.length, count + 1);
    }
    f.streams.at(-1).error(state);
    await settle();
    assert.equal(f.session.failed, true);
    assert.equal(f.posts().length, 4);
    assert.equal(f.video.volume, 0);
    assert.equal(f.video.paused, true);
    t.mock.timers.tick(60_000);
    await settle();
    assert.equal(f.posts().length, 4);
    assert.equal(f.errors.length, 1);
  });
}

test("malformed and wrong-job messages do not replenish reconnect attempts", async (t) => {
  const f = fixture(t);
  await f.session.start();
  for (const delay of [500, 1000, 2000]) {
    const stream = f.streams.at(-1);
    stream.onmessage({ data: "{" });
    stream.message(null);
    stream.message({ job_id: "JOB", state: "invented" });
    stream.message(job("OTHER"));
    stream.error();
    t.mock.timers.tick(delay);
    await settle();
  }
  f.streams.at(-1).error();
  assert.equal(f.session.failed, true);
  assert.equal(f.posts().length, 4);
});

test("valid status resets attempts and clears the initial snapshot deadline", async (t) => {
  const f = fixture(t);
  await f.session.start();
  for (const delay of [500, 1000]) {
    f.streams.at(-1).error(); t.mock.timers.tick(delay); await settle();
  }
  f.streams.at(-1).message(job());
  t.mock.timers.tick(15_001);
  await settle();
  assert.equal(f.posts().length, 3, "inference may outlast the initial snapshot deadline");
  f.streams.at(-1).error(); t.mock.timers.tick(500); await settle();
  assert.equal(f.posts().length, 4);
  assert.equal(f.session.failed, false);
});

test("a connection without initial status times out even if it opens", async (t) => {
  const f = fixture(t);
  await f.session.start();
  const stream = f.streams[0];
  stream.open();
  t.mock.timers.tick(14_999); await settle();
  assert.equal(f.posts().length, 1);
  t.mock.timers.tick(1); await settle();
  assert.equal(stream.readyState, 2);
  t.mock.timers.tick(500); await settle();
  assert.equal(f.posts().length, 2);
  assert.equal(f.streams.length, 2);
});

test("a user pause while held cancels a pending reconnect", async (t) => {
  const f = fixture(t);
  await f.session.start();
  f.streams[0].error();
  f.video.pause(); // Already held; only the MAIN-world bridge sees this intent.
  await settle();
  t.mock.timers.tick(60_000); await settle();
  assert.equal(f.session.playback.wantsPlay, false);
  assert.equal(f.posts().length, 1);
  assert.equal(f.streams.length, 1);
  assert.equal(f.session.failed, false);
});

test("disposal aborts a reconnect POST and ignores its late result", async (t) => {
  const pending = deferred();
  let posts = 0;
  const f = fixture(t, { fetch: ({ path }) => {
    if (path === "/process" && ++posts === 2) return pending.promise;
  } });
  await f.session.start();
  f.streams[0].error(); t.mock.timers.tick(500); await settle();
  const request = f.posts()[1];
  f.session.dispose({ restore: false });
  assert.equal(request.signal.aborted, true);
  pending.resolve(json(job())); await settle();
  const count = f.requests.length;
  t.mock.timers.tick(60_000); await settle();
  assert.equal(f.requests.length, count);
  assert.equal(f.streams.length, 1);
  assert.ok(f.graphs.every(({ closed }) => closed));
});

test("a hung process request fails after 30 seconds without restoring original audio", async (t) => {
  const f = fixture(t, { fetch: ({ path, signal }) => path === "/process" ? untilAborted(signal) : undefined });
  const starting = f.session.start();
  t.mock.timers.tick(29_999); await settle();
  assert.equal(f.session.failed, false);
  t.mock.timers.tick(1); await starting;
  assert.equal(f.session.failed, true);
  assert.equal(f.posts()[0].signal.aborted, true);
  assert.equal(f.video.volume, 0);
  assert.equal(f.video.paused, true);
});

test("optional capabilities time out after five seconds and startup uses defaults", async (t) => {
  const f = fixture(t, { fetch: ({ path, signal }) => path === "/capabilities" ? untilAborted(signal) : undefined });
  const starting = f.session.start();
  await settle();
  t.mock.timers.tick(4_999); await settle();
  assert.equal(f.streams.length, 0);
  t.mock.timers.tick(1); await starting;
  assert.equal(f.session.failed, false);
  assert.equal(f.session.chunkSeconds, 10);
  assert.equal(f.streams.length, 1);
});

for (const stage of ["process", "capabilities", "audio init"]) {
  for (const resume of [false, true]) {
    test(`an explicit startup pause during ${stage}${resume ? " followed by play uses the original POST" : " keeps processing paused"}`, async (t) => {
      const pending = deferred(), reached = deferred();
      const f = fixture(t, {
        fetch: ({ path }) => {
          if (path === `/${stage}`) { reached.resolve(); return pending.promise; }
        },
        init: async () => {
          if (stage === "audio init") { reached.resolve(); await pending.promise; }
        },
      });
      const starting = f.session.start();
      await reached.promise;
      f.video.pause(); // Already held: MAIN-world intent must still reach startup.
      await settle();
      assert.equal(f.session.playback.wantsPlay, false);
      if (resume) {
        await f.video.play();
        await settle();
        assert.equal(f.session.playback.wantsPlay, true);
        assert.equal(f.posts().length, 1, "play must not start a competing resume POST");
      }
      pending.resolve(stage === "process" ? json(job())
        : stage === "capabilities" ? json({ defaults: {} }) : undefined);
      await starting;
      assert.equal(f.posts().length, 1);
      assert.equal(f.streams.length, resume ? 1 : 0);
      if (!resume) {
        assert.equal(f.session.eventSource, null);
        assert.equal(f.session.button.el.dataset.state, "paused");
        t.mock.timers.tick(60_000);
        await settle();
        assert.equal(f.posts().length, 1, "a paused startup must not reconnect later");
      }
      assert.equal(f.video.volume, 0);
      assert.equal(f.video.paused, true);
    });
  }
}

test("initially paused activation still starts processing without requesting playback", async (t) => {
  const f = fixture(t, { paused: true });
  await f.session.start();
  assert.equal(f.posts().length, 1);
  assert.equal(f.streams.length, 1);
  f.session.chunks.set(0, { buffer: { duration: 10 }, playStart: 0 });
  f.session._reconcileBufferState();
  assert.equal(f.session.playback.held, false);
  assert.equal(f.session.playback.wantsPlay, false);
  assert.equal(f.video.paused, true);
  assert.equal(f.video.playCalls, 0);
});

test("a queued download overrides startup pause without a competing process request", async (t) => {
  const pending = deferred(), reached = deferred();
  const f = fixture(t, { init: async () => { reached.resolve(); await pending.promise; } });
  const starting = f.session.start();
  await reached.promise;
  f.video.pause();
  await settle();
  f.session.button._pendingDownload = { format: "mp3" };
  f.session.ensureLiveForDownload();
  await settle();
  assert.equal(f.posts().length, 1);
  pending.resolve();
  await starting;
  assert.equal(f.streams.length, 1);
  assert.equal(f.posts().length, 1);
  assert.equal(f.session.playback.wantsPlay, false);
  assert.equal(f.video.paused, true);
});
