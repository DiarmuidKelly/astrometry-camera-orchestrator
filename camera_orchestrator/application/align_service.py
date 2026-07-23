"""Align service — capture one frame, solve it, report where it landed.

Composes CaptureService (grab a single frame) and the solver (solve_file) into a
pointing check. It sets the camera to JPEG (a throwaway framing shot solves fine
on a JPEG and downloads fast) — so it works regardless of what format the last
command left the camera in (e.g. RAW after a sequence). Each verb sets the format
it needs; nothing is restored afterwards.

Two modes:
- **loose** (session_dir=None): writes an annotated preview next to the frame in
  request.out_dir and returns the centre RA/Dec; persists no record.
- **session** (session_dir set): writes the frame + preview into the session
  folder and records the solved target in that session's manifest. Refuses to
  overwrite the target once the session has sequenced frames (the lock), unless
  force=True.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from camera_orchestrator.application.capture_service import CaptureService
from camera_orchestrator.application.solve_service import solve_file
from camera_orchestrator.config import Config
from camera_orchestrator.domain.errors import CameraError
from camera_orchestrator.domain.models.align import AlignRequest, AlignResult
from camera_orchestrator.domain.models.camera import CaptureRequest
from camera_orchestrator.domain.models.session import SessionManifest, TargetInfo
from camera_orchestrator.domain.ports.session_manifest import SessionManifestRepository
from camera_orchestrator.domain.ports.solver import Solver

SolverFactory = Callable[[], Solver]


class AlignService:
    """Single-frame capture-and-solve for checking/adjusting pointing."""

    def __init__(
        self,
        capture: CaptureService,
        solver_factory: SolverFactory,
        cfg: Config,
        manifest_repo: SessionManifestRepository,
    ):
        self._capture = capture
        self._solver_factory = solver_factory
        self._cfg = cfg
        self._repo = manifest_repo

    def align(
        self,
        request: AlignRequest,
        session_dir: str | None = None,
        name: str | None = None,
        force: bool = False,
    ) -> AlignResult:
        manifest: SessionManifest | None = None
        if session_dir is not None:
            manifest = self._repo.load(session_dir)
            if manifest is not None and manifest.phases and not force:
                raise CameraError(
                    f"session '{manifest.session_id}' already has "
                    f"{sum(p.count for p in manifest.phases)} recorded frames — "
                    f"refusing to overwrite its target (use --force to override)"
                )

        out_dir = session_dir if session_dir is not None else request.out_dir
        cap = self._capture.capture_and_download(CaptureRequest(
            out_dir=out_dir,
            iso=request.iso,
            shutter=request.shutter,
            aperture=request.aperture,
            image_format="jpeg",
            bulb_seconds=request.bulb_seconds,
            count=1,
            download=True,
            select="jpeg",
        ))
        if not cap.frames:
            raise CameraError("align: no frame was downloaded")

        frame = Path(cap.frames[0])
        annotate_out = str(Path(out_dir) / f"{frame.stem}_solved.png")
        job = solve_file(str(frame), self._solver_factory(), self._cfg, annotate_out=annotate_out)

        r = job.result
        result = AlignResult(
            solved=job.solved,
            center_ra_deg=r.center_ra_deg if r else None,
            center_dec_deg=r.center_dec_deg if r else None,
            scale_arcsec_per_px=r.scale_arcsec_per_px if r else None,
            annotated_path=r.annotated_path if (r and job.solved) else None,
            frame_path=str(frame),
        )

        if session_dir is not None:
            if manifest is None:
                manifest = SessionManifest(session_id=Path(session_dir).name, name=name)
            manifest.target = TargetInfo(
                solved=result.solved,
                center_ra_deg=result.center_ra_deg,
                center_dec_deg=result.center_dec_deg,
                scale_arcsec_per_px=result.scale_arcsec_per_px,
                preview=Path(annotate_out).name if result.solved else None,
                frame=frame.name,
            )
            self._repo.save(manifest, session_dir)

        return result
