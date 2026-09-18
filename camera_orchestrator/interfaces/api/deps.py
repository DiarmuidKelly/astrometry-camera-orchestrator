"""FastAPI dependency providers — the API's thin seam onto the composition root.

Every provider here is either a `request.app.state` lookup (per-app singletons:
config, the job registry) or a one-line call to a `composition.build_*` factory.
Nothing in this package imports an adapter; this module is as close as it gets.

Two reasons they are `Depends`-able rather than module-level calls:

- Tests override them (`app.dependency_overrides[get_camera_session] = ...`) and
  never touch hardware.
- The camera-facing services must come from the **shared** builders. The plain
  `build_align_service()` opens a fresh USB connection per call, which collides
  with the live-view stream; the shared variants borrow the one session.
"""
from __future__ import annotations

from typing import Callable

from fastapi import Depends, Request, WebSocket

from camera_orchestrator.application.align_service import AlignService
from camera_orchestrator.application.browse_service import BrowseService
from camera_orchestrator.application.camera_session import CameraSession
from camera_orchestrator.application.capture_service import CaptureService
from camera_orchestrator.application.sequence_service import SequenceService
from camera_orchestrator.composition import (
    build_browse_service,
    build_camera_session,
    build_repository,
    build_shared_align_service,
    build_shared_capture_service,
    build_shared_sequence_service,
    build_solver,
)
from camera_orchestrator.config import Config
from camera_orchestrator.domain.ports.solver import Solver
from camera_orchestrator.domain.ports.storage import SolveRecordRepository
from camera_orchestrator.interfaces.api.jobs import JobRegistry

# Builds a Solver from an effective Config — what BatchSolveService expects.
SolverFactory = Callable[[Config], Solver]


def get_config(request: Request) -> Config:
    """The Config this app was created with (loaded once at startup)."""
    config: Config = request.app.state.config
    return config


def get_registry(request: Request) -> JobRegistry:
    """The per-app job registry. One per app so tests get a clean slate."""
    registry: JobRegistry = request.app.state.jobs
    return registry


def get_ws_registry(websocket: WebSocket) -> JobRegistry:
    """The same registry, for websocket routes.

    A separate provider because FastAPI only injects `Request` into HTTP
    endpoints; a websocket handler is handed a `WebSocket` instead. Both read the
    one `app.state.jobs`, so the socket and the REST routes see identical state.
    """
    registry: JobRegistry = websocket.app.state.jobs
    return registry


def get_camera_session() -> CameraSession:
    """The process-wide CameraSession — single owner of the USB connection."""
    return build_camera_session()


def get_capture_service() -> CaptureService:
    """CaptureService on the shared session (safe alongside live view)."""
    return build_shared_capture_service()


def get_align_service(cfg: Config = Depends(get_config)) -> AlignService:
    """AlignService on the shared session."""
    return build_shared_align_service(cfg)


def get_sequence_service() -> SequenceService:
    """SequenceService on the shared session."""
    return build_shared_sequence_service()


def get_browse_root(request: Request) -> str:
    """The capture root, fixed when the app was created.

    Read from `app.state`, never from the live config: `grab.out_dir` is writable
    through `PUT /api/config`, and a root that a request can move confines
    nothing. A config write changes where *new* captures default to; it does not
    change what the API is allowed to read or write.
    """
    root: str = request.app.state.browse_root
    return root


def get_browse_service(root: str = Depends(get_browse_root)) -> BrowseService:
    """BrowseService confined to the fixed capture root."""
    return build_browse_service(root)


def get_solve_repository() -> SolveRecordRepository:
    """Solve-record persistence port (sidecar JSON today)."""
    return build_repository()


def get_solver_factory() -> SolverFactory:
    """The Config -> Solver factory, injected into BatchSolveService."""
    return build_solver
