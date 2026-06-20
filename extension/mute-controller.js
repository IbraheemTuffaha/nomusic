// MuteController: keeps the host <video> silent while our processed audio plays,
// and mirrors the user's volume/mute intent onto our audio output.
//
// It pins the element's underlying volume to 0 (via the prototype setter +
// page-script.js's main-world patch + an rAF fallback), tracks the user's real
// intent from volume/mute events (and YouTube's localStorage), and pushes the
// effective level back to the audio output through the ``applyVolume`` callback
// the owner provides — so this stays decoupled from the Web Audio graph.
//
// Extracted from Session (former content.js god-object).
import { dlog } from "./settings.js";

export class MuteController {
  /** @param video  the host media element to silence.
   *  @param applyVolume  (level: 0..1, immediate?: boolean) => void — push the
   *         effective volume to the owner's audio output (immediate = no ramp). */
  constructor(video, applyVolume) {
    this.video = video;
    this._applyVolume = applyVolume;
    this.disposed = false;
    this.muteAsserter = null;
    this._ytVolTimer = null;
    this._volIntentHandler = null;
    this._realSetVolume = null;
    this._realGetVolume = null;
  }

  mute() {
    this.originalMuted = this.video.muted;
    this.originalVolume = this.video.volume;

    // What we track across host events:
    //   _userVolume — last slider position the user set (0..1). Survives
    //                 mute toggles so unmuting restores the right level.
    //   _lastMuted  — last muted state we observed. Lets the volumechange
    //                 handler detect mute/unmute clicks (which can fire
    //                 without a volume change).
    this._userVolume = this.originalVolume > 0 ? this.originalVolume : 1.0;
    this._lastMuted = this.originalMuted;

    // We override only ``volume`` and pin it to 0 via the prototype
    // setter + rAF re-assertion. ``muted`` is left alone: audio is
    // already silent because volume is 0, and reading video.muted gives
    // us the user's real mute intent (needed by the volumechange path).
    const proto = HTMLMediaElement.prototype;
    const volumeDesc = Object.getOwnPropertyDescriptor(proto, "volume");
    this._realSetVolume = (v) => volumeDesc.set.call(this.video, v);
    this._realGetVolume = () => volumeDesc.get.call(this.video);

    // Seed our processed-audio gain to whatever the user was listening at, so
    // clicking the button doesn't jump the loudness. Immediate (no ramp).
    this._applyEffectiveVolume(true);

    // Hook the main-world page-script setter patch. Each volume write
    // the host page performs lands here as a CustomEvent before the
    // audio renderer can pick up a non-zero value, so there is no
    // bleed window. The patched setter pins the underlying volume to 0
    // for us.
    this._volIntentHandler = (e) => {
      if (!e || !e.detail) return;
      const { volume, muted } = e.detail;
      if (typeof volume === "number" && volume > 0) {
        this._userVolume = volume;
      }
      if (typeof muted === "boolean") this._lastMuted = muted;
      this._applyEffectiveVolume();
    };
    this.video.addEventListener("nomusic:vol-intent", this._volIntentHandler);

    // Flag this element so page-script.js knows to intercept its
    // volume writes. dataset writes propagate to the main world via
    // the shared DOM.
    this.video.dataset.nomusicVolBlock = "1";

    // Initial pin (also exercises the page-script path).
    this._realSetVolume(0);

    // Fallback rAF re-assertion. If page-script.js failed to load
    // (e.g. a browser that disables MAIN-world content scripts), we
    // still keep underlying volume pinned. Cheap: one comparison +
    // at most one setter call per frame.
    const tick = () => {
      if (this.disposed) return;
      try {
        if (this._realGetVolume() !== 0) this._realSetVolume(0);
      } catch (err) {
        // Element detached from the DOM — there's nothing left to pin, so stop
        // the per-frame loop instead of throwing on every future frame.
        dlog("mute assert: element detached; stopping asserter", err?.name || err);
        this.muteAsserter = null;
        return;
      }
      this.muteAsserter = requestAnimationFrame(tick);
    };
    this.muteAsserter = requestAnimationFrame(tick);

    // YouTube's player updates volume via its own state machine and
    // sometimes doesn't round-trip through video.volume at all, so the
    // volumechange listener never sees the change. They do persist the
    // setting to localStorage on every interaction; we poll that as a
    // redundant signal.
    this._startYouTubeVolumePoll();
  }

  _startYouTubeVolumePoll() {
    if (!/(?:^|\.)youtube\.com$/.test(location.hostname)) return;

    const readYT = () => {
      try {
        const raw = localStorage.getItem("yt-player-volume");
        if (!raw) return null;
        const wrapper = JSON.parse(raw);
        const data =
          typeof wrapper.data === "string"
            ? JSON.parse(wrapper.data)
            : wrapper.data;
        return {
          volume: Math.max(0, Math.min(1, (data.volume ?? 100) / 100)),
          muted: !!data.muted,
        };
      } catch (_err) {
        return null;
      }
    };

    let last = null;
    const tick = () => {
      if (this.disposed) return;
      this._ytVolTimer = setTimeout(tick, 200);
      const yt = readYT();
      if (!yt) return;
      if (last && last.volume === yt.volume && last.muted === yt.muted) {
        return;
      }
      last = yt;
      if (yt.volume > 0) this._userVolume = yt.volume;
      this._lastMuted = yt.muted;
      this._applyEffectiveVolume();
    };
    tick();
  }

  /** Push the current user intent (volume + mute) to the audio output via the
   *  owner's callback. ``immediate`` skips the ramp (used for the initial seed). */
  _applyEffectiveVolume(immediate = false) {
    this._applyVolume(this._lastMuted ? 0 : this._userVolume, immediate);
  }

  /**
   * Fires whenever the host page changes volume or muted on the <video>.
   * We update our intent state (_userVolume from non-zero volume reads,
   * _lastMuted from any mute change) and push the effective volume to
   * our output. Then we re-silence the host's volume so the original audio
   * stays off. The 'muted' attribute is left alone — the page can flip
   * it freely; volume=0 is what actually keeps the host silent.
   */
  handleHostVolumeChange() {
    if (this.disposed || !this._realGetVolume) return;
    const realVol = this._realGetVolume();
    const muted = this.video.muted;

    let changed = false;
    if (realVol > 0) {
      // Page set a real volume — that's the user's intent. (When the
      // page reads back 0 it's our pin, so it doesn't tell us anything.)
      this._userVolume = realVol;
      changed = true;
    }
    if (muted !== this._lastMuted) {
      this._lastMuted = muted;
      changed = true;
    }

    if (changed) this._applyEffectiveVolume();

    if (realVol !== 0) {
      try {
        this._realSetVolume(0);
      } catch (err) {
        dlog("pin volume: element gone", err?.name || err);
      }
    }
  }

  unmute() {
    if (this.muteAsserter) cancelAnimationFrame(this.muteAsserter);
    this.muteAsserter = null;
    if (this._ytVolTimer) clearTimeout(this._ytVolTimer);
    this._ytVolTimer = null;

    if (this._volIntentHandler) {
      this.video.removeEventListener(
        "nomusic:vol-intent",
        this._volIntentHandler,
      );
      this._volIntentHandler = null;
    }

    // Releasing the data attribute makes the main-world setter patch a
    // no-op for this element again. Future volume writes flow through
    // normally.
    delete this.video.dataset.nomusicVolBlock;

    if (this._realSetVolume) this._realSetVolume(this.originalVolume);
  }

  dispose() {
    this.disposed = true;
    this.unmute();
  }
}
