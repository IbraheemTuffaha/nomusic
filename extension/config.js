// config.js — single source of truth for the shipped default backend URL.
//
// No side effects and no chrome.* access, so it is safe to import from the
// popup, the service worker, AND the content-script module graph (via
// settings.js). Because it's reached through content.js's dynamic import of
// main.js, it is also listed in the manifest's web_accessible_resources.
//
// TODO(owner): before packaging for the Chrome Web Store, replace
// "nomusic.example.com" with your real Cloudflare-tunnel hostname. This is the
// single source of truth for RUNTIME code (popup, service worker, content
// scripts all import it), but the host also appears in manifest.json
// "host_permissions", popup.html's input placeholder, and the test fixtures --
// keep those in sync (they can't import this module).
export const DEFAULT_BACKEND = "https://nomusic.example.com";
