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

# How soon to re-check when a release lands while the camera is busy.
_RETRY_DELAY_S = 2.0

# Stop re-checking at this cadence after this many attempts — a borrow that long
# means the connection is genuinely wanted.
_MAX_RETRIES = 5

# Where the idle check goes after the retry cap. It must not simply stop: giving
# up left the camera open indefinitely (mirror up, sensor powered) whenever a
# borrow outlasted five retries, which a sequence does routinely. Back off to a
# lazy poll instead, so the body is still released once the run ends.
_BACKOFF_DELAY_S = 120.0


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
        # The idle-close scheduler's own mutex, deliberately NOT the camera lock.
        # Two of the three call sites that arm a timer are the paths taken when
        # the camera lock could not be acquired, so guarding the schedule with
        # that lock is impossible — which is how timers used to be started and
        # then lost, leaving an uncancellable close to tear down a camera an
        # active borrow had already reclaimed. Reentrant because _close_if_idle
        # re-arms from inside the guarded section.
        self._sched = threading.RLock()
        # Pending debounced close (see release_when_idle).
        self._idle_timer: threading.Timer | None = None
        self._retries = 0
        # Bumped on every (re)schedule and every cancel. A timer whose generation
        # is stale fires into a no-op, so a leaked one cannot close the camera.
        self._generation = 0
        # Single-flight reconnect: the Event a concurrent caller waits on rather
        # than tearing a freshly built session back down behind the first one.
        self._reconnect_lock = threading.Lock()
        self._reconnect_done: threading.Event | None = None

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

    def reconnect(self, timeout: float | None = None) -> None:
        """Tear down the connection and build a fresh one.

        Not a nicety: libgphoto2 caches the in-session directory listing, so
        card writes become visible only in a new session (see
        CaptureService._await_new_card_files). A reconnect is the only way to
        re-read the card.

        **Bounded, and single-flight.** A capture holds the lock for a whole
        phase, so an untimed reconnect parks its caller for minutes. From the API
        that caller is a worker thread out of anyio's 40-slot pool, and a user
        clicking "reconnect" at a camera that *looks* wedged is precisely the one
        who then needs confirm and cancel to work — so the wait is bounded and
        expiry surfaces as CameraBusyError (503, retryable). Concurrent callers
        coalesce onto the first attempt instead of queueing another teardown
        behind a connection that has just been rebuilt.

        Args:
            timeout: Seconds to wait for the camera lock (and for an in-flight
                reconnect). None waits forever — what the CLI wants.

        Raises:
            CameraBusyError: The timeout elapsed with the camera still held.
        """
        with self._reconnect_lock:
            inflight = self._reconnect_done
            if inflight is None:
                self._reconnect_done = done = threading.Event()

        if inflight is not None:
            if not inflight.wait(timeout):
                raise CameraBusyError(
                    f"camera busy — a reconnect already in flight did not finish "
                    f"within {timeout}s"
                )
            return  # somebody else just rebuilt the connection; that is the answer

        try:
            if not self._lock.acquire(timeout=-1 if timeout is None else timeout):
                raise CameraBusyError(
                    f"camera busy — could not reconnect within {timeout}s "
                    f"(capture in progress?)"
                )
            try:
                self._shutdown()
                self._open()
            finally:
                self._lock.release()
        finally:
            with self._reconnect_lock:
                self._reconnect_done = None
            done.set()


    def close(self) -> None:
        """Close the underlying camera and release the session's claim."""
        self._cancel_idle_close()
        with self._lock:
            self._shutdown()

    def release_when_idle(self, delay: float = 15.0) -> None:
        """Close the connection after `delay` seconds if nothing borrows it first.

        An open PTP session keeps the body awake, and live view holds the mirror
        up with the sensor powered — the heaviest draw there is. Once the stream
        stops there is usually nothing to hold the camera for, so let it sleep.

        `delay` of 0 means "as soon as possible" — a deliberate stop. A longer
        delay debounces the implicit case, so a page reload or a sequence between
        phases borrows again and cancels the close rather than paying for a
        reconnect. Any borrow cancels a pending close (see _open).

        **Never blocks.** This is called from a request handler, and a capture
        can hold the camera for hours; if it is busy now, the close is deferred
        to a timer instead of waiting on it.
        """
        if not self._lock.acquire(blocking=False):
            self._arm_idle_timer(max(delay, _RETRY_DELAY_S))
            return
        try:
            self._cancel_idle_close()
            if self._camera is None:
                return
            if delay <= 0 and self._depth == 0:
                self._shutdown()          # deliberate stop, nothing holding it
                return
            self._arm_idle_timer(delay if delay > 0 else _RETRY_DELAY_S)
        finally:
            self._lock.release()

    def _arm_idle_timer(self, delay: float) -> None:
        """(Re)schedule a close attempt. Guarded by the scheduler mutex only.

        Every armed timer carries the generation it was armed under. Cancelling a
        Timer is not reliable on its own — it loses to a callback that has already
        started — so the generation is what actually makes a superseded close a
        no-op.
        """
        with self._sched:
            if self._idle_timer is not None:
                self._idle_timer.cancel()
            self._generation += 1
            timer = threading.Timer(delay, self._close_if_idle, args=(self._generation,))
            timer.daemon = True
            self._idle_timer = timer
            timer.start()

    def _close_if_idle(self, generation: int) -> None:
        """Timer callback: close only if nobody is holding the camera.

        `generation` is the schedule this timer belongs to. Anything older has
        been superseded or cancelled — a borrow has since reclaimed the camera —
        so it must do nothing at all. A stale timer that went ahead and tore the
        connection down is what killed live view a few seconds after a Stop→Start,
        and with two tabs open.

        Retries at the fast cadence are capped, but hitting the cap re-arms at a
        long backoff rather than giving up: a borrow that outlives five retries
        (any real sequence) would otherwise leave the body awake all night.
        """
        with self._sched:
            if generation != self._generation:
                return                              # superseded; not ours to close
            self._idle_timer = None
            if self._retries >= _MAX_RETRIES:
                self._retries = 0
                self._arm_idle_timer(_BACKOFF_DELAY_S)
                return
            if not self._lock.acquire(blocking=False):
                self._retries += 1
                self._arm_idle_timer(_RETRY_DELAY_S)   # still busy; come back later
                return
            try:
                if self._depth > 0:
                    self._retries += 1
                    self._arm_idle_timer(_RETRY_DELAY_S)
                    return
                self._retries = 0
                if self._camera is not None:
                    log.info("Releasing idle camera", extra={"reason": "no borrowers"})
                    self._shutdown()
            finally:
                self._lock.release()

    def _cancel_idle_close(self) -> None:
        """Drop a pending idle close. Takes the scheduler mutex itself.

        Deliberately independent of the camera lock, so it is callable from the
        lock-less paths in release_when_idle as well as from under the lock in
        _open().
        """
        with self._sched:
            self._generation += 1       # void anything already armed or running
            self._retries = 0
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None

    def __enter__(self) -> "CameraSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- internals ---------------------------------------------------------

    def _open(self) -> Camera:
        """Return the live camera, opening it on first use. Lock must be held."""
        self._cancel_idle_close()   # somebody wants it; don't release underneath them
        if self._camera is None:
            camera = self._factory()      # may raise; log only once it succeeded,
            log.info("Opened camera session")   # so a failed open isn't reported as one
            self._camera = camera
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
