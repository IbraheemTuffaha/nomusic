import { test } from "node:test";
import assert from "node:assert/strict";
import { Button } from "../button.js";
import { domFixture } from "./dom-fixture.js";

globalThis.__nomusicTestDirectBackend = true;

function response(body, ok = true, status = ok ? 200 : 500) {
  return { ok, status, json: async () => body };
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

async function settle() {
  for (let i = 0; i < 12; i++) await Promise.resolve();
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
  const blobs = [];
  URL.createObjectURL = (blob) => {
    blobs.push(blob);
    return "blob:nomusic/export";
  };
  URL.revokeObjectURL = () => {};
  t.mock.method(chrome.runtime, "sendMessage", (message, callback) => {
    messages.push(message);
    callback({
      ok: true,
      backendUrl: "http://127.0.0.1:8723",
      grant: "a".repeat(43),
      mediaType: message.filename?.endsWith(".mp4") ? "video/mp4" : "audio/mpeg",
    });
  });
  return { ...dom, button, session, messages, blobs };
}

function fileResponse(type, bytes = [1, 2, 3]) {
  return {
    ok: true,
    status: 200,
    headers: { get: (name) => (String(name).toLowerCase() === "content-type" ? type : null) },
    blob: async () => new Blob([Uint8Array.from(bytes)], { type }),
  };
}

test("ready export saves a typed blob from a one-time grant", async (t) => {
  const { button, messages, elements, blobs } = setup(t);
  const exportId = "a".repeat(32);
  const requests = [];
  const fetch = t.mock.method(globalThis, "fetch", async (url, options) => {
    requests.push({ url, options });
    if (url.endsWith("/exports")) {
      assert.equal(options.method, "POST");
      const body = JSON.parse(options.body);
      assert.deepEqual({ job_id: body.job_id, format: body.format }, { job_id: "job", format: "mp3" });
      assert.match(body.client_id, /^/);
      return response({
        export_id: exportId, state: "ready", filename: "A song.mp3",
        progress: 1, phase: "ready",
      });
    }
    return fileResponse("audio/mpeg");
  });
  await button._startDownload("mp3");
  assert.equal(fetch.mock.callCount(), 2);
  assert.equal(messages.length, 1);
  assert.deepEqual(
    { type: messages[0].type, exportId: messages[0].exportId, clientId: messages[0].clientId, filename: messages[0].filename },
    {
      type: "download-export",
      exportId,
      clientId: messages[0].clientId,
      filename: "A song.mp3",
    },
  );
  const download = requests[1];
  assert.equal(download.url, `http://127.0.0.1:8723/exports/${exportId}/download`);
  assert.equal(download.options.headers["X-Nomusic-Download-Grant"], "a".repeat(43));
  assert.equal(download.options.headers.Authorization, undefined);
  assert.equal(download.options.referrerPolicy, "no-referrer");
  assert.equal(blobs[0].type, "audio/mpeg");
  const anchors = elements.filter((element) => element.tagName === "A");
  assert.equal(anchors.length, 1);
  assert.equal(anchors[0].download, "A song.mp3");
  assert.equal(anchors[0].href, "blob:nomusic/export");
  assert.equal(anchors[0].clickCount, 1);
  assert.equal(anchors[0].isConnected, false);
  assert.equal(requests.some((request) => request.options?.method === "DELETE"), false);
  assert.equal(button._downloading, false);
  assert.equal(button.label.textContent, "nomusic on");
});

test("queued export polls status and renders backend progress before saving", async (t) => {
  const { button, messages, tick, blobs } = setup(t);
  const exportId = "b".repeat(32);
  let statusCalls = 0;
  t.mock.method(globalThis, "fetch", async (url) => {
    if (url.endsWith("/exports")) {
      return response({ export_id: exportId, state: "queued", phase: "queued", progress: 0 });
    }
    if (String(url).endsWith("/download")) return fileResponse("application/octet-stream", [9]);
    statusCalls++;
    return response(statusCalls === 1
      ? { export_id: exportId, state: "building", phase: "encoding", progress: 0.5 }
      : { export_id: exportId, state: "ready", phase: "ready", progress: 1, filename: "clip.mp4" });
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
  assert.equal(blobs[0].type, "video/mp4");
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
