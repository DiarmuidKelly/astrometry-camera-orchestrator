"""Capture service — UI-agnostic orchestration behind the CLI and future API.

Composes the driver's atomic operations (trigger/bulb/wait_for_new_files/
download/set_capture_target) into higher-level workflows. Owns the camera
lifecycle and returns structured results. Knows nothing about argparse, stdout,
or HTTP — the CLI (cmd.py) and a future FastAPI endpoint both call the same
methods.

Errors surface as CameraError; callers decide how to present them (CLI exits,
an API maps to an HTTP status).
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from camera_orchestrator.domain.errors import CameraError
from camera_orchestrator.domain.models.camera import (
    CameraStatus,
    CaptureRequest,
    CaptureResult,
)
from camera_orchestrator.domain.ports.camera import Camera
from camera_orchestrator.log import get_logger

log = get_logger("camera_orchestrator.service")

# A zero-arg factory returning an open Camera. Injectable for tests / backends.
CameraFactory = Callable[[], Camera]

# Called after each frame: (index, total, downloaded_paths_this_frame).
FrameCallback = Callable[[int, int, list[Path]], None]


class CaptureService:
    """Runs status and capture workflows against a camera backend."""

    def __init__(self, camera_factory: CameraFactory):
        """Args:
            camera_factory: Zero-arg callable returning an open Camera. The
                composition root injects the concrete adapter (GphotoCamera);
                tests inject a mock. The service never imports an adapter.
        """
        self._camera_factory = camera_factory

    def status(self) -> CameraStatus:
        """Open the camera and return a status snapshot."""
        with self._camera_factory() as camera:
            return camera.status()

    def capture_to_card(
        self, request: CaptureRequest, on_frame: FrameCallback | None = None
    ) -> CaptureResult:
        """Fire a sequence to the card only — no USB transfer (faster cadence)."""
        return self._run(request, download=False, on_frame=on_frame)

    def capture_and_download(
        self, request: CaptureRequest, on_frame: FrameCallback | None = None
    ) -> CaptureResult:
        """Fire a sequence and download each frame's files to the host."""
        return self._run(request, download=True, on_frame=on_frame)

    # -- composition -------------------------------------------------------

    def _run(
        self,
        request: CaptureRequest,
        *,
        download: bool,
        on_frame: FrameCallback | None = None,
    ) -> CaptureResult:
        with self._camera_factory() as camera:
            status = camera.status()
            if not camera.can_capture:
                raise CameraError(
                    f"{status.model} does not support remote capture — use grab instead"
                )
            camera.apply(request.to_settings())
            camera.set_capture_target(to_card=not download)

            out_dir = Path(request.out_dir)
            frames: list[Path] = []
            for i in range(1, request.count + 1):
                self._shoot(camera, request.bulb_seconds)
                this_frame: list[Path] = []
                if download:
                    refs = camera.wait_for_new_files()
                    if not refs:
                        raise CameraError("shot fired but no file arrived before timeout")
                    this_frame = [camera.download(ref, out_dir) for ref in refs]
                    frames.extend(this_frame)
                log.info("Frame captured",
                         extra={"kind": request.kind, "index": i, "total": request.count,
                                "files": [p.name for p in this_frame]})
                if on_frame is not None:
                    on_frame(i, request.count, this_frame)

            return CaptureResult(
                status=status,
                frames_captured=request.count,
                frames=[str(p) for p in frames],
                download=download,
            )

    @staticmethod
    def _shoot(camera: Camera, bulb_seconds: float | None) -> None:
        """One exposure: bulb if a duration is given, else a normal trigger."""
        if bulb_seconds:
            camera.bulb(bulb_seconds)
        else:
            camera.trigger()
