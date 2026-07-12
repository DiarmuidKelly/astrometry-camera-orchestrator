"""Solver drivers — public API re-exported for backwards compatibility."""
from .api import ApiSolver
from .base import Solver, build_hints, result_from_wcs, scale_hint_from_optics, write_fits
from .docker import DockerSolver

from ..config import Config


def build_solver(cfg: Config) -> Solver:
    return DockerSolver(
        image=cfg.solver.image,
        index_dir=cfg.solver.index_dir,
        cpulimit=cfg.solver.cpulimit,
        extra_args=cfg.solver.solve_args,
    )


__all__ = [
    "Solver",
    "DockerSolver",
    "ApiSolver",
    "build_solver",
    "build_hints",
    "scale_hint_from_optics",
    "write_fits",
    "result_from_wcs",
]
