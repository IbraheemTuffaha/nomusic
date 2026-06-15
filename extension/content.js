// nomusic content script (entry/bootstrap).
//
// What it does on any page:
//   1. Watches the DOM for <video> elements (handles SPA navigation).
//   2. Attaches a small floating button to each discovered <video>.
//   3. On click: tells the local backend to process the page URL, mutes the
//      <video>, fetches separated audio chunks as they become ready, and
//      schedules them through Web Audio so they play in sync.
//
// The implementation is split into ES modules (loaded on demand here so they
// run in the content-script world with full chrome.*/DOM access):
//   settings.js  — config + chrome.storage cache
//   session.js   — Session: per-video playback (networking, scheduling, mute)
//   button.js    — Button: the floating UI
//   main.js      — discovery/observer + startup (imports the above)
//   stretch.js   — StretchClient (pitch-preserving WSOLA), imported by session.js
//
// This file stays a classic content script (MV3 has no module content_scripts)
// and simply bootstraps the module graph.

(() => {
  if (window.__nomusicLoaded) return;
  window.__nomusicLoaded = true;
  import(chrome.runtime.getURL("main.js")).catch((err) =>
    console.error("[nomusic] failed to load", err),
  );
})();
