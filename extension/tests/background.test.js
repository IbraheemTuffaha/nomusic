// Unit tests for background.js — the service worker's storage-defaults seeding
// and backend-ping handler. background.js registers its chrome listeners at
// import time, so we install capturing stubs and dynamic-import it.
import { test } from "node:test";
import assert from "node:assert/strict";

let loadCounter = 0;

async function loadBackground({ stored }) {
  const captured = {};
  let written = null;
  globalThis.chrome = {
    runtime: {
      onInstalled: { addListener: (cb) => (captured.onInstalled = cb) },
      onMessage: { addListener: (cb) => (captured.onMessage = cb) },
    },
    storage: {
      sync: {
        get: async () => stored,
        set: async (patch) => (written = patch),
      },
    },
  };
  // Unique query each call so the module's top-level listener registration
  // re-runs against this call's capturing stubs (ESM caches by specifier).
  await import(`../background.js?load=${++loadCounter}`);
  return { captured, getWritten: () => written };
}

test("onInstalled seeds only the missing storage defaults", async () => {
  const { captured, getWritten } = await loadBackground({
    stored: { model: "already-set" },
  });
  assert.equal(typeof captured.onInstalled, "function");
  await captured.onInstalled();
  const written = getWritten();
  assert.ok(written, "expected a storage.set for the missing defaults");
  assert.ok(!("model" in written), "must not overwrite an existing value");
  assert.equal(written.backendUrl, "http://127.0.0.1:8723");
  assert.equal(written.autoStart, false);
});

test("onInstalled writes nothing when all defaults are present", async () => {
  const { captured, getWritten } = await loadBackground({
    stored: {
      backendUrl: "http://x",
      model: "m",
      keepStems: ["vocals"],
      autoStart: true,
    },
  });
  await captured.onInstalled();
  assert.equal(getWritten(), null);
});

test("ping-backend reports reachability from a capabilities fetch", async () => {
  const { captured } = await loadBackground({ stored: {} });
  globalThis.fetch = async () => ({ ok: true, status: 200 });
  let response;
  const keepOpen = captured.onMessage(
    { type: "ping-backend" },
    null,
    (r) => (response = r),
  );
  assert.equal(keepOpen, true); // async response -> channel kept open
  await new Promise((r) => setTimeout(r, 5));
  assert.deepEqual(response, { ok: true, status: 200 });
});
