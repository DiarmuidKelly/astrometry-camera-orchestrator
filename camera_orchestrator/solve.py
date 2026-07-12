"""File-based solve entry point.

Coordinates: load image → read EXIF → build hints → call solver → return SolveJob.
Hint assembly and solver logic live in camera_orchestrator.solvers.
"""
from __future__ import annotations

import cv2

from .config import Config
from .models import SolveJob
from .solvers import Solver, build_hints
from .utils.exif import read_exif


def solve_file(
    path: str,
    solver: Solver,
    cfg: Config,
    annotate_out: str | None = None,
) -> SolveJob:
    """Load an image, extract EXIF, build hints, and plate-solve it.

    Args:
        path: Path to the image file (JPEG, CR2, TIFF, etc.).
        solver: Solver backend to use (DockerSolver, ApiSolver, etc.).
        cfg: Loaded configuration (optics, search region, location).
        annotate_out: If set, write an annotated overlay image to this path.

    Returns:
        SolveJob with result populated on success, error set on failure.
    """
    exif = None
    try:
        exif = read_exif(path)
    except Exception:
        pass

    frame = cv2.imread(path, cv2.IMREAD_COLOR)
    if frame is None:
        return SolveJob(
            path=path,
            exif=exif,
            error="cv2.imread returned None — unsupported format or corrupt file",
        )

    _, w = frame.shape[:2]
    hints = build_hints(cfg, exif.focal_mm if exif else None, w)

    try:
        result = solver.solve(frame, hints, annotate_out=annotate_out, source_path=path)
        return SolveJob(path=path, exif=exif, hints=hints, result=result)
    except Exception as exc:
        return SolveJob(path=path, exif=exif, hints=hints, error=str(exc))
