// Scheduler cache, continuation and stale-stretch tests with stubbed audio I/O.
// Actual Web Audio timing remains covered by the separate browser checks.
import { test } from "node:test";
import assert from "node:assert/strict";

import { AudioScheduler } from "../audio-scheduler.js";

function makeScheduler() {
  const chunks = new Map();
  return new AudioScheduler(
    /* video */ { playbackRate: 1, currentTime: 0, paused: false },
    {
      chunks,
      getStride: () => 9.5,
      getTotalChunks: () => 12,
    },
  );
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function audioBuffer(length, value = 0) {
  const data = new Float32Array(length).fill(value);
  return {
    length,
    numberOfChannels: 1,
    sampleRate: 8,
    duration: length / 8,
    getChannelData: () => data,
  };
}

test("constructor wires the shared chunk map and getters", () => {
  const s = makeScheduler();
  assert.equal(s._getStride(), 9.5);
  assert.equal(s._getTotalChunks(), 12);
  assert.ok(s.chunks instanceof Map);
  assert.equal(s.disposed, false);
});

test("_requestStretched returns the cached buffer on a hit", () => {
  const s = makeScheduler();
  const fake = { duration: 4.2 };
  s.stretchCache.set("0@2", fake);
  assert.equal(s._requestStretched(0, {}, 2), fake);
});

test("_requestStretched returns null while a key is already in flight", () => {
  const s = makeScheduler();
  s._stretchInflight.set("3@1.5", {});
  assert.equal(s._requestStretched(3, {}, 1.5), null);
});

test("_requestStretched keys cache by (idx, rate)", () => {
  const s = makeScheduler();
  const a = { duration: 1 };
  s.stretchCache.set("5@2", a);
  // Same idx, different rate -> not a hit (would start preparing); same key -> hit.
  assert.equal(s._requestStretched(5, {}, 2), a);
});

test("_onSourceEnded continues the window when the last source ends naturally", () => {
  const s = makeScheduler();
  s.video.paused = false;
  let rescheduled = 0;
  s.reschedule = () => {
    rescheduled++;
  };
  const src = { _nomusicStopped: false };
  s.activeSources = new Set([src]);
  s._srcByIdx = new Map([[4, src]]);

  s._onSourceEnded(src, 4);

  assert.equal(s.activeSources.size, 0); // dropped from the active set
  assert.equal(s._srcByIdx.has(4), false);
  assert.equal(rescheduled, 1); // pulled in the next look-ahead window
});

test("_onSourceEnded does NOT continue when stopped by us (seek/pause/rate)", () => {
  const s = makeScheduler();
  s.video.paused = false;
  let rescheduled = 0;
  s.reschedule = () => {
    rescheduled++;
  };
  const src = { _nomusicStopped: true }; // stopAll marked it
  s.activeSources = new Set([src]);

  s._onSourceEnded(src, 1);

  assert.equal(rescheduled, 0); // the seek/pause handler owns the reschedule
});

test("_onSourceEnded does NOT continue while other sources are still playing", () => {
  const s = makeScheduler();
  s.video.paused = false;
  let rescheduled = 0;
  s.reschedule = () => {
    rescheduled++;
  };
  const a = { _nomusicStopped: false };
  const b = { _nomusicStopped: false };
  s.activeSources = new Set([a, b]);

  s._onSourceEnded(a, 1);

  assert.equal(rescheduled, 0); // b is still active; mid-window, no continuation
  assert.equal(s.activeSources.size, 1);
});

test("_onSourceEnded does NOT continue while the video is paused", () => {
  const s = makeScheduler();
  s.video.paused = true;
  let rescheduled = 0;
  s.reschedule = () => {
    rescheduled++;
  };
  const src = { _nomusicStopped: false };
  s.activeSources = new Set([src]);

  s._onSourceEnded(src, 2);

  assert.equal(rescheduled, 0);
});

for (const change of ["artifact", "eviction", "rate"]) {
for (const outcome of ["resolve", "reject"]) {
  test(`an OLD stretch ${outcome} after ${change} cannot alter NEW work`, async (t) => {
    const s = makeScheduler();
    s.video.paused = false;
    s.video.playbackRate = 2;
    s.audioCtx = { createBuffer: (_channels, frames) => audioBuffer(frames) };
    const old = deferred();
    const current = deferred();
    let calls = 0;
    s.stretcher = { stretch: () => (++calls === 1 ? old.promise : current.promise) };
    const schedule = t.mock.method(s, "scheduleChunk", () => {});
    const reschedule = t.mock.method(s, "reschedule", () => {});
    const warning = t.mock.method(console, "warn", () => {});
    const oldEntry = { buffer: audioBuffer(8, 1), playStart: 0 };
    s.chunks.set(0, oldEntry);
    s._requestStretched(0, oldEntry, 2);

    if (change === "artifact") s.reset();
    else if (change === "eviction") {
      s.chunks.delete(0);
      s.pruneBuffers();
    } else {
      s.video.playbackRate = 1;
      s.pruneBuffers();
      s.video.playbackRate = 2;
    }
    const newEntry = { buffer: audioBuffer(8, 2), playStart: 0 };
    s.chunks.set(0, newEntry);
    s._requestStretched(0, newEntry, 2);
    if (outcome === "resolve") old.resolve({ channels: [new Float32Array(4).fill(1)] });
    else old.reject(new Error("old stretch failed"));
    await new Promise(setImmediate);

    assert.equal(s.stretchCache.size, 0);
    assert.equal(s._stretchInflight.has("0@2"), true);
    assert.equal(s._stretchDisabled, false);
    assert.equal(schedule.mock.callCount(), 0);
    assert.equal(reschedule.mock.callCount(), 0);
    assert.equal(warning.mock.callCount(), 0);
    // A stale completion must not allow a duplicate preparation of NEW.
    s._requestStretched(0, newEntry, 2);
    assert.equal(calls, 2);

    current.resolve({ channels: [new Float32Array(4).fill(2)] });
    await new Promise(setImmediate);
    assert.deepEqual([...s.stretchCache.get("0@2").getChannelData(0)], [2, 2, 2, 2]);
    assert.equal(s._stretchInflight.size, 0);
    assert.equal(schedule.mock.callCount(), 1);
    assert.equal(schedule.mock.calls[0].arguments[1], newEntry);
  });
}
}

test("direct arrivals outside the lookahead do not stretch or schedule", () => {
  const s = makeScheduler();
  s.video.currentTime = 100;
  s.video.playbackRate = 2;
  s.audioCtx = { createBufferSource: () => assert.fail("scheduled out of window") };
  s.stretcher = { available: true };
  s._requestStretched = () => assert.fail("stretched out of window");
  s.scheduleChunk(14, { buffer: audioBuffer(80), playStart: 133 });
  s.scheduleChunk(0, { buffer: audioBuffer(80), playStart: 0 });
  assert.equal(s.activeSources.size, 0);
});

test("window/rate pruning releases obsolete cached audio and active sources", () => {
  const s = makeScheduler();
  s.video.playbackRate = 2;
  s.chunks.set(10, {});
  for (const key of ["0@2", "10@1.5", "10@2"]) {
    s.stretchCache.set(key, audioBuffer(8));
  }
  let stops = 0;
  const obsolete = { _nomusicIdx: 0, stop: () => stops++ };
  const current = { _nomusicIdx: 10 };
  s.activeSources = new Set([obsolete, current]);
  s._srcByIdx = new Map([[0, obsolete], [10, current]]);
  s.pruneBuffers();
  assert.deepEqual([...s.stretchCache.keys()], ["10@2"]);
  assert.deepEqual([...s.activeSources], [current]);
  assert.equal(s._srcByIdx.has(0), false);
  assert.equal(obsolete._nomusicStopped, true);
  assert.equal(stops, 1);
  // Every further speed change releases the preceding prepared rate.
  for (const rate of [0.5, 1, 1.25, 1.5, 2]) {
    s.video.playbackRate = rate;
    s.pruneBuffers();
    s.stretchCache.set(`10@${rate}`, audioBuffer(8));
    assert.equal(s.stretchCache.size, 1);
  }
});


test("stretched-buffer allocation failure enables the resampling fallback", async (t) => {
  const s = makeScheduler();
  s.video.paused = false;
  s.video.playbackRate = 2;
  const entry = { buffer: audioBuffer(8, 1), playStart: 0 };
  s.chunks.set(0, entry);
  s.stretcher = { stretch: async () => ({ channels: [new Float32Array(4)] }) };
  s.audioCtx = { createBuffer() { throw new RangeError("allocation failed"); } };
  const warning = t.mock.method(console, "warn", () => {});
  const reschedule = t.mock.method(s, "reschedule", () => {});

  s._requestStretched(0, entry, 2);
  await new Promise(setImmediate);

  assert.equal(s.stretchCache.size, 0);
  assert.equal(s._stretchInflight.size, 0);
  assert.equal(s._stretchDisabled, true);
  assert.equal(warning.mock.callCount(), 1);
  assert.equal(reschedule.mock.callCount(), 1);
});
