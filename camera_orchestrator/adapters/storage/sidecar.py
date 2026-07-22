"""Filesystem sidecar-JSON implementation of SolveRecordRepository.

Writes one <stem>_solved.json next to each image. Swappable for a SQLite or
other adapter later with no change to the application layer.
"""
from __future__ import annotations

from pathlib import Path

from camera_orchestrator.domain.models.solve import SolveRecord
from camera_orchestrator.domain.ports.storage import SolveRecordRepository


class SidecarSolveRepository(SolveRecordRepository):
    """Persists solve records as per-image sidecar JSON files."""

    def _path(self, image_name: str, dest_dir: str) -> Path:
        return Path(dest_dir) / f"{Path(image_name).stem}_solved.json"

    def save(self, record: SolveRecord, dest_dir: str) -> str:
        path = self._path(record.original_file, dest_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(record.model_dump_json(indent=2))
        return str(path)

    def find_by_image(self, image_name: str, dest_dir: str) -> SolveRecord | None:
        path = self._path(image_name, dest_dir)
        if not path.exists():
            return None
        return SolveRecord.model_validate_json(path.read_text())

    def exists(self, image_name: str, dest_dir: str) -> bool:
        return self._path(image_name, dest_dir).exists()
