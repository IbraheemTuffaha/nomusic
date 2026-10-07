import { readFileSync } from "node:fs";
import vm from "node:vm";

const bridgeSource = readFileSync(new URL("../page-script.js", import.meta.url), "utf8");

// Queue media events like a browser, including the absence of a pause event
// when pause() is called on an already paused element.
export function mediaFixture(t, { bridge = false, youtube = false } = {}) {
  class MediaElement extends EventTarget {
    constructor({ paused = false, volume = 0.4, muted = false } = {}) {
      super();
      this.paused = paused;
      this.ended = false;
      this.dataset = {};
      this._volume = volume;
      this._muted = muted;
      this.playCalls = 0;
      this.pauseCalls = 0;
    }
    play() {
      this.playCalls++;
      if (this.paused) {
        this.paused = false;
        queueMicrotask(() => this.dispatchEvent(new Event("play")));
      }
      return Promise.resolve();
    }
    pause() {
      this.pauseCalls++;
      if (!this.paused) {
        this.paused = true;
        queueMicrotask(() => this.dispatchEvent(new Event("pause")));
      }
    }
    get volume() { return this._volume; }
    set volume(value) {
      value = +value;
      if (!Number.isFinite(value) || value < 0 || value > 1) throw new RangeError("volume");
      if (value !== this._volume) {
        this._volume = value;
        queueMicrotask(() => this.dispatchEvent(new Event("volumechange")));
      }
    }
    get muted() { return this._muted; }
    set muted(value) {
      value = !!value;
      if (value !== this._muted) {
        this._muted = value;
        queueMicrotask(() => this.dispatchEvent(new Event("volumechange")));
      }
    }
  }
  const volume = Object.getOwnPropertyDescriptor(MediaElement.prototype, "volume");
  // Isolated content-script access uses the native descriptor, even when the
  // MAIN world has installed a different setter on its own wrapper prototype.
  class IsolatedMediaElement {}
  Object.defineProperty(IsolatedMediaElement.prototype, "volume", volume);
  const timers = new Map();
  let nextTimer = 0;
  let storedVolume = null;
  const globals = {
    HTMLMediaElement: IsolatedMediaElement,
    location: { hostname: youtube ? "www.youtube.com" : "test.local" },
    localStorage: { getItem: () => storedVolume },
    requestAnimationFrame: () => ++nextTimer,
    cancelAnimationFrame: () => {},
    setTimeout: (fn) => { const id = ++nextTimer; timers.set(id, fn); return id; },
    clearTimeout: (id) => timers.delete(id),
  };
  for (const [key, value] of Object.entries(globals)) {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, key);
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
    t.after(() => {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    });
  }
  const context = vm.createContext({
    window: {}, HTMLMediaElement: MediaElement, CustomEvent,
    document: { addEventListener() {} }, console,
  });
  const installBridge = () => vm.runInContext(bridgeSource, context);
  if (bridge) installBridge();
  return {
    MediaElement, timers, installBridge,
    setStoredVolume: (value) => { storedVolume = JSON.stringify({ data: JSON.stringify(value) }); },
    tick() {
      const callbacks = [...timers.values()];
      timers.clear();
      for (const fn of callbacks) fn();
    },
  };
}

export async function flushMediaEvents() {
  await Promise.resolve();
  await Promise.resolve();
}
