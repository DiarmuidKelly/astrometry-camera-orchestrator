"""Tests for the multiplexed job socket — TestClient over the real app, no hardware.

The app, the registry and the worker threads are all real; only the camera is
faked (`MockCamera`, and `BlockingCamera` where a test needs the shutter to hang).
The client helpers are reused from `tests/test_api_jobs.py` so both suites drive
the same wiring, and `tests/` is an importable package for exactly that.

The behaviour these tests exist to protect is the snapshot-on-connect: a client
that drops mid-job and reconnects must converge on the true state. The bug being
fixed was the opposite — a dropped per-job stream left the client's copy of a
finished job stuck in `running` forever, which pinned live view paused.
"""
from __future__ import annotations

import time

from camera_orchestrator.interfaces.api import routes_ws
from fastapi.testclient import TestClient

from tests.test_api_jobs import (
    STATE_TIMEOUT_S,
    BlockingCamera,
    _await_state,
    _capture_client,
    _client,
    _sequence_client,
)
from tests.test_service import MockCamera

# The watcher's heartbeat interval. Shortened from 15 s so the heartbeat is
# testable, and so a closed socket's parked worker thread exits promptly instead
# of idling out the tick after every test.
routes_ws.WS_TICK_S = 0.2

# How many messages a test will read before giving up waiting for one it wants.
# Generous because heartbeats are interleaved at this tick rate.
MESSAGE_BUDGET = 400


def _read(websocket, wanted: str, *, budget: int = MESSAGE_BUDGET) -> dict:
    """The next message of type `wanted`, skipping heartbeats and other traffic."""
    for _ in range(budget):
        message = websocket.receive_json()
        if message["type"] == wanted:
            return message
    raise AssertionError(f"no {wanted!r} message within {budget} messages")


def _read_until(websocket, job_id: str, *states: str, budget: int = MESSAGE_BUDGET) -> dict:
    """Follow `job_id` on the socket until it reports one of `states`."""
    for _ in range(budget):
        message = websocket.receive_json()
        jobs = [message["job"]] if message["type"] in ("job", "ack") else message.get("jobs", [])
        for job in jobs:
            if job["id"] == job_id and job["state"] in states:
                return job
    raise AssertionError(f"job {job_id} never reached {states}")


def _start_capture(client: TestClient, tmp_path, count: int = 1) -> str:
    response = client.post(
        "/api/jobs/capture", json={"out_dir": str(tmp_path), "count": count})
    assert response.status_code == 200
    return str(response.json()["id"])


# -- the snapshot ----------------------------------------------------------


def test_the_first_message_is_a_snapshot_of_every_job(tmp_path):
    client = _capture_client(MockCamera())
    first = _start_capture(client, tmp_path)
    second = _start_capture(client, tmp_path)
    _await_state(client, second, "succeeded")

    with client.websocket_connect("/api/ws") as websocket:
        snapshot = websocket.receive_json()

    assert snapshot["type"] == "snapshot"
    # Newest first, matching GET /api/jobs — the panel renders it in that order.
    assert [job["id"] for job in snapshot["jobs"]] == [second, first]
    assert all(job["state"] == "succeeded" for job in snapshot["jobs"])


def test_a_snapshot_arrives_even_with_no_jobs():
    # A fresh page load must not hang waiting for the first thing to happen.
    with _client().websocket_connect("/api/ws") as websocket:
        assert websocket.receive_json() == {"type": "snapshot", "jobs": []}


# -- pushes ----------------------------------------------------------------


def test_a_job_started_after_connecting_is_pushed(tmp_path):
    client = _capture_client(MockCamera())
    with client.websocket_connect("/api/ws") as websocket:
        assert websocket.receive_json()["type"] == "snapshot"

        job_id = _start_capture(client, tmp_path, count=2)
        done = _read_until(websocket, job_id, "succeeded")

    # The whole lifecycle arrived on the one connection that was already open —
    # no second connection was opened for this job, which is the entire point.
    assert done["progress"] == {"current": 2, "total": 2, "label": "light frames"}
    assert done["result"]["frames_captured"] == 2


def test_progress_updates_arrive_before_the_terminal_state(tmp_path):
    camera = BlockingCamera()
    client = _capture_client(camera)
    with client.websocket_connect("/api/ws") as websocket:
        _read(websocket, "snapshot")
        job_id = _start_capture(client, tmp_path, count=2)
        running = _read_until(websocket, job_id, "running")
        assert running["started_at"] is not None
        camera.released.set()
        _read_until(websocket, job_id, "succeeded")


def test_two_clients_both_receive_every_update(tmp_path):
    """A laptop and a phone at once — the socket broadcasts, it does not hand off."""
    client = _capture_client(MockCamera())
    with client.websocket_connect("/api/ws") as laptop:
        with client.websocket_connect("/api/ws") as phone:
            _read(laptop, "snapshot")
            _read(phone, "snapshot")
            job_id = _start_capture(client, tmp_path)
            assert _read_until(laptop, job_id, "succeeded")["id"] == job_id
            assert _read_until(phone, job_id, "succeeded")["id"] == job_id


def test_an_idle_socket_heartbeats(tmp_path):
    # Without this a connection killed with no close frame (a slept laptop, a
    # dropped Wi-Fi) hangs silently instead of being noticed and re-dialled.
    with _client().websocket_connect("/api/ws") as websocket:
        _read(websocket, "snapshot")
        assert websocket.receive_json() == {"type": "ping"}


# -- commands --------------------------------------------------------------


def test_confirm_over_the_socket_releases_the_lens_cap_prompt(tmp_path):
    client = _sequence_client(MockCamera())
    with client.websocket_connect("/api/ws") as websocket:
        _read(websocket, "snapshot")
        job_id = client.post(
            "/api/jobs/sequence", json={"out_dir": str(tmp_path), "darks": 1}).json()["id"]

        waiting = _read_until(websocket, job_id, "awaiting_confirmation")
        assert waiting["prompt"]["kind"] == "dark"

        # The prompt is answered on the same connection — no extra request, and
        # so no extra connection, which is what the old per-job design cost.
        websocket.send_json({"type": "confirm", "job_id": job_id})
        done = _read_until(websocket, job_id, "succeeded")

    assert done["prompt"] is None
    assert [phase["kind"] for phase in done["result"]["phases"]] == ["dark"]


def test_cancel_over_the_socket_cancels_the_job(tmp_path):
    camera = BlockingCamera()
    client = _capture_client(camera)
    with client.websocket_connect("/api/ws") as websocket:
        _read(websocket, "snapshot")
        job_id = _start_capture(client, tmp_path, count=3)
        _read_until(websocket, job_id, "running")
        assert camera.firing.wait(STATE_TIMEOUT_S)

        websocket.send_json({"type": "cancel", "job_id": job_id})
        camera.released.set()  # cancellation is cooperative: let the shutter finish
        cancelled = _read_until(websocket, job_id, "cancelled")

    assert cancelled["error"] is None                   # cancelled, not failed


def test_a_command_is_acknowledged_to_the_client_that_sent_it(tmp_path):
    client = _capture_client(MockCamera())
    with client.websocket_connect("/api/ws") as websocket:
        _read(websocket, "snapshot")
        job_id = _start_capture(client, tmp_path)
        websocket.send_json({"type": "cancel", "job_id": job_id})
        ack = _read(websocket, "ack")

    # The broadcast tells everyone the state changed; the ack tells the button
    # that pressed it that its command landed.
    assert ack["command"] == "cancel"
    assert ack["job"]["id"] == job_id


def test_a_command_for_an_unknown_job_is_an_error_message_not_a_disconnect():
    with _client().websocket_connect("/api/ws") as websocket:
        _read(websocket, "snapshot")
        websocket.send_json({"type": "confirm", "job_id": "nope"})
        error = _read(websocket, "error")
        assert "nope" in error["message"]
        # Still usable: one bad command must not cost the page its one socket.
        websocket.send_json({"type": "pong"})
        assert _read(websocket, "ping") == {"type": "ping"}


def test_an_unknown_command_is_rejected_without_closing_the_socket():
    with _client().websocket_connect("/api/ws") as websocket:
        _read(websocket, "snapshot")
        websocket.send_json({"type": "launch_rocket"})
        assert "launch_rocket" in _read(websocket, "error")["message"]
        websocket.send_json({"type": "cancel"})          # no job_id
        assert "job_id" in _read(websocket, "error")["message"]


# -- reconnect -------------------------------------------------------------


def test_reconnecting_resyncs_a_job_that_finished_while_disconnected(tmp_path):
    """The stale-state bug, in miniature.

    The old per-job SSE stream reported a drop and did nothing else, so a job
    that finished while the connection was down stayed non-terminal in the client
    forever — `isExposing` never cleared and live view stayed paused. Here the
    socket is dropped mid-job and the reconnect's snapshot carries the truth.
    """
    camera = BlockingCamera()
    client = _capture_client(camera)

    with client.websocket_connect("/api/ws") as websocket:
        _read(websocket, "snapshot")
        job_id = _start_capture(client, tmp_path)
        assert _read_until(websocket, job_id, "running")["ended_at"] is None

    # Connection gone. The job carries on and finishes unobserved.
    camera.released.set()
    _await_state(client, job_id, "succeeded")

    with client.websocket_connect("/api/ws") as websocket:
        snapshot = _read(websocket, "snapshot")

    resynced = next(job for job in snapshot["jobs"] if job["id"] == job_id)
    assert resynced["state"] == "succeeded"             # converged, not stale
    assert resynced["ended_at"] is not None


def test_a_closed_socket_does_not_stop_the_next_one(tmp_path):
    """Sockets come and go with page loads; the registry outlives all of them."""
    client = _capture_client(MockCamera())
    for _ in range(3):
        with client.websocket_connect("/api/ws") as websocket:
            _read(websocket, "snapshot")
            job_id = _start_capture(client, tmp_path)
            _read_until(websocket, job_id, "succeeded")


def test_the_per_job_sse_route_is_gone(tmp_path):
    # Reintroducing it would reintroduce the connection-budget exhaustion that
    # killed live view; the socket carries every job instead.
    client = _capture_client(MockCamera())
    job_id = _start_capture(client, tmp_path)
    assert client.get(f"/api/jobs/{job_id}/events").status_code == 404


# -- the registry primitive ------------------------------------------------


def test_wait_any_wakes_on_any_job_and_returns_them_all(tmp_path):
    """`wait_any` is the registry-wide counterpart of the per-job `wait`."""
    client = _capture_client(MockCamera())
    registry = client.app.state.jobs

    revision, jobs = registry.wait_any(-1, 0.0)
    assert jobs == []                                   # nothing has happened yet

    job_id = _start_capture(client, tmp_path)
    updated, jobs = registry.wait_any(revision, STATE_TIMEOUT_S)
    assert updated > revision                           # submitting is a change
    assert [job.id for job in jobs] == [job_id]

    # The per-job cursor still works: one connection type did not break the other.
    assert registry.version_of(job_id) >= 0


def test_wait_any_returns_on_its_timeout_when_nothing_changes():
    registry = _client().app.state.jobs
    revision = registry.revision()

    started = time.monotonic()
    unchanged, jobs = registry.wait_any(revision, 0.1)

    # A timeout is not an error — it is the watcher's heartbeat tick.
    assert unchanged == revision and jobs == []
    assert time.monotonic() - started >= 0.05
