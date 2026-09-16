"""Camera session — single owner of the one live camera connection.

libgphoto2 claims the USB device exclusively: a second ``gp.Camera().init()``
while a session is open fails with "Could not claim the USB device". That is
fine for a one-shot CLI run, but a web UI streams live view *while* the user
fires a capture, so both must share one connection.

CameraSession owns at most one open Camera and arbitrates access behind a
reentrant lock. Callers borrow it — they never own it:

    with session.acquire() as camera:
        camera.trigger()

The borrowed object is a non-closing proxy: CaptureService does
``with self._camera_factory() as camera:`` and ``__exit__`` calls ``close()``,
which must not tear down a connection the live-view stream is still using. The
proxy's ``close()`` is therefore a no-op; the lock is released when the
borrowing context exits.

Depends on the Camera port only — the concrete backend arrives as an injected
factory from the composition root.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from camera_orchestrator.domain.errors import CameraBusyError
from camera_orchestrator.domain.models.camera import CameraFile, CameraStatus, CaptureSettings
from camera_orchestrator.domain.ports.camera import Camera
from camera_orchestrator.log import get_logger

log = get_logger("camera_orchestrator.camera_session")

# A zero-arg factory returning an open Camera (the composition root supplies the
# concrete adapter; tests supply a fake).
CameraFactory = Callable[[], Camera]


class CameraSession:
    """Owns a single live Camera and serialises access to it.

    Opens lazily on first acquire and keeps the connection open between
    acquires, so a live-view stream and a capture workflow interleave on one
    USB claim instead of fighting over it.
    """

    def __init__(self, camera_factory: CameraFactory):
        """Args:
            camera_factory: Zero-arg callable returning an open Camera. Called
                on first acquire and again on every reconnect().
        """
        self._factory = camera_factory
        self._camera: Camera | None = None
        # How many borrows are currently held. The lock alone cannot answer
        # "is the camera actually in use right now?", and job state is the wrong
        # proxy for it: an align job spends most of its life plate-solving, with
        # the camera idle and perfectly able to stream live view.
        self._depth = 0
        # Reentrant: a service may acquire while already holding the session on
        # the same thread (nested composition) without deadlocking.
        self._lock = threading.RLock()

    # -- state -------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        """True if an underlying camera connection is currently held."""
        return self._camera is not None

    @property
    def in_use(self) -> bool:
        """True while some borrower holds the camera for an operation.

        This is the honest answer to "can live view have a frame right now?" —
        unlike job state, which stays 'running' through a plate solve that never
        touches the hardware.
        """
        return self._depth > 0

    # -- access ------------------------------------------------------------

    @contextmanager
    def acquire(self, timeout: float | None = None) -> Iterator[Camera]:
        """Borrow the shared camera for the duration of the block.

        Blocks until any other holder releases. Yields a non-closing proxy: the
        connection outlives the block, only the lock is released.

        Args:
            timeout: Seconds to wait for the connection. None waits forever
                (what a capture wants). A live-view stream should pass a short
                timeout — CaptureService holds the borrow for a whole sequence,
                so an untimed preview would block for minutes.

        Raises:
            CameraBusyError: The timeout elapsed with another holder in place.
        """
        if not self._lock.acquire(timeout=-1 if timeout is None else timeout):
            raise CameraBusyError(
                f"camera busy — not released within {timeout}s (capture in progress?)"
            )
        self._depth += 1
        try:
            self._open()
            yield _CameraProxy(self)
        finally:
            self._depth -= 1
            self._lock.release()

    def borrow(self) -> Camera:
        """Return a proxy shaped like a camera *factory* result.

        Lets CaptureService keep its ``with self._camera_factory() as camera:``
        shape — the proxy takes the session lock on ``__enter__`` and releases
        it on ``__exit__`` without closing the connection.
        """
        return _CameraProxy(self)

    def preview(self, timeout: float | None = None) -> bytes:
        """Grab one live-view frame, holding the lock only for that frame.

        The per-frame shape an MJPEG route wants: a concurrent capture waits for
        the in-flight frame (~100ms), not for the whole stream.

        Args:
            timeout: Seconds to wait for the connection. Pass a short value in a
                stream so a running capture surfaces as CameraBusyError (skip the
                frame, keep the stream alive) instead of stalling the response.

        Raises:
            CameraBusyError: Another holder kept the connection past `timeout`.
            CameraError: Live view is off on the body, or the grab failed.
        """
        with self.acquire(timeout=timeout) as camera:
            return camera.capture_preview()

    # -- lifecycle ---------------------------------------------------------

    def reconnect(self) -> None:
        """Tear down the connection and build a fresh one.

        Not a nicety: libgphoto2 caches the in-session directory listing, so
        card writes become visible only in a new session (see
        CaptureService._await_new_card_files). A reconnect is the only way to
        re-read the card.
        """
        with self._lock:
            self._shutdown()
            self._open()

    def close(self) -> None:
        """Close the underlying camera and release the session's claim."""
        with self._lock:
            self._shutdown()

    def __enter__(self) -> "CameraSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- internals ---------------------------------------------------------

    def _open(self) -> Camera:
        """Return the live camera, opening it on first use. Lock must be held."""
        if self._camera is None:
            log.info("Opening camera session")
            self._camera = self._factory()
        return self._camera

    def _shutdown(self) -> None:
        """Close the live camera if any and forget it. Lock must be held."""
        camera, self._camera = self._camera, None
        if camera is not None:
            log.info("Closing camera session")
            camera.close()


class _CameraProxy(Camera):
    """Non-closing view onto a CameraSession's camera.

    Implements the full Camera port by delegating each call to whatever camera
    the session currently holds (so a reconnect swaps underneath transparently),
    taking the session lock per call. ``close()`` is deliberately a no-op —
    borrowers must not end the shared connection.
    """

    def __init__(self, session: CameraSession):
        self._session = session

    # -- lifecycle: borrow, never own --------------------------------------

    def close(self) -> None:
        """No-op — the session owns the connection, not the borrower."""

    def __enter__(self) -> "Camera":
        self._session._lock.acquire()
        self._session._depth += 1
        try:
            self._session._open()
        except BaseException:
            self._session._depth -= 1
            self._session._lock.release()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self._session._depth -= 1
        self._session._lock.release()

    # -- delegation --------------------------------------------------------

    def _camera(self) -> Camera:
        """The session's current camera. Lock must be held by the caller."""
        return self._session._open()

    @property
    def can_capture(self) -> bool:
        with self._session._lock:
            return self._camera().can_capture

    def status(self) -> CameraStatus:
        with self._session._lock:
            return self._camera().status()

    def apply(self, settings: CaptureSettings) -> None:
        with self._session._lock:
            self._camera().apply(settings)

    def set_capture_target(self, to_card: bool) -> None:
        with self._session._lock:
            self._camera().set_capture_target(to_card)

    def trigger(self) -> None:
        with self._session._lock:
            self._camera().trigger()

    def bulb(self, seconds: float) -> None:
        with self._session._lock:
            self._camera().bulb(seconds)

    def capture_preview(self) -> bytes:
        with self._session._lock:
            return self._camera().capture_preview()

    def flush_events(self, timeout_ms: int | None = None) -> None:
        with self._session._lock:
            self._camera().flush_events(timeout_ms)

    def wait_for_new_files(self, timeout_ms: int | None = None) -> list[CameraFile]:
        with self._session._lock:
            return self._camera().wait_for_new_files(timeout_ms)

    def list_files(self) -> list[CameraFile]:
        with self._session._lock:
            return self._camera().list_files()

    def download(self, ref: CameraFile, out_dir: Path) -> Path:
        with self._session._lock:
            return self._camera().download(ref, out_dir)
