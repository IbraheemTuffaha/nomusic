// Background service worker.
//
// The content script talks to the local backend directly. The worker owns
// storage defaults, answers the backend health probe, and hands prepared export
// responses to chrome.downloads so page memory never holds a whole file.

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

const activeDownloads = new Map();

chrome.downloads.onChanged?.addListener((delta) => {
  const entry = activeDownloads.get(delta.id);
  if (!entry) return;
  const state = delta.state?.current;
  if (state === "complete") {
    activeDownloads.delete(delta.id);
    if (entry.tabId != null) {
      chrome.tabs?.sendMessage?.(entry.tabId, {
        type: "download-export-complete",
        downloadId: delta.id,
      });
    }
  } else if (state === "interrupted") {
    activeDownloads.delete(delta.id);
    if (entry.cancelUrl) {
      fetch(entry.cancelUrl, { method: "DELETE", cache: "no-store", keepalive: true })
        .catch(() => {});
    }
    if (entry.tabId != null) {
      chrome.tabs?.sendMessage?.(entry.tabId, {
        type: "download-export-failed",
        downloadId: delta.id,
        error: delta.error?.current || "browser download interrupted",
      });
    }
  }
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
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
  if (msg?.type === "download-export") {
    const url = typeof msg.url === "string" ? msg.url : "";
    const filename = typeof msg.filename === "string" ? msg.filename : "";
    if (!url || !filename) {
      sendResponse({ ok: false, error: "download URL and filename are required" });
      return false;
    }
    chrome.downloads.download(
      { url, filename, conflictAction: "uniquify", saveAs: false },
      (downloadId) => {
        const error = chrome.runtime.lastError;
        if (error) {
          sendResponse({ ok: false, error: error.message || String(error) });
        } else {
          activeDownloads.set(downloadId, {
            tabId: sender?.tab?.id,
            cancelUrl: typeof msg.cancelUrl === "string" ? msg.cancelUrl : "",
          });
          sendResponse({ ok: true, downloadId });
        }
      },
    );
    return true;
  }
  return false;
});
