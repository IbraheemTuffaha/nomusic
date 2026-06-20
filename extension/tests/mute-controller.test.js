// Unit tests for mute-controller.js — the user-intent volume logic. The
// constructor binds to a live <video>/prototype, so we build a bare instance
// via Object.create and exercise the pure intent methods directly.
import { test } from "node:test";
import assert from "node:assert/strict";

import { MuteController } from "../mute-controller.js";

function bareController(fields) {
  return Object.assign(Object.create(MuteController.prototype), fields);
}

test("_applyEffectiveVolume pushes the user volume when not muted", () => {
  let pushed;
  const mc = bareController({
    _lastMuted: false,
    _userVolume: 0.7,
    _applyVolume: (level) => {
      pushed = level;
    },
  });
  mc._applyEffectiveVolume();
  assert.equal(pushed, 0.7);
});

test("_applyEffectiveVolume pushes 0 when muted", () => {
  let pushed;
  const mc = bareController({
    _lastMuted: true,
    _userVolume: 0.7,
    _applyVolume: (level) => {
      pushed = level;
    },
  });
  mc._applyEffectiveVolume();
  assert.equal(pushed, 0);
});

test("handleHostVolumeChange adopts a real volume read as user intent and re-silences the host", () => {
  let pushed;
  let reSilenced = false;
  const mc = bareController({
    disposed: false,
    video: { muted: false },
    _userVolume: 0.3,
    _lastMuted: false,
    _realGetVolume: () => 0.5,
    _realSetVolume: () => {
      reSilenced = true;
    },
    _applyVolume: (level) => {
      pushed = level;
    },
  });
  mc.handleHostVolumeChange();
  assert.equal(mc._userVolume, 0.5); // adopted the page's volume
  assert.equal(pushed, 0.5); // pushed to our output
  assert.equal(reSilenced, true); // host pinned back to 0
});

test("handleHostVolumeChange tracks a mute toggle without changing user volume", () => {
  let pushed;
  const mc = bareController({
    disposed: false,
    video: { muted: true },
    _userVolume: 0.4,
    _lastMuted: false,
    _realGetVolume: () => 0, // page reads 0 (our pin) -> no volume intent
    _realSetVolume: () => {},
    _applyVolume: (level) => {
      pushed = level;
    },
  });
  mc.handleHostVolumeChange();
  assert.equal(mc._lastMuted, true);
  assert.equal(mc._userVolume, 0.4); // unchanged
  assert.equal(pushed, 0); // muted -> 0
});
