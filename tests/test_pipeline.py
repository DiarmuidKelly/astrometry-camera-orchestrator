from pathlib import Path
from unittest.mock import patch

import numpy as np

from camera_orchestrator.config import Config
from camera_orchestrator.domain.models import SolveResult
from camera_orchestrator.application.solve_service import build_hints
from camera_orchestrator.application.solve_service import solve_file
from camera_orchestrator.domain.ports.solver import Solver

SAMPLE_IMAGE = str(Path(__file__).parent / "fixtures" / "IMG_4341.JPG")


class MockSolver(Solver):
    def __init__(self, result: SolveResult | None = None):
        self._result = result
        self.source_path = "__unset__"

    def solve(self, frame_bgr, hints, annotate_out=None, source_path=None):
        self.source_path = source_path
        return self._result


class _FakeRaw:
    """Stand-in for a rawpy RawPy object (context manager + postprocess)."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def postprocess(self):
        return np.zeros((40, 60, 3), dtype=np.uint8)  # RGB HxWx3


def _cfg(**kwargs) -> Config:
    return Config.model_validate(kwargs)


def test_build_hints_exif_overrides_config():
    cfg = _cfg(optics={"focal_mm": 70, "sensor_width_mm": 22.3})
    hints = build_hints(cfg, exif_focal_mm=200.0, frame_width_px=6000)
    assert hints.scale_low is not None
    cfg_only = build_hints(cfg, exif_focal_mm=None, frame_width_px=6000)
    assert hints.scale_low != cfg_only.scale_low


def test_build_hints_falls_back_to_config_focal():
    cfg = _cfg(optics={"focal_mm": 200, "sensor_width_mm": 22.3})
    hints = build_hints(cfg, exif_focal_mm=None, frame_width_px=6000)
    assert hints.scale_low is not None
    assert hints.scale_high is not None


def test_build_hints_no_scale_without_focal():
    cfg = _cfg(optics={"sensor_width_mm": 22.3})
    hints = build_hints(cfg, exif_focal_mm=None, frame_width_px=6000)
    assert hints.scale_low is None
    assert hints.scale_high is None


def test_build_hints_search_from_config():
    cfg = _cfg(search={"ra_deg": 277.5, "dec_deg": -6.5, "radius_deg": 30.0})
    hints = build_hints(cfg, exif_focal_mm=None, frame_width_px=6000)
    assert hints.ra_deg == 277.5
    assert hints.dec_deg == -6.5
    assert hints.radius_deg == 30.0


def test_solve_file_with_mock_solver_success():
    result = SolveResult(
        center_ra_deg=267.73,
        center_dec_deg=-29.46,
        scale_arcsec_per_px=3.97,
        width_px=6000,
        height_px=4000,
    )
    cfg = _cfg(optics={"sensor_width_mm": 22.3})
    job = solve_file(SAMPLE_IMAGE, MockSolver(result=result), cfg)
    assert job.solved is True
    assert job.result is not None
    assert job.result.center_ra_deg == 267.73


def test_solve_file_with_mock_solver_no_solution():
    cfg = _cfg(optics={"sensor_width_mm": 22.3})
    job = solve_file(SAMPLE_IMAGE, MockSolver(result=None), cfg)
    assert job.solved is False
    assert job.result is None


def test_solve_file_corrupt_image(tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    cfg = _cfg()
    job = solve_file(str(bad), MockSolver(), cfg)
    assert job.solved is False
    assert job.error is not None


def test_solve_file_jpeg_passes_source_path():
    solver = MockSolver(result=SolveResult(
        center_ra_deg=1.0, center_dec_deg=2.0, scale_arcsec_per_px=3.0,
        width_px=6000, height_px=4000,
    ))
    solve_file(SAMPLE_IMAGE, solver, _cfg())
    # JPEG: original file handed straight to solve-field.
    assert solver.source_path == SAMPLE_IMAGE


def test_solve_file_cr2_decodes_via_rawpy(tmp_path):
    cr2 = tmp_path / "IMG_0001.CR2"
    cr2.write_bytes(b"fake-raw")
    solver = MockSolver(result=SolveResult(
        center_ra_deg=1.0, center_dec_deg=2.0, scale_arcsec_per_px=3.0,
        width_px=60, height_px=40,
    ))
    with patch("camera_orchestrator.application.solve_service.rawpy.imread",
               return_value=_FakeRaw()):
        job = solve_file(str(cr2), solver, _cfg())
    assert job.solved is True
    # CR2: decoded to an array, so source_path is None (solver writes a FITS).
    assert solver.source_path is None
