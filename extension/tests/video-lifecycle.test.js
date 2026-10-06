// Exercise actual discovery/retirement against a small DOM, including mutation
// batches that remove and reinsert the same player. Browser layout is separate.
import { test } from "node:test";
import assert from "node:assert/strict";
import { domFixture } from "./dom-fixture.js";
import { Button } from "../button.js";

function descendants(root, tag) {
  return root.children.flatMap(child => [
    ...(child.tagName === tag.toUpperCase() ? [child] : []),
    ...descendants(child, tag),
  ]);
}

test("discovery preserves reparenting and completely retires disconnected videos", async (t) => {
  const { document, window } = domFixture(t);
  let mutations;
  const globals = {
    getComputedStyle: () => ({ position: "relative" }),
    MutationObserver: class {
      constructor(callback) { mutations = callback; }
      observe() {}
    },
    setInterval: () => 1,
  };
  for (const [key, value] of Object.entries(globals)) {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, key);
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
    t.after(() => descriptor ? Object.defineProperty(globalThis, key, descriptor) : delete globalThis[key]);
  }
  window.setTimeout = globalThis.setTimeout;
  document.documentElement = document.body;
  document.querySelectorAll = tag => descendants(document.body, tag);
  const host = document.createElement("div");
  const otherHost = document.createElement("div");
  for (const element of [host, otherHost]) {
    element.getBoundingClientRect = () => ({ width: 640, height: 360 });
    element.querySelectorAll = tag => descendants(element, tag);
    document.body.appendChild(element);
  }
  const video = document.createElement("video");
  Object.assign(video, { clientWidth: 640, nodeType: 1 });
  video.getBoundingClientRect = () => ({ width: 640, height: 360 });
  host.appendChild(video);
  const buttons = [];
  const originalPosition = Button.prototype.position;
  t.mock.method(Button.prototype, "position", function (...args) {
    if (!buttons.includes(this)) buttons.push(this);
    return originalPosition.apply(this, args);
  });
  await import(`../main.js?lifecycle=${Date.now()}`);
  await new Promise(setImmediate);
  assert.equal(buttons.length, 1);
  const button = buttons[0];
  const disposed = [];
  button.session = { disposed: false, checkSource() {}, dispose(options) { disposed.push(options); } };
  button.openMenu();

  // One delivery may contain both removal and insertion. The final DOM owns
  // identity, so a persistent miniplayer must retain the session and button.
  otherHost.appendChild(video);
  mutations([{ addedNodes: [video], removedNodes: [video] }]);
  assert.equal(disposed.length, 0);
  assert.equal(buttons.length, 1);
  assert.equal(button.el.parentElement, otherHost);

  // Existing players are hidden rather than retired just because layout is
  // temporarily too small. Reappearing uses the same control/session.
  video.getBoundingClientRect = () => ({ width: 0, height: 0 });
  mutations([{ addedNodes: [], removedNodes: [] }]);
  assert.equal(button.el.style.display, "none");
  assert.equal(disposed.length, 0);
  video.getBoundingClientRect = () => ({ width: 640, height: 360 });

  video.remove();
  mutations([{ addedNodes: [], removedNodes: [video] }]);
  assert.deepEqual(disposed, [{ restore: false }]);
  assert.equal(button.el.isConnected, false);
  assert.equal(button.menu.isConnected, false);
  assert.equal(button.session, null);
  mutations([{ addedNodes: [], removedNodes: [video] }]);
  assert.equal(disposed.length, 1);

  // Later reuse is a fresh attachment, not an orphaned entry in the WeakMap.
  host.appendChild(video);
  mutations([{ addedNodes: [video], removedNodes: [] }]);
  assert.equal(buttons.length, 2);
  assert.notEqual(buttons[1], button);
  assert.equal(buttons[1].el.isConnected, true);
  buttons[1].destroy();
});
