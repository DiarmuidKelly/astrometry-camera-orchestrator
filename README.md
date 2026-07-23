# astrometry-camera-orchestrator

Python tool for plate-solving astrophotography frames using a dockerised [astrometry.net](https://astrometry.net) solver, with camera image grab support via gphoto2.

## What it does

- Batch plate-solves a folder of JPEG or Canon CR2 RAW images (CR3 not yet supported)
- Writes per-image `_solved.json` sidecars with EXIF, astrometric solution, and observer metadata
- Produces NGC-annotated overlay images
- Uses scale hints derived from EXIF focal length + sensor geometry to speed up solving
- Tethered capture from a Canon DSLR (5D Mark II) via python-gphoto2 — set ISO/shutter/aperture, single or bulb exposures, sequences to card or downloaded (JPEG)
- `align` — shoot one frame, solve it, and report the true centre RA/Dec + an annotated preview to check pointing
- `sequence` — a multi-phase imaging run (lights, darks, bias) recorded into a per-session `session.json` manifest, with lens-cap prompts for the calibration phases
- Grabs the latest image from a connected Canon camera via gphoto2 (one-shot or polled)

## Requirements

- Python 3.11+
- Docker — [diarmuidk/astrometry-dockerised-solver](https://github.com/DiarmuidKelly/astrometry-dockerised-solver)
- Astrometry index files (e.g. `index-4109.fits`, `index-4110.fits`)
- `gphoto2` and `gio` — for camera grab (`sudo apt install gphoto2`)

## Setup

Uses [uv](https://docs.astral.sh/uv/) for dependency and environment management.

```bash
uv sync                              # create the env + install deps
cp config.example.yaml config.yaml   # edit paths and optics
```

## Before a session — prepare your config

`config.yaml` is the per-session control surface: `align` and `batch` read the
optics and search hint from it at solve time. Set these before you run anything —
a wrong value here doesn't error, it just makes solves slow or fail.

1. **`solver.index_dir`** — path to your astrometry index files. Nothing solves without it.
2. **`optics.sensor_width_mm`** — must match the body you're shooting. This is a
   single global value, so **change it when you switch cameras**:
   - `35.8` — Canon **5D Mark II** (full-frame)
   - `22.3` — Canon **M50 II** (APS-C)

   With `focal_mm: null`, the plate scale is derived from EXIF focal length × this
   width; the wrong sensor width skews the scale hint (~1.6× between the two bodies)
   and the solver searches the wrong scale.
3. **`search.ra_deg` / `dec_deg` / `radius_deg`** — centre on tonight's region so
   solves are fast. Widen `radius_deg`, or null the RA/Dec, for a blind solve of a
   target well outside that circle.
4. **`location.lat` / `lon`** — observer coordinates recorded in the `_solved.json` sidecars.

(A future UI/API will pass the search region per-request instead of via config;
until then, edit it here per session.)

## Usage

Every command runs the same way — through uv, no venv activation needed:

```bash
uv run camera-orchestrator <command> [options]
uv run camera-orchestrator --help          # list all commands
```

That's the only invocation used below.

### Quick start — a night's imaging

Per target, two steps: **align** to check/record pointing, then **sequence** to
shoot. `--name` groups both into one session folder (`<out>/<date>-<name>/`).

```bash
# 1. Aim, then check where you're pointing (writes an annotated preview + the target)
uv run camera-orchestrator align --name orion --iso 800 --shutter 1/60

# 2. Shoot the run: lights + calibration (bias auto-uses the fastest shutter)
uv run camera-orchestrator sequence --name orion --iso 800 --shutter 2 \
    --lights 60 --darks 20 --bias 20

# Next target — new name, repeat
uv run camera-orchestrator align    --name m31 --iso 800 --shutter 1/60
uv run camera-orchestrator sequence --name m31 --iso 800 --shutter 2 --lights 60
```

Set the lens to **manual focus** first. Frames go to the card by default; each
session folder gets a `session.json` recording the target, timings, and which
files belong to which phase. The other commands below (`batch`, `grab`,
`capture`) are lower-level tools; align + sequence are the nightly workflow.

### Batch solve

```bash
uv run camera-orchestrator batch /path/to/images --annotate
uv run camera-orchestrator batch /path/to/images --annotate --mode fast   # faster, lower accuracy
```

Full options via `uv run camera-orchestrator batch --help`:

```
positional arguments:
  folder                Folder containing images

options:
  --annotate            Save annotated overlay to <folder>/annotated/
  --mode {fast,accurate}
                        Override solver mode from config
  --cpulimit CPULIMIT   Override solver CPU time limit in seconds
  --reprocess           Re-solve images that already have a sidecar JSON
```

### Camera grab

gvfs is unmounted automatically before listing files.

```bash
uv run camera-orchestrator grab                   # download latest image to grab.out_dir from config
uv run camera-orchestrator grab --poll 5          # poll every 5s, download new images as they appear
uv run camera-orchestrator grab --out ~/Pictures  # override output directory
```

Full options via `uv run camera-orchestrator grab --help`:

```
options:
  --out OUT       Output directory (default: grab.out_dir from config)
  --force         Overwrite if file already exists
  --poll SECONDS  Poll camera every N seconds for new files
```

### Tethered capture

Drive a connected Canon DSLR (validated on the 5D Mark II) over USB. Capture is
card-only by default (fast for bulk sequences); `--download` transfers to `--out`.

```bash
uv run camera-orchestrator capture --status
uv run camera-orchestrator capture --iso 800 --shutter 2 --count 30                 # 30 subs to the card
uv run camera-orchestrator capture --iso 800 --shutter 2 --count 30 --download      # download each frame
uv run camera-orchestrator capture --bulb 30 --count 20 --download                  # 30s bulb subs
```

Notes:
- Set the lens to **manual focus** for astro — autofocus hunts in the dark.
- Bodies that lock the shutter in a PTP session (e.g. Canon M50 II) are grab-only
  (`can_capture` is False); use `grab` for those.

### Sessions: `align` + `sequence`

A **session** is a folder (`<out>/<YYYYMMDD>-<name>/`) that owns a `session.json`
manifest. Two verbs write into it: `align` records the solved target, `sequence`
fires the phases and records what they produced. Pass `--name` to record into a
session; omit it for a loose, unrecorded run in the parent directory.

**`align` — check pointing.** Shoot one frame, plate-solve it, and report where
the camera is actually pointing. Only the JPEG is pulled to disk (a RAW+JPEG body
leaves the RAW on the card); the solved frame gets a `<stem>_solved.png` overlay.

```bash
uv run camera-orchestrator align --iso 800 --shutter 1/60                 # loose check, nothing recorded
uv run camera-orchestrator align --iso 800 --shutter 1/60 --name orion    # records the target into orion's session
```

Re-run freely to refine framing — each align overwrites the session's target.
Once the session has sequenced frames the target **locks**; a later `align --name
orion` refuses unless you pass `--force` (so you can't rewrite the target of a run
that's already in the can).

**`sequence` — a full imaging run.** Fire lights, darks, and bias. Each phase with
a count of 0 is skipped, so you can shoot lights-only per target and
calibration-only once a night. Frames shoot RAW; bias ignores `--shutter` and
forces the fastest speed (1/4000); darks inherit the light exposure. Calibration
phases prompt you to cap the lens first. Card-only by default — the frames' card
filenames are still recorded in the manifest (best-effort: the camera is
reconnected and its card polled until the frames appear).

```bash
# One target: 60×2s lights, recorded into orion's session
uv run camera-orchestrator sequence --iso 800 --shutter 2 --lights 60 --name orion

# Calibration once per night — matched to the lights' ISO + exposure
uv run camera-orchestrator sequence --iso 800 --shutter 2 --darks 20 --bias 20 --name cal
```

Typical multi-target night: shoot calibration **once** (darks + bias at your
working ISO/exposure), then per object `align --name <obj>` to frame and record
the target, followed by a lights-only `sequence --name <obj>`. The same darks/bias
masters apply to every target that shares that ISO and exposure, and each session
folder carries a `session.json` describing exactly what it holds.

## Config

`config.yaml` holds static settings. The image folder (for batch) and output directory (for grab) are passed as CLI arguments or set in config.

```yaml
grab:
  out_dir: ./incoming     # where downloaded images are saved
  poll_interval: null     # set to e.g. 5 to enable polling by default
```

### Environment variable overrides

| Variable | Values | Description |
|---|---|---|
| `LOG_LEVEL` | `DEBUG` `INFO` `WARNING` `ERROR` | Override log verbosity |
| `LOG_FORMAT` | `text` `json` | Override log format (json = one JSON object per line) |

```bash
LOG_LEVEL=DEBUG LOG_FORMAT=json uv run camera-orchestrator batch /path/to/images
```

## Architecture

Hexagonal (ports & adapters). Dependencies point inward; the `domain/` layer
imports nothing external, and third-party/I/O libraries live only in `adapters/`.

```
camera_orchestrator/
  domain/         contracts + pure logic — models/, ports/, optics.py, errors.py
  adapters/       implement the ports (gphoto2, docker, astropy, files) —
                  camera/, solvers/, storage/, exif.py
  application/    use-case services depending on ports only —
                  capture_service, solve_service, grab_service
  interfaces/     inbound adapters — cli.py (argparse)
  composition.py  the one module wiring ports -> concrete adapters (DI root)
  config.py, log/
main.py           entrypoint shim
```

Conventions and the dependency rule are documented in
[`CLAUDE.md`](CLAUDE.md); the full design rationale and migration record is in
[`docs/20260722-hexagonal-architecture.md`](docs/20260722-hexagonal-architecture.md).

## Development

```bash
make install-dev   # install runtime + dev dependencies
make test          # unit tests
make lint          # ruff + mypy
make fmt           # ruff format
```

Integration tests require Docker and astrometry index files:

```bash
make test-integration
```

## Status

Batch solving works and has been tested against JPEG and CR2 files. Tethered
capture (single, bulb, and sequences; card-only or downloaded), `align`, and
`sequence` are validated on the Canon 5D Mark II. Camera grab (one-shot and
polled) works with the Canon M50 II, which is grab-only (it locks the shutter in a
PTP session).

## License

GPL v3 — see [LICENSE](LICENSE).
