/*
 * jobs.js — job tracking: the single job socket, progress, cancel, and
 * escalation of `awaiting_confirmation` to the full-screen physical prompt.
 *
 * ONE connection for every job, opened once here — see the note in api.js for
 * why a stream per job broke live view. A job in `awaiting_confirmation` blocks
 * the whole sequence server-side, so it is handed to prompts.js and takes over
 * the live-view area — it must not be possible to miss it and wonder why the run
 * stopped.
 *
 * The socket's snapshot is authoritative: every (re)connect overwrites what the
 * panel holds. That is deliberate. The previous per-job streams reported a drop
 * and then did nothing, so a job that finished while the connection was down
 * stayed non-terminal in the client forever — which left `isExposing` true and
 * live view paused for the rest of the night.
 */

import { connectJobs } from "./api.js";
import { PhysicalPrompt } from "./prompts.js";
import { el, formatDuration, formatTimestamp, qs, replaceChildren } from "./util.js";

const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);
const ACTIVE = new Set(["pending", "running", "awaiting_confirmation"]);

const STATE_LABELS = {
  pending: "Queued",
  running: "Running",
  awaiting_confirmation: "Waiting for you",
  succeeded: "Done",
  failed: "Failed",
  cancelled: "Cancelled",
};

export class JobsPanel {
  /**
   * @param {HTMLElement} root       the job list panel
   * @param {HTMLElement} promptRoot the takeover container over the live view
   * @param {object} handlers  {onActivityChange, onJobFinished, onStateChange,
   *                            onError}
   */
  constructor(root, promptRoot, handlers = {}) {
    this.root = root;
    this.listEl = qs("[data-job-list]", root);
    this.emptyEl = qs("[data-job-empty]", root);
    this.prompt = new PhysicalPrompt(promptRoot);

    this.jobs = new Map(); // id -> job
    this.contexts = new Map(); // id -> the request body that created it
    this.dismissed = new Set(); // ids the user cleared; a snapshot must not resurrect them
    this.finished = new Set(); // ids already reported to onJobFinished (fires once)
    this.handlers = handlers;

    // One socket for the life of the page. It also replaces the old start-up
    // GET /api/jobs: the first frame is a snapshot, so a reload mid-sequence
    // picks the run back up with no extra request.
    this.socket = connectJobs({
      onSnapshot: (jobs) => this._onSnapshot(jobs),
      onJob: (job) => job && this._onJob(job),
      onStatus: (connected) => {
        this.connected = connected;
      },
      onError: (err) => this.handlers.onError?.(err),
    });

    // Elapsed times tick locally; the server only pushes on state change.
    setInterval(() => this._renderList(), 1000);
  }

  /**
   * Note a job the UI has just started, with the request body that made it.
   *
   * No subscription happens here — the socket is already carrying every job, and
   * the server pushes this one the moment it is registered. The local copy just
   * means the list paints before the first push arrives.
   *
   * @param {object} job
   * @param {object} [context] the request body, used to describe the next phase
   */
  track(job, context = null) {
    if (context) this.contexts.set(job.id, context);
    this.dismissed.delete(job.id);
    this._onJob(job);
  }

  /** Replace local state wholesale from a (re)connect snapshot. */
  _onSnapshot(jobs) {
    const seen = new Set();
    for (const job of jobs || []) {
      seen.add(job.id);
      if (this.dismissed.has(job.id)) continue;
      this._record(job);
    }
    // A job the server has never heard of cannot exist: drop anything left over
    // from a previous process (a restart while the tab stayed open).
    for (const id of Array.from(this.jobs.keys())) {
      if (!seen.has(id)) {
        this.jobs.delete(id);
        this.contexts.delete(id);
      }
    }
    this._render();
  }

  _onJob(job) {
    if (this.dismissed.has(job.id)) return;
    this._record(job);
    this._render();
  }

  /** Store one job and fire the terminal-transition side effects, once. */
  _record(job) {
    this.jobs.set(job.id, job);
    if (!TERMINAL.has(job.state)) {
      this.finished.delete(job.id); // cannot happen server-side, but stays honest
      return;
    }
    if (this.finished.has(job.id)) return;
    this.finished.add(job.id);
    if (this.prompt.jobId === job.id) this.prompt.hide();
    this.handlers.onJobFinished?.(job);
  }

  /* ------------------------------------------------------------- queries */

  get activeJob() {
    // Most recent active job: what the live-view prompt bar should describe.
    return Array.from(this.jobs.values())
      .filter((job) => ACTIVE.has(job.state))
      .sort((a, b) => new Date(b.created_at) - new Date(a.created_at))[0] || null;
  }

  get hasActiveJob() {
    return this.activeJob !== null;
  }

  /** True while the camera is physically busy (so live view must pause).
   *
   * Not simply "a camera job is running": an align spends most of its life
   * plate-solving in Docker with the camera idle and perfectly able to stream.
   * Treating the whole job as busy blanked live view for the entire solve —
   * ~15s staring at a paused stream with the camera visibly ready.
   *
   * The server is the authority (it keys off actual borrows of the camera); this
   * is the client-side approximation, so lean towards resuming early. The stream
   * skips frames by itself while the device really is held.
   */
  get isExposing() {
    const job = this.activeJob;
    if (!job) return false;
    if (job.state === "awaiting_confirmation") return false; // shutter is idle
    if (!["capture", "align", "sequence"].includes(job.kind)) return false;
    // Align reports its phase; once past the frame it is solving, not shooting.
    if (job.kind === "align") {
      const label = job.progress?.label || "";
      return /captur/i.test(label);
    }
    return true;
  }

  /** Enter handler, wired up in keys.js. Returns true if it consumed the key. */
  confirmPending() {
    return this.prompt.confirm();
  }

  /* ------------------------------------------------------------ rendering */

  _render() {
    this._renderList();
    this._renderPrompt();
    this.handlers.onActivityChange?.(this.hasActiveJob);
    this.handlers.onStateChange?.(this.activeJob);
  }

  _renderPrompt() {
    const waiting = Array.from(this.jobs.values()).find(
      (job) => job.state === "awaiting_confirmation" && job.prompt,
    );
    if (!waiting) {
      if (this.prompt.isOpen) this.prompt.hide();
      return;
    }
    this.prompt.show(waiting, {
      context: this.contexts.get(waiting.id),
      onConfirm: (id) => this._confirm(id),
      onCancel: () => this._cancel(waiting.id),
    });
  }

  _renderList() {
    const jobs = Array.from(this.jobs.values()).sort(
      (a, b) => new Date(b.created_at) - new Date(a.created_at),
    );
    this.emptyEl.hidden = jobs.length > 0;
    replaceChildren(this.listEl, jobs.map((job) => this._renderJob(job)));
  }

  _renderJob(job) {
    const progress = job.progress;
    const pct =
      progress && progress.total
        ? Math.round((progress.current / progress.total) * 100)
        : null;

    const started = job.started_at ? new Date(job.started_at) : null;
    const ended = job.ended_at ? new Date(job.ended_at) : null;
    const elapsed = started ? ((ended || new Date()) - started) / 1000 : null;

    return el("li", { class: "job", "data-state": job.state }, [
      el("div", { class: "job-head" }, [
        el("span", { class: "job-kind", text: job.kind }),
        el("span", {
          class: "badge",
          "data-state": job.state,
          text: STATE_LABELS[job.state] || job.state,
        }),
        el("span", {
          class: "job-elapsed mono",
          text: elapsed === null ? "—" : formatDuration(elapsed),
        }),
      ]),
      progress
        ? el("div", { class: "job-progress" }, [
            el(
              "div",
              {
                class: "bar",
                role: "progressbar",
                "aria-valuemin": "0",
                "aria-valuemax": "100",
                "aria-valuenow": String(pct ?? 0),
                "aria-label": `${job.kind} progress`,
              },
              [el("div", { class: "bar-fill", style: `width:${pct ?? 0}%` })],
            ),
            el("div", { class: "job-progress-text mono" }, [
              `${progress.current}/${progress.total}`,
              progress.label ? ` · ${progress.label}` : "",
            ]),
          ])
        : null,
      job.error
        ? el("p", { class: "job-error", role: "alert", text: job.error })
        : null,
      job.result ? el("p", { class: "job-result", text: summarise(job.result) }) : null,
      el("div", { class: "job-foot" }, [
        el("span", { class: "muted mono", text: formatTimestamp(job.created_at) }),
        ACTIVE.has(job.state)
          ? el("button", {
              class: "btn btn-quiet",
              type: "button",
              text: "Cancel",
              onclick: () => this._cancel(job.id),
            })
          : el("button", {
              class: "btn btn-quiet",
              type: "button",
              text: "Dismiss",
              onclick: () => {
                // Remembered, so the next snapshot does not bring it back.
                this.dismissed.add(job.id);
                this.jobs.delete(job.id);
                this.contexts.delete(job.id);
                this._render();
              },
            }),
      ]),
    ]);
  }

  /* -------------------------------------------------------------- actions */

  /* Commands go out on the same socket, so answering a prompt costs no extra
   * connection. api.js falls back to the POST route while it is reconnecting;
   * either way the resulting state arrives as a normal push. */

  async _confirm(id) {
    try {
      const job = await this.socket.confirm(id);
      if (job) this._onJob(job);
    } catch (err) {
      this.handlers.onError?.(err);
    }
  }

  async _cancel(id) {
    try {
      const job = await this.socket.cancel(id);
      if (job) this._onJob(job);
    } catch (err) {
      this.handlers.onError?.(err);
    }
  }
}

/** One-line summary of a job result; shapes vary per verb. */
function summarise(result) {
  if (!result || typeof result !== "object") return String(result ?? "");
  if (result.solved) {
    return "Plate solved — see the session browser for RA/Dec and the preview.";
  }
  const parts = [];
  if (result.out_dir) parts.push(result.out_dir);
  if (typeof result.files === "number") parts.push(`${result.files} files`);
  if (result.solved === false) parts.push("not solved");
  return parts.length ? parts.join(" · ") : "Complete.";
}
