"""Command-line interface — argument parsing and command handlers.

All argparse / stdout / sys.exit lives here. Command handlers translate CLI
flags into service-layer calls (CaptureService, solver, grab) and render the
results; they contain no camera or solving logic themselves.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from camera_orchestrator.application.batch_service import BatchSolveResult, BatchSolveService
from camera_orchestrator.application.grab_service import grab_latest, poll
from camera_orchestrator.application.session_paths import resolve_session
from camera_orchestrator.application.solve_service import solve_file
from camera_orchestrator.composition import (
    build_align_service,
    build_capture_service,
    build_repository,
    build_sequence_service,
    build_solver,
)
from camera_orchestrator.config import Config
from camera_orchestrator.domain.errors import CameraError, GrabError
from camera_orchestrator.domain.models.align import AlignRequest
from camera_orchestrator.domain.models.camera import CameraStatus, CaptureRequest
from camera_orchestrator.domain.models.session import SequenceRequest
from camera_orchestrator.domain.models.solve import SolveRecord
from camera_orchestrator.log import get_logger

log = get_logger("camera_orchestrator.batch")  # reconfigured after config load in main()


def cmd_batch(args: argparse.Namespace, cfg: Config) -> None:
    service = BatchSolveService(
        solver_factory=build_solver,
        repository=build_repository(),
        cfg=cfg,
        mode=args.mode,
        cpulimit=args.cpulimit,
    )
    folder = Path(args.folder)
    annotate_dir = folder / "annotated" if args.annotate else None

    def on_plan(pending: int, skipped: int) -> None:
        if skipped:
            log.info("Skipping already-solved images — pass --reprocess to re-solve all",
                     extra={"skipped": skipped})
        if not pending:
            return
        log.info("Starting batch solve",
                 extra={"images": pending, "solver": service.cfg.solver.image,
                        "mode": service.cfg.solver.mode})
        if service.cfg.search.ra_deg is not None:
            log.info("Search hint",
                     extra={"ra": service.cfg.search.ra_deg, "dec": service.cfg.search.dec_deg,
                            "radius_deg": service.cfg.search.radius_deg})
        if annotate_dir:
            log.info("Annotated output", extra={"dir": str(annotate_dir)})

    def on_image_start(index: int, total: int, path: Path) -> None:
        log.info("Solving", extra={"image": path.name, "index": index, "total": total})

    def on_image(index: int, total: int, record: SolveRecord) -> None:
        if record.solved and record.solve is not None:
            log.info("Solved", extra={
                "image": record.original_file,
                "ra": round(record.solve.center_ra_deg, 4),
                "dec": round(record.solve.center_dec_deg, 4),
                "scale": round(record.solve.scale_arcsec_per_px, 2),
            })
        else:
            log.warning("No solution", extra={
                "image": record.original_file,
                "error": record.error or "solver returned None",
            })

    result = service.run(
        str(folder),
        annotate=args.annotate,
        reprocess=args.reprocess,
        on_plan=on_plan,
        on_image_start=on_image_start,
        on_image=on_image,
    )

    _render_batch_result(result)


def _render_batch_result(result: BatchSolveResult) -> None:
    """Map a batch outcome onto log lines and the CLI's exit codes."""
    if result.status == "no_images":
        log.error("No images found", extra={"folder": result.folder})
        sys.exit(1)
    if result.status == "all_solved":
        log.info("All images already solved")
        sys.exit(0)

    log.info("Batch complete",
             extra={"solved": result.solved, "total": len(result.records),
                    "results": result.results_path})


def _log_status(status: CameraStatus) -> None:
    log.info("Camera", extra={
        "model": status.model, "battery": status.battery,
        "free_shots": status.free_shots, "can_capture": status.can_capture,
    })


def cmd_capture(args: argparse.Namespace, cfg: Config) -> None:
    service = build_capture_service()
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


def _resolve_session(args: argparse.Namespace, cfg: Config) -> tuple[str, str | None]:
    """Argparse adapter over application.session_paths.resolve_session.

    Pulls the root (--out or grab.out_dir) and --name out of the Namespace; the
    <root>/<YYYYMMDD>-<name> rule itself lives in the application layer so the
    API resolves sessions identically.
    """
    return resolve_session(args.out or cfg.grab.out_dir, getattr(args, "name", None))


def cmd_align(args: argparse.Namespace, cfg: Config) -> None:
    out_dir, session_dir = _resolve_session(args, cfg)
    request = AlignRequest(
        out_dir=out_dir,
        iso=args.iso,
        shutter=args.shutter,
        aperture=args.aperture,
        bulb_seconds=args.bulb,
    )
    try:
        result = build_align_service(cfg).align(
            request, session_dir=session_dir, name=args.name, force=args.force)
    except CameraError as exc:
        log.error(str(exc))
        sys.exit(1)

    if (result.solved and result.center_ra_deg is not None
            and result.center_dec_deg is not None
            and result.scale_arcsec_per_px is not None):
        log.info("Aligned", extra={
            "ra": round(result.center_ra_deg, 4),
            "dec": round(result.center_dec_deg, 4),
            "scale": round(result.scale_arcsec_per_px, 2),
            "annotated": result.annotated_path,
            "session": session_dir,
        })
    else:
        log.warning("No solution", extra={"frame": result.frame_path})


def cmd_sequence(args: argparse.Namespace, cfg: Config) -> None:
    out_dir, session_dir = _resolve_session(args, cfg)
    request = SequenceRequest(
        out_dir=out_dir,
        iso=args.iso,
        shutter=args.shutter,
        bulb_seconds=args.bulb,
        aperture=args.aperture,
        lights=args.lights,
        darks=args.darks,
        bias=args.bias,
        download=args.download,
    )

    def before_phase(kind: str) -> None:
        if kind in ("dark", "bias"):
            input(f"Cover the lens for {kind} frames, then press Enter…")
        else:
            input(f"Ready for {kind} frames (lens uncovered)? Press Enter…")

    try:
        manifest = build_sequence_service().run(
            request, session_dir=session_dir, before_phase=before_phase)
    except CameraError as exc:
        log.error(str(exc))
        sys.exit(1)

    log.info("Sequence complete", extra={
        "counts": {p.kind: p.count for p in manifest.phases},
        "session": session_dir,
        "manifest": str(Path(session_dir) / "session.json") if session_dir else None,
    })


def cmd_solve(args: argparse.Namespace, cfg: Config) -> None:
    path = Path(args.file)
    if not path.is_file():
        log.error("File not found", extra={"path": str(path)})
        sys.exit(1)

    solver = build_solver(cfg)
    repo = build_repository()

    if repo.exists(path.name, str(path.parent)) and not args.force:
        log.error(
            "Sidecar already exists — pass --force to re-solve",
            extra={"path": str(path)},
        )
        sys.exit(1)

    annotate_out = str(path.parent / (path.stem + "_solved.png")) if args.annotate else None

    log.info("Solving", extra={"file": str(path)})
    job = solve_file(str(path), solver, cfg, annotate_out=annotate_out)
    record = job.to_record(cfg)

    repo.save(record, str(path.parent))

    if job.solved and record.solve is not None:
        log.info("Solved", extra={
            "ra": round(record.solve.center_ra_deg, 4),
            "dec": round(record.solve.center_dec_deg, 4),
            "scale": round(record.solve.scale_arcsec_per_px, 2),
        })
        if annotate_out:
            log.info("Annotated overlay saved", extra={"path": annotate_out})
    else:
        log.error("No solution", extra={"error": record.error or "solver returned None"})
        sys.exit(1)


def cmd_serve(args: argparse.Namespace, cfg: Config) -> None:
    """Run the web UI + JSON API under uvicorn until interrupted.

    Imported lazily: fastapi/uvicorn are only needed for this one verb, and
    every other command should start without paying for the import.
    """
    import uvicorn

    from camera_orchestrator.interfaces.api import create_app

    log.info("Serving web UI", extra={"host": args.host, "port": args.port,
                                      "url": f"http://{args.host}:{args.port}/"})
    if args.reload:
        # --reload needs an import string so the reloader can re-import the app
        # in its child process; the config path travels via the environment.
        from camera_orchestrator.interfaces.api.app import CONFIG_ENV_VAR

        os.environ[CONFIG_ENV_VAR] = args.config
        uvicorn.run("camera_orchestrator.interfaces.api.app:reloadable_app",
                    host=args.host, port=args.port, reload=True, factory=True,
                    log_level=cfg.logging.level.lower())
        return

    uvicorn.run(create_app(cfg, config_path=args.config), host=args.host, port=args.port,
                log_level=cfg.logging.level.lower())


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

    al = sub.add_parser("align", help="Capture one frame and solve it to check pointing")
    al.add_argument("--out", default=None, help="Parent output directory (default: grab.out_dir from config)")
    al.add_argument("--name", default=None,
                    help="Session label. Records the solved target into <out>/<date>-<name>/session.json. "
                         "Omit for a loose, unrecorded pointing check in the parent.")
    al.add_argument("--force", action="store_true",
                    help="Overwrite the target of a session that already has sequenced frames")
    al.add_argument("--iso", default=None, help="ISO setting, e.g. 800")
    al.add_argument("--shutter", default=None, help="Shutter speed, e.g. 2 or 1/60 (ignored with --bulb)")
    al.add_argument("--aperture", default=None, help="Aperture f-number, e.g. 4")
    al.add_argument("--bulb", metavar="SECONDS", type=float, default=None,
                    help="Bulb exposure length in seconds (overrides --shutter)")

    solve_p = sub.add_parser("solve", help="Plate-solve a single image file in place")
    solve_p.add_argument("file", help="Path to the image file (JPEG, CR2, etc.)")
    solve_p.add_argument("--annotate", action="store_true",
                         help="Write an annotated overlay to <file>_solved.png alongside the source")
    solve_p.add_argument("--force", action="store_true",
                         help="Re-solve even if a sidecar JSON already exists")
    solve_p.add_argument("--mode", choices=["fast", "accurate"], default=None,
                         help="Override solver mode from config")

    seq = sub.add_parser("sequence", help="Fire an imaging sequence: lights + darks + bias")
    seq.add_argument("--out", default=None, help="Parent output directory (default: grab.out_dir from config)")
    seq.add_argument("--name", default=None,
                     help="Session label. Records phases into <out>/<date>-<name>/session.json. "
                          "Omit for a loose, unrecorded run in the parent.")
    seq.add_argument("--iso", default=None, help="ISO for lights and darks, e.g. 800")
    seq.add_argument("--shutter", default=None, help="Shutter for lights and darks (bias uses fastest)")
    seq.add_argument("--aperture", default=None, help="Aperture f-number, e.g. 4")
    seq.add_argument("--bulb", metavar="SECONDS", type=float, default=None,
                     help="Bulb exposure for lights and darks (overrides --shutter)")
    seq.add_argument("--lights", type=int, default=0, help="Number of light frames")
    seq.add_argument("--darks", type=int, default=0, help="Number of dark frames (lens capped)")
    seq.add_argument("--bias", type=int, default=0, help="Number of bias frames (fastest shutter, lens capped)")
    seq.add_argument("--download", action="store_true",
                     help="Transfer frames over USB to the session folder (default: shoot to the card only)")

    srv = sub.add_parser("serve", help="Serve the web UI and JSON API")
    srv.add_argument("--host", default="127.0.0.1",
                     help="Interface to bind (default: 127.0.0.1, this machine only). "
                          "Use --host 0.0.0.0 to expose it to the LAN so a phone at the "
                          "scope can reach it — there is no authentication, so only do "
                          "that on a network you trust.")
    srv.add_argument("--port", type=int, default=8000, help="Port to listen on (default: 8000)")
    srv.add_argument("--reload", action="store_true",
                     help="Reload on source changes (development only)")

    return parser


def main() -> None:
    args = build_parser().parse_args()

    cfg = Config.load(args.config)

    global log
    log = get_logger("camera_orchestrator.batch",
                     fmt=cfg.logging.format, level=cfg.logging.level)

    if args.command == "solve":
        if args.mode:
            cfg.solver.mode = args.mode
        cmd_solve(args, cfg)
    elif args.command == "grab":
        cmd_grab(args, cfg)
    elif args.command == "capture":
        cmd_capture(args, cfg)
    elif args.command == "align":
        cmd_align(args, cfg)
    elif args.command == "sequence":
        cmd_sequence(args, cfg)
    elif args.command == "serve":
        cmd_serve(args, cfg)
    elif args.command == "batch":
        # --mode / --cpulimit are passed through to the service, which copies the
        # config rather than mutating this shared one.
        cmd_batch(args, cfg)
