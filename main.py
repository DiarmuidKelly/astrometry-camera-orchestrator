#!/usr/bin/env python3
"""camera-orchestrator — entry point.

Usage:
    python main.py --config config.yaml batch <folder> [--annotate] [--mode fast|accurate]
    python main.py --config config.yaml grab [--out ./incoming] [--force] [--poll SECONDS]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from camera_orchestrator.config import Config
from camera_orchestrator.grab import grab_latest, poll
from camera_orchestrator.log import get_logger
from camera_orchestrator.solve import solve_file
from camera_orchestrator.solvers import build_solver

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".cr2", ".cr3", ".tif", ".tiff", ".fits", ".png"}

log = get_logger("camera_orchestrator.batch")  # reconfigured after config load in main()


def cmd_batch(args: argparse.Namespace, cfg: Config) -> None:
    solver = build_solver(cfg)

    folder = Path(args.folder)
    images = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    )

    if not images:
        log.error("No images found", extra={"folder": str(folder)})
        sys.exit(1)

    sidecar_dir = folder / "annotated" if args.annotate else folder

    if not args.reprocess:
        pending = [p for p in images if not (sidecar_dir / f"{p.stem}_solved.json").exists()]
        skipped = len(images) - len(pending)
        if skipped:
            log.info("Skipping already-solved images — pass --reprocess to re-solve all",
                     extra={"skipped": skipped})
        images = pending

    if not images:
        log.info("All images already solved")
        sys.exit(0)

    log.info("Starting batch solve",
             extra={"images": len(images), "solver": cfg.solver.image, "mode": cfg.solver.mode})

    if cfg.search.ra_deg is not None:
        log.info("Search hint",
                 extra={"ra": cfg.search.ra_deg, "dec": cfg.search.dec_deg,
                        "radius_deg": cfg.search.radius_deg})

    annotate_dir = folder / "annotated" if args.annotate else None
    if annotate_dir:
        annotate_dir.mkdir(exist_ok=True)
        log.info("Annotated output", extra={"dir": str(annotate_dir)})

    summary = []

    for i, path in enumerate(images, 1):
        log.info("Solving", extra={"image": path.name, "index": i, "total": len(images)})

        out_ext = ".png" if path.suffix.lower() in {".cr2", ".cr3", ".tif", ".tiff", ".fits"} else ".jpg"
        annotate_out = str(annotate_dir / f"{path.stem}_solved{out_ext}") if annotate_dir else None

        job = solve_file(str(path), solver, cfg, annotate_out=annotate_out)
        record = job.to_record(cfg)

        sidecar_path = sidecar_dir / f"{path.stem}_solved.json"
        sidecar_path.write_text(record.model_dump_json(indent=2))
        summary.append(record.model_dump())

        if job.solved and record.solve is not None:
            log.info("Solved", extra={
                "image": path.name,
                "ra": round(record.solve.center_ra_deg, 4),
                "dec": round(record.solve.center_dec_deg, 4),
                "scale": round(record.solve.scale_arcsec_per_px, 2),
            })
        else:
            log.warning("No solution", extra={
                "image": path.name,
                "error": record.error or "solver returned None",
            })

    (folder / "solve_results.json").write_text(json.dumps(summary, indent=2))

    solved = sum(1 for r in summary if r["solved"])
    log.info("Batch complete",
             extra={"solved": solved, "total": len(summary),
                    "results": str(folder / "solve_results.json")})


def main() -> None:
    parser = argparse.ArgumentParser(prog="camera-orchestrator")
    parser.add_argument("--config", default="config.yaml", help="Config YAML path")

    sub = parser.add_subparsers(dest="command", required=True)

    batch = sub.add_parser("batch", help="Plate-solve all images in a folder")
    batch.add_argument("folder", help="Folder containing images")
    batch.add_argument("--annotate", action="store_true",
                       help="Save annotated overlay to <folder>/annotated/")
    batch.add_argument("--mode", choices=["fast", "accurate"], default=None,
                       help="Override solver mode from config")
    batch.add_argument("--cpulimit", type=int, default=None,
                       help="Override solver CPU time limit in seconds")
    batch.add_argument("--reprocess", action="store_true",
                       help="Re-solve images that already have a sidecar JSON")

    grab_p = sub.add_parser("grab", help="Download images from the connected camera")
    grab_p.add_argument("--out", default=None, help="Output directory (default: grab.out_dir from config)")
    grab_p.add_argument("--force", action="store_true", help="Overwrite if file already exists")
    grab_p.add_argument("--poll", metavar="SECONDS", type=float, default=None,
                        help="Poll camera every N seconds (default: grab.poll_interval from config)")

    args = parser.parse_args()

    cfg = Config.load(args.config)

    global log
    log = get_logger("camera_orchestrator.batch",
                     fmt=cfg.logging.format, level=cfg.logging.level)

    if args.command == "grab":
        out_dir = Path(args.out) if args.out else Path(cfg.grab.out_dir)
        interval = args.poll if args.poll is not None else cfg.grab.poll_interval
        if interval is not None:
            poll(out_dir, interval=interval, force=args.force)
        else:
            grab_latest(out_dir, force=args.force)
        return

    if args.command == "batch":
        if args.mode:
            cfg.solver.mode = args.mode
        if args.cpulimit:
            cfg.solver.cpulimit = args.cpulimit
        cmd_batch(args, cfg)


if __name__ == "__main__":
    main()
