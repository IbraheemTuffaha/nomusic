// Background service worker (ES module — see manifest "type": "module").
//
// Today this is intentionally tiny: the content script talks to the backend
// directly, so the worker just owns the storage defaults and answers the
// popup's "is the backend up?" probe.
//
// Reasons to put logic here later: cross-tab job sharing or a periodic backend
// health check.

import { DEFAULT_BACKEND } from "./config.js";

const DEFAULTS = {
  backendUrl: DEFAULT_BACKEND,
  model: null, // null -> backend's default
  keepStems: null, // null -> backend's default
  autoStart: false,
};

// The auto-seeded default from the localhost-only builds (<=0.1.x). An install
// upgraded from those still carries it, so we move it — and only it — forward to
// the public default below.
const OLD_LOCALHOST_BACKEND = "http://127.0.0.1:8723";

chrome.runtime.onInstalled.addListener(async (details) => {
  const current = await chrome.storage.sync.get(Object.keys(DEFAULTS));
  const patched = {};
  for (const [k, v] of Object.entries(DEFAULTS)) {
    if (current[k] === undefined) patched[k] = v;
  }
  // Migration: the public build defaults every install to the hosted backend,
  // but an upgrade keeps its stored backendUrl. Move ONLY the old auto-seeded
  // loopback value forward — never a URL the user deliberately set, so a
  // self-hoster's custom backend survives the update.
  if (
    details?.reason === "update" &&
    current.backendUrl === OLD_LOCALHOST_BACKEND
  ) {
    patched.backendUrl = DEFAULT_BACKEND;
  }
  if (Object.keys(patched).length) {
    await chrome.storage.sync.set(patched);
  }
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === "ping-backend") {
    (async () => {
      try {
        const settings = await chrome.storage.sync.get(["backendUrl"]);
        const base = settings.backendUrl || DEFAULTS.backendUrl;
        const resp = await fetch(`${base}/capabilities`, { cache: "no-store" });
        sendResponse({ ok: resp.ok, status: resp.status });
      } catch (err) {
        sendResponse({ ok: false, error: String(err) });
      }
    })();
    return true; // keep the message channel open for the async response
  }
  return false;
});
