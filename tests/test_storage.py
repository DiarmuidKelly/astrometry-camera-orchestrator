"""Tests for SidecarSolveRepository — filesystem sidecar-JSON persistence."""
from __future__ import annotations

from camera_orchestrator.adapters.storage.sidecar import SidecarSolveRepository
from camera_orchestrator.domain.models.solve import (
    ImageExif,
    ObserverInfo,
    SolveHints,
    SolveRecord,
    SolveResult,
)


def _record(original_file: str = "IMG_0001.JPG", solved: bool = True) -> SolveRecord:
    return SolveRecord(
        original_file=original_file,
        solved_at="2026-07-22T20:00:00+00:00",
        exif=ImageExif(),
        solved=solved,
        solve=SolveResult(
            center_ra_deg=115.4, center_dec_deg=21.5,
            scale_arcsec_per_px=3.96, width_px=6000, height_px=4000,
        ) if solved else None,
        hints_used=SolveHints(),
        observer=ObserverInfo(),
        solver_mode="accurate",
        error=None if solved else "no solution",
    )


def test_save_writes_stem_based_sidecar(tmp_path):
    repo = SidecarSolveRepository()
    path = repo.save(_record("IMG_0001.JPG"), str(tmp_path))
    assert path == str(tmp_path / "IMG_0001_solved.json")
    assert (tmp_path / "IMG_0001_solved.json").exists()


def test_exists_false_before_true_after(tmp_path):
    repo = SidecarSolveRepository()
    assert repo.exists("IMG_0001.JPG", str(tmp_path)) is False
    repo.save(_record("IMG_0001.JPG"), str(tmp_path))
    assert repo.exists("IMG_0001.JPG", str(tmp_path)) is True


def test_find_by_image_round_trips(tmp_path):
    repo = SidecarSolveRepository()
    original = _record("IMG_0001.JPG")
    repo.save(original, str(tmp_path))

    loaded = repo.find_by_image("IMG_0001.JPG", str(tmp_path))
    assert loaded is not None
    assert loaded == original
    assert loaded.solve is not None
    assert loaded.solve.center_ra_deg == 115.4


def test_find_by_image_returns_none_when_missing(tmp_path):
    repo = SidecarSolveRepository()
    assert repo.find_by_image("IMG_9999.JPG", str(tmp_path)) is None


def test_exists_matches_by_stem_not_extension(tmp_path):
    # save writes IMG_0001_solved.json; a query by the .CR2 sibling stem finds it
    repo = SidecarSolveRepository()
    repo.save(_record("IMG_0001.JPG"), str(tmp_path))
    assert repo.exists("IMG_0001.CR2", str(tmp_path)) is True


def test_save_creates_missing_dest_dir(tmp_path):
    repo = SidecarSolveRepository()
    nested = tmp_path / "annotated"
    repo.save(_record("IMG_0001.JPG"), str(nested))
    assert (nested / "IMG_0001_solved.json").exists()
