/*
 * focus.js — the sharpness metric behind the live-view focus readout.
 *
 * METRIC: variance of the Laplacian.
 *
 *   1. Take the region of the frame currently visible in the viewport (so the
 *      number tracks what the user is actually looking at when zoomed into a
 *      corner star, not the whole sensor).
 *   2. Downsample it into a fixed-size buffer. Fixed size keeps the score
 *      comparable across zoom levels and stops the cost growing with frame size.
 *   3. Convert to luminance (Rec. 601 weights).
 *   4. Convolve with the 4-neighbour Laplacian kernel
 *          0  1  0
 *          1 -4  1
 *          0  1  0
 *      which responds to second-order intensity change — i.e. edges.
 *   5. Report the variance of that response.
 *
 * Why this one: a defocused star spreads its light over more pixels and its
 * edges get shallower, so the Laplacian response collapses; a sharp star is a
 * near-delta function and the response spikes. Variance (rather than mean
 * absolute value) rewards those few strong responses heavily, which is exactly
 * the right bias for a star field that is mostly black sky. It is the standard
 * Pech-Pacheco focus measure, needs no thresholding or star detection, and is
 * a handful of array passes — cheap enough to run several times a second.
 *
 * CAVEAT (why the absolute number is not meaningful): the score depends on the
 * scene, exposure and zoom. Only its *trend* matters. The user turns the focus
 * ring and watches for the peak — which is why we keep a peak-hold and a
 * sparkline rather than presenting a target value.
 */

/** Edge length of the analysis buffer. 192² ≈ 37k pixels — plenty of signal. */
const SAMPLE_SIZE = 192;

export class FocusMeter {
  constructor() {
    this._canvas = document.createElement("canvas");
    this._canvas.width = SAMPLE_SIZE;
    this._canvas.height = SAMPLE_SIZE;
    // willReadFrequently: we call getImageData every sample; without it some
    // browsers keep the canvas GPU-side and each read stalls the pipeline.
    this._ctx = this._canvas.getContext("2d", { willReadFrequently: true });
    this._lum = new Float32Array(SAMPLE_SIZE * SAMPLE_SIZE);
  }

  /**
   * Score the given source region.
   *
   * @param {CanvasImageSource} source  usually the live-view <img>
   * @param {object} crop  {sx, sy, sw, sh} in source pixels — the visible area
   * @returns {number|null} sharpness score, or null if the frame is not ready
   */
  measure(source, crop) {
    const { sx, sy, sw, sh } = crop;
    if (!(sw > 1) || !(sh > 1)) return null;

    try {
      this._ctx.drawImage(
        source, sx, sy, sw, sh, 0, 0, SAMPLE_SIZE, SAMPLE_SIZE,
      );
    } catch {
      // Frame not decoded yet, or a cross-origin source. Either way: no score.
      return null;
    }

    let pixels;
    try {
      pixels = this._ctx.getImageData(0, 0, SAMPLE_SIZE, SAMPLE_SIZE).data;
    } catch {
      return null; // tainted canvas — should not happen same-origin
    }

    const lum = this._lum;
    for (let i = 0, p = 0; i < lum.length; i += 1, p += 4) {
      // Rec. 601 luma. Integer-ish weights; exact coefficients are irrelevant
      // to a relative sharpness comparison.
      lum[i] = 0.299 * pixels[p] + 0.587 * pixels[p + 1] + 0.114 * pixels[p + 2];
    }

    // Welford-free two-pass variance over the interior (border has no
    // full neighbourhood, so it is skipped rather than edge-padded).
    let sum = 0;
    let sumSq = 0;
    let n = 0;
    for (let y = 1; y < SAMPLE_SIZE - 1; y += 1) {
      const row = y * SAMPLE_SIZE;
      for (let x = 1; x < SAMPLE_SIZE - 1; x += 1) {
        const i = row + x;
        const laplacian =
          4 * lum[i] - lum[i - 1] - lum[i + 1]
          - lum[i - SAMPLE_SIZE] - lum[i + SAMPLE_SIZE];
        sum += laplacian;
        sumSq += laplacian * laplacian;
        n += 1;
      }
    }
    if (n === 0) return null;
    const mean = sum / n;
    const variance = sumSq / n - mean * mean;
    return variance > 0 ? variance : 0;
  }
}

/**
 * Fixed-length rolling history plus a peak-hold, rendered as a sparkline.
 * Kept separate from the metric so the maths above stays pure.
 */
export class FocusHistory {
  constructor(capacity = 120) {
    this.capacity = capacity;
    this.values = [];
    this.peak = 0;
  }

  push(value) {
    this.values.push(value);
    if (this.values.length > this.capacity) this.values.shift();
    if (value > this.peak) this.peak = value;
  }

  /** Called when the user changes zoom/pan — old scores are no longer alike. */
  reset() {
    this.values.length = 0;
    this.peak = 0;
  }

  get latest() {
    return this.values.length ? this.values[this.values.length - 1] : null;
  }

  /** Latest as a fraction of the session peak — drives the "% of best" bar. */
  get fractionOfPeak() {
    if (!this.peak || this.latest === null) return 0;
    return Math.min(1, this.latest / this.peak);
  }

  /**
   * Draw the history into a canvas. Scaled to the window max (not the global
   * peak) so small movements near a plateau are still visible.
   */
  render(canvas) {
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || 240;
    const height = canvas.clientHeight || 48;
    if (canvas.width !== width * dpr || canvas.height !== height * dpr) {
      canvas.width = width * dpr;
      canvas.height = height * dpr;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    if (this.values.length < 2) return;

    const max = Math.max(...this.values, 1e-6);
    const min = Math.min(...this.values);
    const span = Math.max(max - min, max * 0.05, 1e-6);
    const step = width / (this.capacity - 1);
    const yFor = (v) => height - 2 - ((v - min) / span) * (height - 4);

    ctx.beginPath();
    this.values.forEach((value, i) => {
      // Right-align: the newest sample sits at the right edge.
      const x = width - (this.values.length - 1 - i) * step;
      const y = yFor(value);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = "#ff5a3c";
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.stroke();

    // Peak marker: a faint line at the best score ever seen this session.
    if (this.peak >= min && this.peak <= max) {
      const y = yFor(this.peak);
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(width, y);
      ctx.strokeStyle = "rgba(255,90,60,0.35)";
      ctx.lineWidth = 1;
      ctx.stroke();
    }
  }
}

/**
 * Format a score for display. Raw variances run from ~1 to ~10⁵ depending on
 * the scene, so we show a rounded integer and let the sparkline carry nuance.
 */
export function formatScore(value) {
  if (value === null || value === undefined) return "—";
  if (value >= 1000) return Math.round(value).toLocaleString();
  if (value >= 10) return value.toFixed(0);
  return value.toFixed(1);
}
