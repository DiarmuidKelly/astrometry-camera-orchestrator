"""ApiSolver — astrometry-api-server backend (not yet implemented).

Wire this up when the persistent API server container is ready. The setup
target in the Makefile will handle spinning up the container; this driver
just sends HTTP solve requests to it.
"""
from __future__ import annotations

import numpy as np

from camera_orchestrator.domain.models.solve import SolveHints, SolveResult
from camera_orchestrator.domain.ports.solver import Solver


class ApiSolver(Solver):
    """Plate solver that talks to a persistent astrometry API server (stub)."""

    def __init__(self, base_url: str):
        """Args:
            base_url: Base URL of the astrometry API server, e.g. http://localhost:8080.
        """
        self.base_url = base_url

    def solve(
        self,
        frame_bgr: np.ndarray,
        hints: SolveHints,
        annotate_out: str | None = None,
        source_path: str | None = None,
    ) -> SolveResult | None:
        """Not implemented — raises NotImplementedError until the API server driver is wired up."""
        raise NotImplementedError(
            "ApiSolver is not yet implemented. Use DockerSolver for now."
        )
