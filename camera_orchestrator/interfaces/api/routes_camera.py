"""Camera routes — status, live view, reconnect.

All three go through the shared `CameraSession`, never through a fresh
connection: libgphoto2 claims the USB device exclusively, so a second claim while
the stream is running fails outright.

The routes are `async def` but every camera call is pushed to a worker thread.
python-gphoto2 is blocking C, and a preview grab is ~100 ms — running one on the
event loop would stall every other request for the life of the stream.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import anyio.to_thread
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse

from camera_orchestrator.application.camera_session import CameraSession
from camera_orchestrator.domain.errors import CameraBusyError, CameraError
from camera_orchestrator.interfaces.api.jobs import JobRegistry
from camera_orchestrator.domain.models.camera import CameraStatus
from camera_orchestrator.interfaces.api.deps import get_camera_session, get_registry
from camera_orchestrator.interfaces.api.models import CameraStatusResponse, OkResponse
from camera_orchestrator.log import get_logger

log = get_logger("camera_orchestrator.api.camera")

router = APIRouter(prefix="/api/camera", tags=["camera"])

# Live view pacing. ~12 fps is plenty for focusing and leaves the session lock
# free between frames, so a capture waits one frame rather than the whole stream.
FRAME_INTERVAL_S = 1 / 12

# Short on purpose: a capture holding the session must surface as "skip this
# frame", not as a stalled response.
STREAM_PREVIEW_TIMEOUT_S = 0.5

# The one-shot fallback can afford to wait a little longer — it has no next frame.
SINGLE_PREVIEW_TIMEOUT_S = 2.0

# A status read is quick, but it queues behind a running capture; bounded so the
# UI's status poll never hangs.
STATUS_TIMEOUT_S = 2.0

MJPEG_BOUNDARY = "frame"

_NO_STORE = {"Cache-Control": "no-store, no-cache, must-revalidate"}


def _read_status(session: CameraSession) -> CameraStatus:
    """Blocking status read against the shared session (runs in a thread)."""
    with session.acquire(timeout=STATUS_TIMEOUT_S) as camera:
        return camera.status()


@router.get("/status", response_model=CameraStatusResponse)
async def camera_status(
    session: CameraSession = Depends(get_camera_session),
) -> CameraStatusResponse:
    """Camera status, or why it could not be read.

    Deliberately never raises: "no camera plugged in yet" is the normal state
    when the UI first loads, and a 5xx there would make the whole page look
    broken. Every failure comes back as connected=False plus a message.
    """
    try:
        status = await anyio.to_thread.run_sync(_read_status, session)
    except Exception as exc:  # includes CameraError, CameraBusyError, driver errors
        return CameraStatusResponse(
            connected=False, session_open=session.is_open, error=str(exc) or repr(exc))
    return CameraStatusResponse(
        connected=True, session_open=session.is_open, camera=status)


def _part(frame: bytes) -> bytes:
    """One multipart part wrapping a JPEG frame."""
    return (
        f"--{MJPEG_BOUNDARY}\r\n"
        f"Content-Type: image/jpeg\r\n"
        f"Content-Length: {len(frame)}\r\n\r\n"
    ).encode() + frame + b"\r\n"


async def _mjpeg_frames(
    request: Request,
    session: CameraSession,
    first: bytes | None = None,
    registry: JobRegistry | None = None,
) -> AsyncIterator[bytes]:
    """Yield multipart JPEG parts until the client leaves or live view stops.

    Two failure modes, handled differently:
    - CameraBusyError: a capture holds the connection. Skip the frame and keep
      going — the stream must survive a capture, that is the whole point.
    - CameraError: live view is off on the body (or the camera vanished). End the
      stream cleanly so the browser's <img> fires onerror and the UI can say
      "enable live view on the camera".
    """
    if first is not None:
        yield _part(first)
        await asyncio.sleep(FRAME_INTERVAL_S)
    while not await request.is_disconnected():
        if registry is not None and registry.camera_job_holding_device() is not None:
            # A capture/align/sequence owns the body. Don't even reach for the
            # device: between phases and during the card-listing reconnect poll
            # the session lock is briefly free, and a preview slipping in there
            # would re-open live view on the camera mid-run.
            await asyncio.sleep(FRAME_INTERVAL_S)
            continue
        try:
            frame = await anyio.to_thread.run_sync(
                session.preview, STREAM_PREVIEW_TIMEOUT_S)
        except CameraBusyError:
            await asyncio.sleep(FRAME_INTERVAL_S)
            continue
        except CameraError as exc:
            log.info("Live view stream ended", extra={"reason": str(exc)})
            return
        yield _part(frame)
        await asyncio.sleep(FRAME_INTERVAL_S)


@router.get("/liveview.mjpg")
async def liveview(
    request: Request,
    session: CameraSession = Depends(get_camera_session),
    registry: JobRegistry = Depends(get_registry),
) -> StreamingResponse:
    """Motion-JPEG live view — drop straight into `<img src=...>`.

    The first frame is grabbed *before* the response starts so a camera that
    cannot preview (unplugged, or live view switched off on the body) comes back
    as a 409 rather than a 200 with an empty body. A browser silently leaves an
    empty-bodied `<img>` pending forever — no `error` event — which left the UI
    stuck on "Connecting…" with no camera attached. A status code fires `onerror`
    and the panel can say "enable live view on the body".

    CameraBusyError is deliberately not fatal: a capture holding the connection
    is transient, and the stream is supposed to survive one.
    """
    first: bytes | None = None
    if registry.camera_job_holding_device() is None:
        try:
            first = await anyio.to_thread.run_sync(
                session.preview, SINGLE_PREVIEW_TIMEOUT_S)
        except CameraBusyError:
            pass  # a capture owns the camera; open the stream and pick up after it
    return StreamingResponse(
        _mjpeg_frames(request, session, first, registry),
        media_type=f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
        headers=_NO_STORE,
    )


@router.get("/frame.jpg")
async def single_frame(
    session: CameraSession = Depends(get_camera_session),
    registry: JobRegistry = Depends(get_registry),
) -> Response:
    """One live-view frame — the polling fallback where MJPEG is awkward."""
    holder = registry.camera_job_holding_device()
    if holder is not None:
        raise CameraBusyError(
            f"camera is busy with {holder.kind} job {holder.id} — live view is "
            "locked out until it finishes"
        )
    frame = await anyio.to_thread.run_sync(session.preview, SINGLE_PREVIEW_TIMEOUT_S)
    return Response(content=frame, media_type="image/jpeg", headers=_NO_STORE)


@router.post("/reconnect", response_model=OkResponse)
async def reconnect(
    session: CameraSession = Depends(get_camera_session),
) -> OkResponse:
    """Drop the USB claim and open a fresh one.

    The escape hatch for the two states the hardware gets into: a stale session
    after the body auto-powered off, and a cached directory listing that will not
    show new card files.
    """
    await anyio.to_thread.run_sync(session.reconnect)
    return OkResponse(ok=True)
