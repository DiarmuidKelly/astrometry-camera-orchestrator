/*
 * app.js — composition root for the front end.
 *
 * Mirrors the backend's own convention: this is the only module that knows
 * about all the others. Each panel is self-contained and talks to the outside
 * world exclusively through api.js and the callbacks wired up here.
 *
 * The thing this file is really responsible for is the ONSCREEN PROMPT: at any
 * moment the live view must say either what Enter will do right now, or what
 * the camera is doing instead. That is assembled here because it is the one
 * statement that needs to combine capture settings, job state and camera state.
 */

import { getCameraStatus, getHealth, reconnectCamera } from "./api.js";
import { MOCK_ENABLED } from "./mock.js";
import { CapturePanel } from "./capture.js";
import { JobsPanel } from "./jobs.js";
import { LiveView } from "./liveview.js";
import { SessionBrowser } from "./browser.js";
import { ConfigPanel } from "./config.js";
import { installKeyBindings } from "./keys.js";
import { qs } from "./util.js";

/** Camera status is cheap and the body auto-powers-off; re-check regularly. */
const STATUS_POLL_MS = 10_000;

function main() {
  const banner = qs("[data-banner]");
  const bannerText = qs("[data-banner-text]");

  const showError = (err) => {
    bannerText.textContent = err?.message || String(err);
    banner.hidden = false;
  };
  qs("[data-banner-dismiss]").addEventListener("click", () => {
    banner.hidden = true;
  });

  /* ------------------------------------------------------------ live view */

  const liveView = new LiveView(qs("[data-panel='live']"));
  const startBtn = qs("[data-live-start]");
  const modeSelect = qs("[data-live-mode]");

  /** Latest camera status, kept so the prompt can speak about it. */
  let cameraStatus = null;

  // Declared up front because the panels' constructors fire their change
  // callbacks immediately, and those callbacks reach back into each other.
  let capture = null;
  let browser = null;

  const setLiveRunning = (running) => {
    startBtn.textContent = running ? "Stop live view  (q)" : "Start live view";
    startBtn.dataset.running = String(running);
    if (running) liveView.start();
    else liveView.stop();
    updatePrompt();
  };
  startBtn.addEventListener("click", () => {
    setLiveRunning(startBtn.dataset.running !== "true");
  });
  qs("[data-live-retry]").addEventListener("click", () => liveView.start());
  modeSelect.addEventListener("change", () => liveView.setMode(modeSelect.value));

  // The mock live view is a generated still, so it has to be polled.
  if (MOCK_ENABLED) {
    modeSelect.value = "poll";
    liveView.setMode("poll");
  }

  // Stop streaming while the tab is hidden: it holds the camera open for
  // nothing and the body will happily power itself off mid-session.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && startBtn.dataset.running === "true") {
      liveView.stop();
      startBtn.dataset.wasRunning = "true";
    } else if (!document.hidden && startBtn.dataset.wasRunning === "true") {
      startBtn.dataset.wasRunning = "false";
      liveView.start();
    }
  });

  /* ---------------------------------------------------------------- jobs */

  const jobs = new JobsPanel(qs("[data-panel='jobs']"), qs("[data-takeover]"), {
    onError: showError,
    onActivityChange: () => {
      // The camera cannot preview and expose at the same time. `isExposing` is
      // false while a job sits on the lens-cap prompt — the shutter is idle
      // then, so live view can keep running and the user can see the cap.
      liveView.setBusy(jobs.isExposing);
      capture?.setBusy(jobs.hasActiveJob);
      updatePrompt();
    },
    onStateChange: () => updatePrompt(),
    onJobFinished: () => {
      browser?.load(browser.path);
      refreshStatus();
    },
  });

  /* ------------------------------------------------------------- capture */

  capture = new CapturePanel(qs("[data-panel='capture']"), {
    onJob: (job, body) => jobs.track(job, body),
    onChange: () => updatePrompt(),
    onError: showError,
  });

  /* ------------------------------------------------------------- browser */

  browser = new SessionBrowser(qs("[data-panel='browser']"), {
    onJob: (job, body) => jobs.track(job, body),
    onError: showError,
  });

  /* ------------------------------------------------------------- config */

  const config = new ConfigPanel(qs("[data-panel='config']"), { onError: showError });

  /* -------------------------------------------------------- the prompt */

  const JOB_VERBS = {
    capture: "Firing",
    sequence: "Sequence",
    align: "Aligning",
    batch: "Batch solving",
    solve: "Solving",
  };

  function updatePrompt() {
    // Panels wire their callbacks during construction, so this can fire before
    // every panel exists. Nothing to say until the settings are readable.
    if (!capture) return;
    const job = jobs.activeJob;

    if (job && job.state === "awaiting_confirmation") {
      // The takeover carries the instruction; the bar just explains the pause.
      liveView.setPrompt({
        title: "Waiting for you",
        detail: "The run is paused until you confirm — press Enter when ready.",
        tone: "attention",
      });
      return;
    }

    if (job) {
      const progress = job.progress;
      const verb = JOB_VERBS[job.kind] || job.kind;
      liveView.setPrompt({
        title: progress && progress.total
          ? `${verb} ${progress.current} / ${progress.total}…`
          : `${verb}…`,
        detail: progress?.label
          ? `${progress.label} frames`
          : "Working — live view resumes when this finishes.",
        tone: "busy",
        progress,
      });
      return;
    }

    if (cameraStatus && !cameraStatus.connected) {
      // Show the driver's own message rather than guessing why. The observer is
      // standing next to the rig and can see more than we can infer from here.
      liveView.setPrompt({
        title: "Camera not detected",
        detail: cameraStatus.error || "No camera is responding over USB.",
        tone: "warn",
      });
      return;
    }

    if (cameraStatus?.camera && cameraStatus.camera.can_capture === false) {
      liveView.setPrompt({
        title: "Grab-only body",
        detail:
          `${cameraStatus.camera.model} locks the shutter in a PTP session, so `
          + "captures cannot be fired from here.",
        tone: "warn",
      });
      return;
    }

    liveView.setPrompt({
      title: `Enter → ${capture.describePrimary()}`,
      detail: `${capture.describeIntegration()} · +/- zoom · arrows pan · f resets peak`,
      tone: "idle",
    });
  }

  /* ------------------------------------------------------------ keyboard */

  installKeyBindings({
    zoomIn: () => liveView.zoomIn(),
    zoomOut: () => liveView.zoomOut(),
    zoomReset: () => liveView.zoomReset(),
    pan: (dx, dy) => liveView.pan(dx, dy),
    resetFocus: () => liveView.resetFocus(),
    // Enter: confirm a pending physical prompt if there is one, else fire.
    confirm: () => jobs.confirmPending(),
    fire: () => {
      if (jobs.hasActiveJob) return; // already busy; Enter must not queue a second run
      capture.fire();
    },
    stop: () => {
      if (startBtn.dataset.running === "true") setLiveRunning(false);
    },
  });

  /* -------------------------------------------------------- camera status */

  const statusDot = qs("[data-camera-dot]");
  const statusText = qs("[data-camera-text]");
  const statusDetail = qs("[data-camera-detail]");

  async function refreshStatus() {
    try {
      const status = await getCameraStatus();
      cameraStatus = status;
      const camera = status.camera;
      const connected = Boolean(status.connected);
      statusDot.dataset.state = connected
        ? camera?.can_capture === false
          ? "limited"
          : "ok"
        : "down";
      statusText.textContent = connected
        ? camera?.model || "Camera connected"
        : "No camera detected";
      const bits = [];
      if (camera?.lens) bits.push(camera.lens);
      if (camera?.battery) bits.push(`battery ${camera.battery}`);
      if (Number.isFinite(camera?.free_shots)) {
        bits.push(`${camera.free_shots} shots free`);
      }
      if (Number.isFinite(camera?.shutter_count)) {
        bits.push(`${camera.shutter_count.toLocaleString()} actuations`);
      }
      if (camera && camera.can_capture === false) {
        bits.push("grab-only body");
      }
      if (status.error) bits.push(status.error);
      statusDetail.textContent = bits.join(" · ") || "—";
    } catch (err) {
      cameraStatus = { connected: false, camera: null, error: err.message };
      statusDot.dataset.state = "down";
      statusText.textContent = "Backend unreachable";
      statusDetail.textContent = err.message;
    }
    updatePrompt();
  }

  qs("[data-camera-reconnect]").addEventListener("click", async (event) => {
    event.target.disabled = true;
    try {
      await reconnectCamera();
      await refreshStatus();
      if (startBtn.dataset.running === "true") liveView.start();
    } catch (err) {
      showError(err);
    } finally {
      event.target.disabled = false;
    }
  });

  /* ---------------------------------------------------------------- boot */

  getHealth()
    .then(({ version }) => {
      qs("[data-version]").textContent = `v${version}`;
    })
    .catch(showError);

  if (MOCK_ENABLED) qs("[data-mock-flag]").hidden = false;

  updatePrompt();
  refreshStatus();
  setInterval(refreshStatus, STATUS_POLL_MS);
  jobs.hydrate();
  browser.load("");
  config.load();

  // Live view is the product; open it immediately rather than making the user
  // find the button in the dark.
  setLiveRunning(true);
}

main();
