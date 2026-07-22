"""FITS/WCS I/O for the solver adapters (astropy + cv2)."""
from __future__ import annotations

import cv2
import numpy as np
from astropy.io import fits
from astropy.wcs import WCS

from camera_orchestrator.domain.models.solve import SolveResult


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
