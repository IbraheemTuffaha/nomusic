// Production chunk acquisition with deterministic network, decoder and timer I/O.
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  ChunkLoader,
  BACKWARD_SECONDS,
  FORWARD_SECONDS,
} from "../chunk-loader.js";

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

// A real event-loop turn drains the fetch/body/decode promise chain. Only timeout
// and Date are mocked, so this does not advance a retry or network deadline.
const settle = () => new Promise(setImmediate);

function audioBuffer(tag) {
  return { tag, duration: 10, length: 80, numberOfChannels: 1, sampleRate: 8 };
}

function response(idx) {
  return { ok: true, arrayBuffer: async () => ({ idx }) };
}

function indexFromUrl(url) {
  return Number(String(url).split("/").at(-1));
}

function untilAborted(signal) {
  return new Promise((_resolve, reject) => {
    const abort = () => reject(new DOMException("Aborted", "AbortError"));
    if (signal.aborted) abort();
    else signal.addEventListener("abort", abort, { once: true });
  });
}

function fixture(t, { totalChunks = 100, decode = async ({ idx }) => audioBuffer(idx) } = {}) {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"] });
  let time = 0;
  const chunks = new Map();
  const delivered = [];
  const errors = [];
  const loader = new ChunkLoader({
    chunks,
    getTime: () => time,
    getStride: () => 9.5,
    getTotalChunks: () => totalChunks,
    getChunkUrl: (idx) => `https://backend.example/chunk/JOB/${idx}`,
    decode,
    onChunk: (idx, entry) => delivered.push({ idx, entry }),
    onError: (message) => errors.push(message),
    onWindowChange: () => {},
  });
  t.after(() => loader.dispose());
  return { loader, chunks, delivered, errors, seek: (value) => { time = value; } };
}

for (const failure of ["HTTP 503", "network rejection", "body rejection"]) {
  test(`${failure} retries successfully without another readiness event`, async (t) => {
    const f = fixture(t, { totalChunks: 1 });
    let attempts = 0;
    t.mock.method(globalThis, "fetch", async () => {
      attempts++;
      if (attempts > 1) return response(0);
      if (failure === "network rejection") throw new TypeError("Network failed");
      if (failure === "body rejection") {
        return { ok: true, arrayBuffer: async () => { throw new TypeError("Body failed"); } };
      }
      return { ok: false, status: 503 };
    });

    f.loader.updateAvailable([0]);
    await settle();
    assert.equal(attempts, 1);
    assert.equal(f.chunks.size, 0);
    t.mock.timers.tick(499);
    await settle();
    assert.equal(attempts, 1);
    t.mock.timers.tick(1);
    await settle();

    assert.equal(attempts, 2);
    assert.equal(f.chunks.get(0).buffer.tag, 0);
    assert.deepEqual(f.delivered.map(({ idx }) => idx), [0]);
    assert.deepEqual(f.errors, []);
  });
}

test("100 available chunks share three slots across fetch, body and decode", async (t) => {
  const requests = [];
  const decodes = [];
  const f = fixture(t, {
    decode: ({ idx }) => {
      const pending = deferred();
      decodes.push({ idx, ...pending });
      return pending.promise;
    },
  });
  t.mock.method(globalThis, "fetch", (url, { signal }) => {
    const headers = deferred();
    const body = deferred();
    requests.push({ idx: indexFromUrl(url), signal, headers, body });
    return headers.promise;
  });

  f.loader.updateAvailable(Array.from({ length: 100 }, (_, idx) => idx));
  await settle();
  assert.equal(requests.length, 3);
  assert.equal(requests[0].idx, 0);
  for (const request of requests) {
    request.headers.resolve({ ok: true, arrayBuffer: () => request.body.promise });
  }
  await settle();
  assert.equal(requests.length, 3, "reading bodies must retain the slots");
  for (const request of requests) request.body.resolve({ idx: request.idx });
  await settle();
  assert.equal(decodes.length, 3);
  assert.equal(requests.length, 3, "decoding must retain the slots");

  decodes[1].resolve(audioBuffer(decodes[1].idx));
  await settle();
  assert.equal(requests.length, 4, "one completed decode frees one slot");
  assert.equal(f.chunks.size, 1);
  assert.ok(requests.every(({ idx }) => idx * 9.5 <= FORWARD_SECONDS));
  assert.deepEqual(f.errors, []);
});

test("a hung decode has its own deadline and terminates without retries or late audio", async (t) => {
  const headers = deferred();
  const decode = deferred();
  const f = fixture(t, { totalChunks: 1, decode: () => decode.promise });
  let attempts = 0;
  t.mock.method(globalThis, "fetch", () => {
    attempts++;
    return headers.promise;
  });
  f.loader.updateAvailable([0]);
  t.mock.timers.tick(10_000);
  headers.resolve(response(0));
  await settle();

  t.mock.timers.tick(14_999);
  await settle();
  assert.deepEqual(f.errors, [], "the decode deadline starts after encoded audio arrives");
  t.mock.timers.tick(1);
  await settle();
  assert.equal(f.errors.length, 1);
  assert.match(f.errors[0], /decod/i);
  assert.equal(attempts, 1);
  assert.equal(f.chunks.size, 0);

  f.loader.updateAvailable([0]);
  f.loader.reconcile();
  t.mock.timers.tick(60_000);
  await settle();
  assert.equal(attempts, 1, "the stalled decode must not be replaced by another attempt");
  assert.equal(f.errors.length, 1);
  decode.resolve(audioBuffer("late"));
  await settle();
  assert.equal(f.chunks.size, 0);
  assert.deepEqual(f.delivered, []);
});

test("three stale decodes blocking new work across reset report a terminal timeout", async (t) => {
  const decodes = [];
  let attempts = 0;
  const f = fixture(t, {
    decode: () => {
      const pending = deferred();
      decodes.push(pending);
      return pending.promise;
    },
  });
  t.mock.method(globalThis, "fetch", async (url) => {
    attempts++;
    return response(indexFromUrl(url));
  });
  f.loader.updateAvailable([0, 1, 2, 3]);
  await settle();
  assert.equal(decodes.length, 3);
  f.loader.reset();
  f.loader.updateAvailable([0, 1, 2, 3]);
  await settle();
  assert.equal(attempts, 3, "all slots remain occupied by old decode operations");

  t.mock.timers.tick(14_999);
  await settle();
  assert.deepEqual(f.errors, []);
  t.mock.timers.tick(1);
  await settle();
  assert.equal(f.errors.length, 1);
  assert.match(f.errors[0], /decod/i);
  assert.equal(attempts, 3, "a watchdog must not free a slot or fetch replacement audio");
  assert.equal(f.chunks.size, 0);

  for (const pending of decodes) pending.resolve(audioBuffer("OLD"));
  await settle();
  assert.equal(attempts, 3);
  assert.deepEqual(f.delivered, []);
  assert.equal(f.errors.length, 1);
});

test("seeking evicts distant decoded audio and seeking back refetches it", async (t) => {
  const f = fixture(t);
  const requested = [];
  t.mock.method(globalThis, "fetch", async (url) => {
    const idx = indexFromUrl(url);
    requested.push(idx);
    return response(idx);
  });
  f.loader.updateAvailable(Array.from({ length: 100 }, (_, idx) => idx));
  await settle();
  assert.ok(f.chunks.has(0));
  assert.equal(requested.includes(99), false);

  const priorRequests = requested.length;
  f.seek(500);
  f.loader.reconcile();
  await settle();

  assert.equal(requested[priorRequests], Math.floor(500 / 9.5), "current audio gets priority");
  assert.equal(f.chunks.has(0), false);
  assert.ok(f.chunks.has(Math.floor(500 / 9.5)));
  assert.ok(f.chunks.size <= Math.ceil((BACKWARD_SECONDS + FORWARD_SECONDS) / 9.5) + 2);
  for (const idx of f.chunks.keys()) {
    assert.ok((idx + 1) * 9.5 >= 500 - BACKWARD_SECONDS);
    assert.ok(idx * 9.5 <= 500 + FORWARD_SECONDS);
  }

  f.seek(0);
  f.loader.reconcile();
  await settle();
  assert.equal(requested.filter((idx) => idx === 0).length, 2);
  assert.ok(f.chunks.has(0));
  assert.equal(f.chunks.has(Math.floor(500 / 9.5)), false);
  assert.deepEqual(f.errors, []);
});

for (const change of ["seek", "reset"]) {
  test(`${change} discards old decoding results without freeing their slots early`, async (t) => {
    const decodes = [];
    const requests = [];
    const f = fixture(t, {
      decode: ({ idx }) => {
        const pending = deferred();
        decodes.push({ idx, ...pending });
        return pending.promise;
      },
    });
    t.mock.method(globalThis, "fetch", async (url) => {
      const idx = indexFromUrl(url);
      requests.push(idx);
      return response(idx);
    });
    const available = Array.from({ length: 100 }, (_, idx) => idx);
    f.loader.updateAvailable(available);
    await settle();
    assert.equal(decodes.length, 3);

    if (change === "seek") {
      f.seek(500);
      f.loader.reconcile();
    } else {
      f.loader.reset();
      f.loader.updateAvailable(available);
    }
    await settle();
    assert.equal(requests.length, 3, "obsolete decode work is still running");

    decodes[0].resolve(audioBuffer("OLD"));
    await settle();
    assert.equal(requests.length, 4);
    assert.equal(requests[3], change === "seek" ? Math.floor(500 / 9.5) : 0);
    assert.equal(f.chunks.size, 0);
    assert.equal(f.delivered.length, 0);
    assert.equal(decodes.length, 4);

    decodes[3].resolve(audioBuffer("NEW"));
    await settle();
    assert.equal(f.chunks.get(requests[3]).buffer.tag, "NEW");
    assert.equal(f.delivered.length, 1);
    for (const pending of decodes.slice(1, 3)) pending.resolve(audioBuffer("OLD"));
    await settle();
    assert.equal(f.delivered.length, 1);
    assert.deepEqual(f.errors, []);
  });
}

test("a seek aborts unwanted requests and cancels their pending retries", async (t) => {
  const f = fixture(t);
  const requests = [];
  t.mock.method(globalThis, "fetch", (url, { signal }) => {
    const idx = indexFromUrl(url);
    requests.push({ idx, signal });
    if (idx === 0) return Promise.resolve({ ok: false, status: 503 });
    return untilAborted(signal);
  });
  f.loader.updateAvailable([0, 1, 2, 52]);
  await settle();
  const oldRequests = requests.filter(({ idx }) => idx < 3);
  assert.equal(oldRequests.length, 3);

  f.seek(500);
  f.loader.reconcile();
  await settle();
  assert.ok(oldRequests.filter(({ idx }) => idx !== 0).every(({ signal }) => signal.aborted));
  assert.ok(requests.some(({ idx }) => idx === 52));
  t.mock.timers.tick(500);
  await settle();
  assert.equal(requests.filter(({ idx }) => idx === 0).length, 1);

  f.seek(0);
  f.loader.reconcile();
  await settle();
  assert.equal(requests.filter(({ idx }) => idx === 0).length, 2);
  t.mock.timers.tick(500);
  await settle();
  assert.equal(requests.filter(({ idx }) => idx === 0).length, 3, "returning starts a fresh retry budget");
  assert.deepEqual(f.errors, []);
});

for (const stage of ["fetch", "body"]) {
  test(`a hanging ${stage} times out and retries`, async (t) => {
    const f = fixture(t, { totalChunks: 1 });
    const signals = [];
    t.mock.method(globalThis, "fetch", (_url, { signal }) => {
      signals.push(signal);
      if (signals.length > 1) return Promise.resolve(response(0));
      if (stage === "fetch") return untilAborted(signal);
      return Promise.resolve({ ok: true, arrayBuffer: () => untilAborted(signal) });
    });
    f.loader.updateAvailable([0]);
    await settle();
    t.mock.timers.tick(14_999);
    await settle();
    assert.equal(signals[0].aborted, false);
    assert.equal(signals.length, 1);

    t.mock.timers.tick(1);
    await settle();
    assert.equal(signals[0].aborted, true);
    assert.equal(signals.length, 1);
    t.mock.timers.tick(500);
    await settle();
    assert.equal(signals.length, 2);
    assert.ok(f.chunks.has(0));
    assert.deepEqual(f.errors, []);
  });
}

test("four failed attempts terminate once and reset permits fresh work", async (t) => {
  const f = fixture(t, { totalChunks: 2 });
  let attempts = 0;
  let recovered = false;
  t.mock.method(globalThis, "fetch", async () => {
    attempts++;
    return recovered ? response(0) : { ok: false, status: 503 };
  });
  f.loader.updateAvailable([0]);
  await settle();
  for (const delay of [500, 1_000, 2_000]) {
    t.mock.timers.tick(delay);
    await settle();
  }
  assert.equal(attempts, 4);
  assert.equal(f.errors.length, 1);
  assert.equal(typeof f.errors[0], "string");
  assert.ok(f.errors[0].length > 0);
  f.loader.updateAvailable([0, 1]);
  f.loader.reconcile();
  t.mock.timers.tick(60_000);
  await settle();
  assert.equal(attempts, 4, "terminal failure must not restart on status events or timers");
  assert.equal(f.errors.length, 1);

  recovered = true;
  f.loader.reset();
  f.loader.updateAvailable([0]);
  await settle();
  assert.equal(attempts, 5);
  assert.ok(f.chunks.has(0));
});

test("an exhausted future chunk becomes terminal only when playback reaches it", async (t) => {
  const f = fixture(t, { totalChunks: 2 });
  let attempts = 0;
  t.mock.method(globalThis, "fetch", async () => {
    attempts++;
    return { ok: false, status: 503 };
  });
  f.loader.updateAvailable([1]);
  await settle();
  for (const delay of [500, 1_000, 2_000]) {
    t.mock.timers.tick(delay);
    await settle();
  }
  assert.equal(attempts, 4);
  assert.deepEqual(f.errors, []);

  f.seek(9.5);
  f.loader.reconcile();
  await settle();
  assert.equal(attempts, 4);
  assert.equal(f.errors.length, 1);
});

test("dispose aborts downloads and prevents late decoding or timers from publishing work", async (t) => {
  const decode = deferred();
  const requests = [];
  const f = fixture(t, { decode: () => decode.promise });
  t.mock.method(globalThis, "fetch", (url, { signal }) => {
    const idx = indexFromUrl(url);
    requests.push({ idx, signal });
    return idx === 0 ? Promise.resolve(response(idx)) : untilAborted(signal);
  });
  f.loader.updateAvailable([0, 1, 2, 3]);
  await settle();
  assert.equal(requests.length, 3);
  f.loader.dispose();
  assert.ok(requests.filter(({ idx }) => idx !== 0).every(({ signal }) => signal.aborted));
  f.loader.updateAvailable([0, 1, 2, 3]);
  f.loader.reconcile();
  t.mock.timers.tick(60_000);
  await settle();
  assert.deepEqual(f.errors, [], "disposed decoding cannot report a timeout");
  decode.resolve(audioBuffer("late"));
  await settle();

  assert.equal(requests.length, 3);
  assert.equal(f.chunks.size, 0);
  assert.deepEqual(f.delivered, []);
  assert.deepEqual(f.errors, []);
});
