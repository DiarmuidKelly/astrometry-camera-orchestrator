# Core workflows: align + session (+ shared CR2 decode)

**Date:** 2026-07-23
**Status:** in progress
**Branch:** `feature/core-workflows`

## Context

Camera-orchestrator now has the atomic camera driver, capture composites,
hexagonal layers, and uv — all merged to main. This adds the two remaining
user-facing workflows as **service compositions** over the existing atomic ports
(camera + solver):

- **align** — shoot one frame, solve it, produce an annotated preview + the true
  centre RA/Dec so you can check/adjust pointing.
- **session** — a multi-phase imaging run: lights, darks, bias.

**solve already exists** as the `batch` command (`solve_file` → `DockerSolver` →
the pre-existing dockerised solver container) and **stays as-is**. It and align
gain **CR2** support via one shared decode. Manual focus is a human step (lens
AF/MF switch) — no focus service.

## Shared foundation — CR2 decode

`application/solve_service.py::solve_file` today calls `cv2.imread`, which returns
None for CR2. Branch by extension:
- **JPEG/PNG** → `cv2.imread`, pass `source_path=path` (solve-field reads it directly).
- **CR2** → decode with **rawpy** (`rawpy.imread(path).postprocess()` → RGB) →
  convert RGB→BGR; pass `source_path=None` so `DockerSolver` writes a FITS from the
  array (solve-field can't read CR2 either — reuse the existing array/FITS path).
- Add `rawpy` to `pyproject.toml` (+ `uv lock`); add `.cr2` to `IMAGE_SUFFIXES` in
  `interfaces/cli.py` so `batch` scans CR2 folders.

Benefits: `batch` solves CR2 folders and `align` solves captured CR2 frames — both
from one change.

## 1. align — pointing/framing check (new)

- **DTOs** `domain/models/align.py`:
  - `AlignRequest{ out_dir, iso?, shutter?, aperture?, bulb_seconds? }`
  - `AlignResult{ solved, center_ra_deg?, center_dec_deg?, scale_arcsec_per_px?, annotated_path?, frame_path }`
- **`application/align_service.py::AlignService`** — composes the existing
  `CaptureService` + `build_solver(cfg)` + `cfg`:
  1. `capture_and_download(CaptureRequest(count=1, download=True, ...))` → frame(s).
  2. Pick the frame to solve: prefer `.jpg/.jpeg` (fast), else `.cr2`.
  3. `solve_file(frame, solver, cfg, annotate_out=<out_dir>/<stem>_solved.png)` —
     annotated keeps the frame's base name + `_solved` suffix (batch convention).
  4. Return `AlignResult`. Blind solve; search space narrowed by existing
     `build_hints` (scale from EXIF focal + `search:` RA/Dec/radius). No target/offset.
     Ephemeral — no sidecar record.
- **CLI** `align` verb; **composition** `build_align_service()`.

## 2. session — imaging run (new; keep `capture`)

- **DTOs** `domain/models/session.py`:
  - `SessionRequest{ out_dir, iso?, shutter?, bulb_seconds?, aperture?, lights, darks, bias, download, order }`
  - `SessionResult{ counts, frames, download }`
- **`application/session_service.py::SessionService`** — composes `CaptureService`:
  - Run phases in `order` (default `light, dark, bias`; flexible — calibration may
    precede lights); skip count-0 phases.
  - Per phase → a `CaptureRequest` (bias uses fastest shutter; `kind` set) →
    `capture_and_download`/`capture_to_card`.
  - `before_phase(kind)` callback before calibration phases (lens-cap prompt) —
    service stays UI-agnostic; CLI supplies `input(...)`.
  - Note: darks ideally right after lights (thermal match) — default order, configurable.
- **CLI** `session` verb; keep the existing `capture` verb. **composition** `build_session_service()`.

## solve — unchanged

Existing `batch`/`DockerSolver`→container stays. Gains CR2 for free via the shared
decode + `.cr2` suffix. No refactor.

## Build order

1. Shared CR2 decode (+ rawpy + `.cr2`).  2. align.  3. session.

## Verification

- `make lint` + `make test` green (services via MockCamera/MockSolver; CR2 via
  monkeypatched rawpy).
- Live 5D II: `align` on a star field → `<stem>_solved.png` + RA/Dec; `session
  --lights 3 --darks 2 --bias 2` → phased with cap prompts; `batch` on a folder
  containing a `.cr2` → solves it.
