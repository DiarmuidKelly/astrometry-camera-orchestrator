"""Tests for CaptureService — a MockCamera implements the atomic ABC (no hardware)."""
from __future__ import annotations

from pathlib import Path

import pytest

from camera_orchestrator.application.capture_service import CaptureService
from camera_orchestrator.domain.errors import CameraError
from camera_orchestrator.domain.models import CameraStatus, CaptureRequest, CaptureSettings
from camera_orchestrator.domain.models.camera import CameraFile
from camera_orchestrator.domain.ports.camera import Camera


class MockCamera(Camera):
    """In-memory camera implementing the atomic interface, recording calls."""

    def __init__(self, produces=None, can_capture=True):
        self._can = can_capture
        # Files each trigger "produces" (returned by wait_for_new_files).
        self._produces = produces if produces is not None else [CameraFile("/store", "IMG_0001.CR3")]
        self.applied: CaptureSettings | None = None
        self.apply_calls = 0
        self.capture_target: bool | None = None
        self.triggers = 0
        self.bulbs: list[float] = []
        self.downloads: list[CameraFile] = []
        self.flushes = 0
        # Names of atomic ops in call order — lets tests assert flush precedes triggers.
        self.calls: list[str] = []

    @property
    def can_capture(self) -> bool:
        return self._can

    def status(self) -> CameraStatus:
        return CameraStatus(model="MockCam", can_capture=self._can)

    def apply(self, settings: CaptureSettings) -> None:
        self.applied = settings
        self.apply_calls += 1

    def set_capture_target(self, to_card: bool) -> None:
        self.capture_target = to_card

    def trigger(self) -> None:
        self.triggers += 1
        self.calls.append("trigger")

    def bulb(self, seconds: float) -> None:
        self.bulbs.append(seconds)
        self.calls.append("bulb")

    def flush_events(self, timeout_ms=None) -> None:
        self.flushes += 1
        self.calls.append("flush")

    def wait_for_new_files(self, timeout_ms=None) -> list[CameraFile]:
        return list(self._produces)

    def list_files(self) -> list[CameraFile]:
        return list(self._produces)

    def download(self, ref: CameraFile, out_dir: Path) -> Path:
        self.downloads.append(ref)
        return out_dir / ref.name


def _service(cam: MockCamera) -> CaptureService:
    return CaptureService(camera_factory=lambda: cam)


def _req(**kw) -> CaptureRequest:
    kw.setdefault("out_dir", "/tmp/out")
    return CaptureRequest(**kw)


def test_capture_and_download_frame_count():
    cam = MockCamera()
    result = _service(cam).capture_and_download(_req(count=3))
    assert cam.triggers == 3
    assert cam.capture_target is False          # download mode leaves target
    assert result.frames_captured == 3
    assert len(result.frames) == 3              # one file per frame
    assert result.download is True


def test_select_all_downloads_every_file():
    cam = MockCamera(produces=[CameraFile("/s", "x.CR2"), CameraFile("/s", "x.JPG")])
    result = _service(cam).capture_and_download(_req(count=1))   # default select="all"
    assert [r.name for r in cam.downloads] == ["x.CR2", "x.JPG"]
    assert len(result.frames) == 2


def test_select_jpeg_downloads_only_jpeg():
    cam = MockCamera(produces=[CameraFile("/s", "x.CR2"), CameraFile("/s", "x.JPG")])
    result = _service(cam).capture_and_download(_req(count=1, select="jpeg"))
    assert [r.name for r in cam.downloads] == ["x.JPG"]
    assert len(result.frames) == 1


def test_select_cr2_downloads_only_cr2():
    cam = MockCamera(produces=[CameraFile("/s", "x.CR2"), CameraFile("/s", "x.JPG")])
    _service(cam).capture_and_download(_req(count=1, select="cr2"))
    assert [r.name for r in cam.downloads] == ["x.CR2"]


def test_select_no_match_raises():
    cam = MockCamera(produces=[CameraFile("/s", "x.CR2")])   # no JPEG present
    with pytest.raises(CameraError, match="matched none"):
        _service(cam).capture_and_download(_req(count=1, select="jpeg"))


def test_download_run_flushes_events_once_before_capturing():
    cam = MockCamera()
    _service(cam).capture_and_download(_req(count=3))
    assert cam.flushes == 1                       # once for the run, not per frame
    assert cam.calls[0] == "flush"                # before any trigger
    assert cam.calls.count("trigger") == 3


def test_card_only_does_not_flush():
    cam = MockCamera()
    _service(cam).capture_to_card(_req(count=2))
    assert cam.flushes == 0                        # fast path untouched


def test_capture_to_card_no_download():
    cam = MockCamera()
    result = _service(cam).capture_to_card(_req(count=4))
    assert cam.triggers == 4
    assert cam.capture_target is True           # card-only
    assert cam.downloads == []
    assert result.frames == []
    assert result.download is False


def test_raw_jpeg_pair_downloads_both():
    cam = MockCamera(produces=[CameraFile("/s", "x.CR3"), CameraFile("/s", "x.JPG")])
    result = _service(cam).capture_and_download(_req(count=1))
    assert len(cam.downloads) == 2
    assert len(result.frames) == 2


def test_bulb_path_uses_bulb():
    cam = MockCamera()
    _service(cam).capture_to_card(_req(count=2, bulb_seconds=30.0))
    assert cam.bulbs == [30.0, 30.0]
    assert cam.triggers == 0


def test_on_frame_fires_per_frame():
    cam = MockCamera()
    seen: list[tuple[int, int]] = []
    _service(cam).capture_to_card(_req(count=3), on_frame=lambda i, n, paths: seen.append((i, n)))
    assert seen == [(1, 3), (2, 3), (3, 3)]


def test_capture_and_download_raises_when_no_files():
    cam = MockCamera(produces=[])
    with pytest.raises(CameraError, match="no file arrived"):
        _service(cam).capture_and_download(_req(count=1))


def test_settings_applied_once():
    cam = MockCamera()
    _service(cam).capture_to_card(_req(count=5))
    assert cam.apply_calls == 1


def test_locked_body_rejected():
    cam = MockCamera(can_capture=False)
    with pytest.raises(CameraError, match="does not support remote capture"):
        _service(cam).capture_to_card(_req(count=1))


def test_status_passthrough():
    cam = MockCamera()
    assert _service(cam).status().model == "MockCam"
