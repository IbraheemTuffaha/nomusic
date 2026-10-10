// Trusted extension service worker.
//
// Page-facing content scripts never receive the operator key and never make
// backend requests directly. This worker owns the key in storage.local,
// validates the small message surface, and performs authenticated setup and
// capability checks. Processing transport is added in the next stack layer.

import {
  DEFAULT_BACKEND,
  OPERATOR_KEY_PATTERN,
  normalizeBackendUrl,
} from "./config.js";

const DEFAULTS = {
  backendUrl: DEFAULT_BACKEND,
  model: null,
  keepStems: null,
  autoStart: false,
};
const AUTH_STORAGE_KEY = "operatorKey";

// Content scripts are page-facing contexts. Restrict the local area so they
// cannot read the operator key even though they share the extension's origin.
try {
  const access = chrome.storage.local?.setAccessLevel?.({
    accessLevel: "TRUSTED_CONTEXTS",
  });
  access?.catch?.(() => {});
} catch {
  // Storage errors are reported when a setup operation is attempted.
}

async function readSync(keys = Object.keys(DEFAULTS)) {
  return chrome.storage.sync.get(keys);
}

async function readOperatorKey() {
  const stored = await chrome.storage.local.get(AUTH_STORAGE_KEY);
  const key = stored?.[AUTH_STORAGE_KEY];
  return typeof key === "string" && OPERATOR_KEY_PATTERN.test(key) ? key : null;
}

function trustedSender(sender) {
  // Content-script messages carry a tab. A page can otherwise manufacture a
  // runtime message, so configuration writes require an extension page/worker
  // sender. Chromium may include the active tab on a popup sender too, so
  // validate the sender origin before applying the content-script guard.
  if (sender?.url) {
    try {
      return sender.url.startsWith(chrome.runtime.getURL(""));
    } catch {
      return false;
    }
  }
  if (sender?.tab) return false;
  return true; // unit tests and extension-internal callers
}

function responseError(code, message, status = undefined) {
  return { ok: false, code, message, ...(status === undefined ? {} : { status }) };
}

async function fetchCapabilities(backendUrl, operatorKey) {
  const response = await fetch(`${backendUrl}/capabilities`, {
    cache: "no-store",
    headers: { Authorization: `Bearer ${operatorKey}` },
  });
  if (!response.ok) {
    if (response.status === 401) {
      return responseError("unauthorized", "The operator key was rejected.", response.status);
    }
    if (response.status === 503) {
      return responseError("backend_not_ready", "The backend is not configured or ready.", response.status);
    }
    return responseError("backend_error", `Backend returned HTTP ${response.status}.`, response.status);
  }
  let capabilities;
  try {
    capabilities = await response.json();
  } catch {
    return responseError("backend_error", "The backend returned invalid capabilities.", response.status);
  }
  return { ok: true, status: response.status, capabilities };
}

async function configureAuth(message) {
  let backendUrl;
  try {
    backendUrl = normalizeBackendUrl(message.backendUrl);
  } catch (error) {
    return responseError("invalid_backend", error.message);
  }
  const operatorKey = typeof message.operatorKey === "string"
    ? message.operatorKey.trim() : "";
  if (!OPERATOR_KEY_PATTERN.test(operatorKey)) {
    return responseError("invalid_key", "Enter the complete operator key.");
  }
  const checked = await fetchCapabilities(backendUrl, operatorKey).catch(() =>
    responseError("offline", "The backend could not be reached."));
  if (!checked.ok) return checked;

  const previousKey = await readOperatorKey();
  const previousSync = await readSync(["backendUrl", "model", "keepStems"]);
  const nextSync = { backendUrl };
  if (Array.isArray(message.keepStems)) nextSync.keepStems = message.keepStems.slice(0, 4);
  if (typeof message.model === "string" && message.model.length <= 80) nextSync.model = message.model;
  try {
    await chrome.storage.local.set({ [AUTH_STORAGE_KEY]: operatorKey });
    await chrome.storage.sync.set(nextSync);
  } catch {
    try {
      if (previousKey) await chrome.storage.local.set({ [AUTH_STORAGE_KEY]: previousKey });
      else await chrome.storage.local.remove(AUTH_STORAGE_KEY);
      await chrome.storage.sync.set(previousSync);
    } catch {
      // Keep the response free of credential material even if rollback fails.
    }
    return responseError("storage_error", "Could not save the trusted backend settings.");
  }
  return { ok: true, status: checked.status, capabilities: checked.capabilities };
}

const activeDownloads = new Map();

chrome.runtime.onInstalled.addListener(async () => {
  const current = await readSync();
  const patched = {};
  for (const [key, value] of Object.entries(DEFAULTS)) {
    if (current[key] === undefined) patched[key] = value;
  }
  if (Object.keys(patched).length) await chrome.storage.sync.set(patched);
});

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
  if (msg?.type === "get-auth-state") {
    (async () => {
      try {
        sendResponse({ ok: true, configured: Boolean(await readOperatorKey()) });
      } catch {
        sendResponse(responseError("storage_error", "Trusted storage is unavailable."));
      }
    })();
    return true;
  }

  if (msg?.type === "configure-auth") {
    if (!trustedSender(sender)) {
      sendResponse(responseError("forbidden", "Only the extension settings page may configure auth."));
      return false;
    }
    configureAuth(msg).then(sendResponse).catch(() =>
      sendResponse(responseError("backend_error", "Backend setup failed.")));
    return true;
  }

  if (msg?.type === "clear-auth") {
    if (!trustedSender(sender)) {
      sendResponse(responseError("forbidden", "Only the extension settings page may clear auth."));
      return false;
    }
    chrome.storage.local.remove(AUTH_STORAGE_KEY)
      .then(() => sendResponse({ ok: true }))
      .catch(() => sendResponse(responseError("storage_error", "Could not clear the operator key.")));
    return true;
  }

  if (msg?.type === "save-preferences") {
    if (!trustedSender(sender)) {
      sendResponse(responseError("forbidden", "Only the extension settings page may save preferences."));
      return false;
    }
    const patch = {};
    if (typeof msg.model === "string" || msg.model === null) patch.model = msg.model;
    if (Array.isArray(msg.keepStems)) patch.keepStems = msg.keepStems.slice(0, 4);
    chrome.storage.sync.set(patch)
      .then(() => sendResponse({ ok: true }))
      .catch(() => sendResponse(responseError("storage_error", "Could not save preferences.")));
    return true;
  }

  if (msg?.type === "backend-capabilities") {
    (async () => {
      try {
        const stored = await readSync(["backendUrl"]);
        const backendUrl = normalizeBackendUrl(stored.backendUrl || DEFAULT_BACKEND);
        const key = await readOperatorKey();
        if (!key) {
          sendResponse(responseError("not_configured", "Configure an operator key first."));
          return;
        }
        sendResponse(await fetchCapabilities(backendUrl, key));
      } catch {
        sendResponse(responseError("offline", "The backend could not be reached."));
      }
    })();
    return true;
  }

  if (msg?.type === "ping-backend") {
    (async () => {
      try {
        const stored = await readSync(["backendUrl"]);
        const backendUrl = normalizeBackendUrl(stored.backendUrl || DEFAULT_BACKEND);
        const key = await readOperatorKey();
        if (!key) {
          sendResponse(responseError("not_configured", "Configure an operator key first."));
          return;
        }
        const result = await fetchCapabilities(backendUrl, key);
        sendResponse({
          ok: result.ok,
          ...(result.status === undefined ? {} : { status: result.status }),
          ...(result.code === undefined ? {} : { code: result.code }),
        });
      } catch {
        sendResponse(responseError("offline", "The backend could not be reached."));
      }
    })();
    return true;
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
