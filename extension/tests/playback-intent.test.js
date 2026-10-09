import { test } from "node:test";
import assert from "node:assert/strict";
import { PlaybackIntent } from "../playback-intent.js";
import { mediaFixture, flushMediaEvents } from "./media-fixture.js";

test("holding an initially paused video never claims a wish to play", async (t) => {
  const { MediaElement } = mediaFixture(t, { bridge: true });
  const video = new MediaElement({ paused: true });
  const changes = [];
  const intent = new PlaybackIntent(video, (playing) => changes.push(playing));
  intent.hold();
  intent.release();
  intent.dispose({ restore: true });
  await flushMediaEvents();
  assert.equal(video.paused, true);
  assert.equal(video.playCalls, 0);
  assert.deepEqual(changes, []);
});

test("a no-op page pause during buffering cancels automatic resume", async (t) => {
  const { MediaElement } = mediaFixture(t, { bridge: true });
  const video = new MediaElement();
  const changes = [];
  const intent = new PlaybackIntent(video, (playing) => changes.push(playing));
  intent.hold();
  video.pause(); // Already paused: only the MAIN-world bridge observes this.
  intent.release();
  await flushMediaEvents();
  assert.equal(intent.wantsPlay, false);
  assert.equal(video.paused, true);
  assert.equal(video.playCalls, 0);
  assert.deepEqual(changes, [false]);
  intent.dispose();
});

test("play during a startup hold records intent but waits for release", async (t) => {
  const { MediaElement } = mediaFixture(t, { bridge: true });
  const video = new MediaElement({ paused: true });
  const changes = [];
  const intent = new PlaybackIntent(video, (playing) => changes.push(playing));
  intent.hold();
  await video.play();
  await flushMediaEvents();
  assert.equal(intent.wantsPlay, true);
  assert.equal(video.paused, true);
  assert.deepEqual(changes, [true]);
  intent.release();
  await flushMediaEvents();
  assert.equal(video.paused, false);
  assert.deepEqual(changes, [true]);
  intent.dispose();
});

test("queued internal pause events do not replace intent after release", async (t) => {
  const { MediaElement } = mediaFixture(t, { bridge: true });
  const video = new MediaElement();
  const changes = [];
  const intent = new PlaybackIntent(video, (playing) => changes.push(playing));
  intent.hold();
  intent.release(); // Both native events are still queued.
  await flushMediaEvents();
  assert.equal(video.paused, false);
  assert.equal(intent.wantsPlay, true);
  assert.deepEqual(changes, []);
  intent.dispose();
});

test("native controls preserve pause and play intent without the bridge", async (t) => {
  const { MediaElement } = mediaFixture(t);
  const video = new MediaElement();
  const changes = [];
  const intent = new PlaybackIntent(video, (playing) => changes.push(playing));
  video.pause();
  await flushMediaEvents();
  assert.equal(intent.wantsPlay, false);
  intent.hold();
  await video.play();
  await flushMediaEvents();
  assert.equal(intent.wantsPlay, true);
  assert.equal(video.paused, true);
  assert.deepEqual(changes, [false, true]);
  intent.dispose();
});

test("return to original restores wanted playback once, removal never resumes", async (t) => {
  const { MediaElement } = mediaFixture(t, { bridge: true });
  for (const restore of [false, true]) {
    const video = new MediaElement();
    const intent = new PlaybackIntent(video, () => {});
    intent.hold();
    intent.dispose({ restore });
    intent.dispose({ restore });
    await flushMediaEvents();
    assert.equal(video.playCalls, restore ? 1 : 0);
    assert.equal(video.paused, !restore);
    assert.equal(video.dataset.nomusicPlaybackTrack, undefined);
  }
});

test("bridge leaves unrelated videos unchanged and is installed only once", async (t) => {
  const { MediaElement, installBridge } = mediaFixture(t, { bridge: true });
  installBridge();
  const video = new MediaElement({ paused: true });
  const requests = [];
  video.addEventListener("nomusic:playback-intent", (event) => requests.push(event.detail.playing));
  await video.play();
  video.pause();
  assert.deepEqual(requests, []);
  video.dataset.nomusicPlaybackTrack = "1";
  video.pause();
  assert.deepEqual(requests, [false]);
});
