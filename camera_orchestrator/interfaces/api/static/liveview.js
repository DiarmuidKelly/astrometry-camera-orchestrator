/*
 * liveview.js — MJPEG live view with digital zoom, pan, a focus score and the
 * onscreen action prompt.
 *
 * This is the product. The loop it serves is: aim → zoom in on a star → turn
 * the focus ring by hand → press Enter to fire. So:
 *
 *  - The stream is just <img src="/api/camera/liveview.mjpg">; the browser
 *    decodes it for us.
 *  - Zoom/pan is a pure CSS transform on that <img> inside an overflow:hidden
 *    viewport. No re-request, no round trip — instant response, which matters
 *    when you are nudging a focus ring and watching for a change.
 *  - The focus score reads pixels back off the same <img> through an off-screen
 *    canvas (focus.js), over the VISIBLE CROP only — the user zoomed into one
 *    star for precisely this reason.
 *  - A prompt line always says what Enter will do, or what the camera is doing.
 *
 * Keyboard handling lives in keys.js; this class exposes the verbs it calls.
 */

import { frameUrl, liveViewUrl } from "./api.js";
import { FocusHistory, FocusMeter, formatScore } from "./focus.js";
import { qs } from "./util.js";

/**
 * Centre-crop steps carried over from the OpenCV reference tool. The value is
 * the fraction of the frame that stays visible, so 0.25 is 4x magnification.
 */
const CROP_STEPS = [1.0, 0.75, 0.5, 0.33, 0.25];

/** How often to compute a focus score. 4 Hz: responsive, negligible CPU. */
const SAMPLE_MS = 250;

/** Poll interval for the single-frame fallback path. */
const POLL_MS = 500;

/** Consecutive identical scores before we call the stream stalled (~3s). */
const STALL_SAMPLES = 12;

/** ~10s of history at 4 Hz — enough to see the peak as the ring turns past. */
const HISTORY_SAMPLES = 40;

// State copy. Report what is observed, not a guess at the cause — the observer
// can see the rig and the log has the detail; inventing a diagnosis here just
// sends them after the wrong thing.
const STREAM_MESSAGES = {
  idle: ["Live view stopped", "Press Start, or q to stop."],
  connecting: ["Connecting…", "Waiting for the first frame."],
  live: ["Live", null],
  busy: [
    "Live view paused",
    "A capture is using the camera. The stream resumes when the job finishes.",
  ],
  stalled: [
    "Stream stalled",
    "No new frames received.",
  ],
  error: [
    "No live view",
    "The camera is not returning preview frames. Live view must be enabled on "
      + "the body: Menu → Live View shooting → Enable, then the Start/Stop button.",
  ],
};

export class LiveView {
  constructor(root) {
    this.root = root;
    this.viewport = qs(".viewport", root);
    this.img = qs(".viewport-img", root);
    this.statusEl = qs("[data-live-status]", root);
    this.overlay = qs("[data-live-overlay]", root);
    this.overlayText = qs("[data-live-overlay-text]", root);
    this.promptEl = qs("[data-live-prompt]", root);
    this.promptTitleEl = qs("[data-prompt-title]", root);
    this.promptDetailEl = qs("[data-prompt-detail]", root);
    this.promptBar = qs("[data-prompt-bar]", root);
    this.promptBarFill = qs("[data-prompt-bar-fill]", root);
    this.scoreEl = qs("[data-focus-score]", root);
    this.peakEl = qs("[data-focus-peak]", root);
    this.peakBar = qs("[data-focus-bar]", root);
    this.sparkline = qs("[data-focus-spark]", root);
    this.zoomLabel = qs("[data-zoom-label]", root);

    this.meter = new FocusMeter();
    this.history = new FocusHistory(HISTORY_SAMPLES);

    this.cropIndex = 0;
    this.centre = { x: 0.5, y: 0.5 }; // normalised pan centre
    this.running = false;
    this.mode = "stream"; // "stream" | "poll"
    this.busy = false; // set by the job layer while the shutter is working
    this.state = "idle";
    this._identicalCount = 0;
    this._lastSignature = null;
    this._sampleTimer = null;
    this._pollTimer = null;

    this._bindControls();
    this._bindPan();
    this._bindStreamEvents();
    this._applyTransform();
  }

  /* ------------------------------------------------------------- lifecycle */

  start() {
    this.running = true;
    this._setState("connecting");
    this._identicalCount = 0;
    this._lastSignature = null;
    this._loadSource();
    clearInterval(this._sampleTimer);
    this._sampleTimer = setInterval(() => this._sample(), SAMPLE_MS);
  }

  stop() {
    this.running = false;
    clearInterval(this._sampleTimer);
    clearInterval(this._pollTimer);
    this._sampleTimer = null;
    this._pollTimer = null;
    // Blanking src is what actually closes the MJPEG connection; the backend
    // holds the camera open for as long as the socket is up.
    this.img.removeAttribute("src");
    this._setState("idle");
  }

  /**
   * The camera cannot preview and expose at the same time. While a capture is
   * in flight the stream is torn down; when it clears we re-open it ourselves
   * so the user never has to restart live view by hand mid-session.
   */
  setBusy(busy) {
    if (this.busy === busy) return;
    this.busy = busy;
    if (busy) {
      this._setState("busy");
      return;
    }
    if (!this.running) return;
    this._identicalCount = 0;
    this._lastSignature = null;
    this._setState("connecting");
    this._loadSource();
  }

  /** Switch between the multipart stream and single-frame polling. */
  setMode(mode) {
    this.mode = mode;
    if (this.running) this.start();
  }

  _loadSource() {
    clearInterval(this._pollTimer);
    if (this.mode === "poll") {
      const tick = () => {
        this.img.src = frameUrl();
      };
      tick();
      this._pollTimer = setInterval(tick, POLL_MS);
    } else {
      this.img.src = liveViewUrl();
    }
  }

  /* ---------------------------------------------------------- stream state */

  _bindStreamEvents() {
    this.img.addEventListener("error", () => {
      if (!this.running || this.busy) return;
      this._setState("error");
    });
    this.img.addEventListener("load", () => {
      if (!this.running) return;
      if (this.state !== "live") this._setState("live");
    });
  }

  _setState(state) {
    // While busy nothing else may claim the banner — an <img> error during a
    // capture is expected, not a fault.
    if (this.busy && state !== "busy" && state !== "idle") return;
    this.state = state;
    const [label, detail] = STREAM_MESSAGES[state] || STREAM_MESSAGES.idle;
    this.statusEl.textContent = label;
    this.statusEl.dataset.state = state;
    if (detail) {
      this.overlayText.textContent = detail;
      this.overlay.hidden = false;
      this.overlay.dataset.state = state;
    } else {
      this.overlay.hidden = true;
    }
  }

  /* ------------------------------------------------------- onscreen prompt */

  /**
   * The always-visible line under the frame.
   * @param {{title: string, detail?: string, tone?: string,
   *          progress?: {current, total}|null}} prompt
   */
  setPrompt({ title, detail = "", tone = "idle", progress = null }) {
    this.promptTitleEl.textContent = title;
    this.promptDetailEl.textContent = detail;
    this.promptEl.dataset.tone = tone;
    if (progress && progress.total) {
      const pct = Math.round((progress.current / progress.total) * 100);
      this.promptBar.hidden = false;
      this.promptBar.setAttribute("aria-valuenow", String(pct));
      this.promptBarFill.style.width = `${pct}%`;
    } else {
      this.promptBar.hidden = true;
    }
  }

  /* --------------------------------------------------------- zoom and pan */

  get crop() {
    return CROP_STEPS[this.cropIndex];
  }

  /** Magnification factor: 1 / crop-fraction. */
  get scale() {
    return 1 / this.crop;
  }

  zoomIn() {
    this.setZoomIndex(this.cropIndex + 1);
  }

  zoomOut() {
    this.setZoomIndex(this.cropIndex - 1);
  }

  zoomReset() {
    this.centre = { x: 0.5, y: 0.5 };
    this.cropIndex = 0;
    this.history.reset();
    this._applyTransform();
    this._render();
  }

  resetFocus() {
    this.history.reset();
    this._render();
  }

  setZoomIndex(index) {
    const clamped = Math.max(0, Math.min(CROP_STEPS.length - 1, index));
    if (clamped === this.cropIndex) return;
    this.cropIndex = clamped;
    // A different crop samples different pixels, so past scores are no longer
    // comparable. Dropping the history avoids a misleading peak.
    this.history.reset();
    this._clampCentre();
    this._applyTransform();
    this._render();
  }

  /** Pan by whole steps; ±1 is 5% of the visible crop. */
  pan(dx, dy) {
    if (this.scale === 1) return;
    const step = 0.05 * this.crop;
    this.centre.x += dx * step;
    this.centre.y += dy * step;
    this._clampCentre();
    this._applyTransform();
    this.history.reset();
  }

  _clampCentre() {
    // Half the visible crop, in normalised units. At scale 1 the centre is
    // pinned; zoomed in it may roam, but never past the frame edge.
    const half = this.crop / 2;
    const lo = half;
    const hi = 1 - half;
    this.centre.x = hi <= lo ? 0.5 : Math.min(hi, Math.max(lo, this.centre.x));
    this.centre.y = hi <= lo ? 0.5 : Math.min(hi, Math.max(lo, this.centre.y));
  }

  _applyTransform() {
    const s = this.scale;
    // The <img> is width:100% of the viewport, so its layout box is the
    // unzoomed frame. Scale about the element centre, then translate so the
    // chosen centre point lands back in the middle of the viewport.
    const w = this.img.clientWidth || this.viewport.clientWidth;
    const h = this.img.clientHeight || this.viewport.clientHeight;
    const tx = -s * (this.centre.x - 0.5) * w;
    const ty = -s * (this.centre.y - 0.5) * h;
    this.img.style.transform = `translate(${tx}px, ${ty}px) scale(${s})`;
    this.zoomLabel.textContent = s === 1 ? "fit" : `${s.toFixed(2)}×`;
    this.root.dataset.zoomed = s > 1 ? "true" : "false";
  }

  _bindControls() {
    qs("[data-zoom-in]", this.root).addEventListener("click", () => this.zoomIn());
    qs("[data-zoom-out]", this.root).addEventListener("click", () => this.zoomOut());
    qs("[data-zoom-reset]", this.root).addEventListener("click", () => this.zoomReset());
    qs("[data-focus-reset]", this.root).addEventListener("click", () => this.resetFocus());
  }

  _bindPan() {
    let dragging = false;
    let lastX = 0;
    let lastY = 0;

    const begin = (event) => {
      if (this.scale === 1) return;
      dragging = true;
      lastX = event.clientX;
      lastY = event.clientY;
      this.viewport.setPointerCapture?.(event.pointerId);
      this.viewport.classList.add("is-dragging");
    };

    const move = (event) => {
      if (!dragging) return;
      const w = this.img.clientWidth || 1;
      const h = this.img.clientHeight || 1;
      // Dragging right moves the view left across the frame, and a pixel of
      // drag covers 1/scale of a frame pixel at the current zoom.
      this.centre.x -= (event.clientX - lastX) / (w * this.scale);
      this.centre.y -= (event.clientY - lastY) / (h * this.scale);
      lastX = event.clientX;
      lastY = event.clientY;
      this._clampCentre();
      this._applyTransform();
      this.history.reset();
    };

    const end = (event) => {
      if (!dragging) return;
      dragging = false;
      this.viewport.releasePointerCapture?.(event.pointerId);
      this.viewport.classList.remove("is-dragging");
    };

    this.viewport.addEventListener("pointerdown", begin);
    this.viewport.addEventListener("pointermove", move);
    this.viewport.addEventListener("pointerup", end);
    this.viewport.addEventListener("pointercancel", end);

    // Pinch/scroll zoom for the phone and trackpad case. Keyboard is primary;
    // this is the same discrete steps, just reached differently.
    this.viewport.addEventListener(
      "wheel",
      (event) => {
        event.preventDefault();
        if (event.deltaY < 0) this.zoomIn();
        else this.zoomOut();
      },
      { passive: false },
    );

    window.addEventListener("resize", () => this._applyTransform());
  }

  /* ------------------------------------------------------------ focus loop */

  _sample() {
    if (!this.running || this.busy) return;

    const ready = this.img.naturalWidth > 0 && this.img.naturalHeight > 0;
    if (!ready) return;

    // Map the visible crop back to source pixels for the meter.
    const nw = this.img.naturalWidth;
    const nh = this.img.naturalHeight;
    const sw = nw * this.crop;
    const sh = nh * this.crop;
    const sample = this.meter.measure(this.img, {
      sx: this.centre.x * nw - sw / 2,
      sy: this.centre.y * nh - sh / 2,
      sw,
      sh,
    });
    if (sample === null) return;
    const { score, signature } = sample;

    // Liveness is judged on the pixels, NOT on the score. The score is one
    // float derived from 36k pixels, so two different frames of a dark sky
    // legitimately produce the same number — using it here reported "stalled"
    // while frames were visibly arriving.
    if (this._lastSignature !== null && signature === this._lastSignature) {
      this._identicalCount += 1;
      if (this._identicalCount >= STALL_SAMPLES && this.state === "live") {
        this._setState("stalled");
      }
    } else {
      this._identicalCount = 0;
      if (this.state !== "live") this._setState("live");
    }
    this._lastSignature = signature;

    this.history.push(score);
    this._render();
  }

  _render() {
    this.scoreEl.textContent = formatScore(this.history.latest);
    this.peakEl.textContent = formatScore(this.history.peak || null);
    const pct = Math.round(this.history.fractionOfPeak * 100);
    this.peakBar.style.width = `${pct}%`;
    this.peakBar.parentElement.setAttribute("aria-valuenow", String(pct));
    this.history.render(this.sparkline);
  }
}
