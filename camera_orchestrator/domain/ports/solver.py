"""Solver port — the abstract interface every plate-solver backend implements."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from ..models.solve import SolveHints, SolveResult

if TYPE_CHECKING:  # numpy is a data-representation dep of adapters, not the domain
    import numpy as np


class Solver(ABC):
    """Abstract base for plate-solver backends (Docker, API, etc.)."""

    @abstractmethod
    def solve(
        self,
        frame_bgr: "np.ndarray",
        hints: SolveHints,
        annotate_out: str | None = None,
        source_path: str | None = None,
    ) -> SolveResult | None:
        """Plate-solve a frame.

        Args:
            frame_bgr: BGR image array.
            hints: Scale and sky-position hints to narrow the search.
            annotate_out: If set, write an annotated overlay image to this path.
            source_path: Path to the original file on disk (avoids re-encoding).

        Returns:
            SolveResult on success, None if no solution was found.
        """
