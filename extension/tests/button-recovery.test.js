import { test } from "node:test";
import assert from "node:assert/strict";
import { getEventListeners } from "node:events";
import { Button } from "../button.js";
import { domFixture } from "./dom-fixture.js";

function failedButton(t) {
  const dom = domFixture(t);
  const button = new Button({});
  dom.document.body.appendChild(button.el);
  button.session = { failed: true, disposed: false };
  return { ...dom, button };
}

test("playback failure persists with native recovery buttons and cancels queued export", (t) => {
  const { button, document, timers, tick } = failedButton(t);
  button._pendingDownload = { format: "mp3", height: 0 };
  button.setError("Backend unavailable");
  tick();
  assert.equal(button.el.dataset.state, "error");
  assert.equal(button.label.textContent, "Playback failed");
  assert.equal(button.recoveryDetail.textContent, "Backend unavailable");
  assert.equal(button.label.getAttribute("aria-live"), "polite");
  assert.equal(button._pendingDownload, null);
  assert.equal(button.menu.isConnected, false);
  assert.equal(timers.size, 0);
  assert.equal(button.recovery.parentElement, document.body);
  assert.equal(button.el.contains(button.retryBtn), false);
  assert.equal(button.el.contains(button.returnBtn), false);
  for (const action of [button.retryBtn, button.returnBtn]) {
    assert.equal(action.tagName, "BUTTON");
    assert.equal(action.type, "button");
  }
  assert.equal(document.activeElement, document.body); // Failure does not steal focus.
});

test("Retry retains the same session and returns focus to the connecting pill", (t) => {
  const { button, document } = failedButton(t);
  let retries = 0;
  let disposals = 0;
  const session = button.session;
  session.dispose = () => { disposals++; };
  session.retry = async () => {
    retries++;
    session.failed = false;
    button.setStarting();
  };
  button.setError("Playback stopped");
  button.retryBtn.focus();
  button.retryBtn.click();
  assert.equal(retries, 1);
  assert.equal(disposals, 0);
  assert.equal(button.session, session);
  assert.equal(button.label.textContent, "Connecting");
  assert.equal(button.el.dataset.state, "working");
  assert.equal(button.recovery.isConnected, false);
  assert.equal(document.activeElement, button.el);
});

test("Return to original explicitly disposes the session and removes recovery", (t) => {
  const { button, document } = failedButton(t);
  let disposals = 0;
  button.session.dispose = () => { disposals++; };
  button.setError("Playback stopped");
  button.returnBtn.click();
  assert.equal(disposals, 1);
  assert.equal(button.session, null);
  assert.equal(button.el.dataset.state, "idle");
  assert.equal(button.recovery.isConnected, false);
  assert.equal(document.activeElement, button.el);
});

test("failed pill and dismiss expose recovery; Escape closes it without restoring audio", async (t) => {
  const { button, document } = failedButton(t);
  let disposals = 0;
  button.session.dispose = () => { disposals++; };
  button.setError("Playback stopped");
  await button.toggle();
  assert.equal(document.activeElement, button.retryBtn);
  const escape = new Event("keydown", { cancelable: true });
  escape.key = "Escape";
  button.recovery.dispatchEvent(escape);
  assert.equal(button.recovery.isConnected, false);
  assert.equal(button.el.dataset.state, "error");
  assert.equal(document.activeElement, button.el);
  button.dismiss();
  assert.equal(button.recovery.isConnected, true);
  assert.equal(document.activeElement, button.retryBtn);
  assert.equal(button._dismissed, false);
  assert.equal(disposals, 0);
});

test("late playback and export updates cannot replace a persistent failure", (t) => {
  const { button, timers } = failedButton(t);
  button.setError("Audio unavailable");
  button.showStatus({ state: "ready" });
  button.setBuffering();
  button.setPaused();
  button._downloading = true;
  button._showExportProgress({ phase: "encoding", percent: 80 });
  button._restoreAfterDownload();
  button._flashDownloadError();
  assert.equal(button.el.dataset.state, "error");
  assert.equal(button.label.textContent, "Playback failed");
  assert.equal(button.recoveryDetail.textContent, "Audio unavailable");
  assert.equal(button.recovery.isConnected, true);
  assert.equal(button._ready, false);
  assert.equal(timers.size, 0);
});

test("a pending download can finish without dismissing a later playback failure", async (t) => {
  const { button } = failedButton(t);
  button.session.failed = false;
  button.session.jobId = "job";
  button.session.config = { backendUrl: "http://localhost:8723" };
  let finish;
  t.mock.method(globalThis, "fetch", () => new Promise((resolve) => { finish = resolve; }));
  const download = button._startDownload("mp3");
  button.session.failed = true;
  button.setError("Status connection lost");
  finish({
    ok: true,
    status: 200,
    json: async () => ({ export_id: "export", state: "ready", filename: "clip.mp3" }),
  });
  await download;
  assert.equal(button._downloading, false);
  assert.equal(button.el.dataset.state, "error");
  assert.equal(button.label.textContent, "Playback failed");
  assert.equal(button.recoveryDetail.textContent, "Status connection lost");
  assert.equal(button.recovery.isConnected, true);
});

test("download-only errors expire, but a later playback failure cancels that timer", (t) => {
  const { button, tick, timers } = failedButton(t);
  button.session.failed = false;
  button._flashDownloadError();
  assert.equal(button.label.textContent, "Download failed");
  assert.equal(timers.size, 1);
  tick();
  assert.equal(button.el.dataset.state, "active");
  assert.equal(button.recovery.isConnected, false);
  button._flashDownloadError();
  button.session.failed = true;
  button.setError("Backend unavailable");
  assert.equal(timers.size, 0);
  tick();
  assert.equal(button.label.textContent, "Playback failed");
  assert.equal(button.recoveryDetail.textContent, "Backend unavailable");
});

test("idle, disposal and final dismissal remove recovery nodes and listeners", (t) => {
  const { button, window } = failedButton(t);
  for (const reset of [() => button.setIdle(), () => button.dispose(), () => button.dismiss()]) {
    button.session.disposed = true;
    button.setError("Playback stopped");
    assert.equal(getEventListeners(window, "resize").length, 1);
    reset();
    assert.equal(button.recovery.isConnected, false);
    assert.equal(getEventListeners(window, "resize").length, 0);
    assert.equal(getEventListeners(window, "scroll").length, 0);
  }
});
