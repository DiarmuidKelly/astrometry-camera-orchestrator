"""Pure optical calculations — no I/O, no external libraries beyond math."""
from __future__ import annotations

import math


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
