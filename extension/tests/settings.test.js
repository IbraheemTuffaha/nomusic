// Unit tests for settings.js — the shared config + chrome.storage cache.
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_BACKEND,
  SYNC_TOLERANCE_S,
  SYNC_CHECK_MS,
  settings,
  loadSettings,
} from "../settings.js";

test("default constants are sane", () => {
  // Shipped default is the public HTTPS backend (placeholder host swapped at
  // package time). Re-exported from config.js so the popup/SW/content scripts
  // can't drift.
  assert.equal(DEFAULT_BACKEND, "https://nomusic.example.com");
  assert.ok(DEFAULT_BACKEND.startsWith("https://"));
  assert.ok(SYNC_TOLERANCE_S > 0 && SYNC_TOLERANCE_S < 1);
  assert.ok(SYNC_CHECK_MS >= 50);
  assert.equal(settings.backendUrl, DEFAULT_BACKEND);
});

test("loadSettings overlays stored values onto the defaults", async () => {
  chrome.storage.sync.get = async () => ({
    backendUrl: "http://localhost:9999",
    model: "htdemucs_ft",
    keepStems: ["vocals", "other"],
  });
  await loadSettings();
  assert.equal(settings.backendUrl, "http://localhost:9999");
  assert.equal(settings.model, "htdemucs_ft");
  assert.deepEqual(settings.keepStems, ["vocals", "other"]);
});

test("loadSettings falls back to defaults when storage throws", async () => {
  settings.backendUrl = DEFAULT_BACKEND; // reset from the prior test
  chrome.storage.sync.get = async () => {
    throw new Error("permission denied");
  };
  // Must not reject — a storage failure leaves the in-memory defaults intact.
  await loadSettings();
  assert.equal(settings.backendUrl, DEFAULT_BACKEND);
});
