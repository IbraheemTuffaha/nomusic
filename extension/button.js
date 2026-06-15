// Button: the floating pill UI per <video> — status display, menu, MP4
// download. Creates/disposes a Session on toggle. Split out of content.js.
import { settings } from "./settings.js";
import { Session } from "./session.js";

// Strip characters that are illegal in filenames across Windows/macOS/Linux
// (plus control chars), collapse whitespace, and bound the length so a very
// long video title can't produce an unwieldy filename.
function sanitizeFilename(name) {
  return (name || "")
    .replace(/[/\\:*?"<>|\x00-\x1f]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 120);
}

// ---------------------------------------------------------------------------
// Button + per-video attachment
// ---------------------------------------------------------------------------
class Button {
  constructor(video) {
    this.video = video;
    this.session = null;
    // Set once the user dismisses (×): a re-anchor must never bring a
    // dismissed pill back into view.
    this._dismissed = false;
    // Error state is intentionally transient — see _scheduleErrorRevert.
    this._errorRevertTimer = null;
    this.el = document.createElement("button");
    this.el.className = "nomusic-btn";
    this.el.type = "button";
    this.el.title = "Strip music (nomusic)";
    // Progress fill lives inside a clip wrapper so it's clipped to the
    // pill's rounded shape (the dismiss × stays outside the wrapper, so it
    // isn't clipped).
    this.fillClip = document.createElement("span");
    this.fillClip.className = "nomusic-btn__clip";
    this.fill = document.createElement("span");
    this.fill.className = "nomusic-btn__fill";
    this.fillClip.appendChild(this.fill);
    // Brand icon replaces the old colored dot as the leading visual.
    // It picks up the same pulse animation while the backend is working.
    this.icon = document.createElement("img");
    this.icon.className = "nomusic-btn__icon";
    this.icon.src = chrome.runtime.getURL("icons/button.png");
    this.icon.alt = "";
    // Lock the icon's box with inline !important. Some hosts (Telegram Web)
    // force-size every <img> in their message UI to fill its container with a
    // high-specificity !important rule; an inline !important declaration
    // outranks any stylesheet rule, so this stops the 1008x510 wordmark from
    // ballooning across the video. 28x14 keeps its ~2:1 ratio.
    for (const [k, v] of Object.entries({
      width: "28px", height: "14px",
      "max-width": "28px", "max-height": "14px",
      "min-width": "0", "min-height": "0",
    })) {
      this.icon.style.setProperty(k, v, "important");
    }
    this.label = document.createElement("span");
    this.label.className = "nomusic-btn__label";
    this.label.textContent = "nomusic";
    this.pct = document.createElement("span");
    this.pct.className = "nomusic-btn__pct";
    // Latest video title from the backend status stream; used to name the
    // downloaded file. Populated in showStatus().
    this.title = "";
    // Download control: a chevron that opens a dropdown menu (MP3 + MP4 at
    // several resolutions). Only visible once the job is ready (CSS keys it
    // off [data-state="active"]). A <span role="button"> avoids nested-button
    // HTML inside the pill; the menu itself lives on document.body (see
    // _buildMenu) so its <button> items aren't nested in the pill <button>.
    this.dlBtn = document.createElement("span");
    this.dlBtn.className = "nomusic-btn__dl";
    this.dlBtn.setAttribute("role", "button");
    this.dlBtn.setAttribute("aria-haspopup", "menu");
    this.dlBtn.setAttribute("aria-label", "Download…");
    this.dlBtn.title = "Download…";
    this.dlBtn.textContent = "⤵"; // ⤵ download-ish chevron
    this.dlBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      e.preventDefault();
      this.toggleMenu();
    });
    this._menuOpen = false;
    this._downloading = false;
    // A download requested before the track finished processing: {format,
    // height}. Held until the job reaches "ready", then saved automatically.
    this._pendingDownload = null;
    // Latest backend readiness, tracked from showStatus so download() knows
    // whether it can fetch now or must queue and wait.
    this._ready = false;
    this.menu = this._buildMenu();
    // Close the menu on an outside click / scroll / resize. Capture phase so
    // we see the event even if the host page stops propagation.
    this._onDocClick = (e) => {
      if (this._menuOpen && !this.menu.contains(e.target) && e.target !== this.dlBtn)
        this.closeMenu();
    };
    this._onReposition = () => this.closeMenu();
    // <span role="button"> rather than nested <button> — nested buttons
    // are invalid HTML and some browsers (older Safari) misroute clicks
    // when they appear.
    this.dismissBtn = document.createElement("span");
    this.dismissBtn.className = "nomusic-btn__dismiss";
    this.dismissBtn.setAttribute("role", "button");
    this.dismissBtn.setAttribute("aria-label", "Hide nomusic on this video");
    this.dismissBtn.title = "Hide nomusic";
    // The X is drawn geometrically via ::before/::after in content.css
    // because the "×" glyph's font metrics render off-center in the
    // dismiss bubble.
    this.dismissBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      e.preventDefault();
      this.dismiss();
    });
    this.el.append(
      this.fillClip,
      this.icon,
      this.label,
      this.pct,
      this.dlBtn,
      this.dismissBtn,
    );
    this.el.addEventListener("click", (e) => {
      e.stopPropagation();
      e.preventDefault();
      this.toggle();
    });
    this.setIdle();
  }

  /** Hide the button entirely. If nomusic is active, tear the session
   *  down first so the host video is restored. The button stays
   *  attached to the DOM but with display:none, so we don't re-create
   *  it via the MutationObserver. A new <video> on SPA navigation gets
   *  its own button. */
  dismiss() {
    if (this.session && !this.session.disposed) {
      this.session.dispose();
      this.session = null;
    }
    this.closeMenu();
    this.menu.remove(); // it lives on document.body, so clean it up
    this._dismissed = true;
    this._pendingDownload = null; // user is leaving — drop any queued download
    this.el.style.display = "none";
  }

  setIdle() {
    this._clearErrorRevert();
    this.el.dataset.state = "idle";
    this.label.textContent = "nomusic";
    this.pct.textContent = "";
    this.fill.style.width = "0%";
  }

  /** ``status`` is the raw JobStatus from the backend. */
  showStatus(status) {
    // Remember the title for the download filename; it arrives on every
    // snapshot but isn't otherwise displayed.
    if (status.title) this.title = status.title;
    // While a file fetch is in flight the export progress owns the pill
    // (_showExportProgress); ignore late status snapshots so they don't fight.
    if (this._downloading) return;

    const state = status.state;
    this._ready = state === "ready";
    if (state === "ready") {
      // A download queued mid-processing fires the instant the track is done.
      if (this._pendingDownload) {
        const pd = this._pendingDownload;
        this._pendingDownload = null;
        this._startDownload(pd.format, pd.height);
        return;
      }
      this._clearErrorRevert();
      this.el.dataset.state = "active";
      this.label.textContent = "nomusic on";
      this.pct.textContent = "";
      this.fill.style.width = "0%";
      return;
    }
    if (state === "error") {
      this._pendingDownload = null; // can't deliver a file from a failed job
      this.setError(status.phase_label || "Error");
      return;
    }
    this._clearErrorRevert();
    this.el.dataset.state = "working";
    // Relabel the processing phase while a download is queued so it's clear
    // we're finishing the track for the file (and it keeps going on pause).
    this.label.textContent = this._pendingDownload
      ? "Preparing"
      : status.phase_label || "Working";
    const p = status.phase_progress;
    if (typeof p === "number" && isFinite(p)) {
      const pct = Math.max(0, Math.min(100, Math.round(p * 100)));
      this.pct.textContent = `${pct}%`;
      this.fill.style.width = `${pct}%`;
    } else {
      this.pct.textContent = "";
      this.fill.style.width = "0%";
    }
  }

  setBuffering() {
    // An in-flight export owns the pill (Preparing/Encoding N%); a playback
    // buffer event must not clobber it. _showExportProgress has the inverse
    // guard, so the export display is fully isolated while _downloading.
    if (this._downloading) return;
    this._clearErrorRevert();
    this.el.dataset.state = "working";
    this.label.textContent = "Buffering";
    this.pct.textContent = "";
    this.fill.style.width = "0%";
  }

  /** User paused while the backend was still working. Distinct from
   *  "Buffering" (which auto-resumes): this means we've let the worker go
   *  idle. Keeps the current % + fill so the frozen progress is visible,
   *  and the non-"working" state stops the icon pulse. */
  setPaused() {
    if (this._downloading) return; // export owns the pill — see setBuffering
    this._clearErrorRevert();
    this.el.dataset.state = "paused";
    this.label.textContent = "Paused";
  }

  setError(label) {
    this.el.dataset.state = "error";
    this.label.textContent = label || "Error";
    this.pct.textContent = "";
    this.fill.style.width = "0%";
    this._scheduleErrorRevert();
  }

  // Error is transient feedback, not a sticky mode. After a brief moment
  // the pill returns to its idle shape so the user can click again
  // cleanly instead of staring at a red bar.
  _scheduleErrorRevert() {
    this._clearErrorRevert();
    this._errorRevertTimer = setTimeout(() => {
      this._errorRevertTimer = null;
      if (this.el.dataset.state === "error") this.setIdle();
    }, 2500);
  }

  _clearErrorRevert() {
    if (this._errorRevertTimer) {
      clearTimeout(this._errorRevertTimer);
      this._errorRevertTimer = null;
    }
  }

  async toggle() {
    if (this.session && !this.session.disposed) {
      this.session.dispose();
      this.session = null;
      return;
    }
    this.session = new Session(this.video, this);
    await this.session.start();
  }

  dispose() {
    // Drop the body-level menu node and its open-state listeners. Without
    // this, a button torn down on SPA navigation (video emptied) leaves its
    // hidden menu orphaned on document.body, one per navigation. openMenu()
    // re-appends it if this same button is later reused.
    this.closeMenu();
    this.menu.remove();
    this.setIdle();
  }

  // The download menu. MP3 (audio only) plus MP4 at a few resolution caps.
  // Resolution is a ceiling — the backend grabs the best stream up to it and
  // falls back when a video doesn't offer that height.
  static MENU_ITEMS = [
    { section: "Audio" },
    { label: "MP3 — audio only", format: "mp3", height: 0 },
    { section: "Video (MP4)" },
    { label: "Best available", format: "mp4", height: 0 },
    { label: "2160p · 4K", format: "mp4", height: 2160 },
    { label: "1440p", format: "mp4", height: 1440 },
    { label: "1080p", format: "mp4", height: 1080 },
    { label: "720p", format: "mp4", height: 720 },
    { label: "480p", format: "mp4", height: 480 },
  ];

  _buildMenu() {
    const menu = document.createElement("div");
    menu.className = "nomusic-menu";
    menu.setAttribute("role", "menu");
    menu.hidden = true;
    for (const it of Button.MENU_ITEMS) {
      if (it.section) {
        const h = document.createElement("div");
        h.className = "nomusic-menu__section";
        h.textContent = it.section;
        menu.appendChild(h);
        continue;
      }
      const b = document.createElement("button");
      b.type = "button";
      b.className = "nomusic-menu__item";
      b.textContent = it.label;
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        e.preventDefault();
        this.closeMenu();
        this.download(it.format, it.height);
      });
      menu.appendChild(b);
    }
    // Lives on body (not inside the pill <button>) to avoid nested buttons
    // and host-page overflow clipping; positioned on open via openMenu().
    document.body.appendChild(menu);
    return menu;
  }

  toggleMenu() {
    this._menuOpen ? this.closeMenu() : this.openMenu();
  }

  openMenu() {
    if (this._downloading) return; // no menu while a download is in flight
    // The menu lives on document.body and is removed on dispose() (so a
    // torn-down button doesn't orphan it). The button itself is reusable
    // after dispose() → setIdle(), so re-attach before measuring/positioning.
    if (!this.menu.isConnected) document.body.appendChild(this.menu);
    const r = this.el.getBoundingClientRect();
    this.menu.hidden = false; // unhide first so offsetWidth/Height are real
    const mw = this.menu.offsetWidth;
    const mh = this.menu.offsetHeight;
    let left = Math.max(8, r.right - mw); // right-align to the pill
    let top = r.bottom + 6;
    if (top + mh > window.innerHeight - 8) top = r.top - 6 - mh; // flip up
    this.menu.style.left = `${Math.round(left)}px`;
    this.menu.style.top = `${Math.round(Math.max(8, top))}px`;
    this._menuOpen = true;
    document.addEventListener("click", this._onDocClick, true);
    window.addEventListener("scroll", this._onReposition, true);
    window.addEventListener("resize", this._onReposition, true);
  }

  closeMenu() {
    if (!this._menuOpen) return;
    this.menu.hidden = true;
    this._menuOpen = false;
    document.removeEventListener("click", this._onDocClick, true);
    window.removeEventListener("scroll", this._onReposition, true);
    window.removeEventListener("resize", this._onReposition, true);
  }

  /** Entry point for a download menu pick. If the track is fully processed we
   *  fetch + save immediately; otherwise we queue it and keep the worker alive
   *  to completion (the user can pause / stop watching), saving automatically
   *  when it's ready. ``format`` is "mp3" or "mp4"; ``height`` caps MP4 res. */
  download(format, height = 0) {
    if (!this.session?.jobId) return;
    if (this._downloading) return; // a fetch is already in flight
    if (this._ready) {
      this._startDownload(format, height);
      return;
    }
    // Not done yet: queue it and pin the worker so processing runs to the end
    // regardless of play/pause, then save when it reaches "ready".
    this._pendingDownload = { format, height };
    this.closeMenu();
    this.el.dataset.state = "working";
    this.label.textContent = "Preparing";
    this.session.ensureLiveForDownload();
  }

  /** Fetch the finished export from the backend and save it to disk. We fetch
   *  the bytes and save via a blob: URL because a direct cross-origin
   *  <a download> to the backend would have its filename ignored. */
  async _startDownload(format, height = 0) {
    const jobId = this.session?.jobId;
    if (!jobId) return;
    if (this._downloading) return; // ignore double-clicks mid-download
    this._downloading = true;

    const ext = format === "mp4" ? "mp4" : "mp3";
    const q = height ? `?max_height=${height}` : "";
    const url =
      format === "mp4"
        ? `${settings.backendUrl}/video/${jobId}${q}`
        : `${settings.backendUrl}/audio/${jobId}?format=mp3`;

    // Busy feedback — freeze the pill while preparing.
    this._clearErrorRevert();
    this.el.dataset.state = "working";
    this.label.textContent = format === "mp4" ? "Preparing…" : "Saving…";
    this.pct.textContent = "";
    this.fill.style.width = "0%";

    // MP4 prep can take a while (download + mux/re-encode); poll the backend
    // so the pill shows real "Fetching N%" / "Encoding N%" progress.
    let pollTimer = null;
    if (format === "mp4") {
      const progUrl = `${settings.backendUrl}/video/${jobId}/progress${q}`;
      const poll = async () => {
        try {
          const r = await fetch(progUrl, { cache: "no-store" });
          if (r.ok) this._showExportProgress(await r.json());
        } catch (_e) {
          /* transient; keep polling */
        }
      };
      pollTimer = setInterval(poll, 600);
      poll();
    }

    let objUrl = null;
    try {
      // no-store: never reuse a cached response. Older backends served raw
      // Opus at the ?format=mp3 URL with a 24h cache header, which the
      // browser would otherwise keep handing back instead of the real MP3.
      const resp = await fetch(url, { cache: "no-store" });
      if (!resp.ok) {
        throw new Error(
          resp.status === 425 ? "not ready" : `HTTP ${resp.status}`,
        );
      }
      const blob = await resp.blob();
      // Stop progress polling the moment the bytes arrive, before we restore
      // the label, so a late poll can't overwrite it.
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
      objUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = objUrl;
      a.download = `${sanitizeFilename(this.title) || "nomusic"}.${ext}`;
      a.rel = "noopener";
      document.body.appendChild(a);
      a.click();
      a.remove();
      this._restoreAfterDownload();
    } catch (err) {
      console.warn("[nomusic] download failed", err);
      this._flashDownloadError();
    } finally {
      if (pollTimer) clearInterval(pollTimer);
      this._downloading = false;
      // Revoke after the click-initiated download has had time to start;
      // revoking immediately can cancel it in some browsers.
      if (objUrl) setTimeout(() => URL.revokeObjectURL(objUrl), 10000);
    }
  }

  /** Render a polled export-progress snapshot onto the pill. */
  _showExportProgress(p) {
    if (!this._downloading || !p) return;
    if (p.phase === "idle" || p.phase === "done") return;
    const label = p.phase === "downloading" ? "Fetching" : "Encoding";
    const pct = Math.max(0, Math.min(100, Math.round(p.percent || 0)));
    this.el.dataset.state = "working";
    this.label.textContent = label;
    this.pct.textContent = `${pct}%`;
    this.fill.style.width = `${pct}%`;
  }

  /** Return the pill to its post-download resting visual: "nomusic on" if the
   *  session is still live, otherwise idle. */
  _restoreAfterDownload() {
    if (this.session && !this.session.disposed) {
      this.el.dataset.state = "active";
      this.label.textContent = "nomusic on";
      this.pct.textContent = "";
      this.fill.style.width = "0%";
    } else {
      this.setIdle();
    }
  }

  _flashDownloadError() {
    this.el.dataset.state = "error";
    this.label.textContent = "Download failed";
    this.pct.textContent = "";
    this.fill.style.width = "0%";
    this._clearErrorRevert();
    this._errorRevertTimer = setTimeout(() => {
      this._errorRevertTimer = null;
      if (this.el.dataset.state === "error") this._restoreAfterDownload();
    }, 2500);
  }

  position(host) {
    // Anchor inside the nearest positioned ancestor of the video. The host
    // page often wraps the <video> in a player container; we live inside it.
    // Top-right keeps us clear of the bottom control bar / scrubber, which
    // otherwise overlaps the pill (badly at the end of a video when the
    // progress bar is full).
    host.appendChild(this.el);
    this.el.style.right = "12px";
    this.el.style.top = "12px";
  }
}


export { Button };
