# astrometry-camera-orchestrator

Python tool for plate-solving astrophotography frames using a dockerised [astrometry.net](https://astrometry.net) solver, with camera image grab support via gphoto2.

## What it does

- Batch plate-solves a folder of JPEG or Canon CR2 RAW images (CR3 not yet supported)
- Writes per-image `_solved.json` sidecars with EXIF, astrometric solution, and observer metadata
- Produces NGC-annotated overlay images
- Uses scale hints derived from EXIF focal length + sensor geometry to speed up solving
- Tethered capture from a Canon DSLR (5D Mark II) via python-gphoto2 — set ISO/shutter/aperture, single or bulb exposures, sequences to card or downloaded (JPEG)
- `align` — shoot one frame, solve it, and report the true centre RA/Dec + an annotated preview to check pointing
- `session` — a multi-phase imaging run (lights, darks, bias) with lens-cap prompts for the calibration phases
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

Run the CLI with uv (no manual venv activation needed):

```bash
uv run camera-orchestrator <command> ...   # installed console script
uv run python main.py <command> ...        # equivalent
```

The examples below use `python main.py …`; prefix them with `uv run`, or run
`uv sync` once and activate `.venv` (`source .venv/bin/activate`).

### Batch solve

```bash
python main.py batch /path/to/images --annotate
python main.py batch /path/to/images --annotate --mode fast   # faster, lower accuracy
```

Full options via `python main.py batch --help`:

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
python main.py grab                   # download latest image to grab.out_dir from config
python main.py grab --poll 5          # poll every 5s, download new images as they appear
python main.py grab --out ~/Pictures  # override output directory
```

Full options via `python main.py grab --help`:

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
python main.py capture --status
python main.py capture --iso 800 --shutter 2 --count 30                 # 30 subs to the card
python main.py capture --iso 800 --shutter 2 --count 30 --download      # download each frame
python main.py capture --bulb 30 --count 20 --download                  # 30s bulb subs
```

Notes:
- Set the lens to **manual focus** for astro — autofocus hunts in the dark.
- Bodies that lock the shutter in a PTP session (e.g. Canon M50 II) are grab-only
  (`can_capture` is False); use `grab` for those.

### Align — check pointing

Shoot one frame, plate-solve it, and report where the camera is actually pointing.
Only the JPEG is pulled to disk (a RAW+JPEG body leaves the RAW on the card); the
solved frame gets a `<stem>_solved.png` annotated overlay next to it.

```bash
python main.py align --iso 800 --shutter 1/60           # frame + solve, log centre RA/Dec
python main.py align --iso 800 --shutter 1/60 --out ~/align
```

Use it to confirm framing before starting a session; re-run after nudging the mount.

### Session — a full imaging run

Run lights, darks, and bias in one command. Each phase with a count of 0 is
skipped, so you can shoot lights-only per target and calibration-only once a night.
Bias frames ignore `--shutter` and force the fastest speed (1/4000); darks inherit
the light exposure. Calibration phases prompt you to cap the lens first.

```bash
# One target: 60×2s lights (card-only, fast cadence)
python main.py session --iso 800 --shutter 2 --lights 60

# Calibration once per night — matched to the lights' ISO + exposure
python main.py session --iso 800 --shutter 2 --darks 20 --bias 20
```

Typical multi-target night: shoot calibration **once** (darks + bias at your
working ISO/exposure), then per object run `align` to frame followed by a
lights-only `session`. The same darks/bias masters apply to every target that
shares that ISO and exposure.

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
LOG_LEVEL=DEBUG LOG_FORMAT=json python main.py batch /path/to/images
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
`session` are validated on the Canon 5D Mark II. Camera grab (one-shot and polled)
works with the Canon M50 II, which is grab-only (it locks the shutter in a PTP
session).

## License

GPL v3 — see [LICENSE](LICENSE).
