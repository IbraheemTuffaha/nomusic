// Shared non-secret config + the in-memory settings cache that mirrors
// chrome.storage.sync. The operator key is intentionally absent: content
// scripts run in page-facing contexts and must never be able to read it.
// chrome.storage drives the popup; each new Session snapshots this cache.
// Active sessions keep one backend/model/stem identity until toggled off/on.

export { DEFAULT_BACKEND } from "./config.js";
import { DEFAULT_BACKEND } from "./config.js";
export const SYNC_TOLERANCE_S = 0.08;
export const SYNC_CHECK_MS = 250;

// Pitch-preserving playback at non-1x speeds. When true, each chunk is
// time-stretched (pitch preserved) by the vendored SoundTouch library
// (third_party/soundtouch/) and scheduled at srcRate 1, matching the native
// player. When false — or if the library fails to load — playback falls back
// to resampling, which keeps sync but shifts pitch.
export const PITCH_PRESERVE = true;

// Debug logging for seek/buffer/prioritize state transitions. Off in shipped
// builds; flip to ``true`` locally to trace state in DevTools (every line is
// prefixed with [nomusic]).
const DEBUG = false;
export const dlog = DEBUG
  ? (...args) => console.log("[nomusic]", ...args)
  : () => {};

export const settings = {
  backendUrl: DEFAULT_BACKEND,
  model: null,
  keepStems: null,
};

export async function loadSettings() {
  try {
    const stored = await chrome.storage.sync.get([
      "backendUrl",
      "model",
      "keepStems",
    ]);
    if (stored.backendUrl) settings.backendUrl = stored.backendUrl;
    if (stored.model !== undefined) settings.model = stored.model;
    if (stored.keepStems !== undefined) settings.keepStems = stored.keepStems;
  } catch (err) {
    // Storage permission missing? Fall back to defaults.
    dlog("loadSettings failed; using defaults", err?.name || err);
  }
}

chrome.storage?.onChanged?.addListener?.((changes) => {
  if (changes.backendUrl) settings.backendUrl = changes.backendUrl.newValue;
  if (changes.model) settings.model = changes.model.newValue;
  if (changes.keepStems) settings.keepStems = changes.keepStems.newValue;
});
