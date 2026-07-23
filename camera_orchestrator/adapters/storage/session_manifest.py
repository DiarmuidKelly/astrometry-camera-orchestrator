"""Filesystem sidecar-JSON implementation of SessionManifestRepository.

Writes one session.json into the session folder. Swappable for a SQLite or other
adapter later with no change to the application layer.
"""
from __future__ import annotations

from pathlib import Path

from camera_orchestrator.domain.models.session import SessionManifest
from camera_orchestrator.domain.ports.session_manifest import SessionManifestRepository

MANIFEST_NAME = "session.json"


class SidecarSessionRepository(SessionManifestRepository):
    """Persists a session manifest as session.json in the session folder."""

    def _path(self, session_dir: str) -> Path:
        return Path(session_dir) / MANIFEST_NAME

    def save(self, manifest: SessionManifest, session_dir: str) -> str:
        path = self._path(session_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(manifest.model_dump_json(indent=2))
        return str(path)

    def load(self, session_dir: str) -> SessionManifest | None:
        path = self._path(session_dir)
        if not path.exists():
            return None
        return SessionManifest.model_validate_json(path.read_text())

    def exists(self, session_dir: str) -> bool:
        return self._path(session_dir).exists()
