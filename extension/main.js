// Entry module: discover <video> elements (incl. SPA-added), attach a Button
// to each, and keep them anchored across layout/navigation changes.
import { loadSettings } from "./settings.js";
import { Button } from "./button.js";

// ---------------------------------------------------------------------------
// Discovery: attach to every <video>, including ones added later.
// ---------------------------------------------------------------------------
const attached = new WeakMap();
// Enumerable view of live buttons so layout-change handlers (fullscreen /
// resize) can re-anchor them — the WeakMap above isn't iterable.
const liveButtons = new Set();

function anchorButton(btn) {
  const host = pickHost(btn.video);
  if (!host) {
    // No visible player to anchor to (the video's player was torn down /
    // hidden, e.g. after navigating to the YouTube home feed). Hide the pill
    // rather than let it strand in a window-sized fallback host; it re-appears
    // on the next re-anchor once a real player is back.
    btn.el.style.display = "none";
    return;
  }
  // Our position:absolute needs a positioned host, re-asserted every time: the
  // host can be re-created or have its inline position reset by the page around
  // fullscreen / SPA navigation, which otherwise lets the button's offset
  // parent climb to a window-sized ancestor (the "stuck in the corner" bug).
  if (getComputedStyle(host).position === "static") {
    host.style.position = "relative";
  }
  btn.position(host);
  if (!btn._dismissed) btn.el.style.display = "";
}

function attachToVideo(video) {
  // Skip tiny/decorative videos (autoplay ads, etc.).
  if (video.clientWidth > 0 && video.clientWidth < 200) return;
  const existing = attached.get(video);
  if (existing) {
    // Already has a live button — reanchorButtons() handles repositioning it.
    if (liveButtons.has(existing)) return;
    // Its button was retired when this element briefly disconnected, but the
    // same element is back: YouTube reuses one persistent <video> across SPA
    // routes. Tear the stale button (and any session) down and re-attach below.
    existing.session?.dispose?.();
    existing.dispose?.();
    existing.el.remove();
  }
  // No visible player box yet — a route change can land before the new player
  // is laid out. Record nothing so the next refresh() pass retries once it has
  // a size, instead of skipping this element forever (the "no button until I
  // reload" bug on YouTube SPA navigation).
  if (!pickHost(video)) return;

  const btn = new Button(video);
  anchorButton(btn);
  attached.set(video, btn);
  liveButtons.add(btn);
}

// Re-anchor every live button to its video's current host. Called on layout
// shifts that can re-parent the player (fullscreen toggle, window resize).
function reanchorButtons() {
  for (const btn of liveButtons) {
    if (!btn.video || !btn.video.isConnected) {
      btn.el.style.display = "none"; // its video is gone — don't leave it stranded
      liveButtons.delete(btn);
      continue;
    }
    anchorButton(btn);
  }
}

function pickHost(video) {
  // Only anchor to an ancestor that wraps the *rendered* video. If the video
  // has no visible box — a torn-down/hidden watch player after navigating to
  // the home feed, a background tab, etc. — there's no good host; return null
  // so the caller hides the button instead of pinning it to a window-sized
  // fallback (the "button stuck in the masthead corner on the home page" bug).
  const vr = video.getBoundingClientRect();
  if (vr.width < 200 || vr.height < 100) return null;
  // Prefer the closest visible block ancestor; YouTube wraps its <video> in
  // .html5-video-container nested inside .html5-video-player.
  let node = video.parentElement;
  while (node && node !== document.body) {
    const rect = node.getBoundingClientRect();
    if (rect.width >= 240 && rect.height >= 135) return node;
    node = node.parentElement;
  }
  return null;
}

function scan(root) {
  const videos = root.querySelectorAll?.("video");
  if (!videos) return;
  videos.forEach(attachToVideo);
}

// Re-run discovery + re-anchoring: scan() attaches a button to any new (or
// reused-after-disconnect) video; reanchorButtons() repositions/shows/hides the
// live ones for the current layout.
function refresh() {
  scan(document);
  reanchorButtons();
}

// A route change or layout shift usually lands a beat before the new player has
// a size, so a single pass anchors nothing. Re-run discovery in a short burst
// to absorb that settle time. Overlapping bursts coalesce (timers are reset).
let refreshTimers = [];
function scheduleRefresh() {
  for (const t of refreshTimers) clearTimeout(t);
  refreshTimers = [0, 150, 400, 800, 1500, 2500].map((d) =>
    window.setTimeout(refresh, d),
  );
}

function init() {
  scan(document);
  const observer = new MutationObserver((mutations) => {
    for (const m of mutations) {
      for (const node of m.addedNodes) {
        if (node.nodeType === 1) {
          if (node.tagName === "VIDEO") attachToVideo(node);
          else scan(node);
        }
      }
    }
  });
  observer.observe(document.documentElement, {
    childList: true,
    subtree: true,
  });

  // Re-anchor on layout shifts that re-parent, resize, or remove the player:
  //  - fullscreen toggle: exiting it (then navigating without a reload) could
  //    leave the button pinned to the window corner instead of the new video.
  //  - yt-navigate-finish: YouTube's SPA route change (watch <-> home <-> next
  //    video) without a reload — re-anchors to the new player, or hides the
  //    pill on pages with no player (the home feed).
  //  - resize: a debounced generic safety net for other hosts.
  document.addEventListener("fullscreenchange", reanchorButtons, true);
  document.addEventListener("webkitfullscreenchange", reanchorButtons, true);
  // YouTube's SPA route change (watch <-> home <-> next video). It reuses one
  // persistent <video>, so the MutationObserver never re-fires on navigation —
  // re-run discovery with a settle burst so the button re-appears on the new
  // player without a manual reload.
  document.addEventListener("yt-navigate-finish", scheduleRefresh, true);
  // Generic SPA backstop: the History API emits no event on pushState, and not
  // every site dispatches a navigation event, so poll the URL and refresh on a
  // change. Cheap — it only does work when the route actually changed.
  let lastUrl = location.href;
  setInterval(() => {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      scheduleRefresh();
    }
  }, 400);
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(reanchorButtons, 250);
  });
}

// Load settings, then start discovery.
loadSettings().finally(init);
