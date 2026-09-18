"""Tests for BatchSolveService — in-memory record repo, patched/mock solver.

solve_file is patched at its batch_service import site for the folder-mechanics
tests; one test drives the real solve_file with MockSolver over the sample JPEG
fixture to prove the wiring holds end to end.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

from camera_orchestrator.application.batch_service import (
    RESULTS_FILENAME,
    BatchSolveService,
    effective_config,
)
from camera_orchestrator.config import Config
from camera_orchestrator.domain.models.solve import SolveJob, SolveRecord, SolveResult

from tests.test_pipeline import SAMPLE_IMAGE, MockSolver  # reuse the Solver mock


class FakeRepo:
    """In-memory SolveRecordRepository keyed by (dest_dir, image_name)."""

    def __init__(self, solved: set[tuple[str, str]] | None = None):
        self.store: dict[tuple[str, str], SolveRecord] = {}
        self.preexisting = solved or set()

    def save(self, record, dest_dir):
        self.store[(dest_dir, record.original_file)] = record
        return f"{dest_dir}/{record.original_file}.json"

    def find_by_image(self, image_name, dest_dir):
        return self.store.get((dest_dir, image_name))

    def exists(self, image_name, dest_dir):
        return (dest_dir, image_name) in self.store or (dest_dir, image_name) in self.preexisting


def _cfg(**kwargs) -> Config:
    return Config.model_validate(kwargs)


def _service(repo=None, cfg=None, **overrides) -> BatchSolveService:
    return BatchSolveService(
        solver_factory=lambda c: MockSolver(),
        repository=repo or FakeRepo(),
        cfg=cfg or _cfg(),
        **overrides,
    )


def _images(folder: Path, *names: str) -> list[Path]:
    paths = []
    for name in names:
        p = folder / name
        p.write_bytes(b"fake-image")
        paths.append(p)
    return paths


def _solved_job(path: str) -> SolveJob:
    return SolveJob(
        path=path,
        result=SolveResult(
            center_ra_deg=115.4, center_dec_deg=21.5, scale_arcsec_per_px=3.96,
            width_px=60, height_px=40,
        ),
    )


def _patch_solve():
    return patch("camera_orchestrator.application.batch_service.solve_file",
                 side_effect=lambda path, solver, cfg, annotate_out=None: _solved_job(path))


def test_empty_folder_returns_no_images(tmp_path):
    result = _service().run(str(tmp_path))
    assert result.status == "no_images"                 # control flow, not an exception
    assert result.total == 0
    assert result.records == []
    assert not (tmp_path / RESULTS_FILENAME).exists()   # nothing attempted, nothing written


def test_non_image_files_are_ignored(tmp_path):
    _images(tmp_path, "notes.txt", "session.json")
    assert _service().run(str(tmp_path)).status == "no_images"


def test_all_already_solved_returns_without_solving(tmp_path):
    _images(tmp_path, "a.jpg", "b.jpg")
    repo = FakeRepo(solved={(str(tmp_path), "a.jpg"), (str(tmp_path), "b.jpg")})
    with _patch_solve() as mock_solve:
        result = _service(repo=repo).run(str(tmp_path))
    assert result.status == "all_solved"
    assert result.skipped == 2
    assert result.total == 2                            # total counts what was found
    assert mock_solve.call_count == 0                   # the solver was never touched


def test_resume_skips_solved_images(tmp_path):
    _images(tmp_path, "a.jpg", "b.jpg", "c.CR2")
    repo = FakeRepo(solved={(str(tmp_path), "a.jpg")})
    with _patch_solve() as mock_solve:
        result = _service(repo=repo).run(str(tmp_path))
    assert result.status == "ok"
    assert result.skipped == 1
    assert result.solved == 2
    assert [Path(c.args[0]).name for c in mock_solve.call_args_list] == ["b.jpg", "c.CR2"]


def test_reprocess_resolves_everything(tmp_path):
    _images(tmp_path, "a.jpg", "b.jpg")
    repo = FakeRepo(solved={(str(tmp_path), "a.jpg")})
    with _patch_solve() as mock_solve:
        result = _service(repo=repo).run(str(tmp_path), reprocess=True)
    assert result.skipped == 0
    assert mock_solve.call_count == 2                   # --reprocess ignores the sidecars


def test_records_are_saved_through_the_repository(tmp_path):
    _images(tmp_path, "a.jpg")
    repo = FakeRepo()
    with _patch_solve():
        _service(repo=repo).run(str(tmp_path))
    assert (str(tmp_path), "a.jpg") in repo.store


def test_annotate_writes_sidecars_into_the_annotated_subfolder(tmp_path):
    _images(tmp_path, "a.jpg")
    repo = FakeRepo()
    with _patch_solve() as mock_solve:
        _service(repo=repo).run(str(tmp_path), annotate=True)
    annotated = tmp_path / "annotated"
    assert annotated.is_dir()                           # created before solving
    assert (str(annotated), "a.jpg") in repo.store      # sidecars follow the overlays
    assert mock_solve.call_args.kwargs["annotate_out"].endswith("annotated/a_solved.jpg")


def test_no_annotate_dir_without_the_flag(tmp_path):
    _images(tmp_path, "a.jpg")
    with _patch_solve() as mock_solve:
        _service().run(str(tmp_path))
    assert not (tmp_path / "annotated").exists()
    assert mock_solve.call_args.kwargs["annotate_out"] is None


def test_results_file_is_written_with_every_record(tmp_path):
    _images(tmp_path, "a.jpg", "b.jpg")
    with _patch_solve():
        result = _service().run(str(tmp_path))
    payload = json.loads(Path(result.results_path).read_text())
    assert result.results_path == str(tmp_path / RESULTS_FILENAME)
    assert [r["original_file"] for r in payload] == ["a.jpg", "b.jpg"]   # sorted order
    assert all(r["solved"] for r in payload)


def test_failed_solve_is_recorded_but_not_counted_as_solved(tmp_path):
    _images(tmp_path, "a.jpg", "b.jpg")
    with patch("camera_orchestrator.application.batch_service.solve_file",
               side_effect=lambda path, solver, cfg, annotate_out=None: (
                   _solved_job(path) if path.endswith("a.jpg")
                   else SolveJob(path=path, error="no solution"))):
        result = _service().run(str(tmp_path))
    assert result.solved == 1
    assert len(result.records) == 2                     # failures still get a sidecar
    assert result.records[1].error == "no solution"


def test_progress_callbacks_fire_once_per_image(tmp_path):
    _images(tmp_path, "a.jpg", "b.jpg")
    starts: list[tuple[int, int, str]] = []
    done: list[tuple[int, int, str]] = []
    plans: list[tuple[int, int]] = []
    with _patch_solve():
        _service().run(
            str(tmp_path),
            on_plan=lambda pending, skipped: plans.append((pending, skipped)),
            on_image_start=lambda i, total, path: starts.append((i, total, path.name)),
            on_image=lambda i, total, rec: done.append((i, total, rec.original_file)),
        )
    assert plans == [(2, 0)]                            # fired once, before any solving
    assert starts == [(1, 2, "a.jpg"), (2, 2, "b.jpg")]  # 1-based index, running total
    assert done == starts


def test_plan_callback_reports_skips_even_when_nothing_is_pending(tmp_path):
    _images(tmp_path, "a.jpg")
    repo = FakeRepo(solved={(str(tmp_path), "a.jpg")})
    plans: list[tuple[int, int]] = []
    with _patch_solve():
        _service(repo=repo).run(str(tmp_path), on_plan=lambda p, s: plans.append((p, s)))
    assert plans == [(0, 1)]                            # CLI still logs the skip line


def test_overrides_do_not_mutate_the_callers_config(tmp_path):
    cfg = _cfg(solver={"mode": "accurate", "cpulimit": 60})
    service = _service(cfg=cfg, mode="fast", cpulimit=5)
    assert service.cfg.solver.mode == "fast"            # the copy carries the override
    assert service.cfg.solver.cpulimit == 5
    assert cfg.solver.mode == "accurate"                # the shared Config is untouched
    assert cfg.solver.cpulimit == 60


def test_effective_config_without_overrides_is_an_unchanged_copy():
    cfg = _cfg(solver={"mode": "accurate", "cpulimit": 60})
    copy = effective_config(cfg)
    assert copy is not cfg
    assert copy.model_dump() == cfg.model_dump()


def test_solver_is_built_from_the_effective_config(tmp_path):
    _images(tmp_path, "a.jpg")
    seen: list[Config] = []
    service = BatchSolveService(
        solver_factory=lambda c: seen.append(c) or MockSolver(),
        repository=FakeRepo(),
        cfg=_cfg(),
        cpulimit=5,
    )
    with _patch_solve():
        service.run(str(tmp_path))
    assert seen[0].solver.cpulimit == 5                 # overrides reach the solver build


def test_end_to_end_with_the_real_solve_file(tmp_path):
    shutil.copy(SAMPLE_IMAGE, tmp_path / "IMG_4341.JPG")
    solver = MockSolver(result=SolveResult(
        center_ra_deg=267.73, center_dec_deg=-29.46, scale_arcsec_per_px=3.97,
        width_px=6000, height_px=4000,
    ))
    repo = FakeRepo()
    service = BatchSolveService(solver_factory=lambda c: solver, repository=repo,
                                cfg=_cfg(optics={"sensor_width_mm": 22.3}))
    result = service.run(str(tmp_path))
    assert result.solved == 1
    record = result.records[0]
    assert record.solve.center_ra_deg == 267.73
    assert record.exif.focal_mm is not None             # EXIF flows through solve_file
    assert record.hints_used.scale_low is not None      # scale hint from optics + EXIF
