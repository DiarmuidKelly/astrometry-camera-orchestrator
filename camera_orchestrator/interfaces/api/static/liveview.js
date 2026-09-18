/*
 * liveview.js — MJPEG live view with digital zoom, pan and the onscreen
 * action prompt.
 *
 * This is the product. The loop it serves is: aim → zoom in on a star → turn
 * the focus ring by hand → press Enter to fire. So:
 *
 *  - The stream is just <img src="/api/camera/liveview.mjpg">; the browser
 *    decodes it for us.
 *  - Zoom/pan is a pure CSS transform on that <img> inside an overflow:hidden
 *    viewport. No re-request, no round trip — instant response, which matters
 *    when you are nudging a focus ring and watching for a change.
 *  - Clicking the frame centres on that point, so an edge star can be zoomed
 *    into directly rather than zoomed-then-hunted-for.
 *  - A prompt line always says what Enter will do, or what the camera is doing.
 *
 * Keyboard handling lives in keys.js; this class exposes the verbs it calls.
 */

import { frameUrl, liveViewUrl, releaseCamera } from "./api.js";
import { qs } from "./util.js";

/**
 * Centre-crop steps carried over from the OpenCV reference tool. The value is
 * the fraction of the frame that stays visible, so 0.25 is 4x magnification.
 */
const CROP_STEPS = [1.0, 0.75, 0.5, 0.33, 0.25];

/** How often to check whether frames are still arriving. */
const SAMPLE_MS = 500;

/** Poll interval for the single-frame fallback path. */
const POLL_MS = 500;

/** No decoded frame for this long and the stream is considered stalled. */
const STALL_MS = 3000;

/**
 * Reopen attempts before reporting no live view, and the gap between them.
 *
 * A capture drops the mirror, which switches live view off on the body. When
 * the job finishes we re-request the stream immediately, but the camera needs a
 * moment to bring the mirror back up and restart the sensor feed — the first
 * attempt fails with a 409 even though the camera is about to be fine. Without
 * a retry that one failure stuck the UI on "No live view" until the user
 * pressed Start again.
 */
const REOPEN_ATTEMPTS = 6;
const REOPEN_DELAY_MS = 1000;

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
    this.zoomLabel = qs("[data-zoom-label]", root);

    this.cropIndex = 0;
    this.centre = { x: 0.5, y: 0.5 }; // normalised pan centre
    this.running = false;
    this.mode = "stream"; // "stream" | "poll"
    this.busy = false; // set by the job layer while the shutter is working
    this.state = "idle";
    this._lastFrameAt = performance.now();
    this._sampleTimer = null;
    this._reopenTimer = null;
    this._reopenTries = 0;
    this._pollTimer = null;
    this._frameShape = null;   // "WxH" of the last decoded frame

    this._bindControls();
    this._bindPan();
    this._bindStreamEvents();
    this._applyTransform();
  }

  /* ------------------------------------------------------------- lifecycle */

  start() {
    this.running = true;
    // An explicit start (the button, or Retry stream) buys a fresh set of
    // reopen attempts. Without this a stream that had already given up stayed
    // given-up: Retry made exactly one request and fell straight back to
    // "No live view".
    this._reopenTries = 0;
    this._setState("connecting");
    this._lastFrameAt = performance.now();
    this._loadSource();
    clearInterval(this._sampleTimer);
    this._sampleTimer = setInterval(() => this._checkLiveness(), SAMPLE_MS);
  }

  stop() {
    this.running = false;
    clearInterval(this._sampleTimer);
    clearInterval(this._pollTimer);
    clearTimeout(this._reopenTimer);   // don't reopen a stream we just stopped
    this._sampleTimer = null;
    this._pollTimer = null;
    this._reopenTimer = null;
    this._reopenTries = 0;
    // Blanking src is what actually closes the MJPEG connection; the backend
    // holds the camera open for as long as the socket is up.
    this.img.removeAttribute("src");
    // Closing the stream is not enough — while the PTP session is open the body
    // stays in live view with the mirror up. Ask the server to drop it so the
    // camera goes back to rest. Deliberate stop, so release now rather than on
    // the debounce a dropped connection gets.
    releaseCamera().catch(() => {});   // best effort; the debounce covers failure
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
    this._lastFrameAt = performance.now();
    this._reopenTries = 0;
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
    clearTimeout(this._reopenTimer);
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
      if (this._reopenTries < REOPEN_ATTEMPTS) {
        this._reopenTries += 1;
        this._setState("connecting");
        clearTimeout(this._reopenTimer);
        this._reopenTimer = setTimeout(() => {
          if (this.running && !this.busy) this._loadSource();
        }, REOPEN_DELAY_MS);
        return;
      }
      // Giving up has to stop the traffic too. The poll timer used to survive
      // this branch, so a dead backend was asked for frame.jpg at 2 Hz for the
      // rest of the session — each request opening a camera session server-side
      // — while the UI calmly read "No live view". Retry restarts them.
      clearInterval(this._pollTimer);
      clearTimeout(this._reopenTimer);
      this._pollTimer = null;
      this._reopenTimer = null;
      this._setState("error");
    });
    this.img.addEventListener("load", () => {
      if (!this.running) return;
      this._reopenTries = 0;                   // a frame arrived; start over
      this._lastFrameAt = performance.now();   // the one trustworthy signal
      // The frame's aspect ratio decides the letterboxing, and so the pan/zoom
      // geometry. Re-apply only when it actually changes — this fires per frame
      // in poll mode.
      const shape = `${this.img.naturalWidth}x${this.img.naturalHeight}`;
      if (shape !== this._frameShape) {
        this._frameShape = shape;
        this._applyTransform();
      }
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
    this._applyTransform();
  }

  /** Current zoom step, for anything mirroring the control state. */
  get zoomIndex() {
    return this.cropIndex;
  }


  /**
   * The size of the *rendered image* inside the viewport, in CSS pixels.
   *
   * `object-fit: contain` letterboxes any stream whose aspect ratio is not the
   * viewport's, so the <img> element box and the pixels the user can actually
   * see are two different rectangles. Measuring the element box treats the
   * black bars as frame, which puts click-to-focus and drag-to-pan off target
   * on a 4:3 body. Both boxes share a centre, so only the size differs.
   */
  _contentBox() {
    const w = this.img.clientWidth || this.viewport.clientWidth || 1;
    const h = this.img.clientHeight || this.viewport.clientHeight || 1;
    const nw = this.img.naturalWidth;
    const nh = this.img.naturalHeight;
    if (!nw || !nh) return { w, h };   // no frame decoded yet
    const fit = Math.min(w / nw, h / nh);
    return { w: nw * fit, h: nh * fit };
  }

  /**
   * Normalised frame coords (0..1) under a pointer event.
   * Position relative to the *rendered image* maps straight onto the visible
   * crop, which then maps back into the full frame.
   */
  pointToFrame(event) {
    const rect = this.viewport.getBoundingClientRect();
    const { w, h } = this._contentBox();
    const fx = (event.clientX - rect.left - rect.width / 2) / (w || 1);
    const fy = (event.clientY - rect.top - rect.height / 2) / (h || 1);
    return {
      x: this.centre.x + fx * this.crop,
      y: this.centre.y + fy * this.crop,
    };
  }

  /** Centre the view on a normalised frame point — click a star to inspect it. */
  focusOn(point) {
    this.centre.x = point.x;
    this.centre.y = point.y;
    this._clampCentre();
    this._applyTransform();
  }

  setZoomIndex(index, anchor = null) {
    const clamped = Math.max(0, Math.min(CROP_STEPS.length - 1, index));
    // Zooming about a given frame point rather than the middle: stars worth
    // checking focus on are often at the edge, and centre-anchored zoom means
    // zooming in then hunting for them by drag.
    //
    // The anchor is applied BEFORE the unchanged-index bail-out. At max zoom
    // the index cannot move, and returning early there meant pinching or
    // double-clicking on a new star at 4x did nothing at all.
    if (anchor) {
      this.centre.x = anchor.x;
      this.centre.y = anchor.y;
      this._clampCentre();
      this._applyTransform();
    }
    if (clamped === this.cropIndex) return;
    this.cropIndex = clamped;
    this._clampCentre();
    this._applyTransform();
  }

  /** Pan by whole steps; ±1 is 5% of the visible crop. */
  pan(dx, dy) {
    if (this.scale === 1) return;
    const step = 0.05 * this.crop;
    this.centre.x += dx * step;
    this.centre.y += dy * step;
    this._clampCentre();
    this._applyTransform();
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
    // Scale about the element centre, then translate so the chosen frame point
    // lands back in the middle of the viewport. The displacement is measured in
    // *rendered image* pixels, not element pixels, so a letterboxed stream pans
    // by what the user sees rather than by the black bars.
    const { w, h } = this._contentBox();
    const tx = -s * (this.centre.x - 0.5) * w;
    const ty = -s * (this.centre.y - 0.5) * h;
    this.img.style.transform = `translate(${tx}px, ${ty}px) scale(${s})`;
    this.zoomLabel.textContent = s === 1 ? "fit" : `${s.toFixed(2)}×`;
    // The bar is one of several ways to zoom, so it follows rather than leads.
    if (this.zoomBar) this.zoomBar.value = String(this.cropIndex);
    this.root.dataset.zoomed = s > 1 ? "true" : "false";
  }

  _bindControls() {
    qs("[data-zoom-in]", this.root).addEventListener("click", () => this.zoomIn());
    qs("[data-zoom-out]", this.root).addEventListener("click", () => this.zoomOut());
    qs("[data-zoom-reset]", this.root).addEventListener("click", () => this.zoomReset());

    this.zoomBar = qs("[data-zoom-bar]", this.root);
    this.zoomBar.max = String(CROP_STEPS.length - 1);
    this.zoomBar.addEventListener("input", () => {
      this.setZoomIndex(Number(this.zoomBar.value));
    });
  }

  _bindPan() {
    // How far a pointer may travel and still count as a tap ("centre on this
    // star") rather than a drag ("pan"). A mouse is precise; a gloved fingertip
    // or a phone held one-handed in the cold routinely slides 8-15 px during
    // what the user intends as a tap, and a 4 px budget swallowed those taps.
    const slop = (event) => (event.pointerType === "mouse" ? 4 : 14);

    let dragging = false;
    let lastX = 0;
    let lastY = 0;

    let downX = 0;
    let downY = 0;
    let moved = false;

    const begin = (event) => {
      downX = event.clientX;
      downY = event.clientY;
      moved = false;
      if (this.scale === 1) return;   // nothing to pan, but still track the tap
      dragging = true;
      lastX = event.clientX;
      lastY = event.clientY;
      this.viewport.setPointerCapture?.(event.pointerId);
      this.viewport.classList.add("is-dragging");
    };

    const move = (event) => {
      if (Math.hypot(event.clientX - downX, event.clientY - downY) > slop(event)) {
        moved = true;
      }
      if (!dragging) return;
      const { w, h } = this._contentBox();
      // Dragging right moves the view left across the frame, and a pixel of
      // drag covers 1/scale of a frame pixel at the current zoom.
      this.centre.x -= (event.clientX - lastX) / (w * this.scale);
      this.centre.y -= (event.clientY - lastY) / (h * this.scale);
      lastX = event.clientX;
      lastY = event.clientY;
      this._clampCentre();
      this._applyTransform();
    };

    const end = (event) => {
      // A tap that didn't drag means "inspect this star" — centre on it. Edge
      // stars are the awkward case: centre-anchored zoom puts them off-screen.
      if (!moved) this.focusOn(this.pointToFrame(event));
      if (!dragging) return;
      dragging = false;
      this.viewport.releasePointerCapture?.(event.pointerId);
      this.viewport.classList.remove("is-dragging");
    };

    this.viewport.addEventListener("dblclick", (event) => {
      event.preventDefault();
      this.setZoomIndex(this.cropIndex + 1, this.pointToFrame(event));
    });
    this.viewport.addEventListener("pointerdown", begin);
    this.viewport.addEventListener("pointermove", move);
    this.viewport.addEventListener("pointerup", end);
    this.viewport.addEventListener("pointercancel", end);

    // Trackpad mapping, matching what maps and design tools do:
    //   two-finger scroll  -> pan        (wheel with deltaX/deltaY)
    //   pinch              -> zoom       (the browser reports it as ctrl+wheel)
    // Binding plain wheel to zoom fights the hardware: on a trackpad scrolling
    // IS the pan gesture, and there is no middle button to fall back on.
    this.viewport.addEventListener(
      "wheel",
      (event) => {
        event.preventDefault();
        if (event.ctrlKey) {                       // pinch, or ctrl+wheel on a mouse
          const anchor = this.pointToFrame(event);
          if (event.deltaY < 0) this.setZoomIndex(this.cropIndex + 1, anchor);
          else this.setZoomIndex(this.cropIndex - 1, anchor);
          return;
        }
        if (this.scale === 1) return;              // nothing to pan when fitted
        const { w, h } = this._contentBox();
        this.centre.x += event.deltaX / (w * this.scale);
        this.centre.y += event.deltaY / (h * this.scale);
        this._clampCentre();
        this._applyTransform();
      },
      { passive: false },
    );

    window.addEventListener("resize", () => this._applyTransform());
  }

  /* -------------------------------------------------------- liveness watch */

  /**
   * Notice a stream that has died without the <img> firing an error.
   *
   * Uses the load event's timestamp and nothing else. An earlier version
   * inferred this by drawing the frame to a canvas and watching a derived
   * number: drawImage on an MJPEG <img> returns a stale bitmap (the browser
   * re-decodes on its own schedule), so it disagreed with the load event and
   * the two oscillated, flashing "stalled" while frames were visibly arriving.
   */
  _checkLiveness() {
    if (!this.running || this.busy) return;
    if (this.state === "live" && performance.now() - this._lastFrameAt > STALL_MS) {
      this._setState("stalled");
    }
  }
}
