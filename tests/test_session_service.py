"""Tests for SessionService — a fake capture service records per-phase requests."""
from __future__ import annotations

from camera_orchestrator.application.session_service import BIAS_SHUTTER, SessionService
from camera_orchestrator.domain.models.camera import CaptureResult
from camera_orchestrator.domain.models.session import SessionRequest
from camera_orchestrator.domain.models.solve import SolveResult  # noqa: F401  (kept parallel to other tests)
from camera_orchestrator.domain.models.camera import CameraStatus


class FakeCapture:
    """Records the CaptureRequest of each phase; returns a canned result."""

    def __init__(self):
        self.card_calls = []
        self.download_calls = []

    def _result(self, request):
        frames = [f"{request.kind}_{i}.jpg" for i in range(request.count)] if request.download else []
        return CaptureResult(
            status=CameraStatus(model="fake", can_capture=True),
            frames_captured=request.count,
            frames=frames,
            download=request.download,
        )

    def capture_to_card(self, request, on_frame=None):
        self.card_calls.append(request)
        return self._result(request)

    def capture_and_download(self, request, on_frame=None):
        self.download_calls.append(request)
        return self._result(request)


def _req(**kw) -> SessionRequest:
    kw.setdefault("out_dir", "/tmp/out")
    return SessionRequest(**kw)


def test_runs_each_requested_phase_with_counts():
    fake = FakeCapture()
    result = SessionService(fake).run(_req(iso="800", shutter="2", lights=3, darks=2, bias=1))
    kinds = [r.kind for r in fake.card_calls]
    assert kinds == ["light", "dark", "bias"]              # default order
    assert [r.count for r in fake.card_calls] == [3, 2, 1]
    assert result.counts == {"light": 3, "dark": 2, "bias": 1}


def test_skips_zero_count_phases():
    fake = FakeCapture()
    SessionService(fake).run(_req(lights=5))               # no darks/bias
    assert [r.kind for r in fake.card_calls] == ["light"]


def test_bias_uses_fastest_shutter_darks_inherit_exposure():
    fake = FakeCapture()
    SessionService(fake).run(_req(shutter="2", lights=1, darks=1, bias=1))
    by_kind = {r.kind: r for r in fake.card_calls}
    assert by_kind["light"].shutter == "2"
    assert by_kind["dark"].shutter == "2"                  # darks inherit the light exposure
    assert by_kind["bias"].shutter == BIAS_SHUTTER         # bias forced to fastest
    assert by_kind["bias"].bulb_seconds is None


def test_custom_order_calibration_first():
    fake = FakeCapture()
    SessionService(fake).run(_req(lights=1, darks=1, bias=1, order=["bias", "dark", "light"]))
    assert [r.kind for r in fake.card_calls] == ["bias", "dark", "light"]


def test_before_phase_called_per_phase():
    fake = FakeCapture()
    seen = []
    SessionService(fake).run(_req(lights=1, darks=1, bias=1),
                             before_phase=lambda kind: seen.append(kind))
    assert seen == ["light", "dark", "bias"]


def test_download_mode_returns_frames_and_uses_download_path():
    fake = FakeCapture()
    result = SessionService(fake).run(_req(lights=2, download=True))
    assert fake.download_calls and not fake.card_calls
    assert len(result.frames) == 2
    assert result.download is True
