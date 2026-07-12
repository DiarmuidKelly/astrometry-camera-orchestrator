"""Integration tests — require Docker + the astrometry solver container.

Run with:
    pytest tests/test_integration_solver.py -v

Skipped automatically if Docker is unavailable or the index dir is not set.
"""
import os
import subprocess
from pathlib import Path

import cv2
import pytest

from camera_orchestrator.config import Config
from camera_orchestrator.models import SolveHints
from camera_orchestrator.solvers import DockerSolver

SAMPLE_IMAGE = str(Path(__file__).parent / "fixtures" / "IMG_4341.JPG")
CONFIG_PATH = "config.yaml"


def docker_available() -> bool:
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def index_dir_configured() -> bool:
    cfg = Config.load(CONFIG_PATH if os.path.exists(CONFIG_PATH) else None)
    return bool(cfg.solver.index_dir and os.path.exists(cfg.solver.index_dir))


skip_no_docker = pytest.mark.skipif(
    not docker_available(), reason="Docker not available"
)
skip_no_index = pytest.mark.skipif(
    not index_dir_configured(), reason="Solver index_dir not configured or missing"
)
skip_no_sample = pytest.mark.skipif(
    not os.path.exists(SAMPLE_IMAGE), reason="Sample image not present on this machine"
)


@skip_no_docker
@skip_no_index
@skip_no_sample
def test_docker_solver_solves_sample_image():
    cfg = Config.load(CONFIG_PATH)
    solver = DockerSolver(
        image=cfg.solver.image,
        index_dir=cfg.solver.index_dir,
        cpulimit=cfg.solver.cpulimit,
        extra_args=cfg.solver.solve_args,
    )
    frame = cv2.imread(SAMPLE_IMAGE, cv2.IMREAD_COLOR)
    assert frame is not None

    hints = SolveHints(
        ra_deg=cfg.search.ra_deg,
        dec_deg=cfg.search.dec_deg,
        radius_deg=cfg.search.radius_deg,
    )
    result = solver.solve(frame, hints, source_path=SAMPLE_IMAGE)

    assert result is not None, "Solver returned no solution"
    assert 200 < result.center_ra_deg < 340
    assert -60 < result.center_dec_deg < 40
    assert result.scale_arcsec_per_px > 0


@skip_no_docker
@skip_no_index
@skip_no_sample
def test_docker_solver_produces_annotated_png(tmp_path):
    cfg = Config.load(CONFIG_PATH)
    solver = DockerSolver(
        image=cfg.solver.image,
        index_dir=cfg.solver.index_dir,
        cpulimit=cfg.solver.cpulimit,
        extra_args=cfg.solver.solve_args,
    )
    frame = cv2.imread(SAMPLE_IMAGE, cv2.IMREAD_COLOR)
    annotate_out = str(tmp_path / "annotated.jpg")
    hints = SolveHints(
        ra_deg=cfg.search.ra_deg,
        dec_deg=cfg.search.dec_deg,
        radius_deg=cfg.search.radius_deg,
    )
    result = solver.solve(frame, hints, annotate_out=annotate_out, source_path=SAMPLE_IMAGE)

    assert result is not None
    assert result.annotated_path == annotate_out
    assert os.path.exists(annotate_out)
