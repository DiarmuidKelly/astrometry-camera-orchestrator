"""Solver ABC and shared utilities (FITS I/O, WCS parsing, scale hints)."""
from __future__ import annotations

import math
from abc import ABC, abstractmethod

import cv2
import numpy as np
from astropy.io import fits
from astropy.wcs import WCS

from ..models import SolveHints, SolveResult


def scale_hint_from_optics(
    focal_mm: float,
    sensor_width_mm: float,
    frame_width_px: int,
    tol: float = 0.3,
) -> tuple[float, float]:
    """Compute arcsec/pixel scale bounds from lens and sensor geometry.

    Args:
        focal_mm: Lens focal length in millimetres.
        sensor_width_mm: Physical sensor width in millimetres.
        frame_width_px: Image width in pixels.
        tol: Fractional tolerance applied symmetrically around the nominal scale (default ±30%).

    Returns:
        (scale_low, scale_high) in arcsec/pixel.
    """
    fov_w_deg = math.degrees(2 * math.atan((sensor_width_mm / 2) / focal_mm))
    arcsec_per_px = fov_w_deg * 3600 / frame_width_px
    return arcsec_per_px * (1 - tol), arcsec_per_px * (1 + tol)


def write_fits(frame_bgr: np.ndarray, path: str) -> tuple[int, int]:
    """Convert a BGR frame to greyscale and write it as a FITS file.

    The image is flipped vertically before writing because FITS uses a
    bottom-left origin while OpenCV uses top-left.

    Args:
        frame_bgr: BGR image array from cv2.imread.
        path: Output path for the FITS file.

    Returns:
        (width, height) in pixels.
    """
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    hdu = fits.PrimaryHDU(data=np.flipud(gray))
    hdu.writeto(path, overwrite=True)
    h, w = gray.shape[:2]
    return w, h


def result_from_wcs(
    wcs_path: str,
    width: int,
    height: int,
    annotated_path: str | None = None,
) -> SolveResult:
    """Parse a WCS FITS header into a SolveResult.

    Args:
        wcs_path: Path to the .wcs file produced by solve-field.
        width: Image width in pixels (used to compute the frame centre).
        height: Image height in pixels.
        annotated_path: Path to the saved annotated overlay, if produced.

    Returns:
        SolveResult with RA/Dec of the frame centre and arcsec/pixel scale.
    """
    header = fits.getheader(wcs_path)
    wcs = WCS(header)
    cx, cy = (width - 1) / 2.0, (height - 1) / 2.0
    ra, dec = wcs.wcs_pix2world([[cx, cy]], 0)[0]
    try:
        scales = np.sqrt((wcs.pixel_scale_matrix ** 2).sum(axis=0))
        scale = float(np.mean(scales)) * 3600.0
    except Exception:
        scale = float("nan")
    return SolveResult(
        center_ra_deg=float(ra),
        center_dec_deg=float(dec),
        scale_arcsec_per_px=scale,
        width_px=width,
        height_px=height,
        annotated_path=annotated_path,
    )


def build_hints(cfg, exif_focal_mm: float | None, frame_width_px: int) -> SolveHints:
    """Assemble SolveHints from config + per-image focal length.

    EXIF focal length takes priority over the config fallback so a mixed batch
    (70mm + 200mm in the same folder) gets the correct scale hint per frame.
    Position hints (RA/Dec/radius) always come from config.
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


class Solver(ABC):
    """Abstract base for plate-solver backends (Docker, API, etc.)."""

    @abstractmethod
    def solve(
        self,
        frame_bgr: np.ndarray,
        hints: SolveHints,
        annotate_out: str | None = None,
        source_path: str | None = None,
    ) -> SolveResult | None:
        """Plate-solve a frame.

        Args:
            frame_bgr: BGR image array.
            hints: Scale and sky-position hints to narrow the search.
            annotate_out: If set, write an annotated overlay image to this path.
            source_path: Path to the original file on disk (avoids re-encoding).

        Returns:
            SolveResult on success, None if no solution was found.
        """
