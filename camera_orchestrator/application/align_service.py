"""Align service — capture one frame, solve it, report where it landed.

Composes CaptureService (grab a single frame) and the solver (solve_file) into
a pointing check. It requests select="jpeg", so with a RAW+JPEG camera only the
JPEG is pulled down (fast) and the RAW stays on the card — the camera's format
setting is left untouched. Ephemeral: writes an annotated preview next to the
frame and returns the centre RA/Dec; it does not persist a record.
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
from camera_orchestrator.domain.ports.solver import Solver

SolverFactory = Callable[[], Solver]


class AlignService:
    """Single-frame capture-and-solve for checking/adjusting pointing."""

    def __init__(self, capture: CaptureService, solver_factory: SolverFactory, cfg: Config):
        self._capture = capture
        self._solver_factory = solver_factory
        self._cfg = cfg

    def align(self, request: AlignRequest) -> AlignResult:
        cap = self._capture.capture_and_download(CaptureRequest(
            out_dir=request.out_dir,
            iso=request.iso,
            shutter=request.shutter,
            aperture=request.aperture,
            bulb_seconds=request.bulb_seconds,
            count=1,
            download=True,
            select="jpeg",
        ))
        if not cap.frames:
            raise CameraError("align: no frame was downloaded")

        frame = Path(cap.frames[0])
        annotate_out = str(Path(request.out_dir) / f"{frame.stem}_solved.png")
        job = solve_file(str(frame), self._solver_factory(), self._cfg, annotate_out=annotate_out)

        r = job.result
        return AlignResult(
            solved=job.solved,
            center_ra_deg=r.center_ra_deg if r else None,
            center_dec_deg=r.center_dec_deg if r else None,
            scale_arcsec_per_px=r.scale_arcsec_per_px if r else None,
            annotated_path=r.annotated_path if (r and job.solved) else None,
            frame_path=str(frame),
        )
