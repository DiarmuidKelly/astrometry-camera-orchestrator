"""Sequence service — a multi-phase imaging run recorded into a session manifest.

Runs the requested phases (lights / darks / bias) in order, each as a capture
sequence, and records what each produced (settings, timing, filenames) into the
session's manifest. Calls a `before_phase` hook before every phase so the
interface can prompt the human (cover/uncover the lens); the service itself stays
UI-agnostic.

Two modes:
- **session** (session_dir set): appends phase records to session_dir's manifest
  and saves it.
- **loose** (session_dir=None): fires the phases and returns an unsaved manifest
  for logging — nothing is persisted.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from camera_orchestrator.application.capture_service import CaptureService
from camera_orchestrator.domain.models.camera import CaptureRequest
from camera_orchestrator.domain.models.session import (
    PhaseKind,
    PhaseRecord,
    SequenceRequest,
    SessionManifest,
)
from camera_orchestrator.domain.ports.session_manifest import SessionManifestRepository
from camera_orchestrator.log import get_logger

log = get_logger("camera_orchestrator.sequence")

# Bias frames want the shortest practical exposure (read noise only).
BIAS_SHUTTER = "1/4000"

# Invoked before each phase with its kind — the interface prompts/waits here.
PhaseHook = Callable[[PhaseKind], None]

# A clock returning the current UTC time. Injectable for deterministic tests.
Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SequenceService:
    """Runs a lights/darks/bias imaging sequence and records it in the manifest."""

    def __init__(
        self,
        capture: CaptureService,
        manifest_repo: SessionManifestRepository,
        clock: Clock = _utc_now,
    ):
        self._capture = capture
        self._repo = manifest_repo
        self._clock = clock

    def run(
        self,
        request: SequenceRequest,
        session_dir: str | None = None,
        before_phase: PhaseHook | None = None,
    ) -> SessionManifest:
        counts_wanted: dict[PhaseKind, int] = {
            "light": request.lights, "dark": request.darks, "bias": request.bias,
        }
        started = self._clock()
        records: list[PhaseRecord] = []

        for kind in request.order:
            n = counts_wanted.get(kind, 0)
            if n <= 0:
                continue
            if before_phase is not None:
                before_phase(kind)

            cap_req = self._phase_request(request, kind, n)
            phase_start = self._clock()
            if request.download:
                result = self._capture.capture_and_download(cap_req)
                files = [Path(p).name for p in result.frames]
            else:
                result = self._capture.capture_to_card(cap_req, record_files=True)
                files = result.card_frames
            phase_end = self._clock()

            log.info("Sequence phase", extra={"kind": kind, "count": result.frames_captured})
            records.append(PhaseRecord(
                kind=kind,
                count=result.frames_captured,
                iso=cap_req.iso,
                shutter=cap_req.shutter,
                aperture=cap_req.aperture,
                bulb_seconds=cap_req.bulb_seconds,
                started_at=phase_start,
                ended_at=phase_end,
                files=files,
            ))

        ended = self._clock()

        if session_dir is not None:
            manifest = self._repo.load(session_dir)
            if manifest is None:
                manifest = SessionManifest(
                    session_id=Path(session_dir).name,
                    name=request_name(session_dir),
                )
            manifest.download = request.download
            manifest.started_at = manifest.started_at or started
            manifest.ended_at = ended
            manifest.phases.extend(records)
            self._repo.save(manifest, session_dir)
            return manifest

        return SessionManifest(
            session_id="(loose)",
            download=request.download,
            started_at=started,
            ended_at=ended,
            phases=records,
        )

    def _phase_request(self, request: SequenceRequest, kind: PhaseKind, count: int) -> CaptureRequest:
        # Bias: fastest shutter, no bulb. Lights/darks share the requested exposure.
        # All phases shoot RAW — the science frames; JPEG is throwaway for stacking.
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
            image_format="raw",
            bulb_seconds=bulb_seconds,
            count=count,
            kind=kind,
            download=request.download,
        )


def request_name(session_dir: str) -> str | None:
    """Best-effort human label from a session folder name '<date>-<name>'.

    Returns the part after the leading YYYYMMDD- date prefix, or None if the
    folder is just a bare date/timestamp.
    """
    stem = Path(session_dir).name
    head, sep, tail = stem.partition("-")
    if sep and head.isdigit() and tail:
        return tail
    return None
