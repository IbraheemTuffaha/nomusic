// Preloaded (via `node --test --import`) before any module under test, so the
// content-script ES modules — which assume a browser environment — can be
// imported under node. Only the globals touched at *module top level* need to
// exist here; per-test browser behavior is mocked in the individual tests.
const noop = () => {};

// Content-module tests use a hermetic fetch stub. Production content scripts
// leave this flag unset and therefore always route through the service worker.
globalThis.__nomusicTestDirectBackend = true;
globalThis.__nomusicLegacySseTests = true;

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
    sendMessage: (_message, callback) => callback?.({ ok: true, downloadId: 1 }),
    lastError: null,
    onInstalled: { addListener: noop },
    onMessage: { addListener: noop, removeListener: noop },
  },
  downloads: {
    download: (_options, callback) => callback?.(1),
  },
};

globalThis.location ??= { hostname: "test.local", href: "https://test.local/v" };
