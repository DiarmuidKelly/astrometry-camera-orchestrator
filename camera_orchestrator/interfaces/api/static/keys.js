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
 *   0       reset zoom       q         stop live view
 *   Esc     dismiss / blur
 *
 * The one rule that matters: never steal a key from a TEXT FIELD. Typing "2"
 * into the shutter box must not zoom, and Enter in a field must not fire an
 * exposure.
 *
 * Buttons are deliberately NOT in that set. A button keeps focus after a click,
 * so treating it as "theirs" meant one click on Start or on a zoom button
 * silently handed every key to that button for the rest of the session: Enter
 * re-toggled Start instead of firing what the prompt bar promised. Intercepting
 * Enter on a focused button is exactly the behaviour we want, and a pointer
 * click drops focus (see below) so native activation cannot double-fire either.
 */

/** Elements whose keystrokes belong to them, not to us. */
const TEXT_ENTRY = "input, textarea, select, [contenteditable='true']";

/** Controls that must not keep focus after a pointer click. */
const CLICK_FOCUSABLE = "button, a[href], summary, [role='button']";

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
 *  @param {() => void} actions.stop        q
 *  @param {() => void} actions.dismiss     Escape — close the error banner and
 *                                          drop focus; never stops the stream
 * @returns {() => void} uninstall
 */
export function installKeyBindings(actions) {
  // A click leaves the button focused, and a focused button turns Enter and
  // Space into "press me again" — not what the prompt bar says will happen.
  // `detail > 0` means a genuine pointer press; a keyboard-driven click
  // reports 0, so tab-and-Enter navigation is untouched.
  const dropFocus = (event) => {
    if (!event.detail) return;
    const el = event.target?.closest?.(CLICK_FOCUSABLE);
    if (el && el === document.activeElement) el.blur();
  };

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
    } else if (event.key === "q" || event.key === "Q") {
      actions.stop?.();
    } else if (event.key === "Escape") {
      // Escape is the universal "dismiss", not "stop the camera": stopping live
      // view from the one key people press to close things is a trap in the
      // dark. `q` remains the stop key, and the legend says so.
      actions.dismiss?.();
    } else {
      return; // not ours — let it through
    }
    event.preventDefault();
  };

  document.addEventListener("keydown", handler);
  document.addEventListener("click", dropFocus);
  return () => {
    document.removeEventListener("keydown", handler);
    document.removeEventListener("click", dropFocus);
  };
}
