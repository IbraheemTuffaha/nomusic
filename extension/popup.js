// Popup logic: surfaces backend health and lets the user tweak settings.

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
  $("backend").value = stored.backendUrl || "http://127.0.0.1:8723";

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
      "Start the backend: backend/.venv/bin/python backend/server.py";
  }
}

function fmtBytes(n) {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v >= 100 ? 0 : v >= 10 ? 1 : 2)} ${units[i]}`;
}

async function refreshCacheSize() {
  try {
    const resp = await fetch(`${$("backend").value}/cache`, {
      cache: "no-store",
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    const total = data.total_bytes || 0;
    const jobs = data.job_count || 0;
    const sources = data.source_count || 0;
    const videos = data.video_count || 0;
    // videos/ can dwarf the rest (cached multi-GB exports), so surface it in
    // the breakdown rather than letting the total jump unexplained. Omit the
    // segment when there are none, to keep the common case uncluttered.
    const videoSeg = videos ? `, ${videos} video${videos === 1 ? "" : "s"}` : "";
    $("cacheSize").textContent =
      total === 0
        ? "empty"
        : `${fmtBytes(total)} (${jobs} job${jobs === 1 ? "" : "s"}, ` +
          `${sources} source${sources === 1 ? "" : "s"}${videoSeg})`;
    // Keep the button enabled even when empty so a stale "empty" reading
    // (e.g. backend just restarted) isn't a dead-end. The server treats
    // clearing an empty cache as a no-op.
    $("clearCache").disabled = false;
  } catch (_err) {
    $("cacheSize").textContent = "unknown";
    $("clearCache").disabled = false;
  }
}

async function refreshCacheTtl() {
  const el = $("cacheTtl");
  try {
    const resp = await fetch(`${$("backend").value}/capabilities`, {
      cache: "no-store",
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    const ttl = data?.cache?.ttl_days;
    if (typeof ttl === "number" && ttl > 0) {
      el.textContent = `auto-deletes after ${ttl} day${ttl === 1 ? "" : "s"}`;
    } else {
      el.textContent = "no auto-delete";
    }
  } catch (_err) {
    el.textContent = "";
  }
}

// Two-click confirmation. The first click arms the button (red, label
// changes); the second click within ARM_WINDOW_MS actually clears. This
// avoids relying on window.confirm(), which Chrome silently suppresses in
// some extension popup contexts.
const ARM_WINDOW_MS = 4000;
let armTimer = null;

function disarm(btn) {
  btn.classList.remove("armed");
  btn.textContent = "Clear";
  if (armTimer) {
    clearTimeout(armTimer);
    armTimer = null;
  }
}

async function clearCache() {
  const btn = $("clearCache");
  if (!btn.classList.contains("armed")) {
    btn.classList.add("armed");
    btn.textContent = "Confirm";
    armTimer = setTimeout(() => disarm(btn), ARM_WINDOW_MS);
    return;
  }

  disarm(btn);
  const sizeEl = $("cacheSize");
  const prev = sizeEl.textContent;
  btn.disabled = true;
  sizeEl.textContent = "clearing…";
  try {
    const resp = await fetch(`${$("backend").value}/cache/clear`, {
      method: "POST",
    });
    if (!resp.ok) {
      const detail = await resp.text().catch(() => "");
      throw new Error(`HTTP ${resp.status}: ${detail || resp.statusText}`);
    }
    const data = await resp.json();
    $("err").textContent = `freed ${fmtBytes(data.deleted_bytes || 0)}`;
    setTimeout(() => ($("err").textContent = ""), 2500);
  } catch (err) {
    console.error("[nomusic] clear failed", err);
    $("err").textContent = `clear failed: ${err.message}`;
    sizeEl.textContent = prev;
  } finally {
    btn.disabled = false;
  }
  await refreshCacheSize();
}

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
  const backendUrl = $("backend").value.trim() || "http://127.0.0.1:8723";
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
  // Backend URL commits on blur/Enter (not per keystroke), then we re-check
  // health and reload the model list for the new backend.
  $("backend").addEventListener("change", async () => {
    await persist();
    await load();
  });
  $("clearCache").addEventListener("click", clearCache);
  refreshCacheSize();
  refreshCacheTtl();
});
