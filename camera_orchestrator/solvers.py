"""Plate-solver drivers.

The astrometry module talks to a :class:`Solver` interface, so the backend is
swappable. Today: :class:`DockerSolver`, which shells out to the dockerised
``solve-field``. Later: :class:`ApiSolver`, a stub for the astrometry-api-server
once it's ready -- drop-in, no changes to the module.
"""
from __future__ import annotations

import math
import os
import subprocess
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass


def scale_hint_from_optics(focal_mm, sensor_width_mm, frame_width_px, tol=0.3):
    """arcsec/pixel bracket from lens focal length + sensor width.

    Works for any frame (full-res still or downscaled preview): the horizontal
    field of view is fixed by the optics, so arcsec/pixel = FOV / frame width.
    Returns (low, high) with +/-tol margin for the solver's --scale-low/high.
    """
    fov_w_deg = math.degrees(2 * math.atan((sensor_width_mm / 2) / focal_mm))
    arcsec_per_px = fov_w_deg * 3600 / frame_width_px
    return arcsec_per_px * (1 - tol), arcsec_per_px * (1 + tol)

import cv2
import numpy as np
from astropy.io import fits
from astropy.wcs import WCS


@dataclass
class SolveHints:
    """Priors handed to the solver to make solving fast and reliable."""

    scale_low: float | None = None       # arcsec/pixel
    scale_high: float | None = None      # arcsec/pixel
    ra_deg: float | None = None          # search centre
    dec_deg: float | None = None
    radius_deg: float | None = None      # search radius
    downsample: int = 2


@dataclass
class SolveResult:
    wcs: WCS
    center_ra_deg: float
    center_dec_deg: float
    scale_arcsec_per_px: float
    width: int
    height: int
    annotated_path: str | None = None  # path to annotated PNG if requested


class Solver(ABC):
    @abstractmethod
    def solve(self, frame_bgr: np.ndarray, hints: SolveHints) -> SolveResult | None:
        """Plate-solve a frame. Returns ``None`` if no solution is found."""


def _write_fits(frame_bgr: np.ndarray, path: str) -> tuple[int, int]:
    """Write a frame as a single-plane greyscale FITS (most solver-friendly)."""
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    # FITS convention: flip vertically so pixel (0,0) is bottom-left.
    hdu = fits.PrimaryHDU(data=np.flipud(gray))
    hdu.writeto(path, overwrite=True)
    h, w = gray.shape[:2]
    return w, h


class DockerSolver(Solver):
    """Run ``solve-field`` inside the dockerised astrometry solver image."""

    def __init__(
        self,
        image: str,
        index_dir: str,
        cpulimit: int = 30,
        extra_args: list[str] | None = None,
    ):
        self.image = image
        self.index_dir = os.path.abspath(os.path.expanduser(index_dir))
        self.cpulimit = cpulimit
        self.extra_args = extra_args or []

    def solve(self, frame_bgr: np.ndarray, hints: SolveHints,
              annotate_out: str | None = None,
              source_path: str | None = None) -> SolveResult | None:
        """Plate-solve a frame.

        source_path: when solving from a file, pass the original path here so
        the solver receives the untouched JPEG directly — correct contrast and
        colour in the annotated output. When None, a greyscale FITS is written
        from frame_bgr (used for live frame grabs from the 5D II).

        annotate_out: if set, saves the annotated PNG (green constellation
        lines, NGC markers, detected stars) to this path.
        """
        import shutil
        with tempfile.TemporaryDirectory(prefix="cam-orch-solve-") as work:
            if source_path:
                src_name = os.path.basename(source_path)
                shutil.copy2(source_path, os.path.join(work, src_name))
                input_arg = f"/data/{src_name}"
                height, width = frame_bgr.shape[:2]
                flip_annotated = False
            else:
                frame_fits = os.path.join(work, "frame.fits")
                width, height = _write_fits(frame_bgr, frame_fits)
                input_arg = "/data/frame.fits"
                flip_annotated = True

            stem = os.path.splitext(os.path.basename(input_arg))[0]

            cmd = [
                "docker", "run", "--rm",
                "-v", f"{self.index_dir}:/usr/local/astrometry/data:ro",
                "-v", f"{work}:/data",
                self.image,
                "solve-field", input_arg,
                "--dir", "/data",
                "--overwrite",
                "--cpulimit", str(self.cpulimit),
            ] + self.extra_args
            if annotate_out is None:
                cmd.append("--no-plots")

            if hints.scale_low and hints.scale_high:
                cmd += [
                    "--scale-units", "arcsecperpix",
                    "--scale-low", f"{hints.scale_low:.4f}",
                    "--scale-high", f"{hints.scale_high:.4f}",
                ]
            if hints.ra_deg is not None and hints.dec_deg is not None:
                cmd += ["--ra", f"{hints.ra_deg:.6f}", "--dec", f"{hints.dec_deg:.6f}"]
                if hints.radius_deg is not None:
                    cmd += ["--radius", f"{hints.radius_deg:.4f}"]

            proc = subprocess.run(cmd, capture_output=True, text=True)
            wcs_path = os.path.join(work, f"{stem}.wcs")
            if proc.returncode != 0 or not os.path.exists(wcs_path):
                return None

            result = _result_from_wcs(wcs_path, width, height)

            if annotate_out:
                ngc_png = os.path.join(work, f"{stem}-ngc.png")
                if os.path.exists(ngc_png):
                    os.makedirs(os.path.dirname(annotate_out), exist_ok=True)
                    if flip_annotated:
                        img = cv2.imread(ngc_png)
                        cv2.imwrite(annotate_out, np.flipud(img))
                    else:
                        shutil.copy2(ngc_png, annotate_out)
                    result.annotated_path = annotate_out

            return result


class ApiSolver(Solver):
    """Placeholder for the astrometry-api-server backend (not yet wired)."""

    def __init__(self, base_url: str):
        self.base_url = base_url

    def solve(self, frame_bgr: np.ndarray, hints: SolveHints) -> SolveResult | None:
        raise NotImplementedError(
            "ApiSolver is a stub -- the astrometry-api-server backend is not "
            "ready yet. Use the 'docker' solver for now."
        )


def _result_from_wcs(wcs_path: str, width: int, height: int) -> SolveResult:
    header = fits.getheader(wcs_path)
    wcs = WCS(header)
    cx, cy = (width - 1) / 2.0, (height - 1) / 2.0
    ra, dec = wcs.wcs_pix2world([[cx, cy]], 0)[0]
    # Pixel scale from the CD/CDELT matrix (degrees/pixel -> arcsec/pixel).
    try:
        scales = np.sqrt((wcs.pixel_scale_matrix ** 2).sum(axis=0))
        scale = float(np.mean(scales)) * 3600.0
    except Exception:
        scale = float("nan")
    return SolveResult(
        wcs=wcs,
        center_ra_deg=float(ra),
        center_dec_deg=float(dec),
        scale_arcsec_per_px=scale,
        width=width,
        height=height,
    )


def build_solver(cfg) -> Solver:
    return DockerSolver(
        image=cfg.solver.image,
        index_dir=cfg.solver.index_dir,
        cpulimit=cfg.solver.cpulimit,
        extra_args=cfg.solver.solve_args,
    )
