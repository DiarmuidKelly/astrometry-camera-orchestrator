# astrometry-camera-orchestrator

Early-stage Python tool for plate-solving astrophotography frames using a dockerised [astrometry.net](https://astrometry.net) solver. Designed to grow into a full capture-and-solve pipeline with camera tethering via gphoto2.

## What it does

- Batch plate-solves a folder of JPEG images (CR2/TIFF/RAW untested)
- Writes per-image `_solved.json` sidecars with EXIF, astrometric solution, and observer metadata
- Optionally produces NGC-annotated overlay images
- Uses scale hints derived from EXIF focal length + sensor geometry to speed up solving

## Requirements

- Python 3.11+
- [diarmuidk/astrometry-dockerised-solver](https://github.com/DiarmuidKelly/astrometry-dockerised-solver) — the upstream Docker solver this project is built around
- Astrometry index files (e.g. `index-4109.fits`, `index-4110.fits`)

## Setup

```bash
make install
cp config.example.yaml config.yaml   # edit paths and optics
```

## Usage

```bash
make batch FOLDER=/path/to/images
make batch-fast FOLDER=/path/to/images        # faster, lower accuracy
make batch FOLDER=/path/to/images CONFIG=my.yaml
```

Both targets call `python main.py batch` under the hood. Full options:

```
positional arguments:
  folder                Folder containing images

options:
  --config CONFIG       Config YAML path (default: config.yaml)
  --annotate            Save annotated overlay to <folder>/annotated/
  --mode {fast,accurate}
                        Override solver mode from config
  --cpulimit CPULIMIT   Override solver CPU time limit in seconds
  --reprocess           Re-solve images that already have a sidecar JSON
```

## Config

`config.yaml` holds static settings — solver image, index dir, optics (focal length, sensor width), observer location, search region hints, and logging defaults. The image folder is always passed as a CLI argument.

### Environment variable overrides

| Variable | Values | Description |
|---|---|---|
| `LOG_LEVEL` | `DEBUG` `INFO` `WARNING` `ERROR` | Override log verbosity |
| `LOG_FORMAT` | `text` `json` | Override log format (json = one JSON object per line) |

```bash
LOG_LEVEL=DEBUG LOG_FORMAT=json make batch FOLDER=/path/to/images
```

## Development

```bash
make install-dev           # install runtime + dev dependencies
make test                  # unit tests
make test-integration      # requires Docker + index files
make lint                  # ruff + mypy
make fmt                   # ruff format
```

## Status

Early development. Batch solving works. Camera tethering (via gphoto2) is not yet implemented.

## License

GPL v3 — see [LICENSE](LICENSE).
