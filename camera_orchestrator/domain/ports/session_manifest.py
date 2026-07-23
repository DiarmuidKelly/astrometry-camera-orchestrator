"""Session-manifest port — persistence contract for a session's record.

Filesystem sidecar JSON today (SidecarSessionRepository); a SQLite adapter can
replace it later (cross-night calibration queries) with no change to the
application layer.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models.session import SessionManifest


class SessionManifestRepository(ABC):
    """Persists and retrieves a session's manifest (session.json)."""

    @abstractmethod
    def save(self, manifest: SessionManifest, session_dir: str) -> str:
        """Persist the manifest into session_dir and return the file path."""

    @abstractmethod
    def load(self, session_dir: str) -> SessionManifest | None:
        """Return the manifest stored in session_dir, or None if there is none."""

    @abstractmethod
    def exists(self, session_dir: str) -> bool:
        """True if a manifest already exists in session_dir."""
