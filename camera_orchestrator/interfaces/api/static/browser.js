/*
 * browser.js — the session / file browser over incoming/.
 *
 * The important thing this view gets right is `files_recorded` vs
 * `files_present`. A card-only capture (the default, and the fast path for bulk
 * subs) records the filenames the camera wrote to its SD card; those files are
 * NOT on disk and that is correct, not a failure. "96 recorded · 0 on disk" is
 * shown as a neutral "on camera card" state, never as an error.
 */

import { browse, rawFileUrl, startBatch, startSolve } from "./api.js";
import {
  el,
  formatBytes,
  formatDec,
  formatRa,
  formatTimestamp,
  qs,
  replaceChildren,
} from "./util.js";

const SOLVABLE = new Set(["raw", "jpeg"]);

export class SessionBrowser {
  constructor(root, { onJob, onError } = {}) {
    this.root = root;
    this.crumbsEl = qs("[data-crumbs]", root);
    this.bodyEl = qs("[data-browser-body]", root);
    this.statusEl = qs("[data-browser-status]", root);
    this.onJob = onJob || (() => {});
    this.onError = onError || (() => {});
    this.path = "";
    this.listing = null;

    qs("[data-browser-refresh]", root).addEventListener("click", () =>
      this.load(this.path),
    );
    qs("[data-browser-batch]", root).addEventListener("click", () =>
      this._batch(),
    );
  }

  async load(path = "") {
    this.statusEl.textContent = "Loading…";
    try {
      const listing = await browse(path);
      this.listing = listing;
      this.path = listing.path || "";
      this.statusEl.textContent = "";
      this._render();
    } catch (err) {
      this.statusEl.textContent = err.message;
      this.onError(err);
    }
  }

  /* ------------------------------------------------------------ rendering */

  _render() {
    const listing = this.listing;
    if (!listing) return;
    this._renderCrumbs(listing);

    // Newest first — a night's work should be at the top, always.
    const sessions = [...(listing.sessions || [])].sort(
      (a, b) => String(b.session_date || "").localeCompare(String(a.session_date || ""))
        || String(b.folder_name || "").localeCompare(String(a.folder_name || "")),
    );

    const nodes = [];
    if (sessions.length) {
      nodes.push(el("h3", { class: "section-heading", text: "Sessions" }));
      nodes.push(
        el("div", { class: "session-grid" }, sessions.map((s) => this._session(s))),
      );
    }

    const dirs = (listing.directories || []).filter(
      (d) => !sessions.some((s) => s.path === d.path),
    );
    if (dirs.length) {
      nodes.push(el("h3", { class: "section-heading", text: "Folders" }));
      nodes.push(
        el(
          "ul",
          { class: "plain-list" },
          dirs.map((dir) =>
            el("li", {}, [
              el("button", {
                class: "link-btn",
                type: "button",
                text: `${dir.name}/`,
                onclick: () => this.load(dir.path),
              }),
            ]),
          ),
        ),
      );
    }

    const files = listing.files || [];
    if (files.length) {
      nodes.push(el("h3", { class: "section-heading", text: "Files" }));
      nodes.push(
        el("ul", { class: "plain-list file-list" }, files.map((f) => this._file(f))),
      );
    }

    if (!nodes.length) {
      nodes.push(el("p", { class: "muted", text: "Nothing here yet." }));
    }
    replaceChildren(this.bodyEl, nodes);
  }

  _renderCrumbs(listing) {
    const crumbs = [];
    const segments = (listing.path || "").split("/").filter(Boolean);
    crumbs.push(
      el("button", {
        class: "link-btn",
        type: "button",
        text: "incoming",
        onclick: () => this.load(""),
      }),
    );
    let accumulated = segments[0] === "incoming" ? "incoming" : "";
    for (const segment of segments.slice(accumulated ? 1 : 0)) {
      accumulated = accumulated ? `${accumulated}/${segment}` : segment;
      const target = accumulated;
      crumbs.push(el("span", { class: "crumb-sep", text: "/" }));
      crumbs.push(
        el("button", {
          class: "link-btn",
          type: "button",
          text: segment,
          onclick: () => this.load(target),
        }),
      );
    }
    if (!listing.is_root && listing.parent !== null && listing.parent !== undefined) {
      crumbs.push(
        el("button", {
          class: "btn btn-quiet crumb-up",
          type: "button",
          text: "↑ Up",
          onclick: () => this.load(listing.parent),
        }),
      );
    }
    replaceChildren(this.crumbsEl, crumbs);
  }

  /* -------------------------------------------------------- session card */

  _session(s) {
    const frames = s.frames || {};
    const target = s.target;
    const recorded = s.files_recorded ?? 0;
    const present = s.files_present ?? s.files_on_disk ?? 0;
    const onCardOnly = recorded > 0 && present === 0;
    const partial = recorded > 0 && present > 0 && present < recorded;

    return el("article", { class: "card session" }, [
      el("header", { class: "session-head" }, [
        el("h4", { class: "session-name", text: s.name || s.folder_name }),
        el("span", { class: "mono muted", text: s.session_date || "" }),
      ]),
      el("p", { class: "mono muted small", text: s.folder_name || s.session_id }),

      el("div", { class: "chips" }, [
        chip(`${frames.light ?? 0} light`, "light"),
        chip(`${frames.dark ?? 0} dark`, "dark"),
        chip(`${frames.bias ?? 0} bias`, "bias"),
        chip(`${frames.total ?? 0} total`, "total"),
        s.phase_count
          ? chip(
              `${s.phase_count} phase${s.phase_count === 1 ? "" : "s"}`
                + (s.phase_kinds?.length ? `: ${s.phase_kinds.join(", ")}` : ""),
              "phase",
            )
          : null,
      ]),

      // Storage line: the card-only case gets its own neutral wording.
      el("div", { class: "storage", "data-storage": storageState(onCardOnly, partial) }, [
        el("span", { class: "mono", text: `${recorded} recorded` }),
        el("span", { class: "mono", text: `${present} on disk` }),
        el("span", { class: "mono muted", text: formatBytes(s.bytes_on_disk) }),
        el("span", {
          class: "storage-note",
          text: onCardOnly
            ? "Card-only run — frames are on the camera's SD card, not yet copied."
            : partial
              ? "Partially downloaded — the rest is still on the card."
              : recorded === 0
                ? "No frames recorded in the manifest."
                : "All recorded frames are on disk.",
        }),
      ]),

      s.has_manifest === false
        ? el("p", { class: "warn", text: "No session.json manifest in this folder." })
        : s.manifest_readable === false
          ? el("p", { class: "warn", text: "session.json could not be read." })
          : null,

      target && target.solved
        ? el("div", { class: "solved" }, [
            el("span", { class: "badge", "data-state": "succeeded", text: "Plate solved" }),
            el("dl", { class: "kv mono" }, [
              el("dt", { text: "RA" }),
              el("dd", { text: formatRa(target.center_ra_deg) }),
              el("dt", { text: "Dec" }),
              el("dd", { text: formatDec(target.center_dec_deg) }),
              el("dt", { text: "Scale" }),
              el("dd", {
                text: Number.isFinite(target.scale_arcsec_per_px)
                  ? `${target.scale_arcsec_per_px.toFixed(2)}"/px`
                  : "—",
              }),
            ]),
            target.preview
              ? el("img", {
                  class: "preview",
                  loading: "lazy",
                  alt: `Annotated plate-solve preview for ${s.name || s.folder_name}`,
                  src: rawFileUrl(target.preview),
                })
              : null,
          ])
        : el("p", { class: "muted small", text: "Not plate solved." }),

      el("div", { class: "session-actions" }, [
        el("button", {
          class: "btn btn-quiet",
          type: "button",
          text: "Open folder",
          onclick: () => this.load(s.path),
        }),
        el("button", {
          class: "btn btn-quiet",
          type: "button",
          text: "Batch solve",
          onclick: () => this._batch(s.path),
        }),
      ]),
    ]);
  }

  /* ---------------------------------------------------------- file row */

  _file(f) {
    return el("li", { class: "file-row" }, [
      el("span", { class: "file-kind", "data-kind": f.kind, text: f.kind || "other" }),
      el("a", {
        class: "link-btn",
        href: rawFileUrl(f.path),
        target: "_blank",
        rel: "noopener",
        text: f.name,
      }),
      el("span", { class: "mono muted", text: formatBytes(f.size_bytes) }),
      el("span", {
        class: "mono muted small",
        text: formatTimestamp(f.modified_at),
      }),
      SOLVABLE.has(f.kind)
        ? el("button", {
            class: "btn btn-quiet",
            type: "button",
            text: "Solve",
            onclick: () => this._solve(f.path),
          })
        : null,
    ]);
  }

  /* -------------------------------------------------------------- actions */

  async _batch(folder = this.path) {
    try {
      this.onJob(
        await startBatch({
          folder,
          annotate: true,
          reprocess: false,
          mode: null,
          cpulimit: null,
        }),
      );
    } catch (err) {
      this.onError(err);
    }
  }

  async _solve(file) {
    try {
      this.onJob(await startSolve({ file, annotate: true, force: false }));
    } catch (err) {
      this.onError(err);
    }
  }
}

const chip = (text, kind) => el("span", { class: "chip", "data-chip": kind, text });

function storageState(onCardOnly, partial) {
  if (onCardOnly) return "card";
  if (partial) return "partial";
  return "disk";
}
