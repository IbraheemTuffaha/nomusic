// Fixed, authenticated backend transport.
//
// Production callers send an operation name and validated payload to the
// service worker. The explicit test adapter below is only enabled by the Node
// test harness; it keeps the existing unit tests hermetic without reintroducing
// page-side backend access in the extension.

export class BackendError extends Error {
  constructor(message, { status, code } = {}) {
    super(message);
    this.name = "BackendError";
    this.status = status;
    this.code = code;
  }
}

export function explainBackendError(error, fallback = "Backend request failed") {
  switch (error?.code) {
    case "revoked": return "Operator key revoked. Update the key in nomusic settings.";
    case "unauthorized": return "Operator key rejected. Check the key in nomusic settings.";
    case "not_configured": return "Configure an operator key in nomusic settings.";
    case "auth_not_configured": return "Backend authentication is not configured. Run nomusic auth generate, then reconnect.";
    case "offline":
    case "timeout": return "Backend unavailable. Start it and retry.";
    case "busy": return "Backend is busy. Retry in a moment.";
    case "backend_not_ready": return "Backend is not ready. Run nomusic doctor and retry.";
    default: return fallback;
  }
}

function throwResponseError(response, body) {
  const detail = body?.detail;
  const code = typeof detail === "object" ? detail.code : undefined;
  const message = typeof detail === "string" ? detail :
    typeof detail?.message === "string" ? detail.message : `HTTP ${response.status}`;
  throw new BackendError(message, { status: response.status, code });
}

function directEndpoint(operation, payload, base) {
  const root = base.replace(/\/+$/, "");
  const id = (value) => encodeURIComponent(value);
  switch (operation) {
    case "capabilities": return { url: `${root}/capabilities`, method: "GET" };
    case "process": return { url: `${root}/process`, method: "POST", json: payload };
    case "interest-renew": return { url: `${root}/process/${id(payload.jobId)}/interest`, method: "POST", json: { client_id: payload.clientId, ...(payload.leaseSeconds ? { lease_seconds: payload.leaseSeconds } : {}) } };
    case "interest-release": return { url: `${root}/process/${id(payload.jobId)}/interest?client_id=${id(payload.clientId)}`, method: "DELETE" };
    case "prioritize": return { url: `${root}/process/${id(payload.jobId)}/prioritize`, method: "POST", json: { from_chunk: payload.fromChunk } };
    case "status": return { url: `${root}/status/${id(payload.jobId)}`, method: "GET" };
    case "chunk": return { url: `${root}/chunk/${id(payload.jobId)}/${id(payload.chunkIndex)}`, method: "GET", binary: true };
    case "export-submit": return { url: `${root}/exports`, method: "POST", json: { job_id: payload.jobId, format: payload.format, ...(payload.maxHeight ? { max_height: payload.maxHeight } : {}), ...(payload.clientId ? { client_id: payload.clientId } : {}) } };
    case "export-status": return { url: `${root}/exports/${id(payload.exportId)}`, method: "GET" };
    case "export-cancel": return { url: `${root}/exports/${id(payload.exportId)}?client_id=${id(payload.clientId)}`, method: "DELETE" };
    case "export-download": return { url: `${root}/exports/${id(payload.exportId)}/download`, method: "GET", binary: true };
    default: throw new BackendError("Unsupported backend operation", { code: "invalid_operation" });
  }
}

async function directRequest(operation, payload, { backendUrl, signal } = {}) {
  const request = directEndpoint(operation, payload, backendUrl);
  const response = await fetch(request.url, {
    method: request.method,
    headers: request.json ? { "Content-Type": "application/json" } : undefined,
    body: request.json ? JSON.stringify(request.json) : undefined,
    cache: "no-store",
    signal,
  });
  if (!response.ok) {
    let body = null;
    try { body = await response.json(); } catch { /* status is enough */ }
    throwResponseError(response, body);
  }
  return request.binary ? response.arrayBuffer() : response.json();
}

function withTimeout(promise, timeoutMs, signal) {
  if (!timeoutMs) return promise;
  return new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      settled = true;
      reject(new BackendError("Backend request timed out", { code: "timeout" }));
    }, timeoutMs);
    const abort = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(new DOMException("The operation was aborted", "AbortError"));
    };
    signal?.addEventListener("abort", abort, { once: true });
    promise.then((value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      resolve(value);
    }, (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      reject(error);
    });
  });
}

export function backendRequest(operation, payload = {}, options = {}) {
  if (globalThis.__nomusicTestDirectBackend) {
    return withTimeout(directRequest(operation, payload, options), options.timeoutMs, options.signal);
  }
  const promise = chrome.runtime.sendMessage({
    type: "backend-request",
    operation,
    ...payload,
  }).then((response) => {
    if (!response?.ok) {
      throw new BackendError(response?.message || "Backend request failed", {
        status: response?.status,
        code: response?.code,
      });
    }
    if (response.bodyBase64 !== undefined) {
      const binary = atob(response.bodyBase64);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      return bytes.buffer;
    }
    return response.data;
  });
  return withTimeout(promise, options.timeoutMs, options.signal);
}
