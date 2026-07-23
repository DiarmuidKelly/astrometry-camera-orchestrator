"""Session service — a multi-phase imaging run composed from CaptureService.

Runs the requested phases (lights / darks / bias) in order, each as a capture
sequence. Calls a `before_phase` hook before every phase so the interface can
prompt the human (cover/uncover the lens); the service itself stays UI-agnostic.
"""
from __future__ import annotations

from typing import Callable

from camera_orchestrator.application.capture_service import CaptureService
from camera_orchestrator.domain.models.camera import CaptureRequest
from camera_orchestrator.domain.models.session import PhaseKind, SessionRequest, SessionResult
from camera_orchestrator.log import get_logger

log = get_logger("camera_orchestrator.session")

# Bias frames want the shortest practical exposure (read noise only).
BIAS_SHUTTER = "1/4000"

# Invoked before each phase with its kind — the interface prompts/waits here.
PhaseHook = Callable[[PhaseKind], None]


class SessionService:
    """Runs a lights/darks/bias imaging session."""

    def __init__(self, capture: CaptureService):
        self._capture = capture

    def run(self, request: SessionRequest, before_phase: PhaseHook | None = None) -> SessionResult:
        counts_wanted: dict[PhaseKind, int] = {
            "light": request.lights, "dark": request.darks, "bias": request.bias,
        }
        counts: dict[str, int] = {}
        frames: list[str] = []

        for kind in request.order:
            n = counts_wanted.get(kind, 0)
            if n <= 0:
                continue
            if before_phase is not None:
                before_phase(kind)
            log.info("Session phase", extra={"kind": kind, "count": n})

            cap_req = self._phase_request(request, kind, n)
            result = (self._capture.capture_and_download(cap_req)
                      if request.download else self._capture.capture_to_card(cap_req))
            counts[kind] = result.frames_captured
            frames.extend(result.frames)

        return SessionResult(counts=counts, frames=frames, download=request.download)

    def _phase_request(self, request: SessionRequest, kind: PhaseKind, count: int) -> CaptureRequest:
        # Bias: fastest shutter, no bulb. Lights/darks share the requested exposure.
        if kind == "bias":
            shutter: str | None = BIAS_SHUTTER
            bulb_seconds = None
        else:
            shutter = request.shutter
            bulb_seconds = request.bulb_seconds
        return CaptureRequest(
            out_dir=request.out_dir,
            iso=request.iso,
            shutter=shutter,
            aperture=request.aperture,
            bulb_seconds=bulb_seconds,
            count=count,
            kind=kind,
            download=request.download,
        )
