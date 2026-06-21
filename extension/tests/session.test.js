// Unit tests for session.js — the chunk-index math and buffer check, which
// drive seek handling and pause/resume. Instantiated with plain stand-ins for
// the <video> and Button (the constructor touches no browser APIs).
import { test } from "node:test";
import assert from "node:assert/strict";

import { Session, resolveSourceUrl, normalizeWatchUrl } from "../session.js";

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
  s.fetchedIdx = new Set([0, 1, 2]);
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
  assert.equal(s.fetchedIdx.size, 0); // dedup cleared so chunks refetch
  assert.equal(s.totalChunks, 5);
  assert.equal(opened, 1); // stream reopened on the new id
});

test("_resumeProcessing keeps the same job_id when the url is unchanged", async () => {
  const s = makeSession();
  s.jobId = "SAME";
  s.fetchedIdx = new Set([0, 1]);
  s.eventSource = null;
  s.requestJob = async () => ({ job_id: "SAME", total_chunks: 4 });
  let opened = 0;
  s._openEventStream = () => {
    opened++;
  };
  s._sendPrioritizeHint = () => {};

  await s._resumeProcessing();

  assert.equal(s.jobId, "SAME");
  assert.equal(s.fetchedIdx.size, 2); // not cleared; it's the same job
  assert.equal(opened, 1); // reopened the (closed) stream
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
