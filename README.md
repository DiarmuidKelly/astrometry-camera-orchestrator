# astrometry-camera-orchestrator

Early-stage Python tool for plate-solving astrophotography frames using a dockerised [astrometry.net](https://astrometry.net) solver. Designed to grow into a full capture-and-solve pipeline for Canon DSLRs.

## What it does

- Batch plate-solves a folder of images (JPEG, CR2, TIFF)
- Writes per-image `_solved.json` sidecars with EXIF, astrometric solution, and observer metadata
- Optionally produces NGC-annotated overlay images
- Uses scale hints derived from EXIF focal length + sensor geometry to speed up solving

## Requirements

- Python 3.11+
- [diarmuidk/astrometry-dockerised-solver](https://github.com/DiarmuidKelly/astrometry-dockerised-solver) — the upstream Docker solver this project is built around
- Astrometry index files (e.g. `index-4109.fits`, `index-4110.fits`)

## Setup

```bash
make install-dev
cp config.example.yaml config.yaml   # edit paths and optics
```

## Usage

```bash
make batch FOLDER=/path/to/images
make batch-fast FOLDER=/path/to/images        # faster, lower accuracy
make batch FOLDER=/path/to/images CONFIG=my.yaml
```

## Config

`config.yaml` holds static settings — solver image, index dir, optics (focal length, sensor width), observer location, and search region hints. The image folder is always passed as a CLI argument.

## Development

```bash
make test                  # unit tests
make test-integration      # requires Docker + index files
make lint                  # ruff + mypy
make fmt                   # ruff format
```

## Status

Early development. Batch solving works. Camera tethering (Canon 5D Mark II via gphoto2) is not yet implemented.

## License

GPL v3 — see [LICENSE](LICENSE).
