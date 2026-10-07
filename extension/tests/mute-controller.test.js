// Unit tests for mute-controller.js — the user-intent volume logic. The
// constructor binds to a live <video>/prototype, so we build a bare instance
// via Object.create and exercise the pure intent methods directly.
import { test } from "node:test";
import assert from "node:assert/strict";

import { MuteController } from "../mute-controller.js";
import { mediaFixture, flushMediaEvents } from "./media-fixture.js";

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
    _nativeMuted: false,
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
    _nativeMuted: false,
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

test("genuine initial zero stays silent through refresh and disposal", async (t) => {
  const { MediaElement } = mediaFixture(t, { bridge: true });
  const video = new MediaElement({ volume: 0 });
  const levels = [];
  const mc = new MuteController(video, (level) => levels.push(level));
  mc.mute();
  mc.refresh();
  await flushMediaEvents();
  assert.deepEqual(levels, [0, 0]);
  mc.dispose();
  assert.equal(video.volume, 0);
});

test("page zero volume differs from the extension's native suppression write", async (t) => {
  const { MediaElement } = mediaFixture(t, { bridge: true });
  const video = new MediaElement({ volume: 0.6 });
  const levels = [];
  const mc = new MuteController(video, (level) => levels.push(level));
  video.addEventListener("volumechange", () => mc.handleHostVolumeChange());
  mc.mute();
  await flushMediaEvents();
  assert.equal(levels.at(-1), 0.6);
  video.volume = 0;
  assert.equal(levels.at(-1), 0);
  video.muted = true;
  await flushMediaEvents();
  video.muted = false;
  await flushMediaEvents();
  assert.equal(levels.at(-1), 0);
  mc.dispose();
  assert.equal(video.volume, 0);
  assert.equal(video.muted, false);
});

test("return restores the latest slider and mute intent, once", async (t) => {
  const { MediaElement } = mediaFixture(t, { bridge: true });
  const video = new MediaElement({ volume: 0.4 });
  const mc = new MuteController(video, () => {});
  video.addEventListener("volumechange", () => mc.handleHostVolumeChange());
  mc.mute();
  video.volume = 0.7;
  video.muted = true;
  await flushMediaEvents();
  mc.dispose();
  assert.equal(video.volume, 0.7);
  assert.equal(video.muted, true);
  video.volume = 0.2;
  video.muted = false;
  mc.dispose();
  assert.equal(video.volume, 0.2);
  assert.equal(video.muted, false);
});

test("YouTube storage starts as a baseline and later zero/mute changes apply", (t) => {
  const fixture = mediaFixture(t, { youtube: true });
  fixture.setStoredVolume({ volume: 100, muted: false });
  const video = new fixture.MediaElement({ volume: 0 });
  const levels = [];
  const mc = new MuteController(video, (level) => levels.push(level));
  mc.mute();
  fixture.tick();
  assert.equal(levels.at(-1), 0);
  fixture.setStoredVolume({ volume: 35, muted: false });
  fixture.tick();
  assert.equal(levels.at(-1), 0.35);
  fixture.setStoredVolume({ volume: 0, muted: true });
  fixture.tick();
  assert.equal(levels.at(-1), 0);
  mc.dispose();
  assert.equal(video.volume, 0);
  assert.equal(video.muted, true);
  assert.equal(fixture.timers.size, 0);
});

test("invalid explicit volume values cannot replace the last valid level", (t) => {
  const { MediaElement } = mediaFixture(t);
  const video = new MediaElement({ volume: 0.3 });
  const levels = [];
  const mc = new MuteController(video, (level) => levels.push(level));
  mc.mute();
  for (const volume of [NaN, Infinity, -0.1, 1.1, "0.9"]) {
    video.dispatchEvent(new CustomEvent("nomusic:vol-intent", { detail: { volume } }));
    assert.equal(levels.at(-1), 0.3);
  }
  mc.dispose();
});

test("MAIN-world volume coercion stays suppressed and invalid assignments still fail", (t) => {
  const { MediaElement } = mediaFixture(t, { bridge: true });
  const video = new MediaElement();
  const levels = [];
  const mc = new MuteController(video, (level) => levels.push(level));
  mc.mute();
  video.volume = "0.5";
  assert.equal(video.volume, 0);
  assert.equal(levels.at(-1), 0.5);
  for (const volume of [NaN, Infinity, -0.1, 1.1]) {
    assert.throws(() => { video.volume = volume; }, RangeError);
    assert.equal(levels.at(-1), 0.5);
    assert.equal(video.volume, 0);
  }
  mc.dispose();
});

for (const initialMuted of [false, true]) {
  test(`immediate disposal preserves a native mute change from ${initialMuted}`, async (t) => {
    const { MediaElement } = mediaFixture(t, { bridge: true });
    const video = new MediaElement({ volume: 0.4, muted: initialMuted });
    const mc = new MuteController(video, () => {});
    video.addEventListener("volumechange", () => mc.handleHostVolumeChange());
    mc.mute();
    video.muted = !initialMuted;
    // MutationObserver source cleanup can precede native media-event tasks.
    mc.dispose();
    assert.equal(video.muted, !initialMuted);
    assert.equal(video.volume, 0.4);
    await flushMediaEvents();
    assert.equal(video.muted, !initialMuted);
  });

  test(`volume-only events preserve polled mute intent over native ${initialMuted}`, async (t) => {
    const fixture = mediaFixture(t, { bridge: true, youtube: true });
    fixture.setStoredVolume({ volume: 40, muted: initialMuted });
    const video = new fixture.MediaElement({ volume: 0.4, muted: initialMuted });
    const levels = [];
    const mc = new MuteController(video, (level) => levels.push(level));
    video.addEventListener("volumechange", () => mc.handleHostVolumeChange());
    mc.mute();
    fixture.setStoredVolume({ volume: 40, muted: !initialMuted });
    fixture.tick();
    // The native event from our initial volume pin has not fired yet.
    await flushMediaEvents();
    assert.equal(levels.at(-1), initialMuted ? 0.4 : 0);
    video.volume = 0.6; // MAIN-world volume intent is not a mute toggle either.
    assert.equal(levels.at(-1), initialMuted ? 0.6 : 0);
    mc.refresh();
    assert.equal(levels.at(-1), initialMuted ? 0.6 : 0);
    mc.dispose();
    assert.equal(video.muted, !initialMuted);
    assert.equal(video.volume, 0.6);
  });
}
