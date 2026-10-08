import { test } from "node:test";
import assert from "node:assert/strict";
import { Button } from "../button.js";
import { domFixture } from "./dom-fixture.js";

function response(body, ok = true, status = ok ? 200 : 500) {
  return { ok, status, json: async () => body };
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

async function settle() {
  for (let i = 0; i < 4; i++) await Promise.resolve();
}

function setup(t) {
  const dom = domFixture(t);
  const button = new Button({});
  button.position(dom.document.body);
  const session = {
    failed: false,
    disposed: false,
    jobId: "job",
    config: { backendUrl: "http://localhost:8723" },
    dispose(options) {
      this.disposed = true;
      button.dispose(options);
    },
  };
  button.session = session;
  const messages = [];
  t.mock.method(chrome.runtime, "sendMessage", (message, callback) => {
    messages.push(message);
    callback({ ok: true, downloadId: 7 });
  });
  return { ...dom, button, session, messages };
}

test("ready export uses the native downloads API without reading a Blob", async (t) => {
  const { button, messages, elements } = setup(t);
  const fetch = t.mock.method(globalThis, "fetch", async (url, options) => {
    assert.equal(url, "http://localhost:8723/exports");
    assert.equal(options.method, "POST");
    assert.deepEqual(JSON.parse(options.body), { job_id: "job", format: "mp3" });
    return response({
      export_id: "export-1", state: "ready", filename: "A song.mp3",
      progress: 1, phase: "ready",
    });
  });
  await button._startDownload("mp3");
  assert.equal(fetch.mock.callCount(), 1);
  assert.deepEqual(messages, [{
    type: "download-export",
    url: "http://localhost:8723/exports/export-1/download",
    filename: "A song.mp3",
  }]);
  assert.equal(elements.filter((element) => element.tagName === "A").length, 0);
  assert.equal(button._downloading, false);
  assert.equal(button.label.textContent, "nomusic on");
});

test("queued export polls status and renders backend progress before saving", async (t) => {
  const { button, messages, tick } = setup(t);
  let statusCalls = 0;
  t.mock.method(globalThis, "fetch", async (url) => {
    if (url.endsWith("/exports")) {
      return response({ export_id: "export-2", state: "queued", phase: "queued", progress: 0 });
    }
    statusCalls++;
    return response(statusCalls === 1
      ? { export_id: "export-2", state: "building", phase: "encoding", progress: 0.5 }
      : { export_id: "export-2", state: "ready", phase: "ready", progress: 1, filename: "clip.mp4" });
  });
  const download = button._startDownload("mp4", 720);
  await settle();
  assert.equal(button.label.textContent, "Preparing");
  tick();
  await settle();
  assert.equal(button.label.textContent, "Encoding");
  assert.equal(button.pct.textContent, "50%");
  tick();
  await download;
  assert.equal(statusCalls, 2);
  assert.equal(messages[0].filename, "clip.mp4");
  assert.equal(button._downloading, false);
});

test("disposing during preparation aborts the request", async (t) => {
  const { button, session, messages } = setup(t);
  const submit = deferred();
  let signal;
  t.mock.method(globalThis, "fetch", (url, options) => {
    if (url.endsWith("/exports")) {
      signal = options.signal;
      return submit.promise;
    }
    return Promise.resolve(response({ state: "cancelled" }));
  });
  const download = button._startDownload("mp3");
  await settle();
  button.destroy();
  assert.equal(signal.aborted, true);
  submit.resolve(response({ export_id: "export-3", state: "queued", phase: "queued", progress: 0 }));
  await download;
  assert.equal(session.disposed, true);
  assert.equal(messages.length, 0);
  assert.equal(button._downloading, false);
});

test("a failed export reports an error and clears its feedback timer", async (t) => {
  const { button, timers } = setup(t);
  t.mock.method(globalThis, "fetch", async (url) => {
    if (url.endsWith("/exports")) {
      return response({ export_id: "export-4", state: "failed", phase: "failed", error: "disk full" });
    }
    return response({ state: "cancelled" });
  });
  await button._startDownload("mp3");
  assert.equal(button.label.textContent, "Download failed");
  assert.equal(timers.size, 1);
  button.dispose();
  assert.equal(timers.size, 0);
});

test("download refuses failed or disposed sessions", async (t) => {
  const { button, session } = setup(t);
  const fetch = t.mock.method(globalThis, "fetch", () => assert.fail("inactive download"));
  session.failed = true;
  button.download("mp3");
  await button._startDownload("mp3");
  session.failed = false;
  session.disposed = true;
  button.download("mp3");
  await button._startDownload("mp3");
  assert.equal(fetch.mock.callCount(), 0);
  button.destroy();
});
