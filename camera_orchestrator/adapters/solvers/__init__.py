"""Solver adapters — DockerSolver, ApiSolver, and FITS/WCS I/O helpers."""
from .api import ApiSolver
from .docker import DockerSolver

__all__ = ["ApiSolver", "DockerSolver"]
