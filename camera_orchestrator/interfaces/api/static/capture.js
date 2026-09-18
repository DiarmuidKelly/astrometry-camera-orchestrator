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

  /**
   * The frame counts the user has actually asked for, in phase order.
   *
   * One field per kind rather than a count plus a kind dropdown: "32 frames"
   * and "32 lights" were the same thing reached two ways, which read as two
   * different settings. Flats are capture-only — they need an evenly lit
   * source, so they are not a phase in a night's sequence.
   */
  counts() {
    const v = this.values();
    return {
      light: v.lights || 0,
      dark: v.darks || 0,
      bias: v.bias || 0,
      flat: v.flats || 0,
    };
  }

  /** Kinds with a non-zero count, in the order they would be shot. */
  requestedKinds() {
    return Object.entries(this.counts())
      .filter(([, n]) => n > 0)
      .map(([kind]) => kind);
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
    const counts = this.counts();
    const parts = Object.entries(counts)
      .filter(([, n]) => n > 0)
      .map(([name, n]) => `${n} ${name}`);

    if (kind === "sequence") {
      const phases = parts.filter((p) => !p.endsWith("flat"));
      if (!phases.length) return "sequence — set a frame count first";
      return `sequence — ${phases.join(" / ")} × ${exposure}${iso} · ${destination}`;
    }
    if (!parts.length) return "capture — set a frame count first";
    // Capture fires ONE kind per job, so promise exactly that — this line is
    // what the user commits to with Enter and it has to be literally true.
    const [firstKind] = this.requestedKinds();
    const rest = parts.length - 1;
    const tail = rest > 0 ? ` (+${rest} more kind${rest > 1 ? "s" : ""} — use Sequence)` : "";
    return (
      `capture — ${counts[firstKind]} ${firstKind} × ${exposure}${iso}`
      + ` · ${destination}${tail}`
    );
  }

  /** "32 x 2s = 1m 04s on target" — the phrasing the user reasons in.
   *
   * Only lights count toward integration time: darks and bias are calibration,
   * and adding them would inflate the number that decides whether a target has
   * had enough exposure.
   */
  describeIntegration() {
    const v = this.values();
    const seconds = exposureSeconds(v);
    const frames = this.primary === "align" ? 1 : this.counts().light;
    if (!frames) return "no lights set";
    if (seconds === null) return `${frames} × ${v.shutter || "?"} = —`;
    const total = `${frames} × ${formatShutter(v)} = ${formatDuration(frames * seconds)}`;
    return this.primary === "align" ? total : `${total} on target`;
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
      // Capture shoots ONE kind per job. With several counts set, the first in
      // phase order goes now; the rest are what `sequence` is for.
      const [first] = this.requestedKinds();
      if (!first) {
        this.onError(new Error("Set a frame count first — lights, darks, bias or flats."));
        return null;
      }
      body = {
        ...shared,
        image_format: v.image_format || null,
        count: this.counts()[first],
        kind: first,
        download: Boolean(v.download),
        ...(v.select ? { select: v.select } : {}),
      };
      submit = startCapture;
    }

    this.setBusy(true);
    let job;
    try {
      job = await submit(omitEmpty(body));
    } catch (err) {
      // Nothing was created, so this panel owns the buttons again.
      this.setBusy(false);
      this.onError(err);
      return null;
    }
    // From here the JOB LAYER owns the disabled state — it disables on track()
    // and re-enables when the job reaches a terminal state. Clearing busy here
    // (the old `finally`) re-opened the buttons ~130 ms after the click, before
    // the next socket push could disable them again, and a second click inside
    // that window fired a second, unwanted capture.
    try {
      this.onJob(job, body);
    } catch (err) {
      this.setBusy(false); // the handover failed; don't strand the buttons
      this.onError(err);
    }
    return job;
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

/**
 * Drop keys the user left blank so the server applies its own defaults.
 *
 * Sending `null` is only equivalent to omitting for fields typed Optional. A
 * field with a non-null default (`select`, `count`, `kind`) rejects null with a
 * 422, which is how the capture form silently 422'd against the real server
 * while working fine against mock.js — the mock does not validate bodies.
 *
 * Deliberately NOT applied to the config PUT: there `null` is meaningful
 * (`focal_mm: null` means "read the focal length from EXIF"), and stripping it
 * would change what the user asked for.
 */
function omitEmpty(body) {
  return Object.fromEntries(
    Object.entries(body).filter(([, v]) => v !== null && v !== undefined),
  );
}
