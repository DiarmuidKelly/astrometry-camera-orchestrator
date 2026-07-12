#!/usr/bin/env python3
"""Batch plate-solve all images in a folder.

Reads EXIF focal length per file, builds scale hints automatically, runs the
Docker solver, and writes a solve_results.json alongside the images.

Usage:
    python scripts/batch_solve.py /path/to/images --config config.yaml
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from camera_orchestrator.config import Config
from camera_orchestrator.pipeline import solve_file
from camera_orchestrator.solvers import DockerSolver

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".cr2", ".cr3", ".tif", ".tiff", ".fits", ".png"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch plate-solve images in a folder")
    parser.add_argument("folder", help="Folder containing images")
    parser.add_argument("--config", default="config.yaml", help="Config YAML path")
    parser.add_argument("--out", default=None,
                        help="Output JSON path (default: <folder>/solve_results.json)")
    parser.add_argument("--annotate", action="store_true",
                        help="Save annotated PNGs to <folder>/annotated/")
    parser.add_argument("--cpulimit", type=int, default=None,
                        help="Override solver CPU time limit in seconds")
    args = parser.parse_args()

    cfg = Config.load(args.config)
    if args.cpulimit:
        cfg.solver.cpulimit = args.cpulimit

    solver = DockerSolver(
        image=cfg.solver.image,
        index_dir=cfg.solver.index_dir,
        cpulimit=cfg.solver.cpulimit,
    )

    folder = Path(args.folder)
    images = sorted(p for p in folder.iterdir()
                    if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)

    if not images:
        print(f"No images found in {folder}")
        sys.exit(1)

    print(f"Found {len(images)} image(s) — solver: {cfg.solver.image}")
    if cfg.search.ra_deg is not None:
        print(f"Search hint: RA={cfg.search.ra_deg:.4f} Dec={cfg.search.dec_deg:.4f} "
              f"radius={cfg.search.radius_deg}°")

    results = []

    annotate_dir = folder / "annotated" if args.annotate else None
    if annotate_dir:
        annotate_dir.mkdir(exist_ok=True)
        print(f"Annotated PNGs → {annotate_dir}/")

    for i, path in enumerate(images, 1):
        print(f"[{i}/{len(images)}] {path.name} ... ", end="", flush=True)
        annotate_out = str(annotate_dir / path.with_suffix(".png").name) if annotate_dir else None
        job = solve_file(str(path), solver, cfg, annotate_out=annotate_out)

        entry: dict = {
            "file": path.name,
            "exif": {
                "focal_mm": job.exif.focal_mm if job.exif else None,
                "iso": job.exif.iso if job.exif else None,
                "shutter_sec": job.exif.shutter_sec if job.exif else None,
                "aperture": job.exif.aperture if job.exif else None,
                "datetime": job.exif.datetime if job.exif else None,
            },
            "solved": job.solved,
            "result": {
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
            "error": job.error,
        }
        results.append(entry)

        if job.solved:
            print(f"RA={job.result.center_ra_deg:.4f}° Dec={job.result.center_dec_deg:.4f}° "
                  f"scale={job.result.scale_arcsec_per_px:.2f}\"/px")
        else:
            print(f"no solution — {job.error or 'solver returned None'}")

    out_path = args.out or str(folder / "solve_results.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)

    solved = sum(1 for r in results if r["solved"])
    print(f"\n{solved}/{len(results)} solved → {out_path}")


if __name__ == "__main__":
    main()
