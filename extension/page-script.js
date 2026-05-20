// Runs in the host page's main JavaScript context (manifest world: MAIN) at
// document_start. We patch HTMLMediaElement.prototype.volume so that, when an
// element is flagged with data-nomusic-vol-block="1", any volume write is
// intercepted: the requested value is dispatched as a CustomEvent so the
// content script can mirror it onto our processed-audio gain, and the
// underlying volume state is pinned to 0. This is the only way to fully
// eliminate the audible bleed when the host site drags its slider — by the
// time the volumechange event reaches the content script, the audio renderer
// (running on a separate thread) has already produced a few ms of output at
// the page's intended volume.
//
// The patch is global per page but inert on any element that hasn't been
// flagged, so it has no effect on ads, autoplay previews, or other unrelated
// <video>/<audio> elements.

(function () {
  if (window.__nomusicVolumePatched) return;
  window.__nomusicVolumePatched = true;

  const proto = HTMLMediaElement.prototype;
  const desc = Object.getOwnPropertyDescriptor(proto, "volume");
  if (!desc || typeof desc.set !== "function") return;
  const origSet = desc.set;
  const origGet = desc.get;

  Object.defineProperty(proto, "volume", {
    configurable: true,
    enumerable: desc.enumerable,
    get() {
      return origGet.call(this);
    },
    set(v) {
      // Fast path: most calls aren't on a tracked element.
      if (
        this.dataset &&
        this.dataset.nomusicVolBlock === "1" &&
        Number.isFinite(v)
      ) {
        try {
          this.dispatchEvent(
            new CustomEvent("nomusic:vol-intent", {
              detail: { volume: v, muted: !!this.muted },
            }),
          );
        } catch (_err) {
          /* swallow */
        }
        // Pin underlying volume to 0; the audio renderer never sees v.
        origSet.call(this, 0);
        return;
      }
      origSet.call(this, v);
    },
  });
})();
