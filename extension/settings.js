// Shared config + the in-memory settings cache that mirrors chrome.storage.
// chrome.storage drives the popup; this content-script copy is read on every
// request. Imported by session.js, button.js, and the content.js entry (main.js).

// The shipped default lives in config.js so the popup, service worker, and this
// content-script copy can't drift. Re-exported here so existing importers keep
// working unchanged.
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
