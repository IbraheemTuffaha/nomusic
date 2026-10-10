// Preloaded (via `node --test --import`) before any module under test, so the
// content-script ES modules — which assume a browser environment — can be
// imported under node. Only the globals touched at *module top level* need to
// exist here; per-test browser behavior is mocked in the individual tests.
const noop = () => {};

// Most unit suites opt into a direct fetch seam explicitly because it lets them
// assert AbortSignal behavior. A production-transport bridge is kept here for
// tests that leave the flag disabled; it mirrors the worker message response
// shape and therefore exercises the shipped content-script transport.
globalThis.__nomusicTestDirectBackend = false;
globalThis.__nomusicLegacySseTests = false;

function workerEndpoint(message) {
  const root = "http://127.0.0.1:8723";
  const id = (value) => encodeURIComponent(value);
  switch (message.operation) {
    case "capabilities": return { url: `${root}/capabilities`, method: "GET" };
    case "process": return {
      url: `${root}/process`, method: "POST",
      body: JSON.stringify({ url: message.url, ...(message.model ? { model: message.model } : {}), ...(message.keep_stems ? { keep_stems: message.keep_stems } : {}), ...(message.client_id ? { client_id: message.client_id } : {}) }),
    };
    case "interest-renew": return { url: `${root}/process/${id(message.jobId)}/interest`, method: "POST", body: JSON.stringify({ client_id: message.clientId, ...(message.leaseSeconds ? { lease_seconds: message.leaseSeconds } : {}) }) };
    case "interest-release": return { url: `${root}/process/${id(message.jobId)}/interest?client_id=${id(message.clientId)}`, method: "DELETE" };
    case "prioritize": return { url: `${root}/process/${id(message.jobId)}/prioritize`, method: "POST", body: JSON.stringify({ from_chunk: message.fromChunk }) };
    case "status": return { url: `${root}/status/${id(message.jobId)}`, method: "GET" };
    case "chunk": return { url: `${root}/chunk/${id(message.jobId)}/${id(message.chunkIndex)}`, method: "GET", binary: true };
    case "export-submit": return { url: `${root}/exports`, method: "POST", body: JSON.stringify({ job_id: message.jobId, format: message.format, ...(message.maxHeight ? { max_height: message.maxHeight } : {}), ...(message.clientId ? { client_id: message.clientId } : {}) }) };
    case "export-status": return { url: `${root}/exports/${id(message.exportId)}`, method: "GET" };
    case "export-cancel": return { url: `${root}/exports/${id(message.exportId)}?client_id=${id(message.clientId)}`, method: "DELETE" };
    case "export-download": return { url: `${root}/exports/${id(message.exportId)}/download`, method: "GET", binary: true };
    default: throw new Error("unsupported backend operation");
  }
}

async function workerMessage(message) {
  if (message?.type === "download-export") return { ok: true, downloadId: 1 };
  if (message?.type !== "backend-request") return { ok: true };
  const request = workerEndpoint(message);
  const response = await fetch(request.url, {
    method: request.method,
    headers: request.body ? { "Content-Type": "application/json" } : undefined,
    body: request.body,
    cache: "no-store",
  });
  if (!response.ok) {
    let body = null;
    try { body = await response.json(); } catch { /* status is enough */ }
    const detail = body?.detail;
    return {
      ok: false,
      status: response.status,
      code: typeof detail === "object" ? detail.code : undefined,
      message: typeof detail === "object" ? detail.message : detail || `HTTP ${response.status}`,
    };
  }
  if (request.binary) {
    const bytes = new Uint8Array(await response.arrayBuffer());
    let binary = "";
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return { ok: true, status: response.status || 200, bodyBase64: btoa(binary) };
  }
  return { ok: true, status: response.status || 200, data: await response.json() };
}

globalThis.chrome ??= {
  storage: {
    sync: { get: async () => ({}), set: async () => {} },
    local: {
      get: async () => ({}), set: async () => {}, remove: async () => {},
      setAccessLevel: async () => {},
    },
    onChanged: { addListener: noop, removeListener: noop },
  },
  runtime: {
    getURL: (p) => p,
    sendMessage: (message, callback) => {
      const pending = workerMessage(message);
      if (typeof callback === "function") {
        pending.then(callback, (error) => {
          globalThis.chrome.runtime.lastError = { message: error?.message || String(error) };
          callback(undefined);
          globalThis.chrome.runtime.lastError = null;
        });
      }
      return pending;
    },
    lastError: null,
    onInstalled: { addListener: noop },
    onMessage: { addListener: noop, removeListener: noop },
  },
  downloads: {
    download: (_options, callback) => callback?.(1),
  },
};

globalThis.location ??= { hostname: "test.local", href: "https://test.local/v" };
