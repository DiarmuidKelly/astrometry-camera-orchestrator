/*
 * capture.js — the capture settings bar.
 *
 * Settings are set once at the start of a target and then the user is on the
 * keyboard, so this is deliberately a compact bar rather than a big form. It
 * owns the values (persisted via settings.js), the derived integration-time
 * readout, and turning those values into the three job request bodies the API
 * accepts. It knows nothing about job progress.
 *
 * `describePrimary()` is what makes Enter-to-fire safe: the live view spells
 * out exactly what Enter will do — "Enter → fire 32 × 2s @ ISO 3200" — so an
 * exposure is never committed blind in the dark.
 */

import { startAlign, startCapture, startSequence } from "./api.js";
import { loadSettings, saveSettings } from "./settings.js";
import { formatDuration, qs, qsa, shutterToSeconds } from "./util.js";

export class CapturePanel {
  /**
   * @param {HTMLElement} root
   * @param {{onJob: (job, body) => void, onChange: () => void,
   *          onError: (err) => void}} handlers
   */
  constructor(root, { onJob, onChange, onError } = {}) {
    this.root = root;
    this.form = qs("form", root);
    this.onJob = onJob || (() => {});
    this.onChange = onChange || (() => {});
    this.onError = onError || (() => {});
    this.integrationEl = qs("[data-integration]", root);
    this.busy = false;

    this._restore();
    this._bind();
    this._recalculate();
  }

  /* --------------------------------------------------------------- values */

  /** Read the form into a plain object keyed by input name. */
  values() {
    const out = {};
    for (const field of qsa("[name]", this.form)) {
      out[field.name] =
        field.type === "checkbox"
          ? field.checked
          : field.type === "number"
            ? numberOrNull(field.value)
            : field.value.trim();
    }
    return out;
  }

  _restore() {
    const saved = loadSettings();
    for (const field of qsa("[name]", this.form)) {
      if (!(field.name in saved)) continue;
      const value = saved[field.name];
      if (field.type === "checkbox") field.checked = Boolean(value);
      else if (value !== null && value !== undefined) field.value = value;
    }
  }

  _bind() {
    const onEdit = () => {
      saveSettings(this.values());
      this._recalculate();
    };
    this.form.addEventListener("input", onEdit);
    this.form.addEventListener("change", onEdit);
    // Several actions live on this form, so nothing is a real submit.
    this.form.addEventListener("submit", (event) => event.preventDefault());

    for (const button of qsa("[data-action]", this.root)) {
      button.addEventListener("click", () => this.run(button.dataset.action));
    }
  }

  /* ------------------------------------------------------------- readouts */

  /** Which verb Enter fires. Chosen in the bar so the prompt is unambiguous. */
  get primary() {
    return this.values().primary || "capture";
  }

  /**
   * Human description of the primary action, e.g.
   *   "fire 32 × 2s @ ISO 3200 · card only"
   * Rendered next to "Enter →" in the live view.
   */
  describePrimary() {
    const v = this.values();
    const kind = this.primary;
    const exposure = formatShutter(v);
    const iso = v.iso ? ` @ ISO ${v.iso}` : "";
    const destination = v.download ? "download to disk" : "card only";

    if (kind === "align") {
      return `align — 1 × ${exposure}${iso}, then plate solve`;
    }
    if (kind === "sequence") {
      return (
        `sequence — ${v.lights || 0} light / ${v.darks || 0} dark / `
        + `${v.bias || 0} bias × ${exposure}${iso} · ${destination}`
      );
    }
    return `fire ${v.count || 1} × ${exposure}${iso} · ${destination}`;
  }

  /** "32 × 2s = 1m 04s" — the phrasing the user reasons in. */
  describeIntegration() {
    const v = this.values();
    const seconds = exposureSeconds(v);
    const frames = this.primary === "sequence" ? v.lights || 0 : v.count || 1;
    if (seconds === null) return `${frames} × ${v.shutter || "?"} = —`;
    return (
      `${frames} × ${formatShutter(v)} = ${formatDuration(frames * seconds)}`
      + (this.primary === "sequence" ? " on target" : "")
    );
  }

  _recalculate() {
    this.integrationEl.textContent = this.describeIntegration();
    this.onChange();
  }

  /* ----------------------------------------------------------- submission */

  setBusy(busy) {
    this.busy = busy;
    for (const button of qsa("[data-action]", this.root)) button.disabled = busy;
  }

  /** Fire the primary action — the Enter key path. */
  fire() {
    return this.run(this.primary);
  }

  async run(kind) {
    if (this.busy) return null;
    const v = this.values();
    const shared = {
      out_dir: v.out_dir || null,
      iso: v.iso || null,
      shutter: v.shutter || null,
      aperture: v.aperture || null,
      bulb_seconds: v.bulb_seconds ?? null,
      name: v.name || null,
    };

    let body;
    let submit;
    if (kind === "align") {
      body = { ...shared, force: Boolean(v.force) };
      submit = startAlign;
    } else if (kind === "sequence") {
      body = {
        ...shared,
        lights: v.lights ?? 0,
        darks: v.darks ?? 0,
        bias: v.bias ?? 0,
        download: Boolean(v.download),
      };
      submit = startSequence;
    } else {
      body = {
        ...shared,
        image_format: v.image_format || null,
        count: v.count ?? 1,
        kind: v.kind || "light",
        download: Boolean(v.download),
        select: v.select || null,
      };
      submit = startCapture;
    }

    this.setBusy(true);
    try {
      const job = await submit(body);
      this.onJob(job, body);
      return job;
    } catch (err) {
      this.onError(err);
      return null;
    } finally {
      this.setBusy(false);
    }
  }
}

/* ------------------------------------------------------------------ pure */

/** Bulb overrides shutter when set — that is how the CLI behaves. */
function exposureSeconds(values) {
  if (values.bulb_seconds) return Number(values.bulb_seconds);
  return shutterToSeconds(values.shutter);
}

function formatShutter(values) {
  if (values.bulb_seconds) return `${values.bulb_seconds}s bulb`;
  const seconds = shutterToSeconds(values.shutter);
  if (seconds === null) return values.shutter || "?";
  return seconds >= 1 ? `${seconds}s` : `${values.shutter}s`;
}

function numberOrNull(raw) {
  if (raw === "" || raw === null || raw === undefined) return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
}
