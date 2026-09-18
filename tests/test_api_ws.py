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

import anyio
import pytest

from camera_orchestrator.interfaces.api import routes_ws
from camera_orchestrator.interfaces.api.jobs import JobRegistry
from fastapi import WebSocketDisconnect
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

# How long a test waits for the message it wants. Wall-clock, not a message
# count: counting messages meant a heartbeat-only socket burned budget × tick
# (400 × 0.2 s = 80 s) before reporting a failure that was knowable in seconds.
READ_TIMEOUT_S = 20.0


@pytest.fixture
def anyio_backend():
    """The async tests here drive the watcher directly; asyncio is what ships."""
    return "asyncio"


def _read(websocket, wanted: str, *, timeout: float = READ_TIMEOUT_S) -> dict:
    """The next message of type `wanted`, skipping heartbeats and other traffic."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = websocket.receive_json()
        if message["type"] == wanted:
            return message
    raise AssertionError(f"no {wanted!r} message within {timeout}s")


def _read_until(websocket, job_id: str, *states: str, timeout: float = READ_TIMEOUT_S) -> dict:
    """Follow `job_id` on the socket until it reports one of `states`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = websocket.receive_json()
        jobs = [message["job"]] if message["type"] in ("job", "ack") else message.get("jobs", [])
        for job in jobs:
            if job["id"] == job_id and job["state"] in states:
                return job
    raise AssertionError(f"job {job_id} never reached {states} within {timeout}s")


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
        # so no extra connection, which is what the old per-job design cost. The
        # token is echoed from the prompt being displayed, so the answer cannot
        # drift onto a later question.
        websocket.send_json({"type": "confirm", "job_id": job_id,
                             "token": waiting["prompt"]["token"]})
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
        # Wait for the ack before letting the shutter go. Releasing first raced
        # the command: the frames finished, the job succeeded, and the test
        # failed about one run in five (and burned the full read budget doing it).
        assert _read(websocket, "ack")["command"] == "cancel"
        camera.released.set()  # cancellation is cooperative: let the shutter finish
        cancelled = _read_until(websocket, job_id, "cancelled")

    assert cancelled["error"] is None                   # cancelled, not failed


def test_a_socket_confirm_without_the_prompts_token_is_refused(tmp_path):
    # The socket path enforces the same rule as POST /confirm: a replayed command
    # (a reconnect that re-sends what the tab was showing) must not release a
    # phase nobody has capped the lens for.
    client = _sequence_client(MockCamera())
    with client.websocket_connect("/api/ws") as websocket:
        _read(websocket, "snapshot")
        job_id = client.post(
            "/api/jobs/sequence", json={"out_dir": str(tmp_path), "darks": 1}).json()["id"]
        waiting = _read_until(websocket, job_id, "awaiting_confirmation")

        websocket.send_json({"type": "confirm", "job_id": job_id, "token": "stale"})
        assert "token" in _read(websocket, "error")["message"]

        # The socket is still usable and the real token still works.
        websocket.send_json({"type": "confirm", "job_id": job_id,
                             "token": waiting["prompt"]["token"]})
        _read_until(websocket, job_id, "succeeded")


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


# -- dropped frames --------------------------------------------------------


def _finished_job(registry: JobRegistry) -> str:
    """Submit a trivial job and wait for it to reach a terminal state."""
    job = registry.submit("batch", lambda ctx: "done")
    deadline = time.monotonic() + STATE_TIMEOUT_S
    while time.monotonic() < deadline:
        if registry.get(job.id).state == "succeeded":
            return job.id
        time.sleep(0.01)
    raise AssertionError("the job never finished")


@pytest.mark.anyio
async def test_a_dropped_job_update_forces_a_full_resync(tmp_path):
    """A drop must not be recorded as a send.

    `_push` drops on a full buffer and `_watch` used to merge the whole payload
    into `sent` regardless, so the next diff excluded the dropped job. For a
    job's *terminal* frame there is no next update to recover on, and the UI sat
    on `running` for a job that had finished — the exact failure the one-socket
    redesign existed to remove.
    """
    registry = JobRegistry()
    # One slot, and nothing reading it: the stalled-client case, deterministically.
    send, receive = anyio.create_memory_object_stream[dict](1)

    async with send, receive, anyio.create_task_group() as tg:
        tg.start_soon(routes_ws._watch, registry, send)
        await anyio.sleep(routes_ws.WS_TICK_S)     # the snapshot takes the only slot

        job_id = await anyio.to_thread.run_sync(_finished_job, registry)
        await anyio.sleep(routes_ws.WS_TICK_S * 2)  # its updates are pushed, and dropped

        stalled = receive.receive_nowait()          # the client finally drains
        assert stalled["type"] == "snapshot" and stalled["jobs"] == []

        with anyio.fail_after(STATE_TIMEOUT_S):
            while True:
                message = await receive.receive()
                jobs = (message.get("jobs") or []) if message["type"] == "snapshot" else (
                    [message["job"]] if message["type"] == "job" else [])
                if any(job["id"] == job_id and job["state"] == "succeeded" for job in jobs):
                    break                           # the terminal state came back round
        tg.cancel_scope.cancel()


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


# -- cross-site WebSocket hijacking ---------------------------------------


def _connect(client: TestClient, origin: str | None):
    """Open the job socket, optionally claiming to be a page from `origin`."""
    headers = {} if origin is None else {"origin": origin}
    return client.websocket_connect("/api/ws", headers=headers)


def test_a_foreign_origin_is_rejected_before_the_handshake_completes():
    """The one (A) finding: any page you visit could otherwise read and control jobs.

    WebSockets are exempt from the same-origin policy, so evil.example's script
    gets a *working* socket to 127.0.0.1 unless the server refuses the
    handshake — one that hands back the whole job snapshot (session paths, file
    names, the target's RA/Dec) and takes `cancel` and `confirm`.
    """
    client = _client()
    with pytest.raises(WebSocketDisconnect) as rejected:
        with _connect(client, "https://evil.example") as websocket:
            websocket.receive_json()               # never reached: never accepted
    assert rejected.value.code == 1008             # policy violation, not a protocol error


def test_a_lookalike_origin_is_rejected():
    # Substring matching would have let this through: the Host is a *prefix* of
    # the attacker's domain, and the two are entirely different origins.
    client = _client()
    with pytest.raises(WebSocketDisconnect):
        with _connect(client, "http://testserver.evil.example") as websocket:
            websocket.receive_json()


def test_the_pages_own_origin_is_accepted(tmp_path):
    # The UI itself: served from the host the request arrived on. TestClient
    # sends Host: testserver, which is what the front end's origin would be.
    with _connect(_client(), "http://testserver") as websocket:
        assert _read(websocket, "snapshot")["jobs"] == []


def test_a_client_without_an_origin_is_accepted():
    # curl, a script, a future native app: no browser, no ambient authority to
    # abuse, and nothing to protect them from.
    with _connect(_client(), None) as websocket:
        assert _read(websocket, "snapshot")["type"] == "snapshot"


def test_origin_allowed_compares_scheme_host_and_port():
    """The comparison itself, away from the socket — each case is a real bypass.

    The Host header is the reference on purpose: bound to 0.0.0.0 and reached
    from a phone, the page's origin is the LAN IP the phone typed, and nothing
    in config knows that address.
    """
    allowed = routes_ws.origin_allowed

    # The phone-over-LAN case: origin and Host agree, so it is the same server.
    assert allowed("http://192.0.2.10:8000", "192.0.2.10:8000") is True
    assert allowed("http://cam.example:8000", "cam.example:8000") is True

    # Loopback is inside the trust boundary whatever port it came from.
    assert allowed("http://localhost:5173", "127.0.0.1:8000") is True
    assert allowed("http://127.0.0.1:8000", "127.0.0.1:8000") is True

    # A different port on the same host is a different origin.
    assert allowed("http://cam.example:9000", "cam.example:8000") is False
    # ...as is a different scheme's default port.
    assert allowed("https://cam.example", "cam.example:8000") is False
    # ...and an opaque origin, which is what a sandboxed iframe sends.
    assert allowed("null", "cam.example:8000") is False
    assert allowed("file://", "cam.example:8000") is False
