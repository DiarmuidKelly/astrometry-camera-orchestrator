/*
 * mock.js — canned data so the UI can be driven with no backend and no camera.
 *
 * Inert unless the page is loaded with ?mock=1. api.js checks MOCK_ENABLED and
 * routes every call here; nothing else in the app imports this module, so in
 * normal operation the only cost is one URLSearchParams read at start-up.
 *
 * The synthetic live view is a generated star field whose defocus oscillates on
 * a 24-second cycle. That is deliberate: it makes the focus score rise and fall
 * so the sparkline and peak-hold can be verified without a telescope.
 */

export const MOCK_ENABLED = new URLSearchParams(location.search).get("mock") === "1";

/* --------------------------------------------------------- synthetic frame */

const FRAME_W = 960;
const FRAME_H = 640;
const STAR_COUNT = 70;

// Fixed star field — regenerated positions per frame would look like noise.
const STARS = Array.from({ length: STAR_COUNT }, (_, i) => {
  // Deterministic pseudo-random so the field is stable across reloads.
  const r = (n) => {
    const x = Math.sin((i + 1) * n) * 43758.5453;
    return x - Math.floor(x);
  };
  return {
    x: r(12.9898) * FRAME_W,
    y: r(78.233) * FRAME_H,
    brightness: 0.25 + r(37.719) * 0.75,
  };
});

let frameCanvas = null;
// Set while a mock job is capturing, so the live view can show the "paused
// during capture" state that the real backend produces.
let mockCapturing = false;

function renderMockFrame() {
  if (!frameCanvas) {
    frameCanvas = document.createElement("canvas");
    frameCanvas.width = FRAME_W;
    frameCanvas.height = FRAME_H;
  }
  const ctx = frameCanvas.getContext("2d");

  ctx.fillStyle = "#05070c";
  ctx.fillRect(0, 0, FRAME_W, FRAME_H);

  // Defocus cycles 0.8px (sharp) .. 7px (soft) every 24 seconds.
  const phase = (Date.now() % 24000) / 24000;
  const defocus = 0.8 + 6.2 * (0.5 - 0.5 * Math.cos(phase * 2 * Math.PI));

  for (const star of STARS) {
    const radius = defocus * (0.7 + star.brightness);
    const gradient = ctx.createRadialGradient(
      star.x, star.y, 0, star.x, star.y, radius,
    );
    // Energy is conserved-ish: a defocused star is dimmer as well as wider,
    // which is what makes a sharpness metric actually discriminate.
    const peak = star.brightness / (1 + (defocus - 0.8) * 0.6);
    gradient.addColorStop(0, `rgba(255,255,245,${peak})`);
    gradient.addColorStop(1, "rgba(255,255,245,0)");
    ctx.fillStyle = gradient;
    ctx.beginPath();
    ctx.arc(star.x, star.y, radius, 0, Math.PI * 2);
    ctx.fill();
  }

  if (mockCapturing) {
    ctx.fillStyle = "rgba(0,0,0,0.85)";
    ctx.fillRect(0, 0, FRAME_W, FRAME_H);
  }

  return frameCanvas.toDataURL("image/jpeg", 0.7);
}

/* ------------------------------------------------------------- fake state */

const MOCK_SESSIONS = [
  {
    session_id: "20260915-m33",
    folder_name: "20260915-m33",
    path: "incoming/20260915-m33",
    name: "m33",
    session_date: "2026-09-15",
    has_manifest: true,
    manifest_readable: true,
    target: {
      solved: true,
      center_ra_deg: 23.4621,
      center_dec_deg: 30.6599,
      scale_arcsec_per_px: 4.12,
      preview: "incoming/20260915-m33/align/align-annotated.png",
      frame: "incoming/20260915-m33/align/align.jpg",
    },
    frames: { light: 64, dark: 12, bias: 20, total: 96 },
    phase_count: 3,
    phase_kinds: ["light", "dark", "bias"],
    files_recorded: 96,
    files_present: 0, // card-only run: recorded on the SD card, nothing on disk
    files_on_disk: 0,
    bytes_on_disk: 0,
  },
  {
    session_id: "20260912-double-cluster",
    folder_name: "20260912-double-cluster",
    path: "incoming/20260912-double-cluster",
    name: "double-cluster",
    session_date: "2026-09-12",
    has_manifest: true,
    manifest_readable: true,
    target: {
      solved: false,
      center_ra_deg: null,
      center_dec_deg: null,
      scale_arcsec_per_px: null,
      preview: null,
      frame: null,
    },
    frames: { light: 32, dark: 8, bias: 0, total: 40 },
    phase_count: 2,
    phase_kinds: ["light", "dark"],
    files_recorded: 40,
    files_present: 40,
    files_on_disk: 40,
    bytes_on_disk: 40 * 26_500_000,
  },
];

const MOCK_LISTING = {
  path: "incoming",
  name: "incoming",
  is_root: true,
  parent: null,
  sessions: MOCK_SESSIONS,
  directories: [
    { name: "20260915-m33", path: "incoming/20260915-m33" },
    { name: "20260912-double-cluster", path: "incoming/20260912-double-cluster" },
  ],
  files: [
    {
      name: "session.json",
      path: "incoming/session.json",
      size_bytes: 4821,
      modified_at: "2026-09-15T23:41:02Z",
      kind: "manifest",
    },
  ],
};

let mockConfig = {
  solver: {
    image: "dm90/astrometry.net:latest",
    index_dir: "/opt/astrometry/data",
    cpulimit: 120,
    mode: "fast",
  },
  optics: { focal_mm: null, sensor_width_mm: 35.8 },
  search: { ra_deg: 10.6847, dec_deg: 41.269, radius_deg: 5.0 },
  location: { lat: 53.3498, lon: -6.2603 },
  logging: { level: "INFO", format: "text" },
  grab: { out_dir: "incoming", poll_interval: 5.0 },
};

const jobs = new Map();
let jobCounter = 0;

function newJob(kind, total) {
  const id = `mock-${++jobCounter}`;
  const job = {
    id,
    kind,
    state: "pending",
    created_at: new Date().toISOString(),
    started_at: null,
    ended_at: null,
    progress: total ? { current: 0, total, label: "starting" } : null,
    prompt: null,
    result: null,
    error: null,
  };
  jobs.set(id, job);
  // The real registry starts a worker thread on submit and pushes the state
  // change down the socket; the mock does the same rather than waiting for a
  // subscriber that no longer exists.
  setTimeout(() => driveMockJob(job), 100);
  return job;
}

/* ------------------------------------------------------------ the routers */

export async function mockFetch(path, { method = "GET", body = null } = {}) {
  await sleep(120); // make loading states visible

  const [route, query] = path.split("?");
  const params = new URLSearchParams(query || "");

  if (route === "/api/health") return { ok: true, version: "0.8.0+mock" };

  if (route === "/api/config") {
    if (method === "PUT") {
      mockConfig = body;
      return { config: mockConfig, path: "/home/observer/config.yaml" };
    }
    return mockConfig;
  }
  if (route === "/api/config/schema") {
    // Deliberately absent in mock, to exercise the graceful-degradation path.
    const err = new Error("no schema endpoint");
    err.status = 404;
    throw err;
  }

  if (route === "/api/camera/status") {
    return {
      connected: true,
      session_open: true,
      error: null,
      camera: {
        model: "Canon EOS 5D Mark II (mock)",
        lens: "EF 50mm f/1.8",
        battery: "72%",
        shutter_count: 148_302,
        free_shots: 412,
        can_capture: true,
      },
    };
  }
  if (route === "/api/camera/reconnect") return { ok: true };

  if (route === "/api/browse") {
    const requested = params.get("path") || "";
    if (requested && requested !== "incoming") {
      return {
        path: requested,
        name: requested.split("/").pop(),
        is_root: false,
        parent: "incoming",
        sessions: MOCK_SESSIONS.filter((s) => s.path === requested),
        directories: [],
        files: [
          {
            name: "IMG_0421.CR2",
            path: `${requested}/IMG_0421.CR2`,
            size_bytes: 26_500_000,
            modified_at: "2026-09-15T22:14:00Z",
            kind: "raw",
          },
          {
            name: "align-annotated.png",
            path: `${requested}/align/align-annotated.png`,
            size_bytes: 840_000,
            modified_at: "2026-09-15T21:02:00Z",
            kind: "preview",
          },
        ],
      };
    }
    return MOCK_LISTING;
  }

  if (route === "/api/sessions") return { sessions: MOCK_SESSIONS };

  if (route.startsWith("/api/jobs/")) {
    const rest = route.slice("/api/jobs/".length);
    if (method === "POST" && !rest.includes("/")) {
      // rest is a kind: capture | align | sequence | batch | solve
      const total =
        rest === "sequence"
          ? (body?.lights || 0) + (body?.darks || 0) + (body?.bias || 0)
          : rest === "capture"
            ? body?.count || 1
            : rest === "align"
              ? 1
              : 10;
      return newJob(rest, total);
    }
    const [id, action] = rest.split("/");
    const job = jobs.get(id);
    if (!job) throw new Error(`No such job: ${id}`);
    // The REST fallback the socket uses while reconnecting — same effect, so it
    // goes through the same helpers and broadcasts to every fake socket.
    if (action === "confirm") return mockConfirm(id, body?.token);
    if (action === "cancel") return mockCancel(id);
    return job;
  }

  throw new Error(`mock: unhandled route ${path}`);
}

/* ------------------------------------------------------- the fake job socket */

/*
 * The real backend multiplexes every job onto one WebSocket. The mock models the
 * same shape — one fake connection, a snapshot on connect, pushes thereafter —
 * so ?mock=1 exercises the code path that actually ships, reconnect logic aside.
 */

const sockets = new Set();

function broadcast(job) {
  for (const socket of sockets) socket.onJob?.({ ...job });
}

/**
 * Drive a fake job through its lifecycle, including the lens-cap prompt for
 * sequences, so the confirmation modal can be exercised.
 *
 * Started when the job is created, not when something subscribes: with one
 * shared socket there is no per-job subscription to trigger it any more.
 */
function driveMockJob(job) {
  job.state = "running";
  job.started_at = new Date().toISOString();
  mockCapturing = job.kind !== "batch" && job.kind !== "solve";
  broadcast(job);

  const total = job.progress?.total ?? 1;
  let tick = 0;
  let prompted = false;

  const timer = setInterval(() => {
    if (job.state === "cancelled") {
      mockCapturing = false;
      broadcast(job);
      clearInterval(timer);
      return;
    }
    if (job.state === "awaiting_confirmation") {
      broadcast(job);
      return;
    }

    tick += 1;

    // Halfway through a sequence, block on the lens-cap prompt once.
    if (job.kind === "sequence" && !prompted && tick > total / 2) {
      prompted = true;
      job.state = "awaiting_confirmation";
      job.prompt = {
        kind: "lens_cap",
        message: "Cover the lens (and the viewfinder) before dark frames begin.",
        // The real server mints one per prompt and refuses a confirm without it.
        token: `${job.id}-prompt-${tick}`,
      };
      broadcast(job);
      return;
    }

    job.progress = {
      current: Math.min(tick, total),
      total,
      label: job.kind === "sequence" && prompted ? "dark" : "light",
    };

    if (tick >= total) {
      job.state = "succeeded";
      job.ended_at = new Date().toISOString();
      job.result =
        job.kind === "align"
          ? {
              solved: true,
              center_ra_deg: 23.4621,
              center_dec_deg: 30.6599,
              preview: "incoming/20260915-m33/align/align-annotated.png",
            }
          : { files: total, out_dir: "incoming/20260916-mock" };
      mockCapturing = false;
      broadcast(job);
      clearInterval(timer);
      return;
    }
    broadcast(job);
  }, 700);
}

function mockConfirm(id, token) {
  const job = jobs.get(id);
  if (!job) throw new Error(`No such job: ${id}`);
  // Mirrors the server: only the prompt on screen may be answered.
  if (job.state !== "awaiting_confirmation") return job;
  if (token !== job.prompt?.token) {
    throw new Error("confirmation token does not match the pending prompt");
  }
  job.prompt = null;
  job.state = "running";
  broadcast(job);
  return job;
}

function mockCancel(id) {
  const job = jobs.get(id);
  if (!job) throw new Error(`No such job: ${id}`);
  job.state = "cancelled";
  job.ended_at = new Date().toISOString();
  broadcast(job);
  return job;
}

/** Same interface api.js's real JobSocket exposes: confirm/cancel/close. */
function mockConnectJobs(handlers = {}) {
  const socket = { ...handlers };
  sockets.add(socket);
  setTimeout(() => {
    handlers.onStatus?.(true);
    handlers.onSnapshot?.(Array.from(jobs.values()).map((job) => ({ ...job })));
  }, 60);
  return {
    confirm: async (id, token) => mockConfirm(id, token),
    cancel: async (id) => mockCancel(id),
    close: () => sockets.delete(socket),
  };
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// api.js reaches these two through window because they are the only mock hooks
// that must stay reachable without importing mock.js into the hot path.
if (MOCK_ENABLED) {
  window.__mockFrameUrl = renderMockFrame;
  window.__mockConnectJobs = mockConnectJobs;
}
