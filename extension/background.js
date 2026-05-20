// Background service worker.
//
// Today this is intentionally tiny: the content script talks to the local
// backend directly, so the worker just owns the storage defaults and answers
// the popup's "is the backend up?" probe.
//
// Reasons to put logic here later: cross-tab job sharing, a periodic backend
// health check, or migrating away from a localhost origin.

const DEFAULTS = {
  backendUrl: "http://127.0.0.1:8723",
  model: null, // null -> backend's default
  keepStems: null, // null -> backend's default
  autoStart: false,
};

chrome.runtime.onInstalled.addListener(async () => {
  const current = await chrome.storage.sync.get(Object.keys(DEFAULTS));
  const patched = {};
  for (const [k, v] of Object.entries(DEFAULTS)) {
    if (current[k] === undefined) patched[k] = v;
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
