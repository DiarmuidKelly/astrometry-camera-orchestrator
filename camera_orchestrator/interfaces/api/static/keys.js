/*
 * keys.js — global keyboard bindings.
 *
 * The primary workflow is done outdoors, in the dark, with one hand on the
 * lens's focus ring: aim → zoom in on a star → focus by hand → press Enter to
 * fire. So the bindings are global (no clicking into the live view first) and
 * they match the muscle memory of the OpenCV reference tool:
 *
 *   + / =   zoom in          ↑ ↓ ← →   pan the zoomed view
 *   -       zoom out         Enter     fire / confirm
 *   0       reset zoom       q / Esc   stop live view
 *   f       reset focus peak
 *
 * The one rule that matters: never steal a key from a text field. Typing "2"
 * into the shutter box must not zoom, and Enter in a field must not fire an
 * exposure.
 */

/** Elements whose keystrokes belong to them, not to us. */
const TEXT_ENTRY = "input, textarea, select, button, a[href], [contenteditable='true']";

function isTextEntry(target) {
  return Boolean(target && target.closest && target.closest(TEXT_ENTRY));
}

const PANS = {
  ArrowLeft: [-1, 0],
  ArrowRight: [1, 0],
  ArrowUp: [0, -1],
  ArrowDown: [0, 1],
};

/**
 * Install the global handler.
 *
 * @param {object} actions
 *  @param {() => void} actions.zoomIn
 *  @param {() => void} actions.zoomOut
 *  @param {() => void} actions.zoomReset
 *  @param {(dx: number, dy: number) => void} actions.pan  units of ±1
 *  @param {() => void} actions.fire        Enter, when nothing is pending
 *  @param {() => boolean} actions.confirm  Enter, when a prompt is open;
 *                                          returns true if it handled the key
 *  @param {() => void} actions.stop        q / Esc
 *  @param {() => void} actions.resetFocus
 * @returns {() => void} uninstall
 */
export function installKeyBindings(actions) {
  const handler = (event) => {
    if (event.metaKey || event.ctrlKey || event.altKey) return;

    // A pending confirmation outranks everything, including a focused field:
    // Enter means "I'm ready, proceed", the same as it does in the fire loop.
    if (event.key === "Enter" && actions.confirm?.()) {
      event.preventDefault();
      return;
    }

    if (isTextEntry(event.target)) {
      // One exception: Escape blurs the field, so the key loop is reachable
      // again without hunting for somewhere neutral to click.
      if (event.key === "Escape") event.target.blur();
      return;
    }

    if (event.key === "+" || event.key === "=") {
      actions.zoomIn?.();
    } else if (event.key === "-" || event.key === "_") {
      actions.zoomOut?.();
    } else if (event.key === "0") {
      actions.zoomReset?.();
    } else if (PANS[event.key]) {
      const [dx, dy] = PANS[event.key];
      actions.pan?.(dx, dy);
    } else if (event.key === "Enter") {
      actions.fire?.();
    } else if (event.key === "q" || event.key === "Q" || event.key === "Escape") {
      actions.stop?.();
    } else if (event.key === "f" || event.key === "F") {
      actions.resetFocus?.();
    } else {
      return; // not ours — let it through
    }
    event.preventDefault();
  };

  document.addEventListener("keydown", handler);
  return () => document.removeEventListener("keydown", handler);
}
