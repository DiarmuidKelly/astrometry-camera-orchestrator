"""The job socket — one multiplexed WebSocket carrying every job update.

**Why one socket and not a stream per job.** Browsers cap HTTP/1.1 at ~6
concurrent connections per origin. Live view holds one of those permanently
(`<img src="/api/camera/liveview.mjpg">` never ends), so a per-job stream burns
the remaining budget within a handful of jobs — and an exhausted budget does not
fail, it *queues forever*. The observed symptom was live view that "just would
not open" and jobs reported as lost that had in fact succeeded. One socket
carries every job for the life of the page, however many jobs run.

Three properties make a dropped connection self-healing rather than a stale UI:

- **Snapshot on connect.** The first frame is every job this process knows, so a
  reconnecting client resyncs in one round trip. It never has to reason about
  what it missed while it was away — which is what left the old per-job streams
  stuck on a non-terminal job forever after a drop.
- **Heartbeat.** An idle watcher tick emits a `ping`. A connection killed without
  a close frame (sleeping laptop, dropped Wi-Fi) is then noticed by the client's
  receive timeout instead of hanging silently.
- **Broadcast by construction.** Every connection runs its own watcher against
  the shared registry, so a laptop and a phone both get everything. Nothing here
  assumes a single client.

Bidirectional: `confirm` and `cancel` arrive as commands on the same socket, so
answering the lens-cap prompt costs no extra connection either. `POST
/api/jobs/{id}/confirm` and `/cancel` stay as the tested fallback.

The registry is thread-based (`threading.Condition`) and this endpoint is async,
so every registry call that can block is bridged with `anyio.to_thread.run_sync`.
Blocking the event loop here would stall every other request, the MJPEG stream
included — the exact class of failure this module exists to remove.
"""
from __future__ import annotations

from typing import Any

import anyio
import anyio.to_thread
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from camera_orchestrator.interfaces.api.deps import get_ws_registry
from camera_orchestrator.interfaces.api.jobs import JobNotFoundError, JobRegistry
from camera_orchestrator.log import get_logger

log = get_logger("camera_orchestrator.api.ws")

router = APIRouter(tags=["jobs"])

# How long the watcher parks waiting for a change before emitting a heartbeat.
# Long enough not to churn, short enough that a dead connection is noticed and no
# proxy times the socket out for idleness.
WS_TICK_S = 15.0

# Outbound buffer. Deep enough to absorb a burst of per-frame progress while a
# slow client drains, bounded so a client that has stopped reading cannot grow
# the queue without limit.
SEND_BUFFER = 64

Message = dict[str, Any]


def _push(sink: MemoryObjectSendStream[Message], message: Message) -> None:
    """Queue an outbound message; never block the producer.

    A full buffer or a half-closed connection drops the message rather than
    stalling the watcher — the next change resends the job in full, and a
    reconnect resyncs from the snapshot, so nothing is lost permanently.
    """
    try:
        sink.send_nowait(message)
    except anyio.WouldBlock:
        log.warning("Job socket send buffer full — dropping a message",
                    extra={"type": message.get("type")})
    except anyio.BrokenResourceError:
        pass  # the connection is already going away


async def _watch(registry: JobRegistry, sink: MemoryObjectSendStream[Message]) -> None:
    """Emit the snapshot, then one `job` message per changed job, forever.

    Only *changed* jobs go on the wire. The registry's revision is registry-wide,
    so each wake hands back every job; remembering the last payload per id keeps a
    30-frame capture from rebroadcasting every other job on every frame.

    `abandon_on_cancel` matters: the wait is parked in a worker thread for up to
    WS_TICK_S, and a disconnect must close the socket now rather than after the
    tick. The abandoned thread returns a snapshot nobody reads and exits.
    """
    revision = -1  # no real revision matches, so the first wake returns at once
    sent: dict[str, Message] | None = None

    while True:
        revision, jobs = await anyio.to_thread.run_sync(
            registry.wait_any, revision, WS_TICK_S, abandon_on_cancel=True)
        payloads = {job.id: job.model_dump(mode="json") for job in jobs}

        if sent is None:
            _push(sink, {"type": "snapshot", "jobs": list(payloads.values())})
            sent = payloads
            continue

        changed = [job_id for job_id, job in payloads.items() if sent.get(job_id) != job]
        if not changed:
            # wait_any returned on its timeout with nothing new. A heartbeat
            # proves the socket is alive without re-sending unchanged state.
            _push(sink, {"type": "ping"})
            continue
        for job_id in changed:
            _push(sink, {"type": "job", "job": payloads[job_id]})
        sent = payloads


async def _receive(
    websocket: WebSocket,
    registry: JobRegistry,
    sink: MemoryObjectSendStream[Message],
) -> None:
    """Apply `confirm`/`cancel` commands until the client goes away.

    Returns (rather than raising) on disconnect: it is the task whose completion
    tears the connection down, so a closed tab is an ordinary exit, not an error.
    """
    while True:
        try:
            message = await websocket.receive_json()
        except (WebSocketDisconnect, anyio.ClosedResourceError):
            return
        except ValueError:
            _push(sink, {"type": "error", "message": "expected JSON"})
            continue

        if not isinstance(message, dict):
            _push(sink, {"type": "error", "message": "expected a JSON object"})
            continue

        command = message.get("type")
        if command == "pong":
            continue  # heartbeat answer; nothing to do but note the client lives
        if command not in ("confirm", "cancel"):
            _push(sink, {"type": "error", "command": command,
                         "message": f"unknown command {command!r}"})
            continue

        job_id = message.get("job_id")
        if not isinstance(job_id, str):
            _push(sink, {"type": "error", "command": command,
                         "message": "job_id is required"})
            continue

        action = registry.confirm if command == "confirm" else registry.cancel
        try:
            job = await anyio.to_thread.run_sync(action, job_id)
        except JobNotFoundError as exc:
            _push(sink, {"type": "error", "command": command, "job_id": job_id,
                         "message": str(exc)})
            continue
        # The watcher broadcasts the state change to every client; the ack tells
        # *this* client its command landed, which is what a button needs to know.
        _push(sink, {"type": "ack", "command": command, "job": job.model_dump(mode="json")})


async def _send(websocket: WebSocket, source: MemoryObjectReceiveStream[Message]) -> None:
    """Drain the outbound queue. The only task that writes to the socket.

    Funnelling both the watcher and the command acks through one sender keeps two
    tasks from interleaving frames on the same connection.
    """
    async for message in source:
        try:
            await websocket.send_json(message)
        except (WebSocketDisconnect, RuntimeError, anyio.BrokenResourceError):
            return  # client vanished; _receive notices too and ends the socket


@router.websocket("/api/ws")
async def job_socket(
    websocket: WebSocket,
    registry: JobRegistry = Depends(get_ws_registry),
) -> None:
    """Every job update out, `confirm`/`cancel` back in, on one connection."""
    await websocket.accept()
    send_stream, receive_stream = anyio.create_memory_object_stream[Message](SEND_BUFFER)

    async with send_stream, receive_stream, anyio.create_task_group() as tg:
        tg.start_soon(_send, websocket, receive_stream)
        tg.start_soon(_watch, registry, send_stream)
        await _receive(websocket, registry, send_stream)
        # _receive returning *is* the disconnect. Cancelling stops the watcher
        # with the connection, so a closed tab does not leak a parked thread.
        tg.cancel_scope.cancel()
