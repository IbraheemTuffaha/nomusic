// Trusted settings popup. All backend requests go through the service worker;
// this page never persists or returns the operator key to a page context.

import {
  backendPermissionOrigin,
  normalizeBackendUrl,
  isOperatorKey,
} from "./config.js";

const $ = (id) => document.getElementById(id);
const ALL_STEMS = [
  { name: "vocals", desc: "speech & lead vocals" },
  { name: "drums", desc: "percussion (music)" },
  { name: "bass", desc: "bass (music)" },
  { name: "other", desc: "ambient + melodic instruments" },
];
const MODEL_HINTS = {
  htdemucs: "fast, balanced",
  htdemucs_ft: "slower, best",
};

let capsLoaded = false;
let saved = {};
let savedTimer = null;

function send(message) {
  return chrome.runtime.sendMessage(message);
}

function setStatus(kind, text) {
  $("status").classList.remove("ok", "bad");
  if (kind) $("status").classList.add(kind);
  $("statusText").textContent = text;
}

function showError(text) {
  $("err").textContent = text || "";
}

function flashSaved() {
  $("saved").textContent = "✓ saved";
  if (savedTimer) clearTimeout(savedTimer);
  savedTimer = setTimeout(() => ($("saved").textContent = ""), 1200);
}

function setControlsEnabled(enabled) {
  $("model").disabled = !enabled;
  for (const cb of $("stems").querySelectorAll('input[type="checkbox"]')) cb.disabled = !enabled;
}

function renderCapabilities(caps) {
  capsLoaded = true;
  setStatus("ok", "backend connected");
  $("device").textContent = caps.engine?.device || "";
  const select = $("model");
  select.innerHTML = "";
  const models = caps.engine?.supported_models || [];
  const defaultModel = caps.engine?.default_model;
  for (const model of models) {
    const option = document.createElement("option");
    option.value = model;
    let hint = MODEL_HINTS[model] || "";
    if (model === defaultModel) hint = hint ? `${hint} — default` : "default";
    option.textContent = hint ? `${model} (${hint})` : model;
    select.appendChild(option);
  }
  const model = models.includes(saved.model)
    ? saved.model : (defaultModel || models[0] || "");
  select.value = model;

  const defaultKeep = caps.defaults?.keep_stems || ["vocals"];
  const keep = Array.isArray(saved.keepStems) ? saved.keepStems : defaultKeep;
  const stems = $("stems");
  stems.innerHTML = "";
  for (const stem of ALL_STEMS) {
    const row = document.createElement("label");
    row.className = "stem-row";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = stem.name;
    checkbox.checked = keep.includes(stem.name);
    const text = document.createElement("span");
    text.innerHTML = `<span class="stem-name">${stem.name}</span> <span class="stem-desc">— ${stem.desc}</span>`;
    row.append(checkbox, text);
    stems.appendChild(row);
  }
  setControlsEnabled(true);
}

async function load() {
  capsLoaded = false;
  saved = await chrome.storage.sync.get(["backendUrl", "model", "keepStems"]);
  $("backend").value = saved.backendUrl || "http://127.0.0.1:8723";
  $("operatorKey").value = "";
  setControlsEnabled(false);
  showError("");

  let auth;
  try { auth = await send({ type: "get-auth-state" }); } catch { auth = null; }
  if (auth?.backendUrl) $("backend").value = auth.backendUrl;
  if (!auth?.ok || !auth.configured) {
    setStatus("bad", "operator key required");
    $("device").textContent = "";
    showError("Enter a key from `nomusic auth generate`, then Connect.");
    return;
  }

  let result;
  try { result = await send({ type: "backend-capabilities" }); } catch { result = null; }
  if (!result?.ok) {
    setStatus("bad", result?.code === "unauthorized" ? "key rejected" : "backend unavailable");
    showError(result?.message || "Start the backend and check the URL.");
    return;
  }
  renderCapabilities(result.capabilities);
}

async function saveAuth() {
  showError("");
  let backendUrl;
  try { backendUrl = normalizeBackendUrl($("backend").value); }
  catch (error) { showError(error.message); return; }
  const operatorKey = $("operatorKey").value.trim();
  if (!isOperatorKey(operatorKey)) {
    showError("The operator key must start with nm_ and contain 64 lowercase hex characters.");
    return;
  }
  const url = new URL(backendUrl);
  if (url.protocol === "https:" && chrome.permissions?.request) {
    let granted = false;
    try {
      granted = await chrome.permissions.request({ origins: [backendPermissionOrigin(backendUrl)] });
    } catch {
      granted = false;
    }
    if (!granted) {
      showError("Permission was denied; the previous backend settings were kept.");
      return;
    }
  }
  const keepStems = capsLoaded
    ? Array.from($("stems").querySelectorAll("input:checked"), (cb) => cb.value)
    : undefined;
  $("saveAuth").disabled = true;
  try {
    const result = await send({
      type: "configure-auth",
      backendUrl,
      operatorKey,
      model: capsLoaded ? ($("model").value || null) : undefined,
      keepStems: keepStems?.length ? keepStems : null,
    });
    if (!result?.ok) {
      showError(result?.message || "The backend rejected this configuration; previous settings were kept.");
      return;
    }
    $("operatorKey").value = "";
    flashSaved();
    await load();
  } catch {
    showError("Could not save the trusted backend settings; previous settings were kept.");
  } finally {
    $("saveAuth").disabled = false;
  }
}

async function savePreferences() {
  if (!capsLoaded) return;
  const keepStems = Array.from($("stems").querySelectorAll("input:checked"), (cb) => cb.value);
  const result = await send({
    type: "save-preferences",
    model: $("model").value || null,
    keepStems: keepStems.length ? keepStems : null,
  }).catch(() => null);
  if (result?.ok) flashSaved();
  else showError(result?.message || "Could not save preferences.");
}

async function clearAuth() {
  const result = await send({ type: "clear-auth" }).catch(() => null);
  if (!result?.ok) { showError(result?.message || "Could not clear the operator key."); return; }
  await load();
}

document.addEventListener("DOMContentLoaded", async () => {
  $("saveAuth").addEventListener("click", saveAuth);
  $("model").addEventListener("change", savePreferences);
  $("stems").addEventListener("change", savePreferences);
  $("backend").addEventListener("keydown", (event) => {
    if (event.key === "Enter") saveAuth();
  });
  $("clearAuth").addEventListener("click", clearAuth);
  await load();
});
