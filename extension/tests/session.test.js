// Unit tests for session.js — the chunk-index math and buffer check, which
// drive seek handling and pause/resume. Instantiated with plain stand-ins for
// the <video> and Button (the constructor touches no browser APIs).
import { test } from "node:test";
import assert from "node:assert/strict";

import { Session } from "../session.js";

function makeSession() {
  const s = new Session(/* video */ {}, /* button */ {});
  // Mirror the backend defaults the session starts with (config.py).
  s.chunkSeconds = 10;
  s.chunkOverlapSeconds = 0.5; // stride = 9.5s
  return s;
}

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
