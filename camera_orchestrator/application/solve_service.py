"""File-based solve entry point.

Coordinates: load image → read EXIF → build hints → call solver → return SolveJob.
Hint assembly and solver logic live in camera_orchestrator.solvers.
"""
from __future__ import annotations

import os

import cv2
import numpy as np
import rawpy

from camera_orchestrator.adapters.exif import read_exif
from camera_orchestrator.config import Config
from camera_orchestrator.domain.models.solve import SolveHints, SolveJob
from camera_orchestrator.domain.optics import scale_hint_from_optics
from camera_orchestrator.domain.ports.solver import Solver

# Camera raw formats we decode ourselves (solve-field can't read them directly,
# so we hand the solver a decoded array and let it write a FITS).
RAW_SUFFIXES = {".cr2"}


def _load_frame(path: str) -> tuple[np.ndarray | None, str | None]:
    """Return (BGR frame, source_path_for_solver).

    For a JPEG/PNG the original file is handed straight to solve-field
    (source_path = path, preserving colour). For a raw (CR2) we decode with
    rawpy and return source_path = None, so the solver writes a FITS from the
    array instead of trying (and failing) to read the raw file.
    """
    if os.path.splitext(path)[1].lower() in RAW_SUFFIXES:
        with rawpy.imread(path) as raw:
            rgb = raw.postprocess()
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), None
    return cv2.imread(path, cv2.IMREAD_COLOR), path


def build_hints(cfg: Config, exif_focal_mm: float | None, frame_width_px: int) -> SolveHints:
    """Assemble SolveHints from config + per-image focal length.

    EXIF focal length takes priority over the config fallback so a mixed batch
    (70mm + 200mm in the same folder) gets the correct scale hint per frame.
    Position hints (RA/Dec/radius) always come from config. Bridges the
    infrastructure Config into a domain SolveHints — an application concern.
    """
    focal_mm = exif_focal_mm or cfg.optics.focal_mm
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

    try:
        frame, source_path = _load_frame(path)
    except Exception as exc:
        return SolveJob(path=path, exif=exif, error=f"could not decode {path}: {exc}")
    if frame is None:
        return SolveJob(
            path=path,
            exif=exif,
            error="could not decode image — unsupported format or corrupt file",
        )

    _, w = frame.shape[:2]
    hints = build_hints(cfg, exif.focal_mm if exif else None, w)

    try:
        result = solver.solve(frame, hints, annotate_out=annotate_out, source_path=source_path)
        return SolveJob(path=path, exif=exif, hints=hints, result=result)
    except Exception as exc:
        return SolveJob(path=path, exif=exif, hints=hints, error=str(exc))
