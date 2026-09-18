/*
 * prompts.js — the full-attention physical-action prompt.
 *
 * A `sequence` runs lights → darks → bias. Darks and bias are worthless unless
 * the lens is capped, so the server genuinely blocks the run on
 * `state: "awaiting_confirmation"` until POST /api/jobs/{id}/confirm arrives.
 * That is a physical instruction to a person who may be several metres away in
 * the dark, walking back from the telescope — so it takes over the live-view
 * area rather than appearing as a toast or a status line.
 *
 * Two design rules here:
 *  - Capped vs uncapped must be distinguishable WITHOUT reading the word, so
 *    each state gets its own icon and its own colour (deep red / amber — both
 *    night-vision safe; never a large light field).
 *  - The wording is the server's `prompt.message`, verbatim, so the CLI and the
 *    UI cannot drift. Only the headline, icon and framing are ours.
 */

import { el, formatDuration, replaceChildren } from "./util.js";

/**
 * Work out what the user is being asked to do physically.
 * Derived from prompt.kind + prompt.message so a server-side rewording still
 * lands on the right icon.
 */
export function classifyPrompt(prompt) {
  const text = `${prompt?.kind || ""} ${prompt?.message || ""}`.toLowerCase();

  let mode = "generic";
  if (/uncover|remove the cap|remove the lens cap|cap off|uncap/.test(text)) {
    mode = "uncover";
  } else if (/cover|cap on|lens cap|capped|block the lens/.test(text)) {
    mode = "cover";
  }

  let phase = null;
  if (/dark/.test(text)) phase = "dark";
  else if (/bias/.test(text)) phase = "bias";
  else if (/flat/.test(text)) phase = "flat";
  else if (/light/.test(text)) phase = "light";

  // Lights are the only uncapped phase; if the server names a phase but not the
  // action, infer it. (The CLI never prompts before lights, but be safe.)
  if (mode === "generic" && phase) {
    mode = phase === "light" ? "uncover" : "cover";
  }
  return { mode, phase };
}

const HEADLINES = {
  cover: "COVER THE LENS",
  uncover: "UNCOVER THE LENS",
  generic: "ACTION NEEDED",
};

/** Inline SVG so it works with no network and no icon font. */
function icon(mode) {
  const svg = (inner) =>
    el("div", {
      class: "prompt-icon",
      "data-mode": mode,
      "aria-hidden": "true",
      html: `<svg viewBox="0 0 100 100" width="100%" height="100%">${inner}</svg>`,
    });

  if (mode === "cover") {
    // A capped lens: a filled disc inside the barrel ring.
    return svg(`
      <circle cx="50" cy="50" r="42" fill="none" stroke="currentColor" stroke-width="6"/>
      <circle cx="50" cy="50" r="31" fill="currentColor"/>
      <rect x="20" y="46" width="60" height="8" rx="4" fill="#0a0406"/>
    `);
  }
  if (mode === "uncover") {
    // An open aperture: ring plus blades, hollow centre.
    return svg(`
      <circle cx="50" cy="50" r="42" fill="none" stroke="currentColor" stroke-width="6"/>
      <circle cx="50" cy="50" r="22" fill="none" stroke="currentColor" stroke-width="5"/>
      <g stroke="currentColor" stroke-width="5" stroke-linecap="round">
        <line x1="50" y1="8" x2="50" y2="28"/>
        <line x1="50" y1="72" x2="50" y2="92"/>
        <line x1="8" y1="50" x2="28" y2="50"/>
        <line x1="72" y1="50" x2="92" y2="50"/>
      </g>
    `);
  }
  return svg(`
    <circle cx="50" cy="50" r="42" fill="none" stroke="currentColor" stroke-width="6"/>
    <rect x="45" y="24" width="10" height="34" rx="5" fill="currentColor"/>
    <circle cx="50" cy="72" r="6" fill="currentColor"/>
  `);
}

/** "Next: 16 dark frames @ ISO 3200, 2s" — best-effort from the request body. */
function describeNext(phase, context) {
  if (!phase) return null;
  const counts = { dark: context?.darks, bias: context?.bias, light: context?.lights };
  const count = counts[phase];
  const bits = [];
  bits.push(count ? `${count} ${phase} frames` : `${phase} frames`);
  const settings = [];
  if (context?.iso) settings.push(`ISO ${context.iso}`);
  if (context?.bulb_seconds) settings.push(`${context.bulb_seconds}s bulb`);
  else if (context?.shutter) settings.push(`${context.shutter}s`);
  return `Next: ${bits.join("")}${settings.length ? ` @ ${settings.join(", ")}` : ""}`;
}

export class PhysicalPrompt {
  /**
   * @param {HTMLElement} root  the takeover container (hidden when idle)
   */
  constructor(root) {
    this.root = root;
    this.jobId = null;
    this.token = null;
    this.since = null;
    this._onConfirm = null;
    this._onCancel = null;
    this._elapsedEl = null;
    // The waiting time is shown so a forgotten prompt is obviously a stalled
    // run and not a crash — the backend will eventually time the job out.
    setInterval(() => this._tickElapsed(), 1000);
  }

  get isOpen() {
    return this.jobId !== null;
  }

  /**
   * @param {object} job    the job in `awaiting_confirmation`
   * @param {object} opts   {context, onConfirm, onCancel}
   */
  show(job, { context = null, onConfirm, onCancel } = {}) {
    this._onConfirm = onConfirm;
    this._onCancel = onCancel;
    if (this.jobId === job.id && this.token === job.prompt?.token) return; // already up
    this.jobId = job.id;
    // Echoed back on confirm. A new phase mints a new token, so a prompt left on
    // screen from the previous phase cannot answer this one.
    this.token = job.prompt?.token ?? null;
    this.since = Date.now();

    const { mode, phase } = classifyPrompt(job.prompt);
    const next = describeNext(phase, context);
    this._elapsedEl = el("span", { class: "mono", text: "0s" });

    this.root.dataset.mode = mode;
    this.root.hidden = false;
    replaceChildren(
      this.root,
      el("div", { class: "prompt-inner", role: "alertdialog", "aria-modal": "false" }, [
        icon(mode),
        el("h2", { class: "prompt-headline", text: HEADLINES[mode] }),
        phase
          ? el("p", { class: "prompt-phase", text: `for ${phase} frames` })
          : null,
        // Verbatim server wording — the single source of truth for the words.
        el("p", { class: "prompt-message", text: job.prompt?.message || "" }),
        next ? el("p", { class: "prompt-next mono", text: next }) : null,
        el("div", { class: "prompt-waiting" }, [
          el("span", { class: "pulse-dot", "aria-hidden": "true" }),
          "Waiting for you — the sequence is paused (",
          this._elapsedEl,
          ")",
        ]),
        el("div", { class: "prompt-actions" }, [
          el("button", {
            class: "btn btn-huge",
            type: "button",
            text: "Ready — continue  ⏎",
            onclick: () => this.confirm(),
          }),
          el("button", {
            class: "btn btn-quiet",
            type: "button",
            text: "Cancel run",
            onclick: () => {
              const cancel = this._onCancel;
              this.hide();
              cancel?.();
            },
          }),
        ]),
        el("p", {
          class: "prompt-hint",
          text: "Press Enter when the lens is in the right state.",
        }),
      ]),
    );
    // Move focus to the big button so a tap-free confirm also works for
    // screen-reader and keyboard users arriving fresh at the page.
    this.root.querySelector(".btn-huge")?.focus({ preventScroll: true });
  }

  hide() {
    this.jobId = null;
    this.token = null;
    this.since = null;
    this.root.hidden = true;
    this.root.replaceChildren();
  }

  /** Enter handler. Returns true when it consumed the key. */
  confirm() {
    if (!this.isOpen) return false;
    const confirm = this._onConfirm;
    const id = this.jobId;
    const token = this.token;
    this.hide();
    confirm?.(id, token);
    return true;
  }

  _tickElapsed() {
    if (!this.isOpen || !this._elapsedEl) return;
    this._elapsedEl.textContent = formatDuration((Date.now() - this.since) / 1000);
  }
}
