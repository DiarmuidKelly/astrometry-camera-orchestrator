"""Tests for AlignService — capture (MockCamera) + solve (patched solve_file)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from camera_orchestrator.application.align_service import AlignService
from camera_orchestrator.application.capture_service import CaptureService
from camera_orchestrator.config import Config
from camera_orchestrator.domain.errors import CameraError
from camera_orchestrator.domain.models.align import AlignRequest
from camera_orchestrator.domain.models.camera import CameraFile
from camera_orchestrator.domain.models.session import PhaseRecord, SessionManifest
from camera_orchestrator.domain.models.solve import SolveJob, SolveResult

from tests.test_sequence_service import FakeRepo  # in-memory manifest repo
from tests.test_service import MockCamera  # reuse the atomic-ABC mock


def _service(produces, repo=None):
    cam = MockCamera(produces=produces)
    repo = repo or FakeRepo()
    svc = AlignService(CaptureService(camera_factory=lambda: cam), lambda: object(), Config(), repo)
    return svc, cam, repo


def _solved_job(path: str) -> SolveJob:
    return SolveJob(
        path=path,
        result=SolveResult(
            center_ra_deg=115.4, center_dec_deg=21.5,
            scale_arcsec_per_px=3.96, width_px=60, height_px=40,
            annotated_path=str(Path(path).with_name(f"{Path(path).stem}_solved.png")),
        ),
    )


def _patch_solve():
    return patch("camera_orchestrator.application.align_service.solve_file",
                 side_effect=lambda path, solver, cfg, annotate_out=None: _solved_job(path))


def test_align_downloads_only_jpeg_of_raw_jpeg_pair(tmp_path):
    svc, cam, _ = _service([CameraFile("/s", "IMG_2.CR2"), CameraFile("/s", "IMG_2.JPG")])
    with _patch_solve() as mock_solve:
        result = svc.align(AlignRequest(out_dir=str(tmp_path), iso="800", shutter="1"))

    # only the JPEG was pulled down — the RAW stays on the card
    assert [r.name for r in cam.downloads] == ["IMG_2.JPG"]
    assert mock_solve.call_args.args[0].endswith("IMG_2.JPG")
    assert mock_solve.call_args.kwargs["annotate_out"].endswith("IMG_2_solved.png")
    assert result.solved is True
    assert result.center_ra_deg == 115.4
    assert result.annotated_path.endswith("IMG_2_solved.png")


def test_align_reports_no_solution(tmp_path):
    svc, _, _ = _service([CameraFile("/s", "IMG_3.JPG")])
    with patch("camera_orchestrator.application.align_service.solve_file",
               side_effect=lambda path, solver, cfg, annotate_out=None: SolveJob(path=path)):
        result = svc.align(AlignRequest(out_dir=str(tmp_path)))
    assert result.solved is False
    assert result.center_ra_deg is None
    assert result.annotated_path is None
    assert result.frame_path.endswith("IMG_3.JPG")


def test_loose_align_writes_no_manifest(tmp_path):
    svc, _, repo = _service([CameraFile("/s", "IMG_4.JPG")])
    with _patch_solve():
        svc.align(AlignRequest(out_dir=str(tmp_path)))          # no session_dir
    assert repo.store == {}


def test_named_align_records_target(tmp_path):
    session_dir = str(tmp_path / "20260723-orion")
    svc, _, repo = _service([CameraFile("/s", "IMG_5.JPG")])
    with _patch_solve():
        svc.align(AlignRequest(out_dir=session_dir), session_dir=session_dir, name="orion")

    manifest = repo.load(session_dir)
    assert manifest is not None
    assert manifest.name == "orion"
    assert manifest.target.solved is True
    assert manifest.target.center_ra_deg == 115.4
    assert manifest.target.preview == "IMG_5_solved.png"
    assert manifest.target.frame == "IMG_5.JPG"


def _sequenced_manifest(session_dir: str) -> SessionManifest:
    now = datetime(2026, 7, 23, 22, 0, tzinfo=timezone.utc)
    return SessionManifest(
        session_id=Path(session_dir).name,
        phases=[PhaseRecord(kind="light", count=60, started_at=now, ended_at=now)],
    )


def test_align_refuses_to_overwrite_sequenced_target(tmp_path):
    session_dir = str(tmp_path / "20260723-orion")
    repo = FakeRepo()
    repo.save(_sequenced_manifest(session_dir), session_dir)     # already has 60 lights
    svc, _, _ = _service([CameraFile("/s", "IMG_6.JPG")], repo=repo)
    with _patch_solve(), pytest.raises(CameraError, match="refusing to overwrite"):
        svc.align(AlignRequest(out_dir=session_dir), session_dir=session_dir, name="orion")


def test_force_overrides_the_lock(tmp_path):
    session_dir = str(tmp_path / "20260723-orion")
    repo = FakeRepo()
    repo.save(_sequenced_manifest(session_dir), session_dir)
    svc, _, _ = _service([CameraFile("/s", "IMG_7.JPG")], repo=repo)
    with _patch_solve():
        svc.align(AlignRequest(out_dir=session_dir), session_dir=session_dir,
                  name="orion", force=True)
    manifest = repo.load(session_dir)
    assert manifest.target.frame == "IMG_7.JPG"                  # target updated
    assert len(manifest.phases) == 1                            # phases untouched
