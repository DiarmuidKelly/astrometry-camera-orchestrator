"""Composition root — the only module that wires ports to concrete adapters.

Everything else depends on abstractions; here we choose implementations. A
future config switch (e.g. solver backend = docker|api) would live here.
"""
from __future__ import annotations

import threading

from camera_orchestrator.adapters.camera.gphoto import GphotoCamera
from camera_orchestrator.adapters.solvers.docker import DockerSolver
from camera_orchestrator.adapters.storage.session_manifest import SidecarSessionRepository
from camera_orchestrator.adapters.storage.sidecar import SidecarSolveRepository
from camera_orchestrator.application.align_service import AlignService
from camera_orchestrator.application.browse_service import BrowseService
from camera_orchestrator.application.camera_session import CameraSession
from camera_orchestrator.application.capture_service import CaptureService
from camera_orchestrator.application.sequence_service import SequenceService
from camera_orchestrator.config import Config
from camera_orchestrator.domain.ports.camera import Camera
from camera_orchestrator.domain.ports.session_manifest import SessionManifestRepository
from camera_orchestrator.domain.ports.solver import Solver
from camera_orchestrator.domain.ports.storage import SolveRecordRepository


def build_solver(cfg: Config) -> Solver:
    """Build the plate-solver backend from config (Docker today)."""
    return DockerSolver(
        image=cfg.solver.image,
        index_dir=cfg.solver.index_dir,
        cpulimit=cfg.solver.cpulimit,
        extra_args=cfg.solver.solve_args,
    )


def build_camera() -> Camera:
    """Open the camera backend (python-gphoto2)."""
    return GphotoCamera()


def build_capture_service() -> CaptureService:
    """CaptureService wired with the concrete camera factory.

    One fresh connection per workflow — right for the CLI, where a command owns
    the camera for its whole run. Long-lived processes (a web UI streaming live
    view) want build_shared_capture_service() instead.
    """
    return CaptureService(camera_factory=build_camera)


# The process-wide camera session. libgphoto2 claims the USB device
# exclusively, so a live-view stream and a capture request must share one
# connection; this is that single owner. Built on first use.
_session: CameraSession | None = None
_session_lock = threading.Lock()


def build_camera_session() -> CameraSession:
    """Return the process-wide CameraSession (created on first call)."""
    global _session
    with _session_lock:
        if _session is None:
            _session = CameraSession(camera_factory=build_camera)
        return _session


def build_shared_camera() -> Camera:
    """Borrow the shared session's camera — a non-closing proxy.

    Factory-shaped so it can drop straight into CaptureService in place of
    build_camera; closing the proxy releases the borrow, not the connection.
    """
    return build_camera_session().borrow()


def build_shared_capture_service() -> CaptureService:
    """CaptureService backed by the shared session (safe alongside live view).

    The reconnect hook is mandatory here: borrowing the shared session hands back
    one long-lived connection, and libgphoto2 caches its directory listing, so
    card-only filename recording would never see new files without a genuine
    reconnect between polls.
    """
    session = build_camera_session()
    return CaptureService(
        camera_factory=build_shared_camera,
        on_reconnect=session.reconnect,
    )


def build_repository() -> SolveRecordRepository:
    """Build the solve-record persistence backend (filesystem sidecar today)."""
    return SidecarSolveRepository()


def build_session_repository() -> SessionManifestRepository:
    """Build the session-manifest persistence backend (filesystem sidecar today)."""
    return SidecarSessionRepository()


def build_align_service(cfg: Config) -> AlignService:
    """AlignService wired with the capture service, solver factory, and manifest repo."""
    return AlignService(build_capture_service(), lambda: build_solver(cfg), cfg,
                        build_session_repository())


def build_sequence_service() -> SequenceService:
    """SequenceService wired with the capture service and manifest repo."""
    return SequenceService(build_capture_service(), build_session_repository())


def build_shared_align_service(cfg: Config) -> AlignService:
    """AlignService on the shared session — the variant a long-lived server wants.

    build_align_service() opens a fresh connection per call, which collides with
    a live-view stream ("Could not claim the USB device"). Same service, shared
    capture backend.
    """
    return AlignService(build_shared_capture_service(), lambda: build_solver(cfg), cfg,
                        build_session_repository())


def build_shared_sequence_service() -> SequenceService:
    """SequenceService on the shared session (safe alongside live view)."""
    return SequenceService(build_shared_capture_service(), build_session_repository())


def build_browse_service(cfg: Config) -> BrowseService:
    """BrowseService confined to the configured capture root (grab.out_dir)."""
    return BrowseService(build_session_repository(), cfg.grab.out_dir)
