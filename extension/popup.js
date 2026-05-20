// Popup logic: surfaces backend health and lets the user tweak settings.

const $ = (id) => document.getElementById(id);

const ALL_STEMS = [
  { name: "vocals", desc: "speech & lead vocals" },
  { name: "drums", desc: "percussion (music)" },
  { name: "bass", desc: "bass (music)" },
  { name: "other", desc: "ambient + melodic instruments" },
];

async function load() {
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
  } catch (_err) {
    /* fall through */
  }

  if (caps) {
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
      opt.textContent = m + (m === defaultModel ? " (default)" : "");
      select.appendChild(opt);
    }
    select.value = stored.model || defaultModel || models[0] || "";

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
    $("cacheSize").textContent =
      total === 0
        ? "empty"
        : `${fmtBytes(total)} (${jobs} job${jobs === 1 ? "" : "s"}, ` +
          `${sources} source${sources === 1 ? "" : "s"})`;
    $("clearCache").disabled = total === 0;
  } catch (_err) {
    $("cacheSize").textContent = "unknown";
  }
}

async function clearCache() {
  const btn = $("clearCache");
  const sizeEl = $("cacheSize");
  const prev = sizeEl.textContent;
  if (!confirm("Delete all cached source audio and processed chunks?")) return;
  btn.disabled = true;
  sizeEl.textContent = "clearing…";
  try {
    const resp = await fetch(`${$("backend").value}/cache/clear`, {
      method: "POST",
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    $("err").textContent = `freed ${fmtBytes(data.deleted_bytes)}`;
    setTimeout(() => ($("err").textContent = ""), 2000);
  } catch (err) {
    $("err").textContent = `clear failed: ${err.message}`;
    sizeEl.textContent = prev;
    btn.disabled = false;
    return;
  }
  await refreshCacheSize();
}

async function save() {
  const backendUrl = $("backend").value.trim() || "http://127.0.0.1:8723";
  const model = $("model").value || null;
  const checkboxes = $("stems").querySelectorAll('input[type="checkbox"]');
  const keepStems = Array.from(checkboxes)
    .filter((cb) => cb.checked)
    .map((cb) => cb.value);
  await chrome.storage.sync.set({
    backendUrl,
    model,
    keepStems: keepStems.length ? keepStems : null,
  });
  $("err").textContent = "saved";
  setTimeout(() => ($("err").textContent = ""), 1200);
}

document.addEventListener("DOMContentLoaded", async () => {
  await load();
  $("save").addEventListener("click", save);
  $("clearCache").addEventListener("click", clearCache);
  refreshCacheSize();
});
