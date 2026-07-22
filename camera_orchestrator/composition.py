"""Composition root — the only module that wires ports to concrete adapters.

Everything else depends on abstractions; here we choose implementations. A
future config switch (e.g. solver backend = docker|api) would live here.
"""
from __future__ import annotations

from camera_orchestrator.adapters.camera.gphoto import GphotoCamera
from camera_orchestrator.adapters.solvers.docker import DockerSolver
from camera_orchestrator.adapters.storage.sidecar import SidecarSolveRepository
from camera_orchestrator.application.capture_service import CaptureService
from camera_orchestrator.config import Config
from camera_orchestrator.domain.ports.camera import Camera
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
    """CaptureService wired with the concrete camera factory."""
    return CaptureService(camera_factory=build_camera)


def build_repository() -> SolveRecordRepository:
    """Build the solve-record persistence backend (filesystem sidecar today)."""
    return SidecarSolveRepository()
