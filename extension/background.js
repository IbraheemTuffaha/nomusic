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
const AUTH_CONFIG_STORAGE_KEY = "trustedBackend";
const LEGACY_AUTH_STORAGE_KEY = "operatorKey";
const AUTH_CONFIG_VERSION = 1;
const MAX_CHUNK_BYTES = 8 * 1024 * 1024;
const REQUEST_TIMEOUTS_MS = {
  capabilities: 5_000,
  process: 30_000,
  "interest-renew": 5_000,
  "interest-release": 5_000,
  prioritize: 5_000,
  status: 5_000,
  chunk: 15_000,
  "export-submit": 10_000,
  "export-status": 5_000,
  "export-cancel": 5_000,
  "export-download": 30_000,
};
const ID_RE = /^[0-9a-f]{16}$/;
const EXPORT_ID_RE = /^[0-9a-f]{32}$/;
const STEMS = new Set(["vocals", "drums", "bass", "other"]);
const OPERATIONS = new Set([
  "capabilities", "process", "interest-renew", "interest-release",
  "prioritize", "status", "chunk", "export-submit", "export-status",
  "export-cancel", "export-download",
]);

// Content scripts are page-facing contexts. Restrict the local area so they
// cannot read the operator key even though they share the extension's origin.
try {
  const access = chrome.storage.local?.setAccessLevel?.({
    accessLevel: "TRUSTED_CONTEXTS",
  });
  access?.catch?.(() => {});
  const sessionAccess = chrome.storage.session?.setAccessLevel?.({
    accessLevel: "TRUSTED_CONTEXTS",
  });
  sessionAccess?.catch?.(() => {});
} catch {
  // Storage errors are reported when a setup operation is attempted.
}

async function readSync(keys = Object.keys(DEFAULTS)) {
  return chrome.storage.sync.get(keys);
}

async function readTrustedAuth() {
  const stored = await chrome.storage.local.get(AUTH_CONFIG_STORAGE_KEY);
  const config = stored?.[AUTH_CONFIG_STORAGE_KEY];
  if (!config || typeof config !== "object" || Array.isArray(config) ||
      config.version !== AUTH_CONFIG_VERSION ||
      typeof config.backendUrl !== "string" ||
      typeof config.operatorKey !== "string" ||
      !OPERATOR_KEY_PATTERN.test(config.operatorKey)) {
    return null;
  }
  let backendUrl;
  try {
    backendUrl = normalizeBackendUrl(config.backendUrl);
  } catch {
    return null;
  }
  return {
    backendUrl,
    operatorKey: config.operatorKey,
    generation: typeof config.generation === "string" && config.generation
      ? config.generation : "legacy",
  };
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

async function fetchWithTimeout(url, options, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

async function fetchCapabilities(backendUrl, operatorKey) {
  const response = await fetchWithTimeout(`${backendUrl}/capabilities`, {
    cache: "no-store",
    headers: { Authorization: `Bearer ${operatorKey}` },
  }, REQUEST_TIMEOUTS_MS.capabilities);
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

  const previousAuth = await readTrustedAuth();
  const previousSync = await readSync(["backendUrl", "model", "keepStems"]);
  const nextSync = { backendUrl };
  if (Array.isArray(message.keepStems)) nextSync.keepStems = message.keepStems.slice(0, 4);
  if (typeof message.model === "string" && message.model.length <= 80) nextSync.model = message.model;
  const generation = globalThis.crypto?.randomUUID?.() ||
    `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  const nextAuth = {
    version: AUTH_CONFIG_VERSION,
    backendUrl,
    operatorKey,
    generation,
  };
  try {
    await chrome.storage.local.set({ [AUTH_CONFIG_STORAGE_KEY]: nextAuth });
    await chrome.storage.local.remove(LEGACY_AUTH_STORAGE_KEY);
    await chrome.storage.sync.set(nextSync);
  } catch {
    try {
      if (previousAuth) await chrome.storage.local.set({
        [AUTH_CONFIG_STORAGE_KEY]: {
          version: AUTH_CONFIG_VERSION,
          backendUrl: previousAuth.backendUrl,
          operatorKey: previousAuth.operatorKey,
          generation: previousAuth.generation,
        },
      });
      else await chrome.storage.local.remove(AUTH_CONFIG_STORAGE_KEY);
      await chrome.storage.sync.set(previousSync);
    } catch {
      // Keep the response free of credential material even if rollback fails.
    }
    return responseError("storage_error", "Could not save the trusted backend settings.");
  }
  return { ok: true, status: checked.status, capabilities: checked.capabilities };
}

function object(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function boundedText(value, max, name) {
  if (typeof value !== "string" || !value.trim() || value.length > max) {
    throw new Error(`${name} is invalid`);
  }
  return value.trim();
}

function safeJobId(value) {
  const id = boundedText(value, 16, "job id").toLowerCase();
  if (!ID_RE.test(id)) throw new Error("job id is invalid");
  return id;
}

function safeExportId(value) {
  const id = boundedText(value, 32, "export id").toLowerCase();
  if (!EXPORT_ID_RE.test(id)) throw new Error("export id is invalid");
  return id;
}

function safeClientId(value) {
  return boundedText(value, 128, "client id");
}

function validateOperation(operation, input) {
  if (!OPERATIONS.has(operation) || !object(input)) throw new Error("unsupported backend operation");
  switch (operation) {
    case "capabilities":
      return {};
    case "process": {
      const result = { url: boundedText(input.url, 4096, "source URL"), client_id: undefined };
      if (input.model !== undefined && input.model !== null) result.model = boundedText(input.model, 80, "model");
      if (input.keep_stems !== undefined && input.keep_stems !== null) {
        if (!Array.isArray(input.keep_stems) || !input.keep_stems.length || input.keep_stems.length > 4 || input.keep_stems.some((stem) => !STEMS.has(stem))) throw new Error("keep_stems is invalid");
        result.keep_stems = [...input.keep_stems];
      }
      if (input.client_id !== undefined && input.client_id !== null) result.client_id = safeClientId(input.client_id);
      if (result.client_id === undefined) delete result.client_id;
      return result;
    }
    case "interest-renew": {
      const result = { jobId: safeJobId(input.jobId), client_id: safeClientId(input.clientId) };
      if (input.leaseSeconds !== undefined) {
        if (!Number.isFinite(input.leaseSeconds) || input.leaseSeconds <= 0 || input.leaseSeconds > 3600) throw new Error("lease seconds is invalid");
        result.lease_seconds = input.leaseSeconds;
      }
      return result;
    }
    case "interest-release":
      return { jobId: safeJobId(input.jobId), client_id: safeClientId(input.clientId) };
    case "prioritize":
      if (!Number.isInteger(input.fromChunk) || input.fromChunk < 0 || input.fromChunk > 10_000_000) throw new Error("chunk index is invalid");
      return { jobId: safeJobId(input.jobId), from_chunk: input.fromChunk };
    case "status":
      return { jobId: safeJobId(input.jobId) };
    case "chunk":
      if (!Number.isInteger(input.chunkIndex) || input.chunkIndex < 0 || input.chunkIndex > 10_000_000) throw new Error("chunk index is invalid");
      return { jobId: safeJobId(input.jobId), chunkIndex: input.chunkIndex };
    case "export-submit":
      if (!["mp3", "mp4"].includes(input.format)) throw new Error("export format is invalid");
      if (input.maxHeight !== undefined && (!Number.isInteger(input.maxHeight) || input.maxHeight < -1 || input.maxHeight > 10_000)) throw new Error("export height is invalid");
      return { jobId: safeJobId(input.jobId), format: input.format, ...(input.maxHeight ? { max_height: input.maxHeight } : {}), ...(input.clientId ? { client_id: safeClientId(input.clientId) } : {}) };
    case "export-status":
      return { exportId: safeExportId(input.exportId) };
    case "export-cancel":
      return { exportId: safeExportId(input.exportId), client_id: safeClientId(input.clientId) };
    case "export-download":
      return { exportId: safeExportId(input.exportId) };
    default:
      throw new Error("unsupported backend operation");
  }
}

function operationRequest(operation, data, backendUrl) {
  const root = backendUrl.replace(/\/+$/, "");
  const id = (value) => encodeURIComponent(value);
  const json = (url, body) => ({ url, method: "POST", body: JSON.stringify(body), headers: { "Content-Type": "application/json" } });
  switch (operation) {
    case "capabilities": return { url: `${root}/capabilities`, method: "GET" };
    case "process": return json(`${root}/process`, data);
    case "interest-renew": return json(`${root}/process/${id(data.jobId)}/interest`, { client_id: data.client_id, ...(data.lease_seconds ? { lease_seconds: data.lease_seconds } : {}) });
    case "interest-release": return { url: `${root}/process/${id(data.jobId)}/interest?client_id=${id(data.client_id)}`, method: "DELETE" };
    case "prioritize": return json(`${root}/process/${id(data.jobId)}/prioritize`, { from_chunk: data.from_chunk });
    case "status": return { url: `${root}/status/${id(data.jobId)}`, method: "GET" };
    case "chunk": return { url: `${root}/chunk/${id(data.jobId)}/${id(data.chunkIndex)}`, method: "GET", binary: true };
    case "export-submit": return json(`${root}/exports`, {
      job_id: data.jobId,
      format: data.format,
      ...(data.max_height === undefined ? {} : { max_height: data.max_height }),
      ...(data.client_id === undefined ? {} : { client_id: data.client_id }),
    });
    case "export-status": return { url: `${root}/exports/${id(data.exportId)}`, method: "GET" };
    case "export-cancel": return { url: `${root}/exports/${id(data.exportId)}?client_id=${id(data.client_id)}`, method: "DELETE" };
    case "export-download": return { url: `${root}/exports/${id(data.exportId)}/download`, method: "GET", binary: true };
    default: throw new Error("unsupported backend operation");
  }
}

function errorCode(status, detail) {
  if (status === 401 && detail?.code === "credential_revoked") return "revoked";
  if (status === 401) return "unauthorized";
  if (status === 503 && detail?.code === "auth_not_configured") return "auth_not_configured";
  if (status === 503) return "backend_not_ready";
  if (status === 429) return "busy";
  return "backend_error";
}

async function backendRequest(operation, input) {
  const data = validateOperation(operation, input);
  const auth = await readTrustedAuth();
  if (!auth) return responseError("not_configured", "Configure a trusted backend and operator key first.");
  const request = operationRequest(operation, data, auth.backendUrl);
  let response;
  try {
    response = await fetchWithTimeout(request.url, {
      method: request.method,
      headers: { ...(request.headers || {}), Authorization: `Bearer ${auth.operatorKey}` },
      body: request.body,
      cache: "no-store",
    }, REQUEST_TIMEOUTS_MS[operation] || 10_000);
  } catch {
    return responseError("offline", "The backend could not be reached.");
  }
  if (!response.ok) {
    let body = null;
    try { body = await response.json(); } catch { /* use status */ }
    const detail = body?.detail;
    return responseError(errorCode(response.status, detail),
      typeof detail === "string" ? detail : detail?.message || `Backend returned HTTP ${response.status}.`, response.status);
  }
  if (request.binary) {
    const length = Number(response.headers?.get?.("content-length") || 0);
    if (length > MAX_CHUNK_BYTES) return responseError("response_too_large", "The backend response is too large.", response.status);
    const bytes = new Uint8Array(await response.arrayBuffer());
    if (bytes.byteLength > MAX_CHUNK_BYTES) return responseError("response_too_large", "The backend response is too large.", response.status);
    let binary = "";
    const step = 0x8000;
    for (let offset = 0; offset < bytes.length; offset += step) binary += String.fromCharCode(...bytes.subarray(offset, offset + step));
    return { ok: true, status: response.status, bodyBase64: btoa(binary) };
  }
  let dataBody = null;
  try { dataBody = await response.json(); } catch { return responseError("backend_error", "The backend returned invalid JSON.", response.status); }
  return { ok: true, status: response.status, data: dataBody };
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
        const auth = await readTrustedAuth();
        sendResponse({
          ok: true,
          configured: Boolean(auth),
          ...(auth ? { backendUrl: auth.backendUrl } : {}),
        });
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
    Promise.all([
      chrome.storage.local.remove(AUTH_CONFIG_STORAGE_KEY),
      chrome.storage.local.remove(LEGACY_AUTH_STORAGE_KEY),
    ])
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
        const auth = await readTrustedAuth();
        if (!auth) {
          sendResponse(responseError("not_configured", "Configure a trusted backend and operator key first."));
          return;
        }
        sendResponse(await fetchCapabilities(auth.backendUrl, auth.operatorKey));
      } catch {
        sendResponse(responseError("offline", "The backend could not be reached."));
      }
    })();
    return true;
  }

  if (msg?.type === "ping-backend") {
    (async () => {
      try {
        const auth = await readTrustedAuth();
        if (!auth) {
          sendResponse(responseError("not_configured", "Configure a trusted backend and operator key first."));
          return;
        }
        const result = await fetchCapabilities(auth.backendUrl, auth.operatorKey);
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

  if (msg?.type === "backend-request") {
    backendRequest(msg.operation, msg)
      .then(sendResponse)
      .catch((error) => sendResponse(responseError(
        error?.code || "invalid_request",
        error?.message || "Invalid backend request",
      )));
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
