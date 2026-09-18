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
from camera_orchestrator.domain.errors import CameraBusyError, CameraError
from camera_orchestrator.domain.models.camera import CameraFile, CaptureRequest

from tests.test_service import MockCamera  # reuse the atomic-ABC mock

# How long a helper thread is given before a test calls it wedged.
STATE_TIMEOUT_S = 10.0


class TrackingCamera(MockCamera):
    """MockCamera that records how many times it was closed."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.closes = 0

    def close(self) -> None:
        self.closes += 1


class _Factory:
    """Counting camera factory — one fresh TrackingCamera per call.

    `delay` models how long a real USB claim takes, which is what gives
    concurrent callers a window to pile up in.
    """

    def __init__(self, produces=None, delay: float = 0.0):
        self.calls = 0
        self.built: list[TrackingCamera] = []
        self._produces = produces
        self._delay = delay

    def __call__(self) -> TrackingCamera:
        if self._delay:
            time.sleep(self._delay)
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


def _fire(timer: threading.Timer) -> None:
    """Run a Timer's callback by hand, as the timer thread would have done.

    Lets a test play the part of a leaked timer: one that was started, lost its
    reference, and therefore could not be cancelled.
    """
    timer.function(*timer.args)


def test_an_orphaned_idle_timer_cannot_close_a_reclaimed_camera():
    """The live-view-dies-after-a-restart bug.

    _arm_idle_timer used to be called from two lock-less paths, so a started
    timer could be overwritten and become unreferenced — _cancel_idle_close then
    had nothing to cancel, and the orphan later tore down a camera that a new
    borrow had already reclaimed. The generation counter makes a superseded timer
    a no-op instead.
    """
    session, factory = _session()
    with session.acquire():
        pass
    session.release_when_idle(60.0)
    orphan = session._idle_timer                   # pretend this reference was lost
    assert orphan is not None

    session._cancel_idle_close()                   # a borrow reclaims the camera
    _fire(orphan)                                  # the orphan fires anyway

    assert factory.built[0].closes == 0            # live view would have died here
    assert session.is_open is True


def test_hitting_the_retry_cap_re_arms_at_a_long_backoff():
    # Giving up at the cap left the body awake all night whenever a borrow
    # outlasted five retries — which any real sequence does.
    session, _ = _session()
    with session.acquire():
        pass
    session.release_when_idle(60.0)
    timer = session._idle_timer
    assert timer is not None
    session._retries = 5                           # five fast retries already spent

    _fire(timer)

    assert session._idle_timer is not None         # still scheduled, not abandoned
    assert session._idle_timer.interval >= 60.0    # but lazily, not every 2s
    assert session._retries == 0
    session._cancel_idle_close()


def test_an_idle_close_still_fires_after_the_borrow_ends():
    # The whole point of the scheduler: the connection is released once nothing
    # holds it, however long that takes.
    session, factory = _session()
    with session.acquire():
        pass
    session.release_when_idle(0.01)
    # Poll the close itself, not is_open: _shutdown drops its reference to the
    # camera before calling close() on it, so is_open goes false a hair early.
    deadline = time.monotonic() + 5.0
    while factory.built[0].closes == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert factory.built[0].closes == 1
    assert session.is_open is False


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


def test_reconnect_gives_up_rather_than_parking_forever():
    """A bounded reconnect is what keeps confirm and cancel reachable.

    The route bridges this to a worker thread out of anyio's 40-slot pool with
    `abandon_on_cancel=False`. Untimed, a reconnect during a long phase parks
    that thread for the whole phase and cannot be reclaimed — a few clicks on a
    camera that *looks* wedged drained the pool, and the operator lost the two
    controls that would have released the lock.
    """
    session, _ = _session()
    holding = threading.Event()
    done = threading.Event()

    def borrower() -> None:
        with session.acquire():
            holding.set()
            done.wait(STATE_TIMEOUT_S)

    thread = threading.Thread(target=borrower, daemon=True)
    thread.start()
    try:
        assert holding.wait(2.0)
        started = time.monotonic()
        with pytest.raises(CameraBusyError, match="could not reconnect"):
            session.reconnect(timeout=0.1)
        assert time.monotonic() - started < 2.0     # bounded, not "until the phase ends"
    finally:
        done.set()
        thread.join(timeout=2)


def test_concurrent_reconnects_coalesce_into_one():
    # Two impatient clicks must not mean two teardowns — the second would tear
    # down the connection the first had just rebuilt.
    # A slow open is what gives the later callers something to coalesce onto —
    # a real USB claim takes far longer than this.
    factory = _Factory(delay=0.5)
    session = CameraSession(camera_factory=factory)
    with session.acquire():
        pass
    assert factory.calls == 1

    threads = [threading.Thread(target=session.reconnect, args=(5.0,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert factory.calls == 2                       # one rebuild, shared by all four
    assert session.is_open is True


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
