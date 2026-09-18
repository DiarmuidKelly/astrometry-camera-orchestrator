/*
 * config.js — the settings panel over config.yaml.
 *
 * `search.ra_deg` / `dec_deg` change every time the user changes target — M31,
 * then M33, then Altair — and each change was previously a hand-edit of a YAML
 * file between commands. So that group gets top billing and everything else is
 * folded away under "Advanced": per-rig values, set once.
 *
 * Two rules drive the implementation:
 *
 *  1. NULL IS MEANINGFUL. `focal_mm: null` means "trust the EXIF focal length";
 *     `ra_deg: null` means a blind all-sky solve. A blank input must submit
 *     null — never 0 and never "". Coercing focal_mm to 0.0 would silently
 *     skew every plate-scale hint from then on, so "blank = auto" is written
 *     into the help text rather than left as folklore.
 *  2. UNKNOWN KEYS ROUND-TRIP. The PUT is a whole-document replace, so we edit
 *     a clone of exactly what the server gave us and only overwrite the fields
 *     the form owns.
 */

import { getConfig, getConfigSchema, putConfig } from "./api.js";
import { describeAngle, el, parseAngle, qs, replaceChildren } from "./util.js";

/**
 * The form definition. `help` here is the fallback; if /api/config/schema is
 * available its per-field descriptions win, so the UI cannot drift from the
 * Pydantic models.
 */
const GROUPS = [
  {
    key: "search",
    title: "Target search",
    primary: true, // rendered outside the Advanced fold
    blurb:
      "Where to look when plate-solving. Leave RA and Dec blank for a blind "
      + "all-sky solve — slower, but it needs no prior.",
    fields: [
      {
        key: "ra_deg",
        label: "RA",
        type: "angle",
        hours: true,
        nullable: true,
        placeholder: "blank = blind solve",
        help: 'Decimal degrees, or paste sexagesimal such as 0h 42m 44s.',
      },
      {
        key: "dec_deg",
        label: "Dec",
        type: "angle",
        hours: false,
        nullable: true,
        placeholder: "blank = blind solve",
        help: 'Decimal degrees, or paste sexagesimal such as +41° 16\' 09".',
      },
      {
        key: "radius_deg",
        label: "Search radius (°)",
        type: "number",
        step: "0.1",
        nullable: false,
        help: "How far around RA/Dec the solver may look.",
      },
    ],
  },
  {
    key: "solver",
    title: "Solver",
    fields: [
      { key: "image", label: "Docker image", type: "text", nullable: false },
      { key: "index_dir", label: "Index directory", type: "text", nullable: false },
      {
        key: "cpulimit",
        label: "CPU limit (s)",
        type: "number",
        step: "1",
        nullable: false,
        help: "Seconds astrometry.net may spend before giving up.",
      },
      {
        key: "mode",
        label: "Mode",
        type: "select",
        options: ["fast", "accurate"],
        nullable: false,
      },
    ],
  },
  {
    key: "optics",
    title: "Optics",
    blurb:
      "Used to hint the plate scale. Blank means the focal length is read from "
      + "the image EXIF instead.",
    fields: [
      {
        key: "focal_mm",
        label: "Focal length (mm)",
        type: "number",
        step: "0.1",
        nullable: true,
        placeholder: "blank = from EXIF",
      },
      {
        key: "sensor_width_mm",
        label: "Sensor width (mm)",
        type: "number",
        step: "0.1",
        nullable: true,
        placeholder: "blank = unknown",
        help:
          "Must match the body: 35.8 for the 5D Mark II (full-frame), 22.3 for "
          + "the M50 II (APS-C). A wrong value skews the plate-scale hint.",
      },
    ],
  },
  {
    key: "location",
    title: "Location",
    fields: [
      { key: "lat", label: "Latitude (°)", type: "number", step: "0.0001", nullable: true },
      { key: "lon", label: "Longitude (°)", type: "number", step: "0.0001", nullable: true },
    ],
  },
  {
    key: "logging",
    title: "Logging",
    fields: [
      {
        key: "level",
        label: "Level",
        type: "select",
        options: ["DEBUG", "INFO", "WARNING", "ERROR"],
        nullable: false,
      },
      {
        key: "format",
        label: "Format",
        type: "select",
        options: ["text", "json"],
        nullable: false,
      },
    ],
  },
  {
    key: "grab",
    title: "Grab",
    fields: [
      { key: "out_dir", label: "Output directory", type: "text", nullable: false },
      {
        key: "poll_interval",
        label: "Poll interval (s)",
        type: "number",
        step: "0.5",
        nullable: true,
        placeholder: "blank = default",
      },
    ],
  },
];

export class ConfigPanel {
  constructor(root, { onError } = {}) {
    this.root = root;
    this.onError = onError || (() => {});
    this.primaryEl = qs("[data-config-primary]", root);
    this.advancedEl = qs("[data-config-advanced]", root);
    this.statusEl = qs("[data-config-status]", root);
    this.submitEl = qs("[data-config-submit]", root);
    this.revertEl = qs("[data-config-revert]", root);

    this.config = null; // the server's document, untouched
    this.schema = null;
    this.inputs = new Map(); // "group.key" -> {input, echo, error, spec}

    this.submitEl.addEventListener("click", () => this.submit());
    this.revertEl.addEventListener("click", () => this._fill());
    this.submitEl.disabled = true;
  }

  async load() {
    this.statusEl.textContent = "Loading…";
    try {
      // The schema is optional; a 404 resolves to null rather than throwing.
      const [config, schema] = await Promise.all([getConfig(), getConfigSchema()]);
      this.config = config;
      this.schema = schema;
      this._build();
      this._fill();
      this.statusEl.textContent = "";
    } catch (err) {
      this.statusEl.textContent = `Could not load config: ${err.message}`;
      this.onError(err);
    }
  }

  /* ------------------------------------------------------------ rendering */

  _build() {
    this.inputs.clear();
    const primary = [];
    const advanced = [];
    for (const group of GROUPS) {
      // A group the server does not report is one this build does not have;
      // skip it rather than inventing keys on submit.
      if (!(group.key in (this.config || {}))) continue;
      (group.primary ? primary : advanced).push(this._group(group));
    }
    replaceChildren(this.primaryEl, primary);
    replaceChildren(this.advancedEl, advanced);
  }

  _group(group) {
    return el("fieldset", { class: "config-group" }, [
      el("legend", { text: group.title }),
      group.blurb ? el("p", { class: "muted small", text: group.blurb }) : null,
      el("div", { class: "field-grid" }, group.fields.map((spec) =>
        this._field(group.key, spec),
      )),
    ]);
  }

  _field(groupKey, spec) {
    const path = `${groupKey}.${spec.key}`;
    const id = `cfg-${groupKey}-${spec.key}`;
    const help = this._schemaDescription(groupKey, spec.key) || spec.help || "";

    let input;
    if (spec.type === "select") {
      input = el("select", { id, name: path },
        spec.options.map((option) => el("option", { value: option, text: option })),
      );
    } else {
      input = el("input", {
        id,
        name: path,
        // Angles are free text so sexagesimal can be pasted in.
        type: spec.type === "number" ? "number" : "text",
        step: spec.step,
        placeholder: spec.placeholder || (spec.nullable ? "blank = auto" : ""),
        autocomplete: "off",
        spellcheck: "false",
        inputmode: spec.type === "angle" ? "text" : undefined,
      });
    }

    const echo = el("span", { class: "field-echo mono muted" });
    const error = el("p", { class: "field-error", role: "alert", hidden: true });

    input.addEventListener("input", () => {
      this._updateEcho(path);
      this._refreshDirty();
    });
    input.addEventListener("change", () => {
      this._updateEcho(path);
      this._refreshDirty();
    });

    this.inputs.set(path, { input, echo, error, spec, groupKey });

    return el("div", { class: "field" }, [
      el("label", { for: id, text: spec.label }),
      input,
      echo,
      help ? el("p", { class: "field-help muted small", text: help }) : null,
      error,
    ]);
  }

  /** Resolve a field description out of the JSON Schema, $ref and all. */
  _schemaDescription(groupKey, fieldKey) {
    const schema = this.schema;
    if (!schema) return null;
    try {
      let node = schema.properties?.[groupKey];
      if (!node) return null;
      const ref = node.$ref || node.allOf?.[0]?.$ref;
      if (ref) {
        const name = ref.split("/").pop();
        node = schema.$defs?.[name] || schema.definitions?.[name];
      }
      const field = node?.properties?.[fieldKey];
      return field?.description || null;
    } catch {
      return null;
    }
  }

  /* --------------------------------------------------------------- values */

  /** Populate the form from the server document (also the Revert path). */
  _fill() {
    for (const [path, entry] of this.inputs) {
      const value = this._configValue(path);
      entry.input.value =
        value === null || value === undefined ? "" : String(value);
      entry.error.hidden = true;
      this._updateEcho(path);
    }
    this.submitEl.disabled = true;
    this.statusEl.textContent = "";
    this.statusEl.dataset.tone = "";
  }

  _configValue(path) {
    const [groupKey, fieldKey] = path.split(".");
    return this.config?.[groupKey]?.[fieldKey];
  }

  /**
   * Read one input back into a config value.
   * @returns {{value: any}|{error: string}}
   */
  _readField(path) {
    const { input, spec } = this.inputs.get(path);
    const raw = input.value.trim();

    if (raw === "") {
      if (spec.nullable) return { value: null }; // blank means null, not zero
      if (spec.type === "text") return { value: "" };
      return { error: "Required." };
    }

    if (spec.type === "angle") {
      const degrees = parseAngle(raw);
      if (degrees === undefined) return { error: "Not an angle." };
      return { value: degrees };
    }
    if (spec.type === "number") {
      const value = Number(raw);
      if (!Number.isFinite(value)) return { error: "Not a number." };
      return { value };
    }
    return { value: raw };
  }

  /** Show the sexagesimal/decimal echo so a pasted value is verifiable. */
  _updateEcho(path) {
    const entry = this.inputs.get(path);
    if (!entry || entry.spec.type !== "angle") return;
    const read = this._readField(path);
    if ("error" in read) {
      entry.echo.textContent = "?";
      return;
    }
    if (read.value === null) {
      entry.echo.textContent = "blind solve";
      return;
    }
    entry.echo.textContent =
      `${read.value.toFixed(4)}° · ${describeAngle(read.value, { hours: entry.spec.hours })}`;
  }

  _refreshDirty() {
    this.submitEl.disabled = !this._isDirty();
  }

  _isDirty() {
    for (const path of this.inputs.keys()) {
      const read = this._readField(path);
      if ("error" in read) return true; // let them submit to see the error
      const current = this._configValue(path);
      if (!sameValue(read.value, current)) return true;
    }
    return false;
  }

  /* ------------------------------------------------------------- submission */

  async submit() {
    if (!this.config) return;

    // Clone the server's document so any key this form does not know about
    // survives the round trip untouched.
    const next = structuredClone(this.config);
    let invalid = false;

    for (const [path, entry] of this.inputs) {
      const read = this._readField(path);
      if ("error" in read) {
        entry.error.textContent = read.error;
        entry.error.hidden = false;
        invalid = true;
        continue;
      }
      entry.error.hidden = true;
      const [groupKey, fieldKey] = path.split(".");
      next[groupKey] = next[groupKey] || {};
      next[groupKey][fieldKey] = read.value;
    }

    if (invalid) {
      this.statusEl.dataset.tone = "error";
      this.statusEl.textContent = "Fix the highlighted fields first.";
      return;
    }

    this.submitEl.disabled = true;
    this.statusEl.dataset.tone = "";
    this.statusEl.textContent = "Saving…";
    try {
      const result = await putConfig(next);
      this.config = result?.config || next;
      this._fill();
      this.statusEl.dataset.tone = "ok";
      this.statusEl.textContent = result?.path
        ? `Saved to ${result.path}`
        : "Saved.";
    } catch (err) {
      this.statusEl.dataset.tone = "error";
      this.statusEl.textContent = `Not saved: ${err.message}`;
      // Place Pydantic's per-field messages next to the offending inputs.
      for (const [path, message] of Object.entries(err.fields || {})) {
        const entry = this.inputs.get(path);
        if (!entry) continue;
        entry.error.textContent = message;
        entry.error.hidden = false;
      }
      this.submitEl.disabled = false;
      this.onError(err);
    }
  }
}

/** Null-aware equality; floats from a form are compared as numbers. */
function sameValue(a, b) {
  if (a === null || a === undefined) return b === null || b === undefined;
  if (b === null || b === undefined) return false;
  if (typeof a === "number" || typeof b === "number") {
    return Number(a) === Number(b);
  }
  return String(a) === String(b);
}
