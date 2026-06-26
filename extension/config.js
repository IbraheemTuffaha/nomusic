// config.js — single source of truth for the shipped default backend URL.
//
// No side effects and no chrome.* access, so it is safe to import from the
// popup, the service worker, AND the content-script module graph (via
// settings.js). Because it's reached through content.js's dynamic import of
// main.js, it is also listed in the manifest's web_accessible_resources.
//
// TODO(owner): before packaging for the Chrome Web Store, replace
// "nomusic.example.com" with your real Cloudflare-tunnel hostname. It MUST match
// the manifest's host_permissions entry (the extension can only reach hosts it
// declares). This is the ONLY place the default backend host is written.
export const DEFAULT_BACKEND = "https://nomusic.example.com";
