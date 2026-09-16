"""Tests for CameraSession — a counting factory over MockCamera, no hardware.

The session is the single owner of the one USB claim, so what matters here is
lifecycle (how often the factory is called, when close() really closes) and
mutual exclusion between threads. Cameras are MockCamera subclasses that record
closes and observed concurrency; the factory is injected, never patched.
"""
from __future__ import annotations

import threading
import time

import pytest

from camera_orchestrator.application.camera_session import CameraSession
from camera_orchestrator.application.capture_service import CaptureService
from camera_orchestrator.domain.errors import CameraError
from camera_orchestrator.domain.models.camera import CameraFile, CaptureRequest

from tests.test_service import MockCamera  # reuse the atomic-ABC mock


class TrackingCamera(MockCamera):
    """MockCamera that records how many times it was closed."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.closes = 0

    def close(self) -> None:
        self.closes += 1


class _Factory:
    """Counting camera factory — one fresh TrackingCamera per call."""

    def __init__(self, produces=None):
        self.calls = 0
        self.built: list[TrackingCamera] = []
        self._produces = produces

    def __call__(self) -> TrackingCamera:
        self.calls += 1
        cam = TrackingCamera(produces=self._produces)
        self.built.append(cam)
        return cam


def _session(produces=None) -> tuple[CameraSession, _Factory]:
    factory = _Factory(produces=produces)
    return CameraSession(camera_factory=factory), factory


# -- lazy open ------------------------------------------------------------


def test_camera_not_opened_until_first_acquire():
    session, factory = _session()
    assert factory.calls == 0                  # constructing the session claims nothing
    assert session.is_open is False
    with session.acquire():
        assert factory.calls == 1              # opened on first use only
    assert session.is_open is True             # and stays open after the block


def test_session_reused_across_acquires():
    session, factory = _session()
    with session.acquire() as first:
        first.status()
    with session.acquire() as second:
        second.status()
    assert factory.calls == 1                  # one USB claim shared by both borrows
    assert factory.built[0].closes == 0        # nothing tore it down in between


def test_open_failure_propagates_and_leaves_session_closed():
    def boom():
        raise CameraError("could not open camera: [-53] Could not claim the USB device")

    session = CameraSession(camera_factory=boom)
    with pytest.raises(CameraError, match="Could not claim"):
        with session.acquire():
            pass
    assert session.is_open is False            # a failed open must not be remembered


# -- the non-closing proxy ------------------------------------------------


def test_proxy_close_does_not_close_the_real_camera():
    session, factory = _session()
    with session.acquire() as camera:
        camera.close()                         # what CaptureService's __exit__ does
    assert factory.built[0].closes == 0        # live view would have died otherwise
    assert session.is_open is True


def test_borrowed_proxy_works_as_a_context_manager():
    session, factory = _session()
    with session.borrow() as camera:           # the `with self._camera_factory()` shape
        camera.trigger()
    assert factory.built[0].triggers == 1      # delegated to the real camera
    assert factory.built[0].closes == 0        # borrow released, connection kept
    assert factory.calls == 1


def test_capture_service_backed_by_the_session_keeps_it_open():
    session, factory = _session()
    service = CaptureService(camera_factory=session.borrow)
    result = service.capture_to_card(CaptureRequest(out_dir="/tmp/out", count=2))
    assert result.frames_captured == 2
    assert factory.calls == 1                  # the whole workflow rode one connection
    assert factory.built[0].closes == 0        # service exit must not close the session


def test_proxy_delegates_every_port_call():
    session, factory = _session(produces=[CameraFile("/store", "IMG.CR2")])
    with session.acquire() as camera:
        assert camera.can_capture is True
        assert camera.status().model == "MockCam"
        camera.set_capture_target(True)
        camera.bulb(2.0)
        assert [f.name for f in camera.wait_for_new_files()] == ["IMG.CR2"]
        assert [f.name for f in camera.list_files()] == ["IMG.CR2"]
        camera.flush_events()
    cam = factory.built[0]
    assert cam.capture_target is True           # calls landed on the real camera
    assert cam.bulbs == [2.0]
    assert cam.flushes == 1


# -- live view ------------------------------------------------------------


def test_capture_preview_delegates_raw_jpeg_bytes():
    session, factory = _session()
    with session.acquire() as camera:
        frame = camera.capture_preview()
    assert frame == b"\xff\xd8fake-jpeg"        # bytes forwarded unchanged (MJPEG-ready)
    assert "preview" in factory.built[0].calls


def test_preview_helper_grabs_one_frame_per_call():
    session, factory = _session()
    assert session.preview() == b"\xff\xd8fake-jpeg"
    assert session.preview() == b"\xff\xd8fake-jpeg"
    assert factory.calls == 1                   # per-frame borrow, one connection
    assert factory.built[0].calls.count("preview") == 2


# -- reconnect / close ----------------------------------------------------


def test_reconnect_builds_a_fresh_camera():
    session, factory = _session()
    with session.acquire():
        pass
    session.reconnect()
    assert factory.calls == 2                   # card writes only show in a new session
    assert factory.built[0].closes == 1         # the stale session was really torn down
    assert session.is_open is True


def test_reconnected_camera_is_the_one_borrowers_see():
    session, factory = _session()
    with session.acquire():
        pass
    session.reconnect()
    with session.acquire() as camera:
        camera.trigger()
    assert factory.built[1].triggers == 1       # proxy follows the swap transparently
    assert factory.built[0].triggers == 0


def test_close_releases_the_claim_and_reopens_on_next_use():
    session, factory = _session()
    with session.acquire():
        pass
    session.close()
    assert factory.built[0].closes == 1
    assert session.is_open is False              # the USB device is free again
    with session.acquire():
        pass
    assert factory.calls == 2                    # next borrow opens a new connection


def test_close_is_idempotent():
    session, factory = _session()
    with session.acquire():
        pass
    session.close()
    session.close()
    assert factory.built[0].closes == 1           # no double exit() on the driver


def test_session_usable_as_a_context_manager():
    session, factory = _session()
    with session:
        with session.acquire():
            pass
    assert factory.built[0].closes == 1            # leaving the session shuts it down


# -- concurrency ----------------------------------------------------------


class ConcurrencyCamera(TrackingCamera):
    """Records the peak number of threads inside a call at once."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.inside = 0
        self.peak = 0
        self._guard = threading.Lock()

    def trigger(self) -> None:
        with self._guard:
            self.inside += 1
            self.peak = max(self.peak, self.inside)
        time.sleep(0.02)
        super().trigger()
        with self._guard:
            self.inside -= 1


def test_concurrent_acquires_serialise():
    cam = ConcurrencyCamera()
    session = CameraSession(camera_factory=lambda: cam)

    def work() -> None:
        for _ in range(5):
            with session.acquire() as camera:
                camera.trigger()

    threads = [threading.Thread(target=work) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert cam.peak == 1                          # never two claims on the wire at once
    assert cam.triggers == 10                     # and nothing was dropped


def test_nested_acquire_on_one_thread_does_not_deadlock():
    session, factory = _session()
    with session.acquire() as outer:
        with session.acquire() as inner:          # RLock: reentrant for the holder
            inner.trigger()
        outer.trigger()                           # outer still usable after the inner
    assert factory.built[0].triggers == 2
    assert factory.calls == 1


def test_lock_is_released_when_a_borrower_raises():
    session, factory = _session()
    with pytest.raises(CameraError, match="boom"):
        with session.acquire():
            raise CameraError("boom")
    done = threading.Event()
    thread = threading.Thread(target=lambda: (session.preview(), done.set()))
    thread.start()
    thread.join(timeout=2)
    assert done.is_set()                          # a failed borrow must not wedge the lock
    assert factory.calls == 1
