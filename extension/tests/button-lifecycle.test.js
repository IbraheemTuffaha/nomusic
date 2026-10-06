import { test } from "node:test";
import assert from "node:assert/strict";
import { Button } from "../button.js";
import { domFixture } from "./dom-fixture.js";

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
  const disposals = [];
  const session = {
    failed: false, disposed: false, jobId: "job",
    config: { backendUrl: "http://localhost:8723" },
    dispose(options) {
      disposals.push(options);
      this.disposed = true;
      button.dispose();
    },
  };
  button.session = session;
  const create = t.mock.method(URL, "createObjectURL", () => "blob:download");
  const revoke = t.mock.method(URL, "revokeObjectURL", () => {});
  return { ...dom, button, session, disposals, create, revoke };
}

test("destroy aborts an export body and a late completion cannot save or recreate UI", async (t) => {
  const { button, session, disposals, create, elements, timers, document } = setup(t);
  const body = deferred();
  let signal;
  t.mock.method(globalThis, "fetch", async (_url, options) => {
    signal = options.signal;
    return { ok: true, blob: () => body.promise };
  });
  const download = button._startDownload("mp3");
  await settle();
  button.destroy();
  button.destroy();
  assert.equal(signal.aborted, true);
  assert.deepEqual(disposals, [{ restore: false }]);
  assert.equal(session.disposed, true);
  assert.equal(button.session, null);
  body.resolve(new Blob(["old audio"]));
  await download;
  await button.toggle();
  button.openRecovery();
  button.openMenu();
  button.position(document.body);
  assert.equal(create.mock.callCount(), 0);
  assert.equal(elements.filter((element) => element.tagName === "A").length, 0);
  assert.equal(button.el.isConnected, false);
  assert.equal(button.menu.isConnected, false);
  assert.equal(button.recovery.isConnected, false);
  assert.equal(button._download, null);
  assert.equal(button._downloading, false);
  assert.equal(timers.size, 0);
});

test("Retry cancels an old body; its finally cannot clear a newer export", async (t) => {
  const { button, create, elements } = setup(t);
  const oldBody = deferred(), newBody = deferred();
  const signals = [];
  t.mock.method(globalThis, "fetch", async (_url, options) => {
    signals.push(options.signal);
    return { ok: true, blob: () => signals.length === 1 ? oldBody.promise : newBody.promise };
  });
  const oldDownload = button._startDownload("mp3");
  await settle();
  button.setStarting();
  const newDownload = button._startDownload("mp3");
  await settle();
  const current = button._download;
  oldBody.resolve(new Blob(["old audio"]));
  await oldDownload;
  assert.equal(signals[0].aborted, true);
  assert.equal(signals[1].aborted, false);
  assert.equal(button._download, current);
  assert.equal(button._downloading, true);
  assert.equal(button.label.textContent, "Saving…");
  assert.equal(create.mock.callCount(), 0);
  newBody.resolve(new Blob(["new audio"]));
  await newDownload;
  assert.equal(create.mock.callCount(), 1);
  assert.equal(elements.find((element) => element.tagName === "A").clickCount, 1);
  button.dispose();
});

test("an old export rejection cannot flash an error or finish a new operation", async (t) => {
  const { button, timers } = setup(t);
  const oldFetch = deferred(), newFetch = deferred();
  let calls = 0;
  t.mock.method(globalThis, "fetch", () => ++calls === 1 ? oldFetch.promise : newFetch.promise);
  const oldDownload = button._startDownload("mp3");
  button.setStarting();
  const newDownload = button._startDownload("mp3");
  const current = button._download;
  oldFetch.reject(new TypeError("old connection lost"));
  await oldDownload;
  assert.equal(button._download, current);
  assert.equal(button._downloading, true);
  assert.equal(button.label.textContent, "Saving…");
  assert.equal(timers.size, 0);
  button.destroy();
  newFetch.resolve({ ok: true, blob: () => assert.fail("stale body must not be read") });
  await newDownload;
});

test("an old progress JSON result cannot repaint or restart polling after Retry", async (t) => {
  const { button, timers } = setup(t);
  const progress = deferred(), oldFile = deferred(), newFile = deferred();
  let files = 0;
  t.mock.method(globalThis, "fetch", async (url) => {
    if (url.includes("/progress")) return { ok: true, json: () => progress.promise };
    return ++files === 1 ? oldFile.promise : newFile.promise;
  });
  const oldDownload = button._startDownload("mp4");
  await settle();
  button.setStarting();
  const newDownload = button._startDownload("mp3");
  progress.resolve({ phase: "encoding", percent: 99 });
  await settle();
  assert.equal(button.label.textContent, "Saving…");
  assert.equal(button.pct.textContent, "");
  assert.equal(timers.size, 0);
  button.destroy();
  const stale = { ok: true, blob: () => assert.fail("stale export body read") };
  oldFile.resolve(stale);
  newFile.resolve(stale);
  await Promise.all([oldDownload, newDownload]);
});

test("progress polling waits for headers and JSON before scheduling another request", async (t) => {
  const { button, tick, timers } = setup(t);
  const progressHeaders = deferred(), progressBody = deferred(), secondProgress = deferred();
  const file = deferred();
  let polls = 0;
  t.mock.method(globalThis, "fetch", (url) => {
    if (!url.includes("/progress")) return file.promise;
    return ++polls === 1 ? progressHeaders.promise : secondProgress.promise;
  });
  const download = button._startDownload("mp4");
  tick();
  assert.equal(polls, 1);
  assert.equal(timers.size, 0);
  progressHeaders.resolve({ ok: true, json: () => progressBody.promise });
  await settle();
  tick();
  assert.equal(polls, 1);
  progressBody.resolve({ phase: "encoding", percent: 10 });
  await settle();
  assert.equal(timers.size, 1);
  tick();
  tick();
  assert.equal(polls, 2);
  assert.equal(timers.size, 0);
  button.destroy();
  secondProgress.resolve({ ok: true, json: () => assert.fail("stale progress body read") });
  file.resolve({ ok: true, blob: () => assert.fail("stale export body read") });
  await download;
  await settle();
  assert.equal(timers.size, 0);
});

test("dispose revokes completed blob URLs and clears export feedback timers once", async (t) => {
  const { button, timers, revoke, tick } = setup(t);
  t.mock.method(globalThis, "fetch", async () => ({ ok: true, blob: async () => new Blob(["audio"]) }));
  await button._startDownload("mp3");
  button._flashDownloadError();
  assert.equal(timers.size, 2);
  button.dispose();
  button.dispose();
  tick();
  assert.equal(timers.size, 0);
  assert.equal(button._downloadUrls.size, 0);
  assert.equal(revoke.mock.callCount(), 1);
  assert.equal(button._retired, false);
});

test("download refuses failed or disposed sessions and position avoids redundant DOM moves", async (t) => {
  const { button, session, document } = setup(t);
  const fetch = t.mock.method(globalThis, "fetch", () => assert.fail("inactive download"));
  session.failed = true;
  button.download("mp3");
  await button._startDownload("mp3");
  session.failed = false;
  session.disposed = true;
  button.download("mp3");
  await button._startDownload("mp3");
  assert.equal(fetch.mock.callCount(), 0);
  const append = t.mock.method(document.body, "appendChild");
  button.position(document.body);
  button.position(document.body);
  assert.equal(append.mock.callCount(), 0);
  button.destroy();
});
