// Small DOM stand-in for testing injected UI ownership and real event handlers.
// Keyboard activation/layout remain part of the actual browser verification.
export function domFixture(t) {
  class BrowserEventTarget extends EventTarget {
    removeEventListener(type, listener, options) {
      // Node's EventTarget does not match a capture listener when removal
      // uses the boolean shorthand accepted by browser EventTargets.
      super.removeEventListener(type, listener,
        typeof options === "boolean" ? { capture: options } : options);
    }
  }
  const document = new BrowserEventTarget();
  class Element extends BrowserEventTarget {
    constructor(tag) {
      super();
      this.tagName = tag.toUpperCase();
      this.children = [];
      this.parentElement = null;
      this.dataset = {};
      this.attributes = new Map();
      this.style = { setProperty(key, value) { this[key] = value; } };
      this.hidden = false;
      this.offsetWidth = 230;
      this.offsetHeight = 100;
    }
    get isConnected() {
      return this === document.body || !!this.parentElement?.isConnected;
    }
    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    getAttribute(name) { return this.attributes.get(name) ?? null; }
    appendChild(child) {
      child.remove();
      child.parentElement = this;
      this.children.push(child);
      return child;
    }
    append(...children) { for (const child of children) this.appendChild(child); }
    remove() {
      if (!this.parentElement) return;
      const siblings = this.parentElement.children;
      siblings.splice(siblings.indexOf(this), 1);
      this.parentElement = null;
    }
    contains(element) {
      return this === element || this.children.some((child) => child.contains(element));
    }
    focus() { document.activeElement = this; }
    click() {
      this.clickCount = (this.clickCount || 0) + 1;
      this.dispatchEvent(new Event("click", { bubbles: true, cancelable: true }));
    }
    getBoundingClientRect() { return { top: 10, right: 400, bottom: 40 }; }
  }
  const elements = [];
  document.createElement = (tag) => {
    const element = new Element(tag);
    elements.push(element);
    return element;
  };
  document.body = new Element("body");
  document.activeElement = document.body;
  const window = new BrowserEventTarget();
  window.innerHeight = 800;
  const timers = new Map();
  let nextTimer = 0;
  const globals = {
    document, window,
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
  return {
    document, window, timers, elements,
    tick() {
      const callbacks = [...timers.values()];
      timers.clear();
      for (const fn of callbacks) fn();
    },
  };
}
