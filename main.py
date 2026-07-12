#!/usr/bin/env python3
"""camera-orchestrator — entry point.

Usage:
    python main.py batch <folder> [--config config.yaml] [--annotate] [--mode fast|accurate]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from camera_orchestrator.config import Config
from camera_orchestrator.solve import solve_file
from camera_orchestrator.solvers import build_solver

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".cr2", ".cr3", ".tif", ".tiff", ".fits", ".png"}


def cmd_batch(args: argparse.Namespace, cfg: Config) -> None:
    solver = build_solver(cfg)

    folder = Path(args.folder)
    images = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    )

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

        out_ext = ".png" if path.suffix.lower() in {".cr2", ".cr3", ".tif", ".tiff", ".fits"} else ".jpg"
        annotate_out = str(annotate_dir / f"{path.stem}_solved{out_ext}") if annotate_dir else None

        job = solve_file(str(path), solver, cfg, annotate_out=annotate_out)
        record = job.to_record(cfg)

        sidecar_dir = annotate_dir if annotate_dir else folder
        sidecar_path = sidecar_dir / f"{path.stem}_solved.json"
        sidecar_path.write_text(record.model_dump_json(indent=2))

        summary.append(record.model_dump())

        if job.solved and record.solve is not None:
            print(f"RA={record.solve.center_ra_deg:.4f}° "
                  f"Dec={record.solve.center_dec_deg:.4f}° "
                  f"scale={record.solve.scale_arcsec_per_px:.2f}\"/px")
        else:
            print(f"no solution — {record.error or 'solver returned None'}")

    (folder / "solve_results.json").write_text(json.dumps(summary, indent=2))

    solved = sum(1 for r in summary if r["solved"])
    print(f"\n{solved}/{len(summary)} solved → {folder}/solve_results.json")


def main() -> None:
    parser = argparse.ArgumentParser(prog="camera-orchestrator")
    parser.add_argument("--config", default="config.yaml", help="Config YAML path")

    sub = parser.add_subparsers(dest="command", required=True)

    batch = sub.add_parser("batch", help="Plate-solve all images in a folder")
    batch.add_argument("folder", help="Folder containing images")
    batch.add_argument("--config", default="config.yaml", help="Config YAML path")
    batch.add_argument("--annotate", action="store_true",
                       help="Save annotated overlay to <folder>/annotated/")
    batch.add_argument("--mode", choices=["fast", "accurate"], default=None,
                       help="Override solver mode from config")
    batch.add_argument("--cpulimit", type=int, default=None,
                       help="Override solver CPU time limit in seconds")

    args = parser.parse_args()
    cfg = Config.load(args.config)

    if args.command == "batch":
        if args.mode:
            cfg.solver.mode = args.mode
        if args.cpulimit:
            cfg.solver.cpulimit = args.cpulimit
        cmd_batch(args, cfg)


if __name__ == "__main__":
    main()
