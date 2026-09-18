/*
 * util.js — small DOM + formatting helpers shared across modules.
 * No framework; these are the four or five things a framework would give us.
 */

/** querySelector with a loud failure — a typo'd id should not fail silently. */
export function qs(selector, root = document) {
  const el = root.querySelector(selector);
  if (!el) throw new Error(`Missing element: ${selector}`);
  return el;
}

export const qsa = (selector, root = document) =>
  Array.from(root.querySelectorAll(selector));

/**
 * Create an element. `attrs.class`, `attrs.text` and `attrs.html` are special;
 * everything else is set as an attribute. Children may be nodes or strings.
 */
export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

/** Replace a container's children in one shot (no incremental layout shift). */
export function replaceChildren(container, ...nodes) {
  container.replaceChildren(...nodes.flat().filter(Boolean));
}

/* ------------------------------------------------------------ formatting */

export function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["kB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`;
}

/** Seconds -> "1m 04s" / "2h 11m". Used for elapsed time and integration. */
export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) {
    return "—";
  }
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m ${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}

export function formatTimestamp(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * Parse a gphoto2 shutter-speed choice string into seconds.
 * Accepts "2", "0.5", "1/60", "1/4000" and the trailing-quote form '2"'.
 * Returns null when it cannot be read as a number — bulb, for instance.
 */
export function shutterToSeconds(value) {
  if (value === null || value === undefined) return null;
  const text = String(value).trim().replace(/["s]$/i, "");
  if (!text) return null;
  if (text.includes("/")) {
    const [num, den] = text.split("/", 2).map(Number);
    if (!Number.isFinite(num) || !Number.isFinite(den) || den === 0) return null;
    return num / den;
  }
  const seconds = Number(text);
  return Number.isFinite(seconds) ? seconds : null;
}

/** Degrees -> "02h 42m 41s" right ascension. */
export function formatRa(deg) {
  if (!Number.isFinite(deg)) return "—";
  const hours = ((deg % 360) + 360) % 360 / 15;
  const h = Math.floor(hours);
  const minutes = (hours - h) * 60;
  const m = Math.floor(minutes);
  const s = (minutes - m) * 60;
  return `${pad(h)}h ${pad(m)}m ${pad(Math.round(s))}s`;
}

/** Degrees -> "+41° 16' 09"" declination. */
export function formatDec(deg) {
  if (!Number.isFinite(deg)) return "—";
  const sign = deg < 0 ? "-" : "+";
  const abs = Math.abs(deg);
  const d = Math.floor(abs);
  const minutes = (abs - d) * 60;
  const m = Math.floor(minutes);
  const s = (minutes - m) * 60;
  return `${sign}${pad(d)}° ${pad(m)}' ${pad(Math.round(s))}"`;
}

const pad = (n) => String(n).padStart(2, "0");

/**
 * Parse an angle into decimal degrees.
 *
 * Catalogues and planetarium apps hand out sexagesimal — "18h 29m 38.6s" for
 * RA, "-25° 24' 23.6\"" for Dec — but the config stores decimal degrees, and
 * decimal degrees stays the source of truth. This accepts either and always
 * returns degrees.
 *
 *  - An `h` marker means hours, so the result is multiplied by 15.
 *  - A bare number passes through unchanged.
 *  - Empty input returns null — meaningful here, since a null RA/Dec is a
 *    blind all-sky solve rather than "zero degrees".
 *
 * @returns {number|null|undefined} degrees, null for blank, undefined if
 *   the text could not be parsed at all.
 */
export function parseAngle(text) {
  if (text === null || text === undefined) return null;
  const raw = String(text).trim();
  if (!raw) return null;

  const plain = Number(raw);
  if (Number.isFinite(plain)) return plain;

  const isHours = /h/i.test(raw);
  const negative = /^[-−]/.test(raw);
  // Pull out the numbers in order: degrees/hours, minutes, seconds.
  const parts = raw.match(/\d+(?:\.\d+)?/g);
  if (!parts || parts.length === 0) return undefined;

  const [a = 0, b = 0, c = 0] = parts.map(Number);
  let value = Math.abs(a) + b / 60 + c / 3600;
  if (isHours) value *= 15; // 1h of RA = 15°
  return negative ? -value : value;
}

/** Degrees -> a sexagesimal echo, so a pasted value can be eyeballed. */
export function describeAngle(deg, { hours = false } = {}) {
  if (!Number.isFinite(deg)) return "";
  return hours ? formatRa(deg) : formatDec(deg);
}

/** Trailing-edge debounce, for input handlers that hit the network. */
export function debounce(fn, delayMs = 250) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delayMs);
  };
}
