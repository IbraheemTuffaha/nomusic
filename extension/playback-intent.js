// Keeps the user's play/pause choice separate from a pause while waiting for
// processed audio. Native events cover controls; the MAIN-world bridge also
// observes a page's pause() call when the video was already paused by us.
import { dlog } from "./settings.js";

export class PlaybackIntent {
  constructor(video, onChange) {
    this.video = video;
    this.onChange = onChange;
    this.wantsPlay = !video.paused && !video.ended;
    this.held = false;
    this.disposed = false;
    this._onIntent = (event) => {
      if (typeof event.detail?.playing === "boolean") {
        this._record(event.detail.playing);
      }
    };
    this._onPlay = () => {
      // Events are queued: a play event can arrive after we paused again.
      if (!video.paused) this._record(true);
    };
    this._onPause = () => {
      if (video.paused && !this.held) this._record(false);
    };
    video.addEventListener("nomusic:playback-intent", this._onIntent);
    video.addEventListener("play", this._onPlay);
    video.addEventListener("pause", this._onPause);
    video.dataset.nomusicPlaybackTrack = "1";
  }

  _record(playing) {
    if (this.disposed) return;
    const changed = this.wantsPlay !== playing;
    this.wantsPlay = playing;
    if (this.held && !this.video.paused) this._command("pause");
    if (changed) this.onChange(playing);
  }

  _command(method) {
    // Usually isolated-world methods bypass the MAIN-world patch. The marker
    // also makes ownership explicit when a host wraps or forwards a method.
    this.video.dataset.nomusicPlaybackInternal = "1";
    try {
      const result = this.video[method]();
      if (result?.catch) {
        result.catch((err) => dlog(`video.${method}() rejected`, err?.name || err));
      }
    } catch (err) {
      dlog(`video.${method}() failed`, err?.name || err);
    } finally {
      delete this.video.dataset.nomusicPlaybackInternal;
    }
  }

  hold() {
    if (this.disposed) return;
    this.held = true;
    if (!this.video.paused) this._command("pause");
  }

  release() {
    if (this.disposed || !this.held) return;
    this.held = false;
    if (this.wantsPlay && this.video.paused) this._command("play");
  }

  dispose({ restore = false } = {}) {
    if (this.disposed) return;
    this.disposed = true;
    this.video.removeEventListener("nomusic:playback-intent", this._onIntent);
    this.video.removeEventListener("play", this._onPlay);
    this.video.removeEventListener("pause", this._onPause);
    delete this.video.dataset.nomusicPlaybackTrack;
    const resume = restore && this.held && this.wantsPlay;
    this.held = false;
    if (resume && this.video.paused) this._command("play");
  }
}
