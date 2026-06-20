// Preloaded (via `node --test --import`) before any module under test, so the
// content-script ES modules — which assume a browser environment — can be
// imported under node. Only the globals touched at *module top level* need to
// exist here; per-test browser behavior is mocked in the individual tests.
const noop = () => {};

globalThis.chrome ??= {
  storage: {
    sync: { get: async () => ({}), set: async () => {} },
    onChanged: { addListener: noop, removeListener: noop },
  },
  runtime: {
    getURL: (p) => p,
    onInstalled: { addListener: noop },
    onMessage: { addListener: noop },
  },
};

globalThis.location ??= { hostname: "test.local", href: "https://test.local/v" };
