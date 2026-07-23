"""Round-trip tests for SidecarSessionRepository (session.json on disk)."""
from __future__ import annotations

from datetime import datetime, timezone

from camera_orchestrator.adapters.storage.session_manifest import SidecarSessionRepository
from camera_orchestrator.domain.models.session import (
    PhaseRecord,
    SessionManifest,
    TargetInfo,
)


def _manifest() -> SessionManifest:
    now = datetime(2026, 7, 23, 22, 0, tzinfo=timezone.utc)
    return SessionManifest(
        session_id="20260723-orion",
        name="orion",
        target=TargetInfo(solved=True, center_ra_deg=83.8, center_dec_deg=-5.4,
                          preview="IMG_1_solved.png", frame="IMG_1.JPG"),
        started_at=now, ended_at=now,
        phases=[PhaseRecord(kind="light", count=2, iso="800", shutter="2",
                            started_at=now, ended_at=now,
                            files=["IMG_2.CR2", "IMG_3.CR2"])],
    )


def test_save_then_load_round_trips(tmp_path):
    repo = SidecarSessionRepository()
    session_dir = str(tmp_path / "20260723-orion")

    assert repo.exists(session_dir) is False
    path = repo.save(_manifest(), session_dir)
    assert path.endswith("session.json")
    assert repo.exists(session_dir) is True

    loaded = repo.load(session_dir)
    assert loaded.session_id == "20260723-orion"
    assert loaded.target.center_ra_deg == 83.8
    assert loaded.phases[0].files == ["IMG_2.CR2", "IMG_3.CR2"]


def test_load_missing_returns_none(tmp_path):
    assert SidecarSessionRepository().load(str(tmp_path / "nope")) is None
