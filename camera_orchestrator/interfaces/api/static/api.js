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

export const listJobs = () => request("/api/jobs");
export const getJob = (id) => request(`/api/jobs/${id}`);
export const confirmJob = (id) =>
  request(`/api/jobs/${id}/confirm`, { method: "POST" });
export const cancelJob = (id) =>
  request(`/api/jobs/${id}/cancel`, { method: "POST" });

/**
 * Subscribe to a job's SSE stream. Each event's `data` is a full Job JSON.
 *
 * Returns an unsubscribe function. The caller is responsible for calling it —
 * EventSource reconnects forever otherwise, and a finished job's stream closing
 * would look like an error.
 */
export function subscribeJob(id, { onJob, onError } = {}) {
  if (MOCK_ENABLED) {
    return window.__mockSubscribeJob(id, { onJob, onError });
  }

  const source = new EventSource(`/api/jobs/${id}/events`);

  source.onmessage = (event) => {
    try {
      onJob?.(JSON.parse(event.data));
    } catch (err) {
      onError?.(new ApiError("Malformed job event.", { cause: err }));
    }
  };

  source.onerror = () => {
    // EventSource fires onerror both for a genuine drop and for the normal
    // close after a terminal job state. The caller decides which by looking at
    // the last job state it saw, so we only report, never tear down here.
    onError?.(new ApiError("Job event stream interrupted."));
  };

  return () => source.close();
}
