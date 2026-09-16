/*
 * api.js — the single place that knows the shape of the HTTP API.
 *
 * Every other module talks to the backend through here, so a contract change
 * is a one-file edit. When the page is loaded with ?mock=1 the whole module
 * transparently delegates to mock.js instead of fetch(), which lets the UI be
 * exercised with no backend and no camera attached.
 */

import { mockFetch, MOCK_ENABLED } from "./mock.js";

/** Thrown for any non-2xx response or transport failure. */
export class ApiError extends Error {
  constructor(message, { status = 0, cause = null, fields = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.cause = cause;
    /** Per-field validation messages from a 422, keyed "search.ra_deg". */
    this.fields = fields;
  }
}

/**
 * Core request helper. Returns parsed JSON.
 * Network failures (backend not running) and HTTP errors both surface as
 * ApiError so call sites only need one catch.
 */
async function request(path, { method = "GET", body = null } = {}) {
  if (MOCK_ENABLED) return mockFetch(path, { method, body });

  let res;
  try {
    res = await fetch(path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    // fetch() only rejects on transport-level failure — i.e. the server is down.
    throw new ApiError("Cannot reach the orchestrator backend.", { cause: err });
  }

  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    let fields = null;
    try {
      const payload = await res.json();
      if (payload && typeof payload.detail === "string") {
        detail = payload.detail;
      } else if (Array.isArray(payload?.detail)) {
        // FastAPI/Pydantic 422: [{loc: ["body","search","ra_deg"], msg: "..."}]
        // Flatten to "search.ra_deg" -> message so a form can place them.
        fields = {};
        for (const item of payload.detail) {
          const path = (item.loc || [])
            .filter((part) => part !== "body" && typeof part === "string")
            .join(".");
          fields[path] = item.msg || "invalid";
        }
        detail = payload.detail
          .map((item) => `${(item.loc || []).slice(1).join(".")}: ${item.msg}`)
          .join("; ") || detail;
      }
    } catch {
      /* body was not JSON; keep the status line */
    }
    throw new ApiError(detail, { status: res.status, fields });
  }

  if (res.status === 204) return null;
  return res.json();
}

/* ---------------------------------------------------------------- health */

export const getHealth = () => request("/api/health");

/* ---------------------------------------------------------------- config */

export const getConfig = () => request("/api/config");

/** Full-document replace. Throws ApiError with `.fields` set on a 422. */
export const putConfig = (config) =>
  request("/api/config", { method: "PUT", body: config });

/**
 * JSON Schema for the config, used for labels and help text.
 * Optional endpoint: resolves to null rather than throwing if it is absent,
 * so the panel degrades to its hardcoded labels.
 */
export async function getConfigSchema() {
  try {
    return await request("/api/config/schema");
  } catch (err) {
    if (err instanceof ApiError && (err.status === 404 || err.status === 0)) {
      return null;
    }
    return null;
  }
}

/* ---------------------------------------------------------------- camera */

export const getCameraStatus = () => request("/api/camera/status");
export const reconnectCamera = () =>
  request("/api/camera/reconnect", { method: "POST" });

/**
 * Drop the PTP session so the body returns to rest.
 *
 * Stopping the stream alone leaves the camera in live view with the mirror up —
 * the heaviest draw on the battery. Closing the session is what puts it down.
 */
export const releaseCamera = () =>
  request("/api/camera/release", { method: "POST" });

/** MJPEG stream URL. Cache-busted so a retry actually re-opens the stream. */
export const liveViewUrl = () =>
  MOCK_ENABLED
    ? mockLiveViewUrl()
    : `/api/camera/liveview.mjpg?t=${Date.now()}`;

/** Single-frame fallback for browsers/proxies that choke on multipart. */
export const frameUrl = () =>
  MOCK_ENABLED ? mockLiveViewUrl() : `/api/camera/frame.jpg?t=${Date.now()}`;

function mockLiveViewUrl() {
  // Lazily built in mock.js; kept out of the normal path entirely.
  return window.__mockFrameUrl ? window.__mockFrameUrl() : "";
}

/* ------------------------------------------------------------- filesystem */

export const browse = (path = "") =>
  request(`/api/browse?path=${encodeURIComponent(path)}`);

export const listSessions = (path = "") =>
  request(`/api/sessions?path=${encodeURIComponent(path)}`);

/** Direct URL for an <img>/<a>; not a JSON endpoint. */
export const rawFileUrl = (path) =>
  `/api/files/raw?path=${encodeURIComponent(path)}`;

/* ------------------------------------------------------------------ jobs */

const postJob = (kind, body) =>
  request(`/api/jobs/${kind}`, { method: "POST", body });

export const startCapture = (body) => postJob("capture", body);
export const startAlign = (body) => postJob("align", body);
export const startSequence = (body) => postJob("sequence", body);
export const startBatch = (body) => postJob("batch", body);
export const startSolve = (body) => postJob("solve", body);

// Kept as the fallback the socket uses while it is reconnecting, and as the
// tested REST path. Live job state arrives on the socket, not by polling.
export const confirmJob = (id) =>
  request(`/api/jobs/${id}/confirm`, { method: "POST" });
export const cancelJob = (id) =>
  request(`/api/jobs/${id}/cancel`, { method: "POST" });

/* ---------------------------------------------------------- the job socket */

/*
 * ONE connection carries every job. It used to be one EventSource per tracked
 * job, and that is what broke live view: a browser allows ~6 concurrent
 * connections per origin, the MJPEG stream holds one of them permanently and
 * never ends, and EventSource re-dials forever on error — so after a handful of
 * jobs the budget was gone. An exhausted budget does not throw; requests simply
 * queue, so live view "just would not open" and finished jobs looked lost.
 * Do not reintroduce a stream per job.
 */

/** Reconnect delays in ms; the last value repeats. Fast enough to be invisible
 * at the scope, slow enough not to hammer a backend that is genuinely down. */
const WS_BACKOFF_MS = [400, 800, 1600, 3200, 5000, 10_000];

/** Silence that means the connection is dead. The server heartbeats every 15 s,
 * so this is three missed beats — a laptop that slept or Wi-Fi that dropped
 * without a close frame, which otherwise hangs silently forever. */
const WS_SILENCE_MS = 45_000;

/** Failed attempts before the UI is told; below this a blip is invisible. */
const WS_QUIET_ATTEMPTS = 3;

class JobSocket {
  /**
   * @param {object} handlers {onSnapshot(jobs), onJob(job), onStatus(connected),
   *                           onError(err)}
   */
  constructor(handlers) {
    this.handlers = handlers;
    this.attempt = 0;
    this.closed = false;
    this.socket = null;
    this.silenceTimer = null;
    this.retryTimer = null;
    this._open();
  }

  /** ws:// or wss:// derived from the page, so it works over the LAN too. */
  static url() {
    const scheme = location.protocol === "https:" ? "wss:" : "ws:";
    return `${scheme}//${location.host}/api/ws`;
  }

  get connected() {
    return this.socket?.readyState === WebSocket.OPEN;
  }

  _open() {
    if (this.closed) return;
    let socket;
    try {
      socket = new WebSocket(JobSocket.url());
    } catch (err) {
      this._retry();
      return;
    }
    this.socket = socket;

    socket.onopen = () => {
      this.attempt = 0;
      this._armSilenceTimer();
      this.handlers.onStatus?.(true);
      // No resync request needed: the server's first frame is a snapshot of
      // every job, which is what makes a dropped connection self-healing.
    };

    socket.onmessage = (event) => {
      this._armSilenceTimer();
      let message;
      try {
        message = JSON.parse(event.data);
      } catch (err) {
        this.handlers.onError?.(new ApiError("Malformed job message.", { cause: err }));
        return;
      }
      this._dispatch(message);
    };

    socket.onclose = () => {
      this._clearSilenceTimer();
      this.handlers.onStatus?.(false);
      this._retry();
    };

    // A failed connect fires error then close; close does the retrying.
    socket.onerror = () => {};
  }

  _dispatch(message) {
    switch (message.type) {
      case "snapshot":
        this.handlers.onSnapshot?.(message.jobs || []);
        break;
      case "job":
        this.handlers.onJob?.(message.job);
        break;
      case "ack":
        this.handlers.onJob?.(message.job);
        break;
      case "ping":
        this._send({ type: "pong" });
        break;
      case "error":
        this.handlers.onError?.(new ApiError(message.message || "Job command failed."));
        break;
      default:
        break; // forwards-compatible: an unknown message type is not an error
    }
  }

  _armSilenceTimer() {
    this._clearSilenceTimer();
    this.silenceTimer = setTimeout(() => {
      // Nothing for three heartbeats. Close it ourselves so onclose runs the
      // reconnect; a half-open socket never fires anything on its own.
      this.socket?.close();
    }, WS_SILENCE_MS);
  }

  _clearSilenceTimer() {
    if (this.silenceTimer) clearTimeout(this.silenceTimer);
    this.silenceTimer = null;
  }

  _retry() {
    if (this.closed || this.retryTimer) return;
    const delay = WS_BACKOFF_MS[Math.min(this.attempt, WS_BACKOFF_MS.length - 1)];
    this.attempt += 1;
    if (this.attempt === WS_QUIET_ATTEMPTS) {
      this.handlers.onError?.(new ApiError("Lost the job connection — reconnecting."));
    }
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this._open();
    }, delay);
  }

  _send(message) {
    if (!this.connected) return false;
    this.socket.send(JSON.stringify(message));
    return true;
  }

  /** Answer a prompt. Falls back to the POST route while reconnecting. */
  confirm(id) {
    return this._send({ type: "confirm", job_id: id })
      ? Promise.resolve(null)
      : confirmJob(id);
  }

  /** Request cancellation. Falls back to the POST route while reconnecting. */
  cancel(id) {
    return this._send({ type: "cancel", job_id: id })
      ? Promise.resolve(null)
      : cancelJob(id);
  }

  close() {
    this.closed = true;
    this._clearSilenceTimer();
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.socket?.close();
  }
}

/**
 * Open the one job socket. Returns an object with `confirm(id)`, `cancel(id)`
 * and `close()`; updates arrive through the handlers.
 *
 * `onSnapshot` fires on every (re)connect with the complete job list — treat it
 * as authoritative and overwrite, never merge-if-newer. That is the fix for a
 * job that finished while the connection was down: the old per-job streams left
 * such a job non-terminal in the client forever, which pinned live view paused.
 */
export function connectJobs(handlers = {}) {
  if (MOCK_ENABLED) return window.__mockConnectJobs(handlers);
  return new JobSocket(handlers);
}
