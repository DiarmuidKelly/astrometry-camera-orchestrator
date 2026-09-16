/*
 * settings.js — localStorage persistence for the capture form.
 *
 * ISO, shutter, session name and frame counts are the same handful of values
 * all night; retyping them per command is one of the pains this UI exists to
 * remove. Anything the user types is written back on change and restored on
 * load. Deliberately schema-less: unknown keys round-trip untouched so adding
 * a field to the form needs no migration.
 */

const KEY = "camera-orchestrator.capture.v1";

export const DEFAULTS = {
  // Which verb the Enter key fires. The live view spells this out, so the
  // choice is never ambiguous at the moment of committing an exposure.
  primary: "capture",
  name: "",
  iso: "3200",
  shutter: "2",
  aperture: "",
  bulb_seconds: "",
  image_format: "",
  lights: 32,
  darks: 12,
  bias: 20,
  flats: 0,
  download: false,
  out_dir: "",
  select: "",
  force: false,
};

export function loadSettings() {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return { ...DEFAULTS };
    return { ...DEFAULTS, ...JSON.parse(raw) };
  } catch {
    // Private-mode or corrupt value — fall back rather than break the page.
    return { ...DEFAULTS };
  }
}

export function saveSettings(values) {
  try {
    localStorage.setItem(KEY, JSON.stringify(values));
  } catch {
    /* storage unavailable; the form still works for this session */
  }
}
