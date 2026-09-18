"""Tests for the job HTTP routes — real services over MockCamera, no hardware.

Services are built for real (CaptureService, SequenceService) around MockCamera
and the in-memory repos already used elsewhere, and injected with FastAPI
dependency overrides; only the solver is patched. That keeps the thing under test
honest: jobs run on actual worker threads, so the assertions exercise the
threading, not a simulation of it.

Jobs are asynchronous by design, so every assertion goes through `_await_state`,
which polls the job endpoint rather than sleeping a fixed amount.
"""
from __future__ import annotations

import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from camera_orchestrator.application.capture_service import CaptureService
from camera_orchestrator.application.sequence_service import SequenceService
from camera_orchestrator.config import Config
from camera_orchestrator.domain.models.solve import SolveJob, SolveResult
from camera_orchestrator.interfaces.api.app import create_app
from camera_orchestrator.interfaces.api.deps import (
    get_capture_service,
    get_sequence_service,
    get_solve_repository,
    get_solver_factory,
)
from fastapi.testclient import TestClient

from tests.test_batch_service import FakeRepo as FakeSolveRepo  # in-memory sidecar repo
from tests.test_sequence_service import FakeRepo as FakeManifestRepo
from tests.test_service import MockCamera  # reuse the atomic-ABC mock

# Polling budget for a job to reach a state. Generous: these run on worker
# threads and CI is slower than a laptop.
STATE_TIMEOUT_S = 10.0

# The capture root these tests run under. `out_dir`, `folder` and `file` are
# confined to the root fixed at app creation, and every test here works in its
# own `tmp_path` — which lives under the system temp directory, so pointing the
# root at that keeps one helper honest for all of them. Resolved because
# tmp_path is resolved too, and the comparison is path-equality.
TEST_ROOT = str(Path(tempfile.gettempdir()).resolve())

# Overridable dependencies, by the keyword _client() accepts for each.
_DEPS = {
    "get_capture_service": get_capture_service,
    "get_sequence_service": get_sequence_service,
    "get_solve_repository": get_solve_repository,
    "get_solver_factory": get_solver_factory,
}


class BlockingCamera(MockCamera):
    """A camera whose shutter hangs until the test releases it.

    Stands in for the real thing's defining property: a capture owns the USB
    connection for minutes, which is the whole reason jobs exist.
    """

    def __init__(self):
        super().__init__()
        self.released = threading.Event()
        self.firing = threading.Event()

    def trigger(self) -> None:
        self.firing.set()
        self.released.wait(STATE_TIMEOUT_S)
        super().trigger()


def _const(value):
    """A zero-argument provider returning `value`.

    Must take no parameters: FastAPI introspects an override's signature, so a
    `lambda v=value: v` would be read as declaring a query parameter.
    """

    def provide():
        return value

    return provide


def _client(cfg: Config | None = None, confirm_timeout: float = 5.0,
            browse_root: str = TEST_ROOT, **overrides) -> TestClient:
    """App + TestClient with the named dependencies overridden."""
    app = create_app(cfg or Config(), confirm_timeout=confirm_timeout, browse_root=browse_root)
    for dependency, value in overrides.items():
        app.dependency_overrides[_DEPS[dependency]] = _const(value)
    return TestClient(app)


def _capture_client(camera: MockCamera, cfg: Config | None = None) -> TestClient:
    return _client(cfg=cfg, get_capture_service=CaptureService(camera_factory=lambda: camera))


def _await_state(client: TestClient, job_id: str, *states: str) -> dict:
    """Poll a job until it reaches one of `states`, or fail the test."""
    deadline = time.monotonic() + STATE_TIMEOUT_S
    job: dict = {}
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] in states:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job stuck in {job.get('state')!r}, wanted one of {states}: {job}")


def _solved_job(path: str, solver, cfg, annotate_out=None) -> SolveJob:
    return SolveJob(path=path, result=SolveResult(
        center_ra_deg=10.6, center_dec_deg=41.2, scale_arcsec_per_px=3.9,
        width_px=60, height_px=40))


# -- lifecycle -------------------------------------------------------------


def test_capture_job_runs_to_success_with_progress(tmp_path):
    client = _capture_client(MockCamera())
    posted = client.post("/api/jobs/capture", json={
        "out_dir": str(tmp_path), "count": 3, "download": True, "iso": "800"})

    assert posted.status_code == 200
    created = posted.json()
    assert created["kind"] == "capture"
    assert created["state"] in ("pending", "running")   # returns before the work
    assert created["created_at"] is not None
    assert created["ended_at"] is None

    job = _await_state(client, created["id"], "succeeded")
    assert job["result"]["frames_captured"] == 3
    assert len(job["result"]["frames"]) == 3
    assert job["progress"] == {"current": 3, "total": 3, "label": "light frames"}
    assert job["error"] is None
    assert job["started_at"] is not None and job["ended_at"] is not None


def test_capture_job_uses_the_session_folder_when_named(tmp_path):
    client = _capture_client(MockCamera(), cfg=Config.model_validate(
        {"grab": {"out_dir": str(tmp_path)}}))
    posted = client.post("/api/jobs/capture", json={"name": "m33", "download": True})

    job = _await_state(client, posted.json()["id"], "succeeded")
    # Same <root>/<YYYYMMDD>-<name> rule the CLI's --name uses.
    stamp = datetime.now().strftime("%Y%m%d")
    assert job["result"]["frames"][0].startswith(str(tmp_path / f"{stamp}-m33"))
    assert (tmp_path / f"{stamp}-m33").is_dir()


def test_failing_job_records_the_error_and_does_not_hang(tmp_path):
    # A body that locks the shutter in a PTP session (the M50 II case) raises
    # inside the service; the HTTP request that started the job is long gone.
    client = _capture_client(MockCamera(can_capture=False))
    posted = client.post("/api/jobs/capture", json={"out_dir": str(tmp_path), "count": 1})

    job = _await_state(client, posted.json()["id"], "failed")
    assert "does not support remote capture" in job["error"]
    assert job["result"] is None
    assert job["ended_at"] is not None


def test_jobs_are_listed_newest_first(tmp_path):
    client = _capture_client(MockCamera())
    first = client.post("/api/jobs/capture", json={"out_dir": str(tmp_path)}).json()["id"]
    _await_state(client, first, "succeeded")
    second = client.post("/api/jobs/capture", json={"out_dir": str(tmp_path)}).json()["id"]
    _await_state(client, second, "succeeded")

    assert [j["id"] for j in client.get("/api/jobs").json()["jobs"]] == [second, first]


def test_unknown_job_is_404():
    assert _client().get("/api/jobs/nope").status_code == 404


# -- the one-camera-at-a-time rule ----------------------------------------


def test_second_camera_job_is_rejected_with_409(tmp_path):
    camera = BlockingCamera()
    client = _capture_client(camera)
    first = client.post("/api/jobs/capture", json={"out_dir": str(tmp_path)}).json()
    try:
        assert camera.firing.wait(STATE_TIMEOUT_S)      # the first job owns the camera
        second = client.post("/api/jobs/align", json={"out_dir": str(tmp_path)})
        assert second.status_code == 409                # queueing exposures in the
        assert first["id"] in second.json()["detail"]   # dark is worse than an error
    finally:
        camera.released.set()

    _await_state(client, first["id"], "succeeded")
    # Once it is done the camera is free again.
    assert client.post("/api/jobs/capture", json={"out_dir": str(tmp_path)}).status_code == 200


def test_a_solver_job_may_run_while_the_camera_is_busy(tmp_path):
    camera = BlockingCamera()
    client = _capture_client(camera)
    capture = client.post("/api/jobs/capture", json={"out_dir": str(tmp_path)}).json()
    try:
        assert camera.firing.wait(STATE_TIMEOUT_S)
        # batch never touches the camera, so it must not be blocked by one.
        batch = client.post("/api/jobs/batch", json={"folder": str(tmp_path)})
        assert batch.status_code == 200
        assert _await_state(client, batch.json()["id"], "succeeded")["result"]["status"] == "no_images"
    finally:
        camera.released.set()
    _await_state(client, capture["id"], "succeeded")


# -- the lens-cap prompt ---------------------------------------------------


def _sequence_client(camera: MockCamera, **kw) -> TestClient:
    service = SequenceService(CaptureService(camera_factory=lambda: camera), FakeManifestRepo())
    return _client(get_sequence_service=service, **kw)


def _confirm(client: TestClient, job: dict):
    """Answer a job's pending prompt, quoting the token it is showing."""
    return client.post(f"/api/jobs/{job['id']}/confirm",
                       json={"token": job["prompt"]["token"]})


def test_sequence_job_waits_for_a_lens_cap_confirmation(tmp_path):
    client = _sequence_client(MockCamera())
    posted = client.post("/api/jobs/sequence", json={"out_dir": str(tmp_path), "darks": 1})

    job = _await_state(client, posted.json()["id"], "awaiting_confirmation")
    assert job["prompt"]["kind"] == "dark"
    assert job["prompt"]["message"] == "Cover the lens for dark frames, then confirm."
    assert job["prompt"]["token"]                       # scopes the answer to this prompt

    confirmed = _confirm(client, job)
    assert confirmed.status_code == 200

    done = _await_state(client, job["id"], "succeeded")
    assert done["prompt"] is None                       # cleared on confirm
    assert [p["kind"] for p in done["result"]["phases"]] == ["dark"]
    assert done["progress"] == {"current": 1, "total": 1, "label": "complete"}


def test_a_confirm_while_running_does_not_answer_the_next_phases_prompt(tmp_path):
    """The silent-data-corruption bug: one stale confirm, an uncapped bias phase.

    A confirm that lands while the job is `running` (a double-click, an impatient
    second press, a command replayed after a socket reconnect) used to leave the
    Event set, so the *next* ctx.prompt() returned immediately — the bias frames
    were shot with the lens off and the manifest recorded them as calibration.
    """
    camera = BlockingCamera()
    client = _sequence_client(camera)
    posted = client.post(
        "/api/jobs/sequence", json={"out_dir": str(tmp_path), "darks": 1, "bias": 1})
    job_id = posted.json()["id"]

    dark = _await_state(client, job_id, "awaiting_confirmation")
    assert dark["prompt"]["kind"] == "dark"
    assert _confirm(client, dark).status_code == 200

    # The dark phase is now on the wire, with the shutter held open by the fake.
    assert camera.firing.wait(STATE_TIMEOUT_S)
    assert client.get(f"/api/jobs/{job_id}").json()["state"] == "running"
    client.post(f"/api/jobs/{job_id}/confirm")                       # the stale press
    client.post(f"/api/jobs/{job_id}/confirm", json={"token": dark["prompt"]["token"]})
    camera.released.set()

    # The bias phase must still stop and ask. Before the fix it ran straight on.
    bias = _await_state(client, job_id, "awaiting_confirmation", "succeeded", "failed")
    assert bias["state"] == "awaiting_confirmation"
    assert bias["prompt"]["kind"] == "bias"
    assert bias["prompt"]["token"] != dark["prompt"]["token"]        # a fresh question

    assert _confirm(client, bias).status_code == 200
    assert _await_state(client, job_id, "succeeded")["prompt"] is None


def test_a_confirm_without_a_token_is_refused(tmp_path):
    # Absent is treated exactly like wrong: the live prompt must be answered
    # deliberately, by whoever can see it.
    client = _sequence_client(MockCamera())
    posted = client.post("/api/jobs/sequence", json={"out_dir": str(tmp_path), "darks": 1})
    job = _await_state(client, posted.json()["id"], "awaiting_confirmation")

    refused = client.post(f"/api/jobs/{job['id']}/confirm")
    assert refused.status_code == 409
    assert "token" in refused.json()["detail"]
    # Still blocked, still asking — nothing was released by the bad attempt.
    assert client.get(f"/api/jobs/{job['id']}").json()["state"] == "awaiting_confirmation"
    assert _confirm(client, job).status_code == 200


def test_a_confirm_quoting_a_superseded_token_is_refused(tmp_path):
    client = _sequence_client(MockCamera())
    posted = client.post("/api/jobs/sequence", json={"out_dir": str(tmp_path), "darks": 1})
    job = _await_state(client, posted.json()["id"], "awaiting_confirmation")

    stale = client.post(f"/api/jobs/{job['id']}/confirm", json={"token": "from-last-phase"})
    assert stale.status_code == 409
    assert _confirm(client, job).status_code == 200                 # the real one works


def test_a_confirm_for_a_job_that_is_not_waiting_is_a_no_op(tmp_path):
    # Idempotent rather than an error: a second press on a prompt that has
    # already been answered is a normal thing for a person to do in the dark.
    client = _capture_client(MockCamera())
    job_id = client.post("/api/jobs/capture", json={"out_dir": str(tmp_path)}).json()["id"]
    _await_state(client, job_id, "succeeded")

    answered = client.post(f"/api/jobs/{job_id}/confirm", json={"token": "anything"})
    assert answered.status_code == 200
    assert answered.json()["state"] == "succeeded"


def test_job_result_datetimes_survive_json_encoding(tmp_path):
    """A SessionManifest nests datetimes; `Job.result` is typed `Any`.

    If the raw model were stashed there, the socket push and the JSONResponse
    would both have to encode a datetime out of an untyped field. `_encode` dumps
    in json mode up front, so what lands on the wire is already strings.
    """
    client = _sequence_client(MockCamera())
    job_id = client.post(
        "/api/jobs/sequence", json={"out_dir": str(tmp_path), "lights": 1}).json()["id"]

    phase = _await_state(client, job_id, "succeeded")["result"]["phases"][0]
    assert isinstance(phase["started_at"], str)
    assert datetime.fromisoformat(phase["started_at"]).tzinfo is timezone.utc


def test_sequence_job_does_not_prompt_before_lights(tmp_path):
    # Only capped phases need a human. The POST was the go-ahead for lights.
    client = _sequence_client(MockCamera())
    posted = client.post("/api/jobs/sequence", json={"out_dir": str(tmp_path), "lights": 2})

    job = _await_state(client, posted.json()["id"], "succeeded")
    assert job["prompt"] is None
    assert job["result"]["phases"][0]["count"] == 2


def test_sequence_job_fails_if_nobody_confirms(tmp_path):
    # A forgotten browser tab must release the camera, not hold it until sunrise.
    client = _sequence_client(MockCamera(), confirm_timeout=0.05)
    posted = client.post("/api/jobs/sequence", json={"out_dir": str(tmp_path), "bias": 1})

    job = _await_state(client, posted.json()["id"], "failed")
    assert "no confirmation for 'bias'" in job["error"]
    assert job["prompt"] is None


def test_cancelling_a_waiting_sequence_releases_it(tmp_path):
    client = _sequence_client(MockCamera())
    posted = client.post("/api/jobs/sequence", json={"out_dir": str(tmp_path), "darks": 1})
    job = _await_state(client, posted.json()["id"], "awaiting_confirmation")

    assert client.post(f"/api/jobs/{job['id']}/cancel").status_code == 200
    cancelled = _await_state(client, job["id"], "cancelled")
    assert cancelled["error"] is None                   # cancelled, not failed


def test_empty_sequence_is_rejected_up_front(tmp_path):
    response = _client().post("/api/jobs/sequence", json={"out_dir": str(tmp_path)})
    assert response.status_code == 400                  # no job id issued for a no-op


# -- solver jobs -----------------------------------------------------------


def test_solve_job_solves_and_saves_a_record(tmp_path):
    image = tmp_path / "IMG_0001.JPG"
    image.write_bytes(b"\xff\xd8fake")
    repo = FakeSolveRepo()
    client = _client(get_solve_repository=repo, get_solver_factory=lambda cfg: object())

    with patch("camera_orchestrator.interfaces.api.routes_jobs.solve_file",
               side_effect=_solved_job):
        posted = client.post("/api/jobs/solve", json={"file": str(image)})
        job = _await_state(client, posted.json()["id"], "succeeded")

    assert job["result"]["solved"] is True
    assert job["result"]["solve"]["center_ra_deg"] == 10.6
    assert (str(tmp_path), "IMG_0001.JPG") in repo.store   # sidecar persisted


def test_solve_job_missing_file_is_404(tmp_path):
    # Inside the capture root but absent — a 404, distinct from the 400 an
    # out-of-root path gets.
    response = _client().post("/api/jobs/solve", json={"file": str(tmp_path / "x.jpg")})
    assert response.status_code == 404


def test_solve_job_refuses_an_existing_sidecar_without_force(tmp_path):
    image = tmp_path / "IMG_0001.JPG"
    image.write_bytes(b"\xff\xd8fake")
    repo = FakeSolveRepo(solved={(str(tmp_path), "IMG_0001.JPG")})
    response = _client(get_solve_repository=repo).post(
        "/api/jobs/solve", json={"file": str(image)})

    assert response.status_code == 409
    assert "already has a sidecar" in response.json()["detail"]


def test_batch_job_missing_folder_is_404(tmp_path):
    missing = str(tmp_path / "nope")
    assert _client().post("/api/jobs/batch", json={"folder": missing}).status_code == 404


def test_utc_timestamps_are_timezone_aware(tmp_path):
    client = _capture_client(MockCamera())
    job_id = client.post("/api/jobs/capture", json={"out_dir": str(tmp_path)}).json()["id"]
    job = _await_state(client, job_id, "succeeded")

    # The UI does date maths on these; a naive timestamp would silently be read
    # as local time.
    assert datetime.fromisoformat(job["ended_at"]).tzinfo is timezone.utc


# The exact bodies static/capture.js builds, after omitEmpty(). Kept verbatim so
# a server-side default that stops accepting the client's shape fails here rather
# than at the telescope — mock.js does not validate bodies, so nothing else
# exercises the real contract.
_UI_CAPTURE_BODY = {
    "iso": "3200", "shutter": "2", "count": 32, "kind": "light", "download": False,
}
_UI_SEQUENCE_BODY = {
    "iso": "3200", "shutter": "2", "lights": 32, "darks": 12, "bias": 20,
    "download": False,
}
_UI_ALIGN_BODY = {"iso": "3200", "shutter": "2", "force": False}


@pytest.mark.parametrize("path,body", [
    ("/api/jobs/capture", _UI_CAPTURE_BODY),
    ("/api/jobs/sequence", _UI_SEQUENCE_BODY),
    ("/api/jobs/align", _UI_ALIGN_BODY),
])
def test_the_front_ends_own_payload_is_accepted(path, body):
    # Regression: capture.js sent select:null, which the server rejects because
    # `select` has a non-null default — every capture from the UI was a 422.
    client = _client()
    assert client.post(path, json=body).status_code != 422, (
        f"{path} rejected the body the front end actually sends"
    )


def test_a_non_optional_field_still_rejects_null():
    # The other half of the same bug: omitting is fine, null is not, and that
    # distinction is what the client helper exists to respect.
    client = _client()
    body = dict(_UI_CAPTURE_BODY, select=None)
    assert client.post("/api/jobs/capture", json=body).status_code == 422
