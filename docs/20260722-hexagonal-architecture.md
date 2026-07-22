# Hexagonal architecture restructure

**Date:** 2026-07-22
**Status:** complete — ruff + mypy clean (35 files), 69 tests pass, domain runtime-pure
**Branch:** `refactor/hexagonal-architecture`

## Why

The package grew capability-first (grab, solve, capture) with good instincts —
`Solver` and `Camera` are already abstract ports with concrete adapters — but the
layering is informal: `models.py` is a grab-bag, the service defaults to a concrete
driver, pure logic and I/O sit in the same modules, and persistence (sidecar JSON)
has no contract. This restructure makes the layering explicit using **Hexagonal
Architecture (Ports & Adapters)** so components are loosely coupled, contract-based,
and any adapter can later become a remote service (as `ApiSolver` already is for
`astrometry-api-server`).

We deliberately adopt *hexagonal*, not full DDD: this is an integration/orchestration
system with little business-rule complexity, so aggregates/domain-events would be
ceremony. We borrow DDD's **value objects** (the Pydantic models) and **bounded
context** thinking only.

## The one rule: dependencies point inward

```
interfaces ─▶ application ─▶ domain ◀─ adapters
   (CLI)        (services)     (ports +      (gphoto2, docker,
   (API)                        models)       astropy, files)
```

- **domain** imports nothing external (no `gphoto2`, `cv2`, `docker`, `astropy`,
  `argparse`, `fastapi`). It defines the contracts everything else depends on.
- **adapters** implement the domain ports and are the ONLY place third-party/I/O
  libraries live.
- **application** (services) depends only on **ports**, never on concrete adapters.
- **interfaces** (CLI/API) translate external input into application calls.
- **composition.py** is the only module that knows both a port and its concrete
  adapter — it wires them (our dependency injection, no framework).

## Target layout

```
camera_orchestrator/
  domain/                     # pure core — no external libs
    errors.py                 # CameraError, GrabError
    optics.py                 # scale_hint_from_optics (pure math)
    models/
      camera.py               # ImageFormat, CameraFile, CaptureSettings, CameraStatus,
                              #   CaptureRequest, CaptureResult
      solve.py                # ImageExif, ObserverInfo, SolveHints, SolveResult,
                              #   SolveJob, SolveRecord
    ports/
      camera.py               # Camera ABC
      solver.py               # Solver ABC
      storage.py              # SolveRecordRepository ABC
  adapters/                   # implement ports; external libs live here only
    camera/
      gphoto.py               # GphotoCamera (python-gphoto2)
      gvfs.py                 # unmount_gvfs + subprocess helper (shared)
      cli_grab.py             # gphoto2 CLI file ops used by grab
    solvers/
      docker.py               # DockerSolver
      api.py                  # ApiSolver (-> astrometry-api-server)
      fits.py                 # write_fits, result_from_wcs (astropy/cv2 I/O)
    storage/
      sidecar.py              # SidecarSolveRepository (filesystem JSON)
    exif.py                   # read_exif
  application/                # use cases — depend on ports only
    capture_service.py        # CaptureService
    solve_service.py          # solve_file + build_hints (config -> domain)
    grab_service.py           # grab_latest, poll
  interfaces/
    cli.py                    # argparse + command handlers + main (was cmd.py)
  config.py                   # infrastructure config
  composition.py              # build_camera / build_solver / build_repository
  log/                        # logging setup
main.py                       # entrypoint shim -> interfaces.cli.main
```

### Placement notes (judgement calls, recorded for the record)

- **`build_hints` lives in application, not domain** — it reads `Config`
  (`cfg.optics`, `cfg.search`), which is infrastructure. The pure math
  (`scale_hint_from_optics`) stays in `domain/optics.py`; `build_hints` bridges
  config → domain `SolveHints` and so belongs to the application layer.
- **`write_fits` / `result_from_wcs` are adapters** — they do astropy/cv2 I/O, so
  they sit in `adapters/solvers/fits.py`, used by the solver adapters.
- **numpy in the `Solver` port** — `solve()` takes `np.ndarray`. numpy is imported
  under `TYPE_CHECKING` only, so the domain has no hard runtime dependency on it.
- **`SolveRecordRepository` port + `SidecarSolveRepository` adapter** — persistence
  is now behind a contract (`save`, `find_by_image`). Filesystem JSON today; a SQLite
  adapter can drop in later with zero application-layer changes. No database is added
  now — only the seam.

## Migration phases (green at every step)

1. **domain** — split models, add ports/optics/errors. Update importers.
2. **adapters** — move gphoto/solvers/exif/fits/storage/gvfs. Update importers.
3. **application** — capture/solve/grab services depending on ports.
4. **interfaces + composition** — move CLI; add composition root; services take
   injected ports.
5. **tests + entrypoint** — update imports, `main.py` shim; `make lint` + `make test`.

Each phase moves modules and updates *all* their importers before running the suite,
so the tree is never left broken.

## Verification

- `make lint` (ruff + mypy) clean.
- `make test` — all 69 pass.
- Import-boundary check: `domain/` must not import `gphoto2`, `cv2`, `docker`,
  `astropy`, `argparse`, or `fastapi` (grep as a guard).
- Live 5D II smoke when the camera is powered: `capture --status`, a card-only
  sequence, and `--download --format both`.
