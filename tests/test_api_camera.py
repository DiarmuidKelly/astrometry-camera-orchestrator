"""Tests for the system + camera HTTP routes — no hardware, no uvicorn.

The app is built by `create_app()` and driven with FastAPI's TestClient. The
camera dependency is overridden with a *real* CameraSession wrapped around
MockCamera: the session is the piece whose locking behaviour matters, and faking
it away would fake away the thing under test. Cameras that raise on demand cover
the two live-view failure modes (busy vs live view off).
"""
from __future__ import annotations

import threading
import time

from camera_orchestrator.application.camera_session import CameraSession
from camera_orchestrator.config import Config
from camera_orchestrator.domain.errors import CameraBusyError, CameraError
from camera_orchestrator.interfaces.api.app import create_app
from camera_orchestrator.interfaces.api.deps import get_camera_session
from camera_orchestrator.interfaces.api.routes_system import package_version
from fastapi.testclient import TestClient

from tests.test_service import MockCamera  # reuse the atomic-ABC mock


class ScriptedPreviewCamera(MockCamera):
    """MockCamera whose capture_preview replays a script of frames and errors.

    Lets one test walk a stream through "frame, busy, frame, live view off" —
    the exact sequence a capture fired mid-stream produces.
    """

    def __init__(self, script: list):
        super().__init__()
        self._script = list(script)

    def capture_preview(self) -> bytes:
        step = self._script.pop(0) if self._script else CameraError("live view is off")
        if isinstance(step, Exception):
            raise step
        return step


class DeadCamera(MockCamera):
    """A body that is plugged in but refuses everything — status included."""

    def status(self):
        raise CameraError("could not open camera: [-105] Unknown model")


def _client(camera: MockCamera | None = None, cfg: Config | None = None) -> TestClient:
    session = CameraSession(camera_factory=lambda: camera or MockCamera())
    app = create_app(cfg or Config())
    app.dependency_overrides[get_camera_session] = lambda: session
    return TestClient(app)


def test_health_reports_ok_and_the_version_file():
    body = _client().get("/api/health").json()
    assert body["ok"] is True
    assert body["version"] == package_version()   # the VERSION file, not a literal


def test_config_is_served_as_json(tmp_path):
    cfg = Config.model_validate({"grab": {"out_dir": str(tmp_path)}})
    body = _client(cfg=cfg).get("/api/config").json()
    assert body["grab"]["out_dir"] == str(tmp_path)
    assert body["solver"]["cpulimit"] == 60          # defaults come through too


def test_config_schema_carries_the_field_descriptions():
    schema = _client().get("/api/config/schema").json()
    # The settings form renders labels/help from this, so the descriptions have
    # to survive the round trip.
    search = schema["$defs"]["SearchConfig"]["properties"]
    assert "decimal degrees" in search["ra_deg"]["description"]


def test_put_config_persists_and_takes_effect_immediately(tmp_path):
    path = tmp_path / "config.yaml"
    app = create_app(Config(), config_path=str(path))
    client = TestClient(app)

    body = Config().model_dump()
    body["search"]["ra_deg"] = 10.68            # M31 — the value retyped every night
    body["search"]["dec_deg"] = 41.27
    response = client.put("/api/config", json=body)

    assert response.status_code == 200
    assert response.json()["path"] == str(path)
    assert response.json()["config"]["search"]["ra_deg"] == 10.68
    assert path.is_file()                        # written to the file the CLI reads
    # Adopted in-process: the next GET (and the next job) sees the new hint
    # without a restart.
    assert client.get("/api/config").json()["search"]["ra_deg"] == 10.68


def test_put_config_rejects_a_bad_value_with_422(tmp_path):
    path = tmp_path / "config.yaml"
    client = TestClient(create_app(Config(), config_path=str(path)))
    body = Config().model_dump()
    body["optics"]["sensor_width_mm"] = "wide"   # a wrong scale hint fails solves slowly

    response = client.put("/api/config", json=body)
    assert response.status_code == 422
    assert not path.exists()                     # nothing written on a rejected body


def test_camera_status_reports_a_connected_camera():
    response = _client().get("/api/camera/status")
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    assert body["camera"]["model"] == "MockCam"
    assert body["session_open"] is True              # the read opened the session


def test_camera_status_does_not_raise_when_the_camera_is_unreachable():
    # No camera plugged in is the normal state when the UI first loads — a 5xx
    # here would make the whole page look broken.
    response = _client(DeadCamera()).get("/api/camera/status")
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is False
    assert body["camera"] is None
    assert "Unknown model" in body["error"]


def test_single_frame_returns_jpeg_bytes_unchanged():
    response = _client().get("/api/camera/frame.jpg")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == b"\xff\xd8fake-jpeg"


def test_camera_busy_maps_to_503():
    camera = ScriptedPreviewCamera([CameraBusyError("camera busy — capture in progress")])
    response = _client(camera).get("/api/camera/frame.jpg")
    assert response.status_code == 503               # transient: retry, don't give up
    assert "busy" in response.json()["detail"]


def test_camera_error_maps_to_409():
    camera = ScriptedPreviewCamera([CameraError("live view is off on the body")])
    response = _client(camera).get("/api/camera/frame.jpg")
    assert response.status_code == 409               # wrong state, retrying won't help


def test_liveview_streams_frames_and_survives_a_busy_frame():
    camera = ScriptedPreviewCamera([
        b"\xff\xd8one",
        CameraBusyError("capture holds the connection"),  # must be skipped, not fatal
        b"\xff\xd8two",
        CameraError("live view is off"),                  # ends the stream cleanly
    ])
    response = _client(camera).get("/api/camera/liveview.mjpg")

    assert response.status_code == 200
    assert response.headers["content-type"] == "multipart/x-mixed-replace; boundary=frame"
    body = response.content
    assert body.count(b"--frame") == 2               # the busy frame was dropped
    assert b"\xff\xd8one" in body and b"\xff\xd8two" in body
    assert b"Content-Length: 5" in body              # length matches the frame bytes


def test_liveview_with_no_live_view_is_a_status_code_not_an_empty_200():
    """A browser leaves an empty-bodied 200 `<img>` pending forever.

    No `error` event ever fires, so the UI sat on "Connecting…" with no camera
    attached. Failing the request outright is what lets the panel say "enable
    live view on the body".
    """
    camera = ScriptedPreviewCamera([CameraError("live view is off")])
    response = _client(camera).get("/api/camera/liveview.mjpg")

    assert response.status_code == 409                 # CameraError -> 409, per app.py
    assert "live view is off" in response.json()["detail"]


def test_liveview_opens_anyway_when_a_capture_holds_the_camera():
    """Busy is transient — the stream must survive a capture, not refuse to start."""
    camera = ScriptedPreviewCamera([
        CameraBusyError("capture holds the connection"),  # blocks the priming frame
        b"\xff\xd8late",
        CameraError("live view is off"),                  # ends the stream cleanly
    ])
    response = _client(camera).get("/api/camera/liveview.mjpg")

    assert response.status_code == 200
    assert b"\xff\xd8late" in response.content


def test_liveview_accepts_an_unknown_cache_busting_query_param():
    """The front end appends ?t=<now> so a retry re-opens the stream."""
    camera = ScriptedPreviewCamera([b"\xff\xd8one", CameraError("live view is off")])
    assert _client(camera).get("/api/camera/liveview.mjpg?t=1789573221168").status_code == 200


def test_reconnect_rebuilds_the_session():
    opens = {"n": 0}

    def factory() -> MockCamera:
        opens["n"] += 1
        return MockCamera()

    session = CameraSession(camera_factory=factory)
    app = create_app(Config())
    app.dependency_overrides[get_camera_session] = lambda: session
    client = TestClient(app)

    client.get("/api/camera/status")                 # first open
    assert client.post("/api/camera/reconnect").json() == {"ok": True}
    assert opens["n"] == 2                           # torn down and opened again


def _job_holding_camera(app, session, kind: str = "sequence"):
    """Submit a job that genuinely holds the camera, and wait until it does."""
    held = threading.Event()
    release = threading.Event()

    def holds(ctx):
        with session.acquire():        # a real borrow, as a capture would take
            held.set()
            release.wait(timeout=5)
        return None

    app.state.jobs.submit(kind, holds)
    held.wait(timeout=5)
    return release


def test_single_frame_is_locked_out_while_the_camera_is_held():
    # Gated on an actual borrow rather than job state, so that a job which is
    # running but not touching the hardware (an align mid-plate-solve) does not
    # needlessly blank live view.
    session = CameraSession(camera_factory=lambda: MockCamera())
    app = create_app(Config())
    app.dependency_overrides[get_camera_session] = lambda: session
    client = TestClient(app)

    release = _job_holding_camera(app, session)
    try:
        assert client.get("/api/camera/frame.jpg").status_code == 503
    finally:
        release.set()


def test_single_frame_works_again_once_the_camera_is_released():
    session = CameraSession(camera_factory=lambda: MockCamera())
    app = create_app(Config())
    app.dependency_overrides[get_camera_session] = lambda: session
    client = TestClient(app)

    release = _job_holding_camera(app, session)
    release.set()
    for _ in range(50):                 # let the worker actually drop the borrow
        if not session.in_use:
            break
        time.sleep(0.02)

    assert client.get("/api/camera/frame.jpg").status_code == 200


def test_a_sequence_paused_on_the_lens_cap_does_not_lock_out_live_view():
    # The shutter is idle while waiting on the cap, and seeing the cap go on is
    # exactly what live view is for at that moment. Nothing holds the camera
    # between phases, so in_use answers this correctly with no special case.
    session = CameraSession(camera_factory=lambda: MockCamera())
    assert session.in_use is False


def test_session_reports_in_use_only_while_a_borrow_is_held():
    # The signal live view gates on. Must be false the instant the borrow ends,
    # not for as long as some job is nominally "running".
    session = CameraSession(camera_factory=lambda: MockCamera())
    assert session.in_use is False
    with session.acquire():
        assert session.in_use is True                 # held
    assert session.in_use is False                    # released immediately


def test_solving_phase_does_not_block_live_view():
    # An align captures, releases the camera, then plate-solves for ~15s. The
    # camera is idle throughout the solve, so frame.jpg must still serve.
    session = CameraSession(camera_factory=lambda: MockCamera())
    app = create_app(Config())
    app.dependency_overrides[get_camera_session] = lambda: session
    client = TestClient(app)

    solving = threading.Event()
    release = threading.Event()

    def align_like(ctx):
        with session.acquire():       # the capture half — camera genuinely held
            pass
        solving.set()                 # now in the Docker solve; camera idle
        release.wait(timeout=5)
        return None

    app.state.jobs.submit("align", align_like)
    solving.wait(timeout=5)
    try:
        # Job is still "running", but the camera is free — this must not 503.
        assert client.get("/api/camera/frame.jpg").status_code == 200
    finally:
        release.set()


def test_release_closes_the_session_so_the_mirror_drops():
    # Stopping the stream is not enough: while the PTP session is open the body
    # stays in live view with the mirror up, which is the heaviest battery draw.
    session = CameraSession(camera_factory=lambda: MockCamera())
    app = create_app(Config())
    app.dependency_overrides[get_camera_session] = lambda: session
    client = TestClient(app)

    client.get("/api/camera/frame.jpg")                  # opens the session
    assert session.is_open is True

    assert client.post("/api/camera/release").status_code == 200
    assert session.is_open is False                      # connection actually dropped


def test_release_does_not_interrupt_a_capture_in_flight():
    # A capture can hold the camera for hours, so a release request must defer
    # rather than wait on it — this is called from a request handler.
    session = CameraSession(camera_factory=lambda: MockCamera())
    done = threading.Event()

    with session.acquire():                              # a capture holds the camera
        t = threading.Thread(target=lambda: (session.release_when_idle(0.0), done.set()))
        t.start()
        assert done.wait(timeout=5), "release blocked on the in-flight borrow"
        assert session.is_open is True                   # deferred, not yanked
        t.join(timeout=5)

    session.close()


def test_a_borrow_cancels_a_pending_idle_release():
    # A sequence between phases, or a user restarting the stream, must not pay
    # for a reconnect just because the debounce was already ticking.
    session = CameraSession(camera_factory=lambda: MockCamera())
    with session.acquire():
        pass
    session.release_when_idle(30.0)                      # long fuse
    with session.acquire():
        pass                                             # borrowing cancels it
    assert session.is_open is True
    session.close()
