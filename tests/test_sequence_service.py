"""Tests for SequenceService — fake capture + in-memory manifest repo, fixed clock."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from camera_orchestrator.application.sequence_service import BIAS_SHUTTER, SequenceService
from camera_orchestrator.domain.models.camera import CameraStatus, CaptureResult
from camera_orchestrator.domain.models.session import SequenceRequest, SessionManifest


class FakeCapture:
    """Records the CaptureRequest of each phase; returns a canned result."""

    def __init__(self):
        self.card_calls = []
        self.download_calls = []

    def capture_to_card(self, request, on_frame=None, record_files=False):
        self.card_calls.append(request)
        card = [f"{request.kind}_{i}.CR2" for i in range(request.count)] if record_files else []
        return CaptureResult(
            status=CameraStatus(model="fake", can_capture=True),
            frames_captured=request.count, card_frames=card, download=False,
        )

    def capture_and_download(self, request, on_frame=None):
        self.download_calls.append(request)
        frames = [f"/out/{request.kind}_{i}.CR2" for i in range(request.count)]
        return CaptureResult(
            status=CameraStatus(model="fake", can_capture=True),
            frames_captured=request.count, frames=frames, download=True,
        )


class FakeRepo:
    """In-memory SessionManifestRepository."""

    def __init__(self):
        self.store: dict[str, SessionManifest] = {}

    def save(self, manifest, session_dir):
        self.store[session_dir] = manifest
        return f"{session_dir}/session.json"

    def load(self, session_dir):
        return self.store.get(session_dir)

    def exists(self, session_dir):
        return session_dir in self.store


def _clock():
    """Monotonic UTC clock — each call advances one second."""
    base = datetime(2026, 7, 23, 22, 0, 0, tzinfo=timezone.utc)
    n = {"i": 0}

    def now():
        n["i"] += 1
        return base + timedelta(seconds=n["i"])
    return now


def _svc(fake, repo=None):
    return SequenceService(fake, repo or FakeRepo(), clock=_clock())


def _req(**kw) -> SequenceRequest:
    kw.setdefault("out_dir", "/tmp/out")
    return SequenceRequest(**kw)


def test_runs_each_requested_phase_with_counts():
    fake = FakeCapture()
    manifest = _svc(fake).run(_req(iso="800", shutter="2", lights=3, darks=2, bias=1))
    assert [r.kind for r in fake.card_calls] == ["light", "dark", "bias"]   # default order
    assert [p.kind for p in manifest.phases] == ["light", "dark", "bias"]
    assert [p.count for p in manifest.phases] == [3, 2, 1]


def test_skips_zero_count_phases():
    fake = FakeCapture()
    manifest = _svc(fake).run(_req(lights=5))              # no darks/bias
    assert [p.kind for p in manifest.phases] == ["light"]


def test_bias_uses_fastest_shutter_darks_inherit_exposure():
    fake = FakeCapture()
    _svc(fake).run(_req(shutter="2", lights=1, darks=1, bias=1))
    by_kind = {r.kind: r for r in fake.card_calls}
    assert by_kind["light"].shutter == "2"
    assert by_kind["dark"].shutter == "2"                 # darks inherit the light exposure
    assert by_kind["bias"].shutter == BIAS_SHUTTER        # bias forced to fastest
    assert by_kind["bias"].bulb_seconds is None


def test_phases_shoot_raw():
    fake = FakeCapture()
    _svc(fake).run(_req(lights=1, bias=1))
    assert all(r.image_format == "raw" for r in fake.card_calls)


def test_custom_order_calibration_first():
    fake = FakeCapture()
    manifest = _svc(fake).run(_req(lights=1, darks=1, bias=1, order=["bias", "dark", "light"]))
    assert [p.kind for p in manifest.phases] == ["bias", "dark", "light"]


def test_before_phase_called_per_phase():
    fake = FakeCapture()
    seen = []
    _svc(fake).run(_req(lights=1, darks=1, bias=1), before_phase=lambda kind: seen.append(kind))
    assert seen == ["light", "dark", "bias"]


def test_card_only_records_camera_side_filenames():
    fake = FakeCapture()
    manifest = _svc(fake).run(_req(lights=2))
    light = manifest.phases[0]
    assert light.files == ["light_0.CR2", "light_1.CR2"]   # from the list-files diff
    assert manifest.download is False


def test_download_mode_records_local_basenames():
    fake = FakeCapture()
    manifest = _svc(fake).run(_req(lights=2, download=True))
    assert fake.download_calls and not fake.card_calls
    assert manifest.phases[0].files == ["light_0.CR2", "light_1.CR2"]
    assert manifest.download is True


def test_session_mode_saves_manifest_with_timestamps():
    fake, repo = FakeCapture(), FakeRepo()
    _svc(fake, repo).run(_req(lights=1), session_dir="/out/20260723-orion")
    assert repo.exists("/out/20260723-orion")
    saved = repo.load("/out/20260723-orion")
    assert saved.session_id == "20260723-orion"
    assert saved.name == "orion"                           # parsed from folder
    assert saved.started_at is not None and saved.ended_at is not None
    assert saved.phases[0].started_at <= saved.phases[0].ended_at


def test_sequence_appends_to_existing_session():
    fake, repo = FakeCapture(), FakeRepo()
    svc = _svc(fake, repo)
    svc.run(_req(lights=10), session_dir="/out/20260723-orion")      # lights now
    svc.run(_req(darks=5, bias=5), session_dir="/out/20260723-orion")  # calibration later
    saved = repo.load("/out/20260723-orion")
    assert [p.kind for p in saved.phases] == ["light", "dark", "bias"]  # appended
