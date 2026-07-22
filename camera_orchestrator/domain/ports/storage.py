"""Storage port — persistence contract for solve records.

Filesystem sidecar JSON today (SidecarSolveRepository); a SQLite/Postgres
adapter can replace it later with no change to the application layer.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models.solve import SolveRecord


class SolveRecordRepository(ABC):
    """Persists and retrieves per-image solve records."""

    @abstractmethod
    def save(self, record: SolveRecord, dest_dir: str) -> str:
        """Persist a record and return a locator (e.g. the sidecar path)."""

    @abstractmethod
    def find_by_image(self, image_name: str, dest_dir: str) -> SolveRecord | None:
        """Return the stored record for an image, or None if not solved yet."""

    @abstractmethod
    def exists(self, image_name: str, dest_dir: str) -> bool:
        """True if a record already exists for the image (drives resume/skip)."""
