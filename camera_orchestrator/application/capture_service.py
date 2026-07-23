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

import time
from pathlib import Path
from typing import Callable

from camera_orchestrator.domain.errors import CameraError
from camera_orchestrator.domain.models.camera import (
    CameraFile,
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

# Filename extensions for each `select` mode (a shot's files are named by the camera).
_SELECT_EXTS = {"jpeg": (".jpg", ".jpeg"), "cr2": (".cr2",)}


def _select_files(refs: list[CameraFile], mode: str) -> list[CameraFile]:
    """Return the subset of a shot's files to download for the given select mode."""
    if mode == "all":
        return refs
    exts = _SELECT_EXTS[mode]
    return [r for r in refs if r.name.lower().endswith(exts)]


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
        self,
        request: CaptureRequest,
        on_frame: FrameCallback | None = None,
        record_files: bool = False,
    ) -> CaptureResult:
        """Fire a sequence to the card only — no USB transfer (faster cadence).

        record_files: record the camera-side filenames this run produced in
        CaptureResult.card_frames, by diffing a card listing taken before firing
        against one taken from a fresh session afterwards (a reconnect is needed
        for the writes to appear). Lets a session log which card files it
        produced. Off by default so plain capture is unaffected.
        """
        return self._run(request, download=False, on_frame=on_frame, record_files=record_files)

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
        record_files: bool = False,
    ) -> CaptureResult:
        record_card = record_files and not download
        before_names: set[str] = set()

        with self._camera_factory() as camera:
            status = camera.status()
            if not camera.can_capture:
                raise CameraError(
                    f"{status.model} does not support remote capture — use grab instead"
                )
            camera.apply(request.to_settings())
            camera.set_capture_target(to_card=not download)
            if download:
                # Discard any backlog (e.g. from a prior card-only run) so
                # wait_for_new_files only sees the shots we fire below.
                camera.flush_events()
            if record_card:
                # Snapshot the card now — this freshly-opened session lists it
                # accurately. After firing we reopen for a fresh listing (below),
                # because the card writes aren't visible within this session.
                before_names = {f.name for f in camera.list_files()}

            out_dir = Path(request.out_dir)
            frames: list[Path] = []
            for i in range(1, request.count + 1):
                self._shoot(camera, request.bulb_seconds)
                this_frame: list[Path] = []
                if download:
                    refs = camera.wait_for_new_files()
                    if not refs:
                        raise CameraError("shot fired but no file arrived before timeout")
                    to_download = _select_files(refs, request.select)
                    if not to_download:
                        raise CameraError(
                            f"select={request.select!r} matched none of the shot's files: "
                            f"{[r.name for r in refs]}"
                        )
                    this_frame = [camera.download(ref, out_dir) for ref in to_download]
                    frames.extend(this_frame)
                log.info("Frame captured",
                         extra={"kind": request.kind, "index": i, "total": request.count,
                                "files": [p.name for p in this_frame]})
                if on_frame is not None:
                    on_frame(i, request.count, this_frame)

        # Card-only filename capture. Card writes emit FILE_ADDED events, but the
        # camera drops/coalesces them under rapid fire — unreliable for counting.
        # A directory listing is authoritative, but libgphoto2 caches it within a
        # session (so writes only appear after a reconnect) and the card's write
        # buffer flushes with a lag. So reopen fresh sessions and poll until the
        # expected number of new files has appeared.
        card_frames: list[str] = []
        if record_card:
            card_frames = self._await_new_card_files(before_names, request.count)

        return CaptureResult(
            status=status,
            frames_captured=request.count,
            frames=[str(p) for p in frames],
            card_frames=card_frames,
            download=download,
        )

    def _await_new_card_files(
        self,
        before_names: set[str],
        expected: int,
        attempts: int = 15,
        poll_s: float = 2.0,
    ) -> list[str]:
        """Poll fresh sessions until the card shows all `expected` new files.

        A reconnect is required for card writes to appear (the in-session listing
        is cached), and the write buffer flushes with a lag — so we reopen and
        re-list until the expected count is present, or attempts run out
        (returning best-effort so the run is still recorded).
        """
        new: list[str] = []
        for attempt in range(attempts):
            try:
                with self._camera_factory() as lister:
                    new = sorted(
                        f.name for f in lister.list_files() if f.name not in before_names
                    )
            except CameraError:
                pass  # camera briefly unavailable after close — retry
            else:
                if len(new) >= expected:
                    return new
            if attempt < attempts - 1:
                time.sleep(poll_s)
        log.warning("Card listing incomplete — some filenames not recorded",
                    extra={"expected": expected, "found": len(new)})
        return new

    @staticmethod
    def _shoot(camera: Camera, bulb_seconds: float | None) -> None:
        """One exposure: bulb if a duration is given, else a normal trigger."""
        if bulb_seconds:
            camera.bulb(bulb_seconds)
        else:
            camera.trigger()
