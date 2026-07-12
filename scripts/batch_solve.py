#!/usr/bin/env python3
"""Batch plate-solve all images in a folder.

For each image, writes a _solved.json sidecar and optionally a _solved.png
annotated overlay into <folder>/annotated/. A summary solve_results.json is
also written to the folder.

Usage:
    python scripts/batch_solve.py /path/to/images --config config.yaml --annotate
    python scripts/batch_solve.py /path/to/images --config config.yaml --mode fast
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from camera_orchestrator.config import Config
from camera_orchestrator.pipeline import solve_file
from camera_orchestrator.solvers import build_solver

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".cr2", ".cr3", ".tif", ".tiff", ".fits", ".png"}


def _sidecar(path: Path, job, cfg: Config) -> dict:
    return {
        "original_file": path.name,
        "exif": {
            "focal_mm": job.exif.focal_mm if job.exif else None,
            "focal_mm_equiv": round(job.exif.focal_mm * 1.6, 1)
                              if (job.exif and job.exif.focal_mm
                                  and cfg.optics.sensor_width_mm
                                  and cfg.optics.sensor_width_mm < 30) else None,
            "iso": job.exif.iso if job.exif else None,
            "shutter_sec": job.exif.shutter_sec if job.exif else None,
            "aperture": job.exif.aperture if job.exif else None,
            "datetime": job.exif.datetime if job.exif else None,
        },
        "solved": job.solved,
        "solve": {
            "ra_deg": job.result.center_ra_deg,
            "dec_deg": job.result.center_dec_deg,
            "scale_arcsec_per_px": job.result.scale_arcsec_per_px,
            "width_px": job.result.width,
            "height_px": job.result.height,
        } if job.result else None,
        "hints_used": {
            "scale_low": job.hints.scale_low,
            "scale_high": job.hints.scale_high,
            "ra_deg": job.hints.ra_deg,
            "dec_deg": job.hints.dec_deg,
            "radius_deg": job.hints.radius_deg,
        },
        "observer": {
            "lat": cfg.location.lat,
            "lon": cfg.location.lon,
        },
        "solver_mode": cfg.solver.mode,
        "error": job.error,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch plate-solve images in a folder")
    parser.add_argument("folder", help="Folder containing images")
    parser.add_argument("--config", default="config.yaml", help="Config YAML path")
    parser.add_argument("--annotate", action="store_true",
                        help="Save annotated _solved.png to <folder>/annotated/")
    parser.add_argument("--mode", choices=["fast", "accurate"], default=None,
                        help="Override solver mode from config")
    parser.add_argument("--cpulimit", type=int, default=None,
                        help="Override solver CPU time limit in seconds")
    args = parser.parse_args()

    cfg = Config.load(args.config)
    if args.mode:
        cfg.solver.mode = args.mode
    if args.cpulimit:
        cfg.solver.cpulimit = args.cpulimit

    solver = build_solver(cfg)

    folder = Path(args.folder)
    images = sorted(p for p in folder.iterdir()
                    if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)

    if not images:
        print(f"No images found in {folder}")
        sys.exit(1)

    print(f"Found {len(images)} image(s) — solver: {cfg.solver.image} [{cfg.solver.mode}]")
    if cfg.search.ra_deg is not None:
        print(f"Search hint: RA={cfg.search.ra_deg:.4f} Dec={cfg.search.dec_deg:.4f} "
              f"radius={cfg.search.radius_deg}°")

    annotate_dir = folder / "annotated" if args.annotate else None
    if annotate_dir:
        annotate_dir.mkdir(exist_ok=True)
        print(f"Annotated output → {annotate_dir}/")

    summary = []

    for i, path in enumerate(images, 1):
        print(f"[{i}/{len(images)}] {path.name} ... ", end="", flush=True)

        stem = path.stem
        src_suffix = path.suffix.lower()
        out_ext = ".png" if src_suffix in {".cr2", ".cr3", ".tif", ".tiff", ".fits"} else ".jpg"
        annotate_out = str(annotate_dir / f"{stem}_solved{out_ext}") if annotate_dir else None

        job = solve_file(str(path), solver, cfg, annotate_out=annotate_out)

        data = _sidecar(path, job, cfg)

        # Per-file JSON sidecar in the annotated dir (or alongside original if no annotate dir).
        sidecar_dir = annotate_dir if annotate_dir else folder
        sidecar_path = sidecar_dir / f"{stem}_solved.json"
        with open(sidecar_path, "w") as f:
            json.dump(data, f, indent=2)

        summary.append(data)

        if job.solved:
            print(f"RA={job.result.center_ra_deg:.4f}° Dec={job.result.center_dec_deg:.4f}° "
                  f"scale={job.result.scale_arcsec_per_px:.2f}\"/px")
        else:
            print(f"no solution — {job.error or 'solver returned None'}")

    summary_path = folder / "solve_results.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    solved = sum(1 for r in summary if r["solved"])
    print(f"\n{solved}/{len(summary)} solved")
    print(f"Summary  → {summary_path}")
    if annotate_dir:
        print(f"Sidecars → {annotate_dir}/<name>_solved.json")
        print(f"Annotated → {annotate_dir}/<name>_solved.png")


if __name__ == "__main__":
    main()
