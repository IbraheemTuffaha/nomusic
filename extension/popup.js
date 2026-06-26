// Popup logic: surfaces backend health and lets the user tweak settings.

import { DEFAULT_BACKEND } from "./config.js";

const $ = (id) => document.getElementById(id);

const ALL_STEMS = [
  { name: "vocals", desc: "speech & lead vocals" },
  { name: "drums", desc: "percussion (music)" },
  { name: "bass", desc: "bass (music)" },
  { name: "other", desc: "ambient + melodic instruments" },
];

// Plain-language hints for the model dropdown, keyed by the backend model
// name. Non-experts shouldn't need to know what "htdemucs_ft" means; the
// bracket explains the speed/quality tradeoff. Unknown models fall back to
// the bare name.
const MODEL_HINTS = {
  htdemucs: "fast, balanced",
  htdemucs_ft: "slower, best",
};

// True once a reachable backend's /capabilities has populated the model + stem
// controls. persist() reads those controls back, so until they're populated
// (backend down) writing them would clobber the user's saved selections.
let capsLoaded = false;

async function load() {
  // Reset until this load proves the backend is reachable; guards persist()
  // against writing the empty form during an offline reload.
  capsLoaded = false;
  const stored = await chrome.storage.sync.get([
    "backendUrl",
    "model",
    "keepStems",
  ]);
  $("backend").value = stored.backendUrl || DEFAULT_BACKEND;

  let caps = null;
  try {
    const resp = await fetch(`${$("backend").value}/capabilities`, {
      cache: "no-store",
    });
    if (resp.ok) caps = await resp.json();
  } catch (err) {
    // Backend down / wrong URL — fall through and render the offline state.
    console.debug("[nomusic] capabilities fetch failed", err);
  }

  if (caps) {
    capsLoaded = true;
    $("status").classList.add("ok");
    $("statusText").textContent = "backend up";
    $("device").textContent = caps.engine?.device || "";

    const select = $("model");
    select.innerHTML = "";
    select.disabled = false; // re-enable after any prior offline render
    const models = caps.engine?.supported_models || [];
    const defaultModel = caps.engine?.default_model;
    for (const m of models) {
      const opt = document.createElement("option");
      opt.value = m;
      // "name (hint — default)": keep the technical name for transparency,
      // add a plain-language hint, fold the default marker into the bracket.
      let hint = MODEL_HINTS[m] || "";
      if (m === defaultModel) hint = hint ? `${hint} — default` : "default";
      opt.textContent = hint ? `${m} (${hint})` : m;
      select.appendChild(opt);
    }
    // Heal a stale saved model (e.g. one we've since removed): fall back to the
    // default and rewrite storage so content.js stops sending the dead value.
    const valid =
      models.includes(stored.model) ? stored.model
        : (defaultModel || models[0] || "");
    select.value = valid;

    const defaultKeep = caps.defaults?.keep_stems || ["vocals"];
    const keep = stored.keepStems || defaultKeep;
    const stemsEl = $("stems");
    stemsEl.innerHTML = "";
    for (const s of ALL_STEMS) {
      const row = document.createElement("label");
      row.className = "stem-row";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = s.name;
      cb.checked = keep.includes(s.name);
      const text = document.createElement("span");
      text.innerHTML =
        `<span class="stem-name">${s.name}</span> ` +
        `<span class="stem-desc">— ${s.desc}</span>`;
      row.append(cb, text);
      stemsEl.appendChild(row);
    }

    // Persist the healed model AFTER the stem checkboxes are rebuilt, so
    // persist() reads the real keep-stems instead of wiping them from a
    // still-empty #stems; awaited so a storage rejection surfaces instead of
    // being silently dropped.
    if (stored.model && stored.model !== valid) await persist();
  } else {
    $("status").classList.add("bad");
    $("statusText").textContent = "backend not reachable";
    $("err").textContent =
      "Backend unreachable — check your connection, or point Backend URL at your own server.";
    // Disable the model/stem controls while offline. persist() already skips
    // writing them when capsLoaded is false (so saved selections survive), but
    // disabling stops the user making an edit here that would be silently
    // dropped — and flashSaved() then only ever fires for a real write.
    $("model").disabled = true;
    for (const cb of $("stems").querySelectorAll('input[type="checkbox"]')) {
      cb.disabled = true;
    }
  }
}

// The cache panel and its "Clear" control were removed for the public build:
// on a shared hosted backend /cache exposes global usage to every user and
// /cache/clear is admin-only server-side (it requires NOMUSIC_ADMIN_TOKEN, which
// the owner holds and the extension never ships). The popup now only reads the
// backend's /capabilities.

let savedTimer = null;
function flashSaved() {
  const el = $("saved");
  el.textContent = "✓ saved";
  if (savedTimer) clearTimeout(savedTimer);
  savedTimer = setTimeout(() => (el.textContent = ""), 1200);
}

// Persist the current form state. Called on every change — there's no Save
// button; the popup auto-saves.
async function persist() {
  const backendUrl = $("backend").value.trim() || DEFAULT_BACKEND;
  const update = { backendUrl };
  // Only persist model/stems once the backend's capabilities have populated the
  // form. When the backend is unreachable those controls are empty, and writing
  // them would wipe the user's saved model + keep-stems — exactly during the
  // recovery flow (fix the backend URL) that this very field exists for.
  if (capsLoaded) {
    update.model = $("model").value || null;
    const checkboxes = $("stems").querySelectorAll('input[type="checkbox"]');
    const keepStems = Array.from(checkboxes)
      .filter((cb) => cb.checked)
      .map((cb) => cb.value);
    update.keepStems = keepStems.length ? keepStems : null;
  }
  await chrome.storage.sync.set(update);
  flashSaved();
}

document.addEventListener("DOMContentLoaded", async () => {
  await load();
  // Auto-save on any change.
  $("model").addEventListener("change", persist);
  // Stems are recreated by load(); a delegated listener on the container
  // survives those rebuilds, so we bind it once here.
  $("stems").addEventListener("change", persist);
  // Backend URL commits on blur/Enter (not per keystroke). The shipped default
  // host is already in host_permissions; for a self-hosted URL we request the
  // matching optional host permission (no-ops / returns true if already granted)
  // BEFORE any other await so the user gesture is still active. Then re-check
  // health and reload the model list for the new backend.
  $("backend").addEventListener("change", async () => {
    const value = $("backend").value.trim();
    if (value) {
      try {
        const origin = new URL(value).origin + "/*";
        await chrome.permissions.request({ origins: [origin] });
      } catch (err) {
        console.debug("[nomusic] backend permission request failed", err);
      }
    }
    await persist();
    await load();
  });
});
