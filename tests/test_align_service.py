"""Tests for AlignService — capture (MockCamera) + solve (patched solve_file)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from camera_orchestrator.application.align_service import AlignService, _pick_frame
from camera_orchestrator.application.capture_service import CaptureService
from camera_orchestrator.config import Config
from camera_orchestrator.domain.models.align import AlignRequest
from camera_orchestrator.domain.models.solve import SolveJob, SolveResult

from tests.test_service import MockCamera  # reuse the atomic-ABC mock
from camera_orchestrator.domain.models.camera import CameraFile


def _service(produces) -> AlignService:
    cam = MockCamera(produces=produces)
    capture = CaptureService(camera_factory=lambda: cam)
    return AlignService(capture, solver_factory=lambda: object(), cfg=Config())


def _solved_job(path: str) -> SolveJob:
    return SolveJob(
        path=path,
        result=SolveResult(
            center_ra_deg=115.4, center_dec_deg=21.5,
            scale_arcsec_per_px=3.96, width_px=60, height_px=40,
            annotated_path=str(Path(path).with_name(f"{Path(path).stem}_solved.png")),
        ),
    )


def test_pick_frame_prefers_jpeg_over_cr2(tmp_path):
    cr2 = tmp_path / "IMG_1.CR2"
    jpg = tmp_path / "IMG_1.JPG"
    assert _pick_frame([cr2, jpg]) == jpg
    assert _pick_frame([cr2]) == cr2


def test_align_solves_downloaded_jpeg(tmp_path):
    svc = _service(produces=[CameraFile("/store", "IMG_1.JPG")])
    req = AlignRequest(out_dir=str(tmp_path), iso="800", shutter="1")
    with patch("camera_orchestrator.application.align_service.solve_file",
               side_effect=lambda path, solver, cfg, annotate_out=None: _solved_job(path)) as mock_solve:
        result = svc.align(req)

    # solved the JPEG that MockCamera "downloaded" into out_dir
    solved_path = mock_solve.call_args.args[0]
    assert solved_path.endswith("IMG_1.JPG")
    # annotated output uses <stem>_solved.png
    assert mock_solve.call_args.kwargs["annotate_out"].endswith("IMG_1_solved.png")
    assert result.solved is True
    assert result.center_ra_deg == 115.4
    assert result.annotated_path.endswith("IMG_1_solved.png")


def test_align_prefers_jpeg_of_raw_jpeg_pair(tmp_path):
    svc = _service(produces=[CameraFile("/s", "IMG_2.CR2"), CameraFile("/s", "IMG_2.JPG")])
    req = AlignRequest(out_dir=str(tmp_path))
    with patch("camera_orchestrator.application.align_service.solve_file",
               side_effect=lambda path, solver, cfg, annotate_out=None: _solved_job(path)) as mock_solve:
        svc.align(req)
    assert mock_solve.call_args.args[0].endswith("IMG_2.JPG")


def test_align_reports_no_solution(tmp_path):
    svc = _service(produces=[CameraFile("/s", "IMG_3.JPG")])
    req = AlignRequest(out_dir=str(tmp_path))
    with patch("camera_orchestrator.application.align_service.solve_file",
               side_effect=lambda path, solver, cfg, annotate_out=None: SolveJob(path=path)):
        result = svc.align(req)
    assert result.solved is False
    assert result.center_ra_deg is None
    assert result.annotated_path is None
    assert result.frame_path.endswith("IMG_3.JPG")
