// Unit tests for stretch.js — the pitch-preserving time-stretch wrapper over
// the vendored SoundTouch WSOLA library (pure JS, no browser APIs).
import { test } from "node:test";
import assert from "node:assert/strict";

import { StretchClient } from "../stretch.js";

test("a client reports itself available", () => {
  assert.equal(new StretchClient().available, true);
});

test("stretch shortens a stereo buffer by ~rate, pitch-preserved length", async () => {
  const sampleRate = 48000;
  const frames = sampleRate; // 1 second
  const left = new Float32Array(frames);
  const right = new Float32Array(frames);
  for (let i = 0; i < frames; i++) {
    // A 220 Hz sine so WSOLA has real periodic content to work with.
    left[i] = Math.sin((2 * Math.PI * 220 * i) / sampleRate) * 0.5;
    right[i] = left[i];
  }

  const out = await new StretchClient().stretch([left, right], 2, sampleRate);
  assert.ok(out && Array.isArray(out.channels));
  assert.equal(out.channels.length, 2);
  // tempo 2 => output ~half the input length (allow generous WSOLA slack).
  const outLen = out.channels[0].length;
  assert.ok(outLen > frames * 0.3 && outLen < frames * 0.7,
    `expected ~${frames / 2} frames, got ${outLen}`);
});

test("stretch handles a mono buffer (single channel in)", async () => {
  const sampleRate = 48000;
  const frames = sampleRate / 2;
  const mono = new Float32Array(frames);
  for (let i = 0; i < frames; i++) {
    mono[i] = Math.sin((2 * Math.PI * 440 * i) / sampleRate) * 0.4;
  }
  const out = await new StretchClient().stretch([mono], 1.5, sampleRate);
  assert.ok(out.channels[0].length > 0);
});
