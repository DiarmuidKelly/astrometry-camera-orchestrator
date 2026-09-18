"""Tests for shutdown and for the two controls that must never queue.

Two things are under test here, both about what happens when the process is
under strain:

- The app's lifespan. Nothing used to close the camera when the server stopped,
  so a Ctrl-C left the PTP session open with the body in live view — mirror up,
  sensor powered, the heaviest draw there is. The session is a real
  `CameraSession` over a counting fake; the composition root's singleton is
  swapped for the test and restored by monkeypatch.
- `confirm` and `cancel` running inline. They are exercised by calling the route
  coroutines directly, with anyio's thread limiter drained: FastAPI resolves sync
  dependencies in that same threadpool, so going through the HTTP client would
  stall on the dependency rather than on the thing being measured.

No hardware: the camera is a MockCamera subclass and every job runner here is a
plain function that parks until it is cancelled.
"""
from __future__ import annotations

import threading
import time

import anyio
import anyio.to_thread
import pytest

from camera_orchestrator import composition
from camera_orchestrator.application.camera_session import CameraSession
from camera_orchestrator.config import Config
from camera_orchestrator.interfaces.api import routes_camera
from camera_orchestrator.interfaces.api.app import create_app
from camera_orchestrator.interfaces.api.deps import get_camera_session
from camera_orchestrator.interfaces.api.jobs import JobContext, JobRegistry
from camera_orchestrator.interfaces.api.routes_jobs import cancel_job, confirm_job
from fastapi.testclient import TestClient

from tests.test_camera_session import TrackingCamera, _Factory

# Long enough for a worker thread to notice a flag on a loaded machine.
STATE_TIMEOUT_S = 10.0


@pytest.fixture
def anyio_backend():
    """asyncio is what uvicorn runs; test the loop that ships."""
    return "asyncio"


def _installed_session(monkeypatch) -> tuple[CameraSession, _Factory]:
    """Put a fake-backed CameraSession in the composition root, opened.

    monkeypatch restores the real singleton afterwards, so a test that closes it
    cannot leak into the next one.
    """
    factory = _Factory()
    session = CameraSession(camera_factory=factory)
    monkeypatch.setattr(composition, "_session", session)
    with session.acquire():
        pass                                   # claim it, as a first request would
    return session, factory


def _parking_runner(stopped: threading.Event):
    """A job that runs until it is cancelled, then records that it unwound."""

    def runner(ctx: JobContext) -> str:
        while not ctx.cancelled:
            time.sleep(0.01)
        stopped.set()
        ctx.check_cancelled()          # what a real progress callback does
        return "stopped"

    return runner


def _await_state(registry: JobRegistry, job_id: str, *states: str) -> None:
    deadline = time.monotonic() + STATE_TIMEOUT_S
    while time.monotonic() < deadline:
        if registry.get(job_id).state in states:
            return
        time.sleep(0.01)
    raise AssertionError(f"job stuck in {registry.get(job_id).state!r}, wanted {states}")


# -- shutdown --------------------------------------------------------------


def test_shutting_down_the_app_closes_the_camera_session(monkeypatch):
    # Without a lifespan the PTP session outlived the server: the body stayed in
    # live view with the mirror up until the battery went flat.
    session, factory = _installed_session(monkeypatch)
    assert session.is_open is True

    with TestClient(create_app(Config())):
        pass                                   # start, then stop, the app

    assert factory.built[0].closes == 1        # the USB claim was really released
    assert composition._session is None        # and the next request opens a fresh one


def test_shutting_down_cancels_active_jobs_and_waits_for_their_threads(monkeypatch):
    """Runners are daemon threads, which CPython does not unwind at exit.

    So "the process ended" is not enough to release the camera — the job has to
    be cancelled and its thread joined while the interpreter is still alive.
    """
    _installed_session(monkeypatch)
    app = create_app(Config())
    registry: JobRegistry = app.state.jobs
    stopped = threading.Event()

    with TestClient(app):
        job = registry.submit("capture", _parking_runner(stopped))
        _await_state(registry, job.id, "running")

    assert stopped.is_set()                                # the runner unwound...
    assert registry.get(job.id).state == "cancelled"       # ...because it was cancelled
    assert registry.get(job.id).ended_at is not None       # and it was joined, not abandoned


def test_shutdown_is_bounded_when_a_runner_will_not_stop(monkeypatch):
    # A libgphoto2 call cannot be interrupted. Shutdown must give up on it rather
    # than hold the process open: Ctrl-C has to still feel like Ctrl-C.
    _installed_session(monkeypatch)
    registry = JobRegistry()
    release = threading.Event()
    registry.submit("capture", lambda ctx: release.wait(STATE_TIMEOUT_S))
    _await_state(registry, registry.list()[0].id, "running")

    started = time.monotonic()
    registry.shutdown(0.2)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0                                   # bounded by the argument
    release.set()


# -- confirm / cancel must not need a worker thread ------------------------


# How long the drained pool stays drained. A bridged call cannot be abandoned —
# cancelling the await does not return until the thread gets its slot — so the
# slot has to come back on its own, or a regression would hang the suite instead
# of failing it. Comfortably longer than COMMAND_DEADLINE_S.
POOL_HELD_S = 5.0

# What "inline" means in wall-clock terms: a mutex acquire and an Event.set.
COMMAND_DEADLINE_S = 1.5


def _drain_thread_limiter() -> threading.Event:
    """Leave anyio's default threadpool with no free slots.

    The state a wedged camera produces: every slot parked in a driver call that
    cannot be interrupted. In production the pool is 40 deep; the size does not
    matter, only that there is nothing left. Returns the Event that frees it,
    already scheduled to fire at POOL_HELD_S.
    """
    anyio.to_thread.current_default_thread_limiter().total_tokens = 1
    occupied = threading.Event()
    threading.Timer(POOL_HELD_S, occupied.set).start()
    return occupied


@pytest.mark.anyio
async def test_cancel_lands_with_the_thread_pool_drained():
    """Cancel is a mutex acquire plus an Event.set — it must never queue.

    Bridged to a worker thread (as it was), the one control that frees a wedged
    camera queued behind the very threads that wedged it. The route coroutine is
    called directly: FastAPI resolves sync dependencies in this same pool, so an
    HTTP client would stall before reaching the code under test.
    """
    registry = JobRegistry()
    stopped = threading.Event()
    job = registry.submit("capture", _parking_runner(stopped))
    await anyio.to_thread.run_sync(_await_state, registry, job.id, "running")

    occupied = _drain_thread_limiter()
    async with anyio.create_task_group() as tg:
        tg.start_soon(anyio.to_thread.run_sync, occupied.wait)
        await anyio.sleep(0.05)                            # the only slot is now taken

        with anyio.fail_after(COMMAND_DEADLINE_S):
            cancelled = await cancel_job(job.id, registry)
        assert cancelled.state in ("cancelled", "running")  # the request landed at once
        occupied.set()

    assert stopped.wait(STATE_TIMEOUT_S)                    # and the runner saw it


@pytest.mark.anyio
async def test_confirm_lands_with_the_thread_pool_drained():
    # Same argument as cancel: answering the lens-cap prompt is the other control
    # a stalled night depends on.
    registry = JobRegistry(confirm_timeout=STATE_TIMEOUT_S)
    released = threading.Event()

    def runner(ctx: JobContext) -> str:
        ctx.prompt("dark", "Cover the lens for dark frames, then confirm.")
        released.set()
        return "done"

    job = registry.submit("sequence", runner)
    await anyio.to_thread.run_sync(_await_state, registry, job.id, "awaiting_confirmation")
    token = registry.get(job.id).prompt.token

    occupied = _drain_thread_limiter()
    async with anyio.create_task_group() as tg:
        tg.start_soon(anyio.to_thread.run_sync, occupied.wait)
        await anyio.sleep(0.05)

        with anyio.fail_after(COMMAND_DEADLINE_S):
            await confirm_job(job.id, _ConfirmBody(token), registry)
        occupied.set()

    assert released.wait(STATE_TIMEOUT_S)


class _ConfirmBody:
    """The confirm route reads one attribute; a Pydantic instance is not needed."""

    def __init__(self, token: str | None):
        self.token = token


# -- reconnect is bounded --------------------------------------------------


def test_a_reconnect_that_cannot_get_the_camera_is_a_503(monkeypatch):
    """Expiry must be retryable, not a hang.

    Untimed, this parked an uncancellable worker thread for as long as the phase
    lasted; a few clicks drained the pool. Bounded, the user gets a 503 and the
    pool keeps its slots.
    """
    monkeypatch.setattr(routes_camera, "RECONNECT_TIMEOUT_S", 0.1)
    session = CameraSession(camera_factory=lambda: TrackingCamera())
    app = create_app(Config())
    app.dependency_overrides[get_camera_session] = lambda: session

    holding = threading.Event()
    done = threading.Event()

    def borrower() -> None:
        with session.acquire():                 # a capture owning the camera
            holding.set()
            done.wait(STATE_TIMEOUT_S)

    thread = threading.Thread(target=borrower, daemon=True)
    thread.start()
    try:
        assert holding.wait(2.0)
        response = TestClient(app).post("/api/camera/reconnect")
        assert response.status_code == 503      # transient: try again in a moment
        assert "reconnect" in response.json()["detail"]
    finally:
        done.set()
        thread.join(timeout=2)
        session.close()


def test_reconnect_succeeds_when_the_camera_is_free(monkeypatch):
    session = CameraSession(camera_factory=_Factory())
    app = create_app(Config())
    app.dependency_overrides[get_camera_session] = lambda: session
    try:
        assert TestClient(app).post("/api/camera/reconnect").json() == {"ok": True}
        assert session.is_open is True
    finally:
        session.close()

