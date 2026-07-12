"""Core solve pipeline — shared between batch processing and live frame grabbing.

The entry points are:
  solve_file()   — takes a path on disk, reads EXIF + image, solves
  solve_frame()  — takes a numpy BGR array (live grab from 5D II etc.), solves

Both return a SolveJob with EXIF metadata, the hints used, and the result.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2

from .config import Config
from .exif import ImageExif, read_exif
from .solvers import SolveHints, SolveResult, Solver, scale_hint_from_optics


@dataclass
class SolveJob:
    """Result of a single solve attempt, including all metadata."""
    path: str | None
    exif: ImageExif | None
    hints: SolveHints
    result: SolveResult | None
    error: str | None = None

    @property
    def solved(self) -> bool:
        return self.result is not None


def build_hints(cfg: Config, exif: ImageExif | None, frame_width_px: int) -> SolveHints:
    """Assemble SolveHints from config + per-image EXIF.

    EXIF focal length takes priority over the config default so that a mixed
    batch (70mm + 200mm frames in the same folder) gets the right scale per frame.
    """
    focal_mm = (exif.focal_mm if exif else None) or cfg.optics.focal_mm
    sensor_w = cfg.optics.sensor_width_mm

    scale_low = scale_high = None
    if focal_mm and sensor_w and frame_width_px:
        scale_low, scale_high = scale_hint_from_optics(focal_mm, sensor_w, frame_width_px)

    return SolveHints(
        scale_low=scale_low,
        scale_high=scale_high,
        ra_deg=cfg.search.ra_deg,
        dec_deg=cfg.search.dec_deg,
        radius_deg=cfg.search.radius_deg,
    )


def solve_file(path: str, solver: Solver, cfg: Config,
               annotate_out: str | None = None) -> SolveJob:
    """Load an image file, extract EXIF, and plate-solve it.

    annotate_out: if set, copies the solver's annotated PNG to this path.
    """
    exif: ImageExif | None = None
    try:
        exif = read_exif(path)
    except Exception:
        pass

    frame = cv2.imread(path, cv2.IMREAD_COLOR)
    if frame is None:
        return SolveJob(path=path, exif=exif, hints=SolveHints(), result=None,
                        error="cv2.imread returned None — unsupported format or corrupt file")

    h, w = frame.shape[:2]
    hints = build_hints(cfg, exif, w)

    try:
        result = solver.solve(frame, hints, annotate_out=annotate_out, source_path=path)
        return SolveJob(path=path, exif=exif, hints=hints, result=result)
    except Exception as exc:
        return SolveJob(path=path, exif=exif, hints=hints, result=None, error=str(exc))


