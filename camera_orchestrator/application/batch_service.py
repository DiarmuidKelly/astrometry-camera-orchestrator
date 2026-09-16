"""Batch solve service — plate-solve every image in a folder.

Scans a directory, skips images that already have a sidecar record (resume),
solves the rest, persists each record through the repository port and writes a
`solve_results.json` summary next to the images.

UI-agnostic: no argparse, no stdout, no sys.exit. The empty cases ("nothing to
solve", "everything already solved") are ordinary control flow and come back as
a `BatchSolveResult` with the matching `status`; the caller decides whether that
is an error (the CLI exits) or an empty response (an API returns JSON).
Progress is streamed through optional callbacks, mirroring
`CaptureService`'s `FrameCallback`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Literal, Optional

from pydantic import BaseModel, Field

from camera_orchestrator.application.solve_service import solve_file
from camera_orchestrator.config import Config
from camera_orchestrator.domain.models.solve import SolveRecord
from camera_orchestrator.domain.ports.solver import Solver
from camera_orchestrator.domain.ports.storage import SolveRecordRepository

# File types we hand to the solver (JPEGs straight through, CR2 decoded first).
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".cr2"}

# Summary file written into the scanned folder.
RESULTS_FILENAME = "solve_results.json"

# Builds a Solver from the effective config. The composition root injects the
# concrete adapter factory (build_solver); tests inject a mock.
SolverFactory = Callable[[Config], Solver]

# Called once after the scan/skip pass, before any solving: (pending, skipped).
PlanCallback = Callable[[int, int], None]

# Called before each image is solved: (index, total, path).
ImageStartCallback = Callable[[int, int, Path], None]

# Called after each image is solved and saved: (index, total, record).
ImageCallback = Callable[[int, int, SolveRecord], None]

BatchStatus = Literal["ok", "no_images", "all_solved"]


class BatchSolveResult(BaseModel):
    """Outcome of a batch run — counts, per-image records, summary file."""

    status: BatchStatus = Field(description="'ok' if images were solved, 'no_images' if the folder held none, 'all_solved' if every image already had a sidecar.")
    folder: str = Field(description="Folder that was scanned.")
    total: int = Field(description="Number of candidate images found in the folder, before skipping.")
    solved: int = Field(default=0, description="Number of images that returned an astrometric solution.")
    skipped: int = Field(default=0, description="Number of images skipped because a sidecar already existed.")
    records: list[SolveRecord] = Field(default_factory=list, description="Per-image solve records, in the order they were processed.")
    results_path: Optional[str] = Field(default=None, description="Path of the written solve_results.json, or null if nothing was solved.")


def effective_config(
    cfg: Config,
    mode: str | None = None,
    cpulimit: int | None = None,
) -> Config:
    """Return a deep copy of `cfg` with the per-request solver overrides applied.

    Never mutates the caller's Config — an API serves many requests from one
    shared Config object, so overrides must be local to the run.
    """
    out = cfg.model_copy(deep=True)
    if mode:
        out.solver.mode = mode  # type: ignore[assignment]
    if cpulimit:
        out.solver.cpulimit = cpulimit
    return out


class BatchSolveService:
    """Solves every unsolved image in a folder and records the results."""

    def __init__(
        self,
        solver_factory: SolverFactory,
        repository: SolveRecordRepository,
        cfg: Config,
        mode: str | None = None,
        cpulimit: int | None = None,
    ):
        """Args:
            solver_factory: Callable taking the effective Config and returning a
                Solver. The composition root injects the concrete adapter; the
                service never imports one.
            repository: Solve-record persistence port (drives resume/skip).
            cfg: Loaded configuration. Copied internally — never mutated.
            mode: Optional solver-mode override ('fast' | 'accurate').
            cpulimit: Optional solver CPU-time-limit override, in seconds.
        """
        self.cfg = effective_config(cfg, mode, cpulimit)
        self._solver_factory = solver_factory
        self._repo = repository

    def run(
        self,
        folder: str,
        annotate: bool = False,
        reprocess: bool = False,
        on_plan: PlanCallback | None = None,
        on_image_start: ImageStartCallback | None = None,
        on_image: ImageCallback | None = None,
    ) -> BatchSolveResult:
        """Solve the folder's images and return a summary.

        Args:
            folder: Directory to scan (non-recursive).
            annotate: Write annotated overlays into `<folder>/annotated/` and
                keep the sidecars there too.
            reprocess: Re-solve images that already have a sidecar record.
            on_plan: Fired once with (pending, skipped) after the scan.
            on_image_start: Fired before each solve with (index, total, path).
            on_image: Fired after each solve with (index, total, record).

        Returns:
            BatchSolveResult; `status` is 'no_images' or 'all_solved' when there
            was nothing to do (no exception, no exit — the caller renders it).
        """
        root = Path(folder)
        images = sorted(
            p for p in root.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
        )
        found = len(images)

        if not images:
            if on_plan is not None:
                on_plan(0, 0)
            return BatchSolveResult(status="no_images", folder=str(root), total=0)

        sidecar_dir = root / "annotated" if annotate else root

        skipped = 0
        if not reprocess:
            pending = [p for p in images if not self._repo.exists(p.name, str(sidecar_dir))]
            skipped = len(images) - len(pending)
            images = pending

        if on_plan is not None:
            on_plan(len(images), skipped)

        if not images:
            return BatchSolveResult(
                status="all_solved", folder=str(root), total=found, skipped=skipped)

        annotate_dir = root / "annotated" if annotate else None
        if annotate_dir:
            annotate_dir.mkdir(exist_ok=True)

        solver = self._solver_factory(self.cfg)
        total = len(images)
        records: list[SolveRecord] = []

        for i, path in enumerate(images, 1):
            if on_image_start is not None:
                on_image_start(i, total, path)

            annotate_out = (
                str(annotate_dir / f"{path.stem}_solved.jpg") if annotate_dir else None)

            job = solve_file(str(path), solver, self.cfg, annotate_out=annotate_out)
            record = job.to_record(self.cfg)

            self._repo.save(record, str(sidecar_dir))
            records.append(record)

            if on_image is not None:
                on_image(i, total, record)

        results_path = root / RESULTS_FILENAME
        results_path.write_text(
            json.dumps([r.model_dump() for r in records], indent=2))

        return BatchSolveResult(
            status="ok",
            folder=str(root),
            total=found,
            solved=sum(1 for r in records if r.solved),
            skipped=skipped,
            records=records,
            results_path=str(results_path),
        )
