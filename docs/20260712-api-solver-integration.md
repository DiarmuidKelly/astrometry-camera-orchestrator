# API solver integration plan

**Date:** 2026-07-12
**Status:** planned (blocked on api-server release)
**Branch:** `feature--api-solver`

## Goal

Implement `ApiSolver` so camera-orchestrator can plate-solve by POSTing images to a
running `astrometry-api-server` container, instead of shelling out to `docker run`
per image via `DockerSolver`. Keep `DockerSolver` as the default; make the backend
selectable by config.

## Why now

The api-server is now a **self-contained wrapper of the dockerised solver**
(local-exec, no docker.sock). Its `/solve` endpoint accepts a multipart image upload
and returns the astrometric solution as JSON, plus — when `annotate=true` — the
annotated overlay as a base64 PNG. Both were verified end-to-end.

Benefits over `DockerSolver`:
- One long-lived container instead of a fresh `docker run` per frame (lower per-image overhead)
- No Docker socket / index mounting logic in the orchestrator
- Decouples the solver deployment from the orchestrator host

## Upstream dependencies (must land first)

1. **astrometry-api-server** PR [#10] merged — `[MAJOR] self-contained solver`
   (local-exec, `/solve` annotate field). CI green.
2. **astrometry-go-client** `v1.5.0` — released (`Annotate` + `Result.AnnotatedImage`).
3. **Published image** — the api-server image must be pushed to a registry (e.g. GHCR)
   so the orchestrator can `docker run` or `docker compose up` a known tag. Until then,
   only a locally-built image works. **This is the current blocker for reproducible use.**

## Contract mapping

`/solve` response → `SolveResult` (`camera_orchestrator/models.py`):

| `/solve` JSON field        | `SolveResult` field       |
|----------------------------|---------------------------|
| `ra`                       | `center_ra_deg`           |
| `dec`                      | `center_dec_deg`          |
| `pixel_scale`              | `scale_arcsec_per_px`     |
| `wcs_header.IMAGEW`        | `width_px`                |
| `wcs_header.IMAGEH`        | `height_px`               |
| `solved: false`            | return `None`             |

`SolveHints` → `/solve` form fields:

| `SolveHints` field | form field    | notes                                  |
|--------------------|---------------|----------------------------------------|
| `scale_low`        | `scale_low`   | with `scale_units=arcsecperpix`        |
| `scale_high`       | `scale_high`  | (hints are already arcsec/px)          |
| `ra_deg`           | `ra`          | only if set                            |
| `dec_deg`          | `dec`         | only if set                            |
| `radius_deg`       | `radius`      | only if ra/dec set                     |
| (annotate wanted)  | `annotate`    | `true` when `annotate_out` is not None |

## Implementation

### 1. `ApiSolver.solve()` (`camera_orchestrator/solvers/api.py`)

Replace the `NotImplementedError` stub with:
- Build multipart POST to `{base_url}/solve`:
  - `image`: open `source_path` (preferred) in binary; fall back to encoding `frame_bgr`
    to a temp JPEG/PNG if no source path.
  - form fields from the hint mapping above; set `annotate=true` when `annotate_out` is set.
- Timeout: configurable (solve can take seconds); default generous (e.g. 300s) to match
  server-side cpulimit.
- Parse JSON:
  - `solved is False` → return `None`.
  - On success, build `SolveResult` from the mapping. Prefer `wcs_header.IMAGEW/H` for
    dimensions; fall back to `frame_bgr.shape` if absent.
- Annotation:
  - If `annotate_out` set and `annotated_image` present: base64-decode and write the
    **PNG bytes** to `annotate_out`. Save as `.png` (see §3 — do not force `.jpg`).
  - No flip needed: the server returns the overlay in the input's orientation
    (verified for image uploads).
- Errors: on HTTP error / connection failure, raise a clear exception (mirror how
  `DockerSolver` surfaces failures) rather than returning `None`, so genuine transport
  failures are distinguishable from "no solution".

### 2. Backend selection (`config.py` + `solvers/__init__.py`)

- Add to `SolverConfig`:
  - `backend: Literal["docker", "api"] = "docker"`
  - `api_base_url: str = "http://localhost:8080"`
- `build_solver(cfg)` returns `ApiSolver(cfg.solver.api_base_url)` when
  `backend == "api"`, else the existing `DockerSolver`.
- Document both in `config.example.yaml`.

### 3. Annotation extension fix (pre-existing cleanup)

`main.py` currently forces annotated output to `<stem>_solved.jpg`, but the overlay is
PNG. For the API path (and honesty in general), write `<stem>_solved.png`. Decide whether
to also correct the `DockerSolver` path (it copies PNG bytes into a `.jpg` name today).
Recommend: standardise annotated output on `.png`. A later, optional orchestrator-level
step can transcode PNG→JPEG if disk size matters.

### 4. Dependencies

- Add `requests` to `requirements.txt` (or `httpx` if we prefer). Single HTTP client;
  `requests` is simplest and sync-friendly for the batch loop.

### 5. Tests

- **Unit** (`tests/test_api_solver.py`): mock the HTTP layer (e.g. `responses` or
  monkeypatch `requests.post`) and assert:
  - success JSON → correct `SolveResult`
  - `solved: false` → `None`
  - annotate path writes decoded PNG bytes to `annotate_out`
  - hint mapping produces the expected form fields (`scale_units=arcsecperpix`, etc.)
  - HTTP error → raises
- **Integration** (optional, gated like `test_integration_solver.py`): against a
  locally-running container; skipped by default.

## Open questions / follow-ups

- **Image publishing**: which registry/tag? Needed before this is reproducible (blocker).
- **Concurrency**: batch is sequential today. A persistent server invites parallel POSTs
  later, but the server currently names temp files by PID — parallel requests could
  collide server-side. Out of scope here; note for the server.
- **CR3**: still unsupported end-to-end (issue #4); unaffected by this work — the API
  path has the same decode limitation on the client side.
- **Annotation quality**: server returns `solve-field`'s `-ngc.png` (over the downsampled
  field). Full-res overlays via `plotann.py` are a later server enhancement.

## Sequencing

1. Merge api-server PR #10 → publish image.
2. Implement §1–§4 on this branch; add §5 tests.
3. Verify against the published image; open PR.
