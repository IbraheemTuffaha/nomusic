// Shared validation for the trusted extension configuration UI and service
// worker. No credential is kept in this module or returned by these helpers.

export const DEFAULT_BACKEND = "http://127.0.0.1:8723";
export const OPERATOR_KEY_PATTERN = /^nm_[0-9a-f]{64}$/;

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]"]);

export function normalizeBackendUrl(raw) {
  if (typeof raw !== "string" || !raw.trim()) {
    throw new Error("Backend URL is required");
  }
  let url;
  try {
    url = new URL(raw.trim());
  } catch {
    throw new Error("Backend URL is invalid");
  }
  if (!/^https?:$/.test(url.protocol)) {
    throw new Error("Backend URL must use HTTP or HTTPS");
  }
  if (url.username || url.password || url.search || url.hash) {
    throw new Error("Backend URL must not contain credentials or query parameters");
  }
  const loopback = url.protocol === "http:" && LOOPBACK_HOSTS.has(url.hostname);
  if (url.protocol === "http:" && !loopback) {
    throw new Error("Remote backends must use HTTPS");
  }
  if (!url.hostname || url.pathname !== "/" && url.pathname !== "") {
    throw new Error("Backend URL must point to its origin");
  }
  url.pathname = "";
  return url.toString().replace(/\/$/, "");
}

export function isOperatorKey(value) {
  return typeof value === "string" && OPERATOR_KEY_PATTERN.test(value.trim());
}

export function backendPermissionOrigin(backendUrl) {
  const normalized = normalizeBackendUrl(backendUrl);
  return `${new URL(normalized).origin}/*`;
}
