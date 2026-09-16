"""Job routes — start long-running work, then poll or stream its progress.

Each POST validates, hands a closure to the `JobRegistry` and returns the `Job`
straight away. The closure is the only place service calls happen, and it runs on
a worker thread, so nothing here blocks the event loop (the MJPEG stream shares
it).

Validation splits along a clear line: anything knowable before the work starts
(missing folder, existing sidecar, camera already busy) is an HTTP error on the
POST; anything that can only fail mid-run (no solution, camera unplugged at frame
9) lands in `job.error`. A job that has been accepted never disappears — you
always get an id back.
"""
from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

import anyio.to_thread
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from camera_orchestrator.application.align_service import AlignService
from camera_orchestrator.application.batch_service import BatchSolveResult, BatchSolveService
from camera_orchestrator.application.capture_service import CaptureService
from camera_orchestrator.application.sequence_service import SequenceService
from camera_orchestrator.application.session_paths import resolve_session
from camera_orchestrator.application.solve_service import solve_file
from camera_orchestrator.config import Config
from camera_orchestrator.domain.models.align import AlignResult
from camera_orchestrator.domain.models.camera import CaptureResult
from camera_orchestrator.domain.models.session import PhaseKind, SessionManifest
from camera_orchestrator.domain.models.solve import SolveRecord
from camera_orchestrator.domain.ports.storage import SolveRecordRepository
from camera_orchestrator.interfaces.api.deps import (
    SolverFactory,
    get_align_service,
    get_capture_service,
    get_config,
    get_registry,
    get_sequence_service,
    get_solve_repository,
    get_solver_factory,
)
from camera_orchestrator.interfaces.api.jobs import JobContext, JobRegistry
from camera_orchestrator.interfaces.api.models import (
    ACTIVE_STATES,
    AlignJobBody,
    BatchJobBody,
    CaptureJobBody,
    Job,
    JobList,
    SequenceJobBody,
    SolveJobBody,
)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

# How long an SSE stream parks waiting for a change before emitting a keep-alive
# comment. Long enough not to churn, short enough that a proxy will not time out.
SSE_TICK_S = 15.0

# Phases whose prompt means "cap the lens" — the only confirmation a sequence
# needs from the UI. Lights start immediately: the POST was the go-ahead.
CAPPED_PHASES: tuple[PhaseKind, ...] = ("dark", "bias")


def _session_dirs(cfg: Config, out_dir: str | None, name: str | None) -> tuple[str, str | None]:
    """Resolve (out_dir, session_dir) the same way the CLI's --out/--name do."""
    return resolve_session(out_dir or cfg.grab.out_dir, name)


def _ensure_dir(path: str) -> None:
    """Create the output folder up front — services write into it, they don't make it."""
    Path(path).mkdir(parents=True, exist_ok=True)


# -- camera jobs -----------------------------------------------------------


@router.post("/capture", response_model=Job)
async def start_capture(
    body: CaptureJobBody,
    cfg: Config = Depends(get_config),
    registry: JobRegistry = Depends(get_registry),
    capture: CaptureService = Depends(get_capture_service),
) -> Job:
    """Fire a capture run (card-only by default, `download` opts into USB transfer)."""
    out_dir, session_dir = _session_dirs(cfg, body.out_dir, body.name)
    request = body.to_request(session_dir or out_dir)

    def runner(ctx: JobContext) -> CaptureResult:
        _ensure_dir(request.out_dir)
        ctx.set_progress(0, request.count, f"{request.kind} frames")

        def on_frame(index: int, total: int, _paths: list[Path]) -> None:
            # Also the cancellation checkpoint: set_progress raises between
            # frames, which unwinds out of CaptureService and stops the run.
            ctx.set_progress(index, total, f"{request.kind} frames")

        if request.download:
            return capture.capture_and_download(request, on_frame=on_frame)
        # record_files only matters for a named session — that is what a manifest
        # would later list. It costs a reconnect-and-poll, so skip it otherwise.
        return capture.capture_to_card(
            request, on_frame=on_frame, record_files=session_dir is not None)

    return registry.submit("capture", runner)


@router.post("/align", response_model=Job)
async def start_align(
    body: AlignJobBody,
    cfg: Config = Depends(get_config),
    registry: JobRegistry = Depends(get_registry),
    align: AlignService = Depends(get_align_service),
) -> Job:
    """Capture one frame and plate-solve it to check where the scope is pointing."""
    out_dir, session_dir = _session_dirs(cfg, body.out_dir, body.name)
    request = body.to_request(out_dir)

    def runner(ctx: JobContext) -> AlignResult:
        _ensure_dir(session_dir or out_dir)
        # Two coarse steps: the frame, then the solve. The solve is the slow half
        # (bounded by solver.cpulimit) and reports nothing until it returns.
        ctx.set_progress(0, 2, "capturing align frame")
        result = align.align(
            request, session_dir=session_dir, name=body.name, force=body.force)
        ctx.set_progress(2, 2, "solved" if result.solved else "no solution")
        return result

    return registry.submit("align", runner)


@router.post("/sequence", response_model=Job)
async def start_sequence(
    body: SequenceJobBody,
    cfg: Config = Depends(get_config),
    registry: JobRegistry = Depends(get_registry),
    sequence: SequenceService = Depends(get_sequence_service),
) -> Job:
    """Run a lights/darks/bias sequence, pausing for a lens-cap confirmation.

    Progress is **per phase, not per frame**: `SequenceService.run` does not
    forward an `on_frame` to its capture calls, so there is nothing finer to
    report (recorded in docs/20260916-web-ui-api.md).
    """
    out_dir, session_dir = _session_dirs(cfg, body.out_dir, body.name)
    request = body.to_request(session_dir or out_dir)
    counts: dict[PhaseKind, int] = {
        "light": request.lights, "dark": request.darks, "bias": request.bias}
    phases = [kind for kind in request.order if counts.get(kind, 0) > 0]
    if not phases:
        raise HTTPException(
            status_code=400, detail="sequence requests no frames — set lights, darks or bias")

    def runner(ctx: JobContext) -> SessionManifest:
        _ensure_dir(request.out_dir)
        done = {"phases": 0}

        def before_phase(kind: PhaseKind) -> None:
            # SequenceService calls this synchronously and blocks inside it — the
            # CLI passes input(); here the job parks in awaiting_confirmation
            # until POST /confirm (or the registry's timeout fails it).
            if kind in CAPPED_PHASES:
                ctx.prompt(kind, f"Cover the lens for {kind} frames, then confirm.")
            ctx.set_progress(done["phases"], len(phases), f"{kind} frames")
            done["phases"] += 1

        ctx.set_progress(0, len(phases), "starting")
        manifest = sequence.run(request, session_dir=session_dir, before_phase=before_phase)
        ctx.set_progress(len(phases), len(phases), "complete")
        return manifest

    return registry.submit("sequence", runner)


# -- solver jobs (no camera — these may run alongside a capture) -----------


@router.post("/batch", response_model=Job)
async def start_batch(
    body: BatchJobBody,
    cfg: Config = Depends(get_config),
    registry: JobRegistry = Depends(get_registry),
    solver_factory: SolverFactory = Depends(get_solver_factory),
    repository: SolveRecordRepository = Depends(get_solve_repository),
) -> Job:
    """Plate-solve every unsolved image in a folder."""
    folder = Path(body.folder)
    if not folder.is_dir():
        raise HTTPException(status_code=404, detail=f"no such folder: {body.folder}")

    service = BatchSolveService(
        solver_factory=solver_factory, repository=repository, cfg=cfg,
        mode=body.mode, cpulimit=body.cpulimit)

    def runner(ctx: JobContext) -> BatchSolveResult:
        def on_plan(pending: int, _skipped: int) -> None:
            ctx.set_progress(0, pending, "solving")

        def on_image_start(index: int, total: int, path: Path) -> None:
            ctx.set_progress(index - 1, total, path.name)

        def on_image(index: int, total: int, record: SolveRecord) -> None:
            ctx.set_progress(index, total, record.original_file)

        return service.run(
            str(folder), annotate=body.annotate, reprocess=body.reprocess,
            on_plan=on_plan, on_image_start=on_image_start, on_image=on_image)

    return registry.submit("batch", runner)


@router.post("/solve", response_model=Job)
async def start_solve(
    body: SolveJobBody,
    cfg: Config = Depends(get_config),
    registry: JobRegistry = Depends(get_registry),
    solver_factory: SolverFactory = Depends(get_solver_factory),
    repository: SolveRecordRepository = Depends(get_solve_repository),
) -> Job:
    """Plate-solve one image file in place, writing its sidecar alongside."""
    path = Path(body.file)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"no such file: {body.file}")
    if repository.exists(path.name, str(path.parent)) and not body.force:
        raise HTTPException(
            status_code=409,
            detail=f"'{path.name}' already has a sidecar — pass force to re-solve")

    annotate_out = str(path.parent / f"{path.stem}_solved.png") if body.annotate else None

    def runner(ctx: JobContext) -> SolveRecord:
        ctx.set_progress(0, 1, path.name)
        job = solve_file(str(path), solver_factory(cfg), cfg, annotate_out=annotate_out)
        record = job.to_record(cfg)
        repository.save(record, str(path.parent))
        if not job.solved:
            # Mirrors the CLI exiting 1: a sidecar is written either way, but a
            # frame that did not solve is a failed job, not a successful one.
            raise RuntimeError(record.error or "solver returned no solution")
        ctx.set_progress(1, 1, path.name)
        return record

    return registry.submit("solve", runner)


# -- job queries -----------------------------------------------------------


@router.get("", response_model=JobList)
async def list_jobs(registry: JobRegistry = Depends(get_registry)) -> JobList:
    """Every job this process has run, newest first."""
    return JobList(jobs=registry.list())


@router.get("/{job_id}", response_model=Job)
async def get_job(job_id: str, registry: JobRegistry = Depends(get_registry)) -> Job:
    """One job's current state."""
    return registry.get(job_id)


@router.post("/{job_id}/confirm", response_model=Job)
async def confirm_job(job_id: str, registry: JobRegistry = Depends(get_registry)) -> Job:
    """Answer a pending prompt — the UI's replacement for the CLI's Enter key."""
    return await anyio.to_thread.run_sync(registry.confirm, job_id)


@router.post("/{job_id}/cancel", response_model=Job)
async def cancel_job(job_id: str, registry: JobRegistry = Depends(get_registry)) -> Job:
    """Request cancellation.

    Cooperative: a job waiting on a prompt unwinds at once, a running capture
    stops at its next frame boundary, and a blocking driver call has to finish
    first. The returned Job may still read 'running'.
    """
    return await anyio.to_thread.run_sync(registry.cancel, job_id)


async def _job_events(request: Request, registry: JobRegistry, job_id: str) -> AsyncIterator[str]:
    """Emit the job as SSE on every change, then stop once it is terminal."""
    version = -1  # no real version matches, so the first event fires immediately
    while not await request.is_disconnected():
        current, job = await anyio.to_thread.run_sync(
            registry.wait, job_id, version, SSE_TICK_S)
        if current == version:
            # wait() returned on its timeout with nothing new — a comment line
            # keeps the connection warm without re-sending unchanged state.
            yield ": keep-alive\n\n"
            continue
        version = current
        yield f"data: {job.model_dump_json()}\n\n"
        if job.state not in ACTIVE_STATES:
            return


@router.get("/{job_id}/events")
async def job_events(
    job_id: str,
    request: Request,
    registry: JobRegistry = Depends(get_registry),
) -> StreamingResponse:
    """Server-sent events: one `data:` line of Job JSON per state change."""
    registry.get(job_id)  # JobNotFoundError -> 404 before the stream opens
    return StreamingResponse(
        _job_events(request, registry, job_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
