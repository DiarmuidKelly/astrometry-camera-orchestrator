# camera-orchestrator — project guide

Python tool for astrophotography: plate-solving (dockerised astrometry.net) and
tethered camera control (python-gphoto2). This file captures the durable
conventions — read it before changing structure.

## Architecture: hexagonal (ports & adapters)

Dependencies point **inward**. The rule is non-negotiable:

```
interfaces ─▶ application ─▶ domain ◀─ adapters
   (CLI)        (services)   (ports +     (gphoto2, docker,
                              models)       astropy, cv2, files)
```

- **`domain/`** — contracts + pure logic. Imports **nothing external** (no
  gphoto2, cv2, docker, astropy, argparse, subprocess, exifread). numpy appears
  only under `TYPE_CHECKING`. Contains `models/` (Pydantic value objects),
  `ports/` (ABCs: `Camera`, `Solver`, `SolveRecordRepository`), `optics.py`
  (pure math), `errors.py`.
- **`adapters/`** — implement the ports; the **only** place third-party/I/O
  libraries live. `camera/` (GphotoCamera, gvfs, cli_grab), `solvers/` (Docker,
  Api, fits I/O), `storage/` (sidecar JSON), `exif.py`.
- **`application/`** — use-case services depending on **ports only**, never on a
  concrete adapter. `capture_service.py`, `solve_service.py`, `grab_service.py`.
- **`interfaces/`** — inbound adapters. `cli.py` (argparse). A future FastAPI
  layer goes here and calls the same services.
- **`composition.py`** — the ONLY module that knows a port *and* its concrete
  adapter. All wiring/DI lives here (`build_camera`, `build_solver`,
  `build_capture_service`, `build_repository`).
- `config.py` (infra config), `log/` (logging), `main.py` (entrypoint shim).

Full rationale: `docs/20260722-hexagonal-architecture.md`.

## Conventions

- **Absolute imports** for cross-layer refs (`from camera_orchestrator.domain...`)
  — relocation-proof. Relative only for same-package siblings.
- **A service never imports an adapter.** It takes injected factories/ports;
  `composition.py` supplies the concrete adapter. (e.g. `CaptureService` takes a
  `camera_factory`; the composition root passes `GphotoCamera`.)
- **Camera driver is atomic**: `trigger`, `bulb`, `wait_for_new_files`,
  `list_files`, `download`, `set_capture_target`, `apply`, `status`. Multi-step
  workflows (capture-and-download, sequences) are **composed in the service**,
  not baked into the driver. Rule of thumb: one driver method ≈ one gphoto
  primitive; if gphoto has no single call for it, it's a service composite.
- **DTOs are Pydantic** value objects in `domain/models/` — UI-agnostic so CLI
  and a future API build the same request objects.
- **Persistence is behind a port** (`SolveRecordRepository`); `SidecarSolveRepository`
  is the filesystem impl. No database yet — add a new adapter, not a new call site.

## Testing

- Unit tests use **no hardware**: inject a `MockCamera` (implements the atomic
  `Camera` ABC) via `CaptureService(camera_factory=...)`; `MockSolver` for solves.
- Patch adapters at their *new* module home (e.g.
  `camera_orchestrator.application.grab_service.download`).

## Guardrails (must stay green)

**Use the Makefile for dev tasks** — run these, don't invoke pytest/ruff/mypy directly:

```bash
make test    # pytest — no hardware needed; must pass
make lint    # ruff + mypy — both must pass clean
make fmt     # ruff format
```

- `domain/` must have no runtime external imports (numpy only under TYPE_CHECKING).
- New code lands with its test (e.g. a new port impl gets a round-trip test).
- Prefer editing to match surrounding style; keep docstrings + Field descriptions.
- The Makefile is the interface for dev tooling; the app itself runs via the
  Python CLI (`python main.py --help`), never through make.

## Domain notes (astro / hardware)

- **Capture default is card-only** (`--download` opts into USB transfer). Card-only
  is faster for bulk sub sequences; download drains events + pulls RAW+JPEG.
- **Card-only filenames come from a reconnect + poll, NOT events.** libgphoto2
  caches the in-session directory listing (writes appear only after a reconnect),
  and the camera drops/coalesces `FILE_ADDED` events under rapid card-only fire —
  so neither an in-session `list_files` nor an event drain is reliable. To record
  which card files a card-only run produced, snapshot `list_files()` before firing
  then reopen fresh sessions and poll until the expected count appears
  (`CaptureService._await_new_card_files`). The card is the source of truth. Don't
  "simplify" this back to events. The download path *does* use events reliably
  (per-frame wait + download).
- **Each capture verb sets the image format it needs** (`align` → jpeg, `sequence`
  → raw); nothing is restored, so a command never depends on what the last one
  left the camera in.
- **gvfs must be released** before python-gphoto2 can claim the device (the driver
  does this on connect).
- **`can_capture`** is False for bodies that lock the shutter in a PTP session
  (Canon M50 II → grab-only). The 5D Mark II supports full capture + bulb.
- **Manual focus for astro.** AF hunts in the dark and stalls/refuses. The lens
  AF/MF switch is the control (PTP `focusmode` only reflects it). Follow-up:
  make `trigger()` use the `eosremoterelease` MF path so it never AF-hunts.
- The camera **auto-powers-off**; once off it drops USB and can't be woken over
  USB (needs a physical nudge). Disable auto-power-off on the body for sessions.

## Dev commands

Dependencies/env are managed by **uv** (`pyproject.toml` + `uv.lock`; version is
read from the `VERSION` file). The Makefile is **dev tooling only** and wraps uv;
run the app via the console script or `uv run`.

```bash
make install-dev            # uv sync (runtime + dev group)
make lint                   # uv run ruff + mypy
make test                   # uv run pytest

uv run camera-orchestrator batch <folder> --annotate
uv run camera-orchestrator grab --poll 5
uv run camera-orchestrator capture --status
uv run camera-orchestrator capture --iso 800 --shutter 2 --count 30            # card-only
uv run camera-orchestrator capture --iso 800 --shutter 2 --count 30 --download # to disk
```
