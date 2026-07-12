"""DockerSolver — shells out to the dockerised astrometry solve-field."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import cv2
import numpy as np

from ..models import SolveHints, SolveResult
from .base import Solver, result_from_wcs, write_fits


class DockerSolver(Solver):
    """Plate solver that runs astrometry.net solve-field inside a Docker container.

    Each call spins up a fresh container with the index files and image mounted
    into /data, runs solve-field, and tears the container down on exit.
    """

    def __init__(
        self,
        image: str,
        index_dir: str,
        cpulimit: int = 30,
        extra_args: list[str] | None = None,
    ):
        """Args:
            image: Docker image name for the astrometry solver.
            index_dir: Host path to the astrometry index files (.fits).
            cpulimit: Max CPU seconds to allow solve-field before it gives up.
            extra_args: Additional solve-field flags (e.g. --downsample, --objs).
        """
        self.image = image
        self.index_dir = os.path.abspath(os.path.expanduser(index_dir))
        self.cpulimit = cpulimit
        self.extra_args = extra_args or []

    def solve(
        self,
        frame_bgr: np.ndarray,
        hints: SolveHints,
        annotate_out: str | None = None,
        source_path: str | None = None,
    ) -> SolveResult | None:
        """Plate-solve a frame via the dockerised solve-field binary.

        If source_path is given, the original file is passed directly to the
        solver (preserves colour and avoids a greyscale FITS conversion).
        Otherwise the frame array is written as a FITS file first.

        Args:
            frame_bgr: BGR image array (used for dimensions and FITS fallback).
            hints: Scale and position hints to narrow the search.
            annotate_out: If set, write the NGC-annotated overlay to this path.
            source_path: Path to the original source image on disk (preferred input).

        Returns:
            SolveResult on success, None if solve-field found no solution.
        """
        with tempfile.TemporaryDirectory(prefix="cam-orch-solve-") as work:
            if source_path:
                src_name = os.path.basename(source_path)
                shutil.copy2(source_path, os.path.join(work, src_name))
                input_arg = f"/data/{src_name}"
                height, width = frame_bgr.shape[:2]
                flip_annotated = False
            else:
                frame_fits = os.path.join(work, "frame.fits")
                width, height = write_fits(frame_bgr, frame_fits)
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

            saved_annotated: str | None = None
            if annotate_out:
                ngc_png = os.path.join(work, f"{stem}-ngc.png")
                if os.path.exists(ngc_png):
                    os.makedirs(os.path.dirname(annotate_out), exist_ok=True)
                    if flip_annotated:
                        img = cv2.imread(ngc_png)
                        if img is not None:
                            cv2.imwrite(annotate_out, np.flipud(img))
                    else:
                        shutil.copy2(ngc_png, annotate_out)
                    saved_annotated = annotate_out

            return result_from_wcs(wcs_path, width, height, annotated_path=saved_annotated)
