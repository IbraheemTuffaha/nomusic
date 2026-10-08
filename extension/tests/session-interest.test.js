import { test } from "node:test";
import assert from "node:assert/strict";

import { Session } from "../session.js";

class Media extends EventTarget {
  constructor() {
    super();
    this.paused = true;
    this.currentTime = 0;
    this.playbackRate = 1;
    this.ended = false;
    this.dataset = {};
    this.volume = 0.5;
    this.muted = false;
  }
  pause() { this.paused = true; }
  play() { this.paused = false; return Promise.resolve(); }
}

const settle = () => new Promise(setImmediate);

function session() {
  return new Session(new Media(), {
    el: { dataset: { state: "working" } },
    setPaused() {}, setError() {}, showStatus() {}, setBuffering() {}, dispose() {},
  });
}

test("interest heartbeat is independent of SSE and pause stops renewal", async (t) => {
  t.mock.timers.enable({ apis: ["setInterval", "setTimeout", "Date"] });
  const requests = [];
  t.mock.method(globalThis, "fetch", async (url, options = {}) => {
    requests.push({ url, options });
    return { ok: true, status: 200, json: async () => ({ leased: true }) };
  });
  const s = session();
  t.after(() => {
    s.dispose({ restore: false });
    t.mock.timers.reset();
  });
  s._adoptJob({ job_id: "JOB", total_chunks: 1 });
  s._configureInterest({
    interest: { supported: true, lease_seconds: 30, heartbeat_seconds: 10 },
  });
  t.mock.timers.tick(10_000);
  await settle();
  assert.equal(requests.length, 1);
  assert.match(requests[0].url, /\/process\/JOB\/interest$/);
  assert.equal(JSON.parse(requests[0].options.body).client_id, s.clientId);

  s._onUserPause();
  assert.equal(s._interestHeartbeatTimer, null);
  t.mock.timers.tick(30_000);
  await settle();
  assert.equal(requests.length, 1, "pause must stop heartbeat requests");
});

test("disposing a leased session releases only its client interest", async (t) => {
  const requests = [];
  t.mock.method(globalThis, "fetch", async (url, options = {}) => {
    requests.push({ url, options });
    return { ok: true, status: 200, json: async () => ({ leased: true }) };
  });
  const s = session();
  s._adoptJob({ job_id: "JOB", total_chunks: 1 });
  s._configureInterest({ interest: { supported: true, heartbeat_seconds: 10 } });
  s.dispose({ restore: false });
  await settle();
  assert.equal(requests.length, 1);
  assert.match(requests[0].url, /\/process\/JOB\/interest\?client_id=/);
  assert.equal(requests[0].options.method, "DELETE");
});
