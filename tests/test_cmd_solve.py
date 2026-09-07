"""Unit tests for the `solve` CLI command handler."""
from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from camera_orchestrator.config import Config
from camera_orchestrator.domain.models import SolveResult
from camera_orchestrator.domain.ports.solver import Solver
from camera_orchestrator.interfaces.cli import cmd_solve

SAMPLE_IMAGE = str(Path(__file__).parent / "fixtures" / "IMG_4341.JPG")


class MockSolver(Solver):
    def __init__(self, result: SolveResult | None = None):
        self._result = result

    def solve(self, frame_bgr, hints, annotate_out=None, source_path=None):
        return self._result


def _cfg(**kwargs) -> Config:
    return Config.model_validate(kwargs)


def _args(file: str, annotate: bool = False, force: bool = False) -> argparse.Namespace:
    return argparse.Namespace(file=file, annotate=annotate, force=force)


def _solved_result() -> SolveResult:
    return SolveResult(
        center_ra_deg=277.41,
        center_dec_deg=-25.41,
        scale_arcsec_per_px=3.97,
        width_px=6000,
        height_px=4000,
    )


def test_cmd_solve_success(tmp_path):
    img = tmp_path / "IMG_0001.JPG"
    img.write_bytes(Path(SAMPLE_IMAGE).read_bytes())
    cfg = _cfg(optics={"sensor_width_mm": 35.8})

    mock_repo = MagicMock()
    mock_repo.exists.return_value = False

    with patch("camera_orchestrator.interfaces.cli.build_solver", return_value=MockSolver(_solved_result())), \
         patch("camera_orchestrator.interfaces.cli.build_repository", return_value=mock_repo):
        cmd_solve(_args(str(img)), cfg)

    mock_repo.save.assert_called_once()
    saved_record, saved_dir = mock_repo.save.call_args.args
    assert saved_dir == str(tmp_path)
    assert saved_record.solved is True


def test_cmd_solve_blocks_overwrite_without_force(tmp_path):
    img = tmp_path / "IMG_0001.JPG"
    img.write_bytes(Path(SAMPLE_IMAGE).read_bytes())
    cfg = _cfg(optics={"sensor_width_mm": 35.8})

    mock_repo = MagicMock()
    mock_repo.exists.return_value = True  # sidecar already present

    with patch("camera_orchestrator.interfaces.cli.build_solver", return_value=MockSolver(_solved_result())), \
         patch("camera_orchestrator.interfaces.cli.build_repository", return_value=mock_repo), \
         pytest.raises(SystemExit) as exc:
        cmd_solve(_args(str(img), force=False), cfg)

    assert exc.value.code == 1
    mock_repo.save.assert_not_called()


def test_cmd_solve_force_overwrites_existing(tmp_path):
    img = tmp_path / "IMG_0001.JPG"
    img.write_bytes(Path(SAMPLE_IMAGE).read_bytes())
    cfg = _cfg(optics={"sensor_width_mm": 35.8})

    mock_repo = MagicMock()
    mock_repo.exists.return_value = True  # sidecar exists but --force passed

    with patch("camera_orchestrator.interfaces.cli.build_solver", return_value=MockSolver(_solved_result())), \
         patch("camera_orchestrator.interfaces.cli.build_repository", return_value=mock_repo):
        cmd_solve(_args(str(img), force=True), cfg)

    mock_repo.save.assert_called_once()


def test_cmd_solve_file_not_found(tmp_path):
    cfg = _cfg()
    mock_repo = MagicMock()
    mock_repo.exists.return_value = False

    with patch("camera_orchestrator.interfaces.cli.build_solver", return_value=MockSolver()), \
         patch("camera_orchestrator.interfaces.cli.build_repository", return_value=mock_repo), \
         pytest.raises(SystemExit) as exc:
        cmd_solve(_args(str(tmp_path / "missing.JPG")), cfg)

    assert exc.value.code == 1
    mock_repo.save.assert_not_called()


def test_cmd_solve_no_solution_exits(tmp_path):
    img = tmp_path / "IMG_0001.JPG"
    img.write_bytes(Path(SAMPLE_IMAGE).read_bytes())
    cfg = _cfg(optics={"sensor_width_mm": 35.8})

    mock_repo = MagicMock()
    mock_repo.exists.return_value = False

    with patch("camera_orchestrator.interfaces.cli.build_solver", return_value=MockSolver(result=None)), \
         patch("camera_orchestrator.interfaces.cli.build_repository", return_value=mock_repo), \
         pytest.raises(SystemExit) as exc:
        cmd_solve(_args(str(img)), cfg)

    assert exc.value.code == 1
    mock_repo.save.assert_called_once()  # record saved even on failure (mirrors batch behaviour)
