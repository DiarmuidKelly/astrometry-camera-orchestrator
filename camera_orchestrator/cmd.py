"""Command-line interface — argument parsing and command handlers.

All argparse / stdout / sys.exit lives here. Command handlers translate CLI
flags into service-layer calls (CaptureService, solver, grab) and render the
results; they contain no camera or solving logic themselves.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Config
from .grab import GrabError, grab_latest, poll
from .log import get_logger
from .models import CameraStatus, CaptureRequest
from .solve import solve_file
from .solvers import build_solver

IMAGE_SUFFIXES = {".jpg", ".jpeg"}

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

        annotate_out = str(annotate_dir / f"{path.stem}_solved.jpg") if annotate_dir else None

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


def _log_status(status: CameraStatus) -> None:
    log.info("Camera", extra={
        "model": status.model, "battery": status.battery,
        "free_shots": status.free_shots, "can_capture": status.can_capture,
    })


def cmd_capture(args: argparse.Namespace, cfg: Config) -> None:
    # Imported lazily so the rest of the CLI works without python-gphoto2 installed.
    from .camera import CameraError
    from .service import CaptureService

    service = CaptureService()
    try:
        if args.status:
            _log_status(service.status())
            return

        request = CaptureRequest(
            out_dir=args.out or cfg.grab.out_dir,
            iso=args.iso,
            shutter=args.shutter,
            aperture=args.aperture,
            image_format=args.format,
            bulb_seconds=args.bulb,
            count=args.count,
            kind=args.kind,
            download=args.download,
        )
        result = (service.capture_and_download(request)
                  if request.download else service.capture_to_card(request))
    except CameraError as exc:
        log.error(str(exc))
        sys.exit(1)

    _log_status(result.status)
    log.info("Capture done", extra={
        "frames": result.frames_captured, "downloaded": len(result.frames),
    })


def cmd_grab(args: argparse.Namespace, cfg: Config) -> None:
    out_dir = Path(args.out) if args.out else Path(cfg.grab.out_dir)
    interval = args.poll if args.poll is not None else cfg.grab.poll_interval
    try:
        if interval is not None:
            poll(out_dir, interval=interval, force=args.force)
        else:
            grab_latest(out_dir, force=args.force)
    except GrabError as exc:
        log.error(str(exc))
        sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
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

    cap = sub.add_parser("capture", help="Capture a sequence of frames from a tethered camera")
    cap.add_argument("--out", default=None, help="Output directory (default: grab.out_dir from config)")
    cap.add_argument("--iso", default=None, help="ISO setting, e.g. 800")
    cap.add_argument("--shutter", default=None, help="Shutter speed, e.g. 2 or 1/60 (ignored with --bulb)")
    cap.add_argument("--aperture", default=None, help="Aperture f-number, e.g. 4")
    cap.add_argument("--format", choices=["raw", "jpeg", "both"], default=None,
                     help="Image format to shoot (default: leave camera setting unchanged)")
    cap.add_argument("--bulb", metavar="SECONDS", type=float, default=None,
                     help="Bulb exposure length in seconds (overrides --shutter)")
    cap.add_argument("--count", type=int, default=1, help="Number of light frames to capture")
    cap.add_argument("--kind", choices=["light", "dark", "bias"], default="light",
                     help="Frame type label (for logging)")
    cap.add_argument("--download", action="store_true",
                     help="Transfer each frame over USB to --out (default: shoot to the card only; pull later with grab)")
    cap.add_argument("--status", action="store_true", help="Print camera status and exit")

    return parser


def main() -> None:
    args = build_parser().parse_args()

    cfg = Config.load(args.config)

    global log
    log = get_logger("camera_orchestrator.batch",
                     fmt=cfg.logging.format, level=cfg.logging.level)

    if args.command == "grab":
        cmd_grab(args, cfg)
    elif args.command == "capture":
        cmd_capture(args, cfg)
    elif args.command == "batch":
        if args.mode:
            cfg.solver.mode = args.mode
        if args.cpulimit:
            cfg.solver.cpulimit = args.cpulimit
        cmd_batch(args, cfg)
