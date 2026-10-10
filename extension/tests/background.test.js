// Unit tests for background.js — the service worker's storage-defaults seeding
// and backend-ping handler. background.js registers its chrome listeners at
// import time, so we install capturing stubs and dynamic-import it.
import { test } from "node:test";
import assert from "node:assert/strict";
import { OPERATOR_KEY_PATTERN, normalizeBackendUrl } from "../config.js";

let loadCounter = 0;

async function loadBackground({ stored }) {
  const captured = {};
  let written = null;
  const local = {};
  const session = {};
  let accessLevel = null;
  let sessionAccessLevel = null;
  const messages = [];
  globalThis.chrome = {
    runtime: {
      onInstalled: { addListener: (cb) => (captured.onInstalled = cb) },
      onMessage: { addListener: (cb) => (captured.onMessage = cb) },
      getURL: (path) => `chrome-extension://test/${path}`,
      lastError: null,
    },
    downloads: {
      onChanged: { addListener: (cb) => (captured.onChanged = cb) },
      download: (options, callback) => {
        captured.downloadOptions = options;
        callback(42);
      },
    },
    tabs: {
      sendMessage: (tabId, message) => messages.push({ tabId, message }),
    },
    storage: {
      sync: {
        get: async () => stored,
        set: async (patch) => (written = patch),
      },
      local: {
        get: async (key) => typeof key === "string" ? { [key]: local[key] } : { ...local },
        set: async (patch) => Object.assign(local, patch),
        remove: async (key) => delete local[key],
        setAccessLevel: async (value) => (accessLevel = value),
      },
      session: {
        get: async (key) => typeof key === "string" ? { [key]: session[key] } : { ...session },
        set: async (patch) => Object.assign(session, patch),
        remove: async (key) => {
          for (const name of Array.isArray(key) ? key : [key]) delete session[name];
        },
        setAccessLevel: async (value) => (sessionAccessLevel = value),
      },
    },
  };
  // Unique query each call so the module's top-level listener registration
  // re-runs against this call's capturing stubs (ESM caches by specifier).
  await import(`../background.js?load=${++loadCounter}`);
  return {
    captured,
    getWritten: () => written,
    messages,
    local,
    session,
    getAccessLevel: () => accessLevel,
    getSessionAccessLevel: () => sessionAccessLevel,
  };
}

test("backend URL validation keeps loopback HTTP and rejects remote HTTP", () => {
  assert.equal(normalizeBackendUrl("http://127.0.0.1:8723/"), "http://127.0.0.1:8723");
  assert.equal(normalizeBackendUrl("https://backend.example/"), "https://backend.example");
  assert.throws(() => normalizeBackendUrl("http://backend.example"), /HTTPS/);
  assert.throws(() => normalizeBackendUrl("https://user:pass@backend.example"), /credentials/);
  assert.ok(OPERATOR_KEY_PATTERN.test(`nm_${"a".repeat(64)}`));
});

test("download-export delegates the response body to chrome downloads", async () => {
  const { captured, local } = await loadBackground({ stored: { backendUrl: "http://127.0.0.1:8723" } });
  local.operatorKey = `nm_${"e".repeat(64)}`;
  let response;
  const keepOpen = captured.onMessage(
    {
      type: "download-export",
      exportId: "a".repeat(32),
      clientId: "client-1",
      filename: "sample.mp3",
    },
    {},
    (value) => { response = value; },
  );
  assert.equal(keepOpen, true);
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.deepEqual(captured.downloadOptions, {
    url: `http://127.0.0.1:8723/exports/${"a".repeat(32)}/download`,
    headers: [{ name: "Authorization", value: `Bearer ${local.operatorKey}` }],
    filename: "sample.mp3",
    conflictAction: "uniquify",
    saveAs: false,
  });
  assert.deepEqual(response, { ok: true, downloadId: 42 });
});

test("an interrupted native download cancels its export and notifies its tab", async () => {
  const { captured, messages, local } = await loadBackground({ stored: { backendUrl: "http://127.0.0.1:8723" } });
  local.operatorKey = `nm_${"f".repeat(64)}`;
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url, options });
    return { ok: true };
  };
  let response;
  captured.onMessage(
    {
      type: "download-export",
      exportId: "b".repeat(32),
      clientId: "client-1",
      filename: "sample.mp3",
    },
    { tab: { id: 9 } },
    (value) => { response = value; },
  );
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.deepEqual(response, { ok: true, downloadId: 42 });
  captured.onChanged({
    id: 42,
    state: { current: "interrupted" },
    error: { current: "NETWORK_FAILED" },
  });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, `http://127.0.0.1:8723/exports/${"b".repeat(32)}?client_id=client-1`);
  assert.equal(requests[0].options.method, "DELETE");
  assert.equal(requests[0].options.headers.Authorization, `Bearer ${local.operatorKey}`);
  assert.deepEqual(messages, [{
    tabId: 9,
    message: {
      type: "download-export-failed",
      downloadId: 42,
      error: "NETWORK_FAILED",
    },
  }]);
});

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
  const { captured, local } = await loadBackground({ stored: {} });
  local.trustedBackend = {
    version: 1,
    backendUrl: "http://127.0.0.1:8723",
    operatorKey: `nm_${"a".repeat(64)}`,
    generation: "test-a",
  };
  globalThis.fetch = async (_url, options) => {
    assert.equal(options.headers.Authorization, `Bearer ${local.trustedBackend.operatorKey}`);
    return { ok: true, status: 200, json: async () => ({ engine: {} }) };
  };
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

test("configure-auth commits only after an authenticated capabilities check", async () => {
  const { captured, local, getWritten, getAccessLevel } = await loadBackground({
    stored: { backendUrl: "http://127.0.0.1:8723", model: null, keepStems: null },
  });
  const key = `nm_${"b".repeat(64)}`;
  globalThis.fetch = async (_url, options) => {
    assert.equal(options.headers.Authorization, `Bearer ${key}`);
    return { ok: true, status: 200, json: async () => ({ engine: { name: "fake" } }) };
  };
  let response;
  const open = captured.onMessage(
    { type: "configure-auth", backendUrl: "http://127.0.0.1:8723/", operatorKey: key },
    { url: "chrome-extension://test/popup.html" },
    (value) => (response = value),
  );
  assert.equal(open, true);
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(response.ok, true);
  assert.equal(local.trustedBackend.operatorKey, key);
  assert.equal(local.trustedBackend.backendUrl, "http://127.0.0.1:8723");
  assert.equal(getWritten().backendUrl, "http://127.0.0.1:8723");
  assert.deepEqual(getAccessLevel(), { accessLevel: "TRUSTED_CONTEXTS" });
});

test("content-script messages cannot write the operator key", async () => {
  const { captured, local } = await loadBackground({ stored: {} });
  let response;
  const open = captured.onMessage(
    { type: "configure-auth", backendUrl: "http://127.0.0.1:8723", operatorKey: `nm_${"c".repeat(64)}` },
    { tab: { id: 1 }, url: "https://video.example/" },
    (value) => (response = value),
  );
  assert.equal(open, false);
  assert.equal(response.code, "forbidden");
  assert.equal(local.operatorKey, undefined);
});

test("backend-request exposes only validated operations and keeps auth in the worker", async () => {
  const { captured, local } = await loadBackground({ stored: { backendUrl: "http://127.0.0.1:8723" } });
  local.trustedBackend = {
    version: 1,
    backendUrl: "http://127.0.0.1:8723",
    operatorKey: `nm_${"d".repeat(64)}`,
    generation: "test-d",
  };
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url, options });
    return { ok: true, status: 200, json: async () => ({ state: "processing" }) };
  };
  let response;
  captured.onMessage(
    { type: "backend-request", operation: "status", jobId: "a".repeat(16) },
    { tab: { id: 5 }, url: "https://video.example/" },
    (value) => (response = value),
  );
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.deepEqual(response, { ok: true, status: 200, data: { state: "processing" } });
  assert.equal(requests[0].url, `http://127.0.0.1:8723/status/${"a".repeat(16)}`);
  assert.equal(requests[0].options.headers.Authorization, `Bearer ${local.trustedBackend.operatorKey}`);

  let rejected;
  captured.onMessage(
    { type: "backend-request", operation: "fetch-arbitrary", url: "https://secret.example" },
    { tab: { id: 5 } },
    (value) => (rejected = value),
  );
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(rejected.code, "invalid_request");
  assert.equal(requests.length, 1);
});
