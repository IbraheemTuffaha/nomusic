// Unit tests for audio-scheduler.js — constructor wiring and the stretch-cache
// memoization (the pure paths). The scheduling/AudioContext paths need a real
// Web Audio graph and are exercised manually in-browser.
import { test } from "node:test";
import assert from "node:assert/strict";

import { AudioScheduler } from "../audio-scheduler.js";

function makeScheduler() {
  const chunks = new Map();
  return new AudioScheduler(
    /* video */ { playbackRate: 1 },
    {
      chunks,
      getStride: () => 9.5,
      getTotalChunks: () => 12,
    },
  );
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
  s._stretchInflight.add("3@1.5");
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
