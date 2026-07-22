"""Camera port — the abstract interface every camera backend implements.

Atomic operations only. Higher-level workflows (capture-and-download, sequences)
are composed in the application layer, not baked into the driver.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..models.camera import CameraFile, CameraStatus, CaptureSettings


class Camera(ABC):
    """Abstract base for camera backends — atomic operations.

    Bodies differ in capability: only some support remote shutter release
    (can_capture). Callers should check can_capture before triggering.
    """

    def close(self) -> None:
        """Release the camera. Override if the backend holds resources."""

    def __enter__(self) -> "Camera":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    @abstractmethod
    def can_capture(self) -> bool:
        """True if this body supports remote shutter release over USB.

        False for bodies that lock the physical shutter once a PTP session is
        open (e.g. Canon M50 II) — those are list/download only.
        """

    @abstractmethod
    def status(self) -> CameraStatus:
        """Return a read-only snapshot of the camera's current state."""

    @abstractmethod
    def apply(self, settings: CaptureSettings) -> None:
        """Apply exposure settings only (iso/shutter/aperture/format/mode).

        None-valued fields are left untouched. Does not touch capture target —
        that is a separate policy decision (see set_capture_target).
        """

    @abstractmethod
    def set_capture_target(self, to_card: bool) -> None:
        """Route future shots to the memory card (True) or leave for download (False).

        Maps to a single capturetarget config write. Card-only avoids the USB
        transfer; the service decides which a workflow needs.
        """

    # -- atomic capture ----------------------------------------------------

    @abstractmethod
    def trigger(self) -> None:
        """Fire the shutter at the current shutter speed (asynchronous).

        Returns immediately; the frame lands on the card. Discover the produced
        files via wait_for_new_files(). Raises CameraError if the body cannot
        capture or the shutter fails.
        """

    @abstractmethod
    def bulb(self, seconds: float) -> None:
        """Hold the shutter open for `seconds`, then close it (asynchronous).

        Requires bulb-capable hardware; raises CameraError otherwise.
        """

    # -- atomic file operations -------------------------------------------

    @abstractmethod
    def wait_for_new_files(self, timeout_ms: int | None = None) -> list[CameraFile]:
        """Return the camera-side files produced since the last trigger.

        Drains the camera's event queue; a RAW+JPEG shot yields two entries.
        Does not download. Returns an empty list if nothing arrives in time.
        """

    @abstractmethod
    def list_files(self) -> list[CameraFile]:
        """Return every file currently on the camera's storage."""

    @abstractmethod
    def download(self, ref: CameraFile, out_dir: Path) -> Path:
        """Download one camera-side file to out_dir and return its local path."""
