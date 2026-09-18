"""Job routes — start long-running work, then follow it on the job socket.

Progress does **not** stream from here. Live updates are multiplexed onto the one
WebSocket in `routes_ws.py`; these routes start work, list it and answer prompts.
The reasoning is a browser's ~6-connections-per-origin cap — see that module.

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

from fastapi import APIRouter, Depends, HTTPException

from camera_orchestrator.application.align_service import AlignService
from camera_orchestrator.application.batch_service import BatchSolveResult, BatchSolveService
from camera_orchestrator.application.browse_service import BrowsePathError, BrowseService
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
    get_browse_service,
    get_capture_service,
    get_config,
    get_registry,
    get_sequence_service,
    get_solve_repository,
    get_solver_factory,
)
from camera_orchestrator.interfaces.api.jobs import JobContext, JobRegistry
from camera_orchestrator.interfaces.api.models import (
    AlignJobBody,
    BatchJobBody,
    CaptureJobBody,
    ConfirmBody,
    Job,
    JobList,
    SequenceJobBody,
    SolveJobBody,
)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

# Phases whose prompt means "cap the lens" — the only confirmation a sequence
# needs from the UI. Lights start immediately: the POST was the go-ahead.
CAPPED_PHASES: tuple[PhaseKind, ...] = ("dark", "bias")


def _confine(browse: BrowseService, path: str, field: str) -> Path:
    """Resolve a client-supplied path inside the capture root, or 400.

    Every path on these routes arrives as a free-text field over HTTP, and each
    one is acted on: `out_dir` gets created and written into, `folder` gets
    `solve_results.json` plus an `annotated/` subdirectory and is bind-mounted
    into a solver container, `file` is read and gains a sidecar. Unconfined,
    that is "write anywhere the process can write" for anyone who can reach the
    port. They go through the same resolver the browse routes use, against the
    same root, so there is one confinement rule in the app and not two.
    """
    try:
        return browse.resolve(path)
    except BrowsePathError:
        raise HTTPException(
            status_code=400,
            detail=f"{field} '{path}' is outside the capture root {browse.root}") from None


def _session_dirs(
    browse: BrowseService, cfg: Config, out_dir: str | None, name: str | None,
) -> tuple[str, str | None]:
    """Resolve (out_dir, session_dir) the same way the CLI's --out/--name do.

    Confined *after* `resolve_session`, so a `name` of '../../etc' is caught too
    — the folder it produces is the thing that gets created. The confined path is
    what the job then uses, so the directory checked is the directory written.
    """
    resolved, session_dir = resolve_session(out_dir or cfg.grab.out_dir, name)
    confined = str(_confine(browse, resolved, "out_dir"))
    return confined, confined if session_dir is not None else None


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
    browse: BrowseService = Depends(get_browse_service),
) -> Job:
    """Fire a capture run (card-only by default, `download` opts into USB transfer)."""
    out_dir, session_dir = _session_dirs(browse, cfg, body.out_dir, body.name)
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
    browse: BrowseService = Depends(get_browse_service),
) -> Job:
    """Capture one frame and plate-solve it to check where the scope is pointing."""
    out_dir, session_dir = _session_dirs(browse, cfg, body.out_dir, body.name)
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
    browse: BrowseService = Depends(get_browse_service),
) -> Job:
    """Run a lights/darks/bias sequence, pausing for a lens-cap confirmation.

    Progress is **per phase, not per frame**: `SequenceService.run` does not
    forward an `on_frame` to its capture calls, so there is nothing finer to
    report (recorded in docs/20260916-web-ui-api.md).
    """
    out_dir, session_dir = _session_dirs(browse, cfg, body.out_dir, body.name)
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
    browse: BrowseService = Depends(get_browse_service),
) -> Job:
    """Plate-solve every unsolved image in a folder."""
    folder = _confine(browse, body.folder, "folder")
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
    browse: BrowseService = Depends(get_browse_service),
) -> Job:
    """Plate-solve one image file in place, writing its sidecar alongside."""
    path = _confine(browse, body.file, "file")
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
async def confirm_job(
    job_id: str,
    body: ConfirmBody | None = None,
    registry: JobRegistry = Depends(get_registry),
) -> Job:
    """Answer a pending prompt — the UI's replacement for the CLI's Enter key.

    The body carries the `prompt.token` being answered; a confirm without it (or
    with a superseded one) is a 409 rather than a release of whatever the job
    happens to be waiting on next.

    Called **inline**, not on a worker thread: it is a mutex acquire plus an
    Event.set, and bridging it would put one of the two safety-critical controls
    behind anyio's exhaustible thread limiter for no benefit.
    """
    return registry.confirm(job_id, body.token if body is not None else None)


@router.post("/{job_id}/cancel", response_model=Job)
async def cancel_job(job_id: str, registry: JobRegistry = Depends(get_registry)) -> Job:
    """Request cancellation.

    Cooperative: a job waiting on a prompt unwinds at once, a running capture
    stops at its next frame boundary, and a blocking driver call has to finish
    first. The returned Job may still read 'running'.

    Inline for the same reason as `confirm_job` — a user who is cancelling a
    wedged camera must not queue behind the threads that wedged it.
    """
    return registry.cancel(job_id)
