# astrometry-camera-orchestrator

Python tool for plate-solving astrophotography frames using a dockerised [astrometry.net](https://astrometry.net) solver, with camera image grab support via gphoto2.

## What it does

- Batch plate-solves a folder of JPEG images (CR3 decode not yet supported)
- Writes per-image `_solved.json` sidecars with EXIF, astrometric solution, and observer metadata
- Produces NGC-annotated overlay images
- Uses scale hints derived from EXIF focal length + sensor geometry to speed up solving
- Tethered capture from a Canon DSLR (5D Mark II) via python-gphoto2 — set ISO/shutter/aperture, single or bulb exposures, sequences to card or downloaded (JPEG)
- Grabs the latest image from a connected Canon camera via gphoto2 (one-shot or polled)

## Requirements

- Python 3.11+
- Docker — [diarmuidk/astrometry-dockerised-solver](https://github.com/DiarmuidKelly/astrometry-dockerised-solver)
- Astrometry index files (e.g. `index-4109.fits`, `index-4110.fits`)
- `gphoto2` and `gio` — for camera grab (`sudo apt install gphoto2`)

## Setup

```bash
make install
cp config.example.yaml config.yaml   # edit paths and optics
```

## Usage

### Batch solve

```bash
make batch FOLDER=/path/to/images
make batch-fast FOLDER=/path/to/images   # faster, lower accuracy
```

Full options via `python main.py --config config.yaml batch --help`:

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

Requires the camera connected via USB in mass storage mode. gvfs is unmounted automatically before listing files.

```bash
make grab                # download latest image to grab.out_dir from config
make grab POLL=5         # poll every 5 seconds, download new images as they appear
make grab OUT=~/Pictures # override output directory
```

Note: Make uses `VAR=value` syntax, not `--flags` (e.g. `make grab POLL=5`, not `make grab --poll=5`).

Full options via `python main.py --config config.yaml grab --help`:

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
LOG_LEVEL=DEBUG LOG_FORMAT=json make batch FOLDER=/path/to/images
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

Batch solving works and has been tested against JPEG files. Tethered capture
(single, bulb, and sequences; card-only or downloaded) is validated on the Canon
5D Mark II. Camera grab (one-shot and polled) works with the Canon M50 II, which
is grab-only (it locks the shutter in a PTP session).

## License

GPL v3 — see [LICENSE](LICENSE).
