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

**The handshake is checked before it is accepted.** WebSockets are exempt from
the same-origin policy, so a foreign page gets a working socket unless the server
refuses one; `origin_allowed` below is the check and the reasoning.

The registry is thread-based (`threading.Condition`) and this endpoint is async,
so every registry call that can block is bridged with `anyio.to_thread.run_sync`.
Blocking the event loop here would stall every other request, the MJPEG stream
included — the exact class of failure this module exists to remove.
"""
from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlsplit

import anyio
import anyio.to_thread
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from camera_orchestrator.interfaces.api.deps import get_ws_registry
from camera_orchestrator.interfaces.api.jobs import (
    JobNotFoundError,
    JobPromptMismatchError,
    JobRegistry,
)
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

# Close code 1008 ("policy violation") — the right frame for a connection that
# was well-formed but is not allowed, as opposed to 1003 (bad data) or 1011.
WS_POLICY_VIOLATION = 1008

# Hostnames that are loopback but are not IP literals.
_LOOPBACK_NAMES = {"localhost"}

# Default port per scheme, for comparing an Origin that omitted one.
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _is_loopback(hostname: str) -> bool:
    """True for 'localhost' and any address in a loopback range (127/8, ::1)."""
    if hostname in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _authority(host_header: str, scheme: str) -> tuple[str, int] | None:
    """Split a `Host` header into (hostname, port), defaulting the port by scheme.

    Parsed as the authority of a URL rather than split on ':' so an IPv6 literal
    ('[::1]:8000') survives.
    """
    parsed = urlsplit(f"//{host_header}")
    try:
        hostname, port = parsed.hostname, parsed.port
    except ValueError:
        return None  # a non-numeric port: not a Host we can compare against
    if hostname is None:
        return None
    return hostname, port if port is not None else _DEFAULT_PORTS[scheme]


def origin_allowed(origin: str | None, host_header: str | None) -> bool:
    """Whether a WebSocket handshake carrying `origin` may be accepted.

    WebSockets are exempt from the same-origin policy: the browser sends the
    handshake and hands the page a working socket regardless of where the page
    came from. Without this check any site the user visits could read the job
    snapshot — session paths, filenames, the target's RA/Dec — and send `cancel`
    on a running sequence or `confirm` to skip a lens-cap prompt, ruining a
    calibration set silently.

    Three cases:

    - **No Origin at all** → allowed. Browsers always send one on a WebSocket
      handshake, so an absent header means a non-browser client (curl, a script,
      a future native app), which is not the adversary here and has no ambient
      authority to abuse.
    - **A loopback origin** → allowed. Another page served from this machine is
      already inside the trust boundary of a single-user tool.
    - **Anything else** → must match the `Host` the request actually arrived on,
      compared as scheme + hostname + port and not as a substring (so
      `http://127.0.0.1.evil.example` is not 'close enough'). Matching on Host
      rather than on a configured name is what keeps the phone working: with
      `--host 0.0.0.0` the page is served from the LAN IP and its Origin is that
      same LAN IP, so the two agree without anything being configured.
    """
    if origin is None:
        return True
    parsed = urlsplit(origin)
    if parsed.scheme not in _DEFAULT_PORTS or not parsed.hostname:
        return False  # 'null', 'file://' and other opaque origins
    if _is_loopback(parsed.hostname):
        return True
    if not host_header:
        return False
    source = _authority(parsed.netloc, parsed.scheme)
    target = _authority(host_header, parsed.scheme)
    return source is not None and source == target


def _push(sink: MemoryObjectSendStream[Message], message: Message) -> bool:
    """Queue an outbound message; never block the producer. True if it went out.

    A full buffer or a half-closed connection drops the message rather than
    stalling the watcher. The caller **must** act on a False: a drop is only
    harmless while another update for that job is still to come, and a job's
    terminal frame is by definition the last one. Recording a dropped job as
    sent is what left the UI showing `running` for a job that had finished.
    """
    try:
        sink.send_nowait(message)
        return True
    except anyio.WouldBlock:
        log.warning("Job socket send buffer full — dropping a message",
                    extra={"type": message.get("type")})
        return False
    except anyio.BrokenResourceError:
        return False  # the connection is already going away


async def _watch(registry: JobRegistry, sink: MemoryObjectSendStream[Message]) -> None:
    """Emit the snapshot, then one `job` message per changed job, forever.

    Only *changed* jobs go on the wire. The registry's revision is registry-wide,
    so each wake hands back every job; remembering the last payload per id keeps a
    30-frame capture from rebroadcasting every other job on every frame.

    A drop resets the cursor. Only *delivered* payloads are remembered as sent,
    and any drop sets `sent` back to None so the next wake — a real change or the
    heartbeat tick, whichever comes first — re-emits the full snapshot. Without
    that, a dropped **terminal** frame was never resent (there is no next update
    for a finished job) and the UI showed `running` forever.

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
            if _push(sink, {"type": "snapshot", "jobs": list(payloads.values())}):
                sent = payloads
            continue

        changed = [job_id for job_id, job in payloads.items() if sent.get(job_id) != job]
        if not changed:
            # wait_any returned on its timeout with nothing new. A heartbeat
            # proves the socket is alive without re-sending unchanged state.
            _push(sink, {"type": "ping"})
            continue
        delivered = {
            job_id for job_id in changed
            if _push(sink, {"type": "job", "job": payloads[job_id]})
        }
        if len(delivered) != len(changed):
            sent = None  # something was dropped; resync wholesale on the next wake
            continue
        sent.update({job_id: payloads[job_id] for job_id in delivered})


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

        # Both are a mutex acquire plus an Event.set — microseconds. They run
        # inline rather than through anyio.to_thread: the default limiter is 40
        # threads and a wedged camera is exactly when they are all parked, so
        # bridging these would put confirm and cancel behind the very resource
        # the operator is trying to free.
        try:
            if command == "confirm":
                token = message.get("token")
                job = registry.confirm(job_id, token if isinstance(token, str) else None)
            else:
                job = registry.cancel(job_id)
        except (JobNotFoundError, JobPromptMismatchError) as exc:
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
    origin = websocket.headers.get("origin")
    if not origin_allowed(origin, websocket.headers.get("host")):
        # Closed before accept, so the handshake never completes and the page
        # gets a socket that errors instead of one that works.
        log.warning("Rejected a job socket from a foreign origin", extra={"origin": origin})
        await websocket.close(code=WS_POLICY_VIOLATION)
        return
    await websocket.accept()
    send_stream, receive_stream = anyio.create_memory_object_stream[Message](SEND_BUFFER)

    async with send_stream, receive_stream, anyio.create_task_group() as tg:
        tg.start_soon(_send, websocket, receive_stream)
        tg.start_soon(_watch, registry, send_stream)
        await _receive(websocket, registry, send_stream)
        # _receive returning *is* the disconnect. Cancelling stops the watcher
        # with the connection, so a closed tab does not leak a parked thread.
        tg.cancel_scope.cancel()
