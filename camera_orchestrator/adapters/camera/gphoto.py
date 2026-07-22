"""GphotoCamera — libgphoto2 backend via python-gphoto2.

Wraps a live PTP session with the camera. Releases the gvfs auto-mount before
claiming the device (same conflict grab.py handles), then talks to the camera
through the Python binding rather than shelling out to the gphoto2 CLI.

Exposes atomic operations only (trigger, bulb, wait_for_new_files, list_files,
download, set_capture_target). Multi-step workflows — capture-and-download,
sequences — are composed in the service layer.
"""
from __future__ import annotations

import time
from pathlib import Path

import gphoto2 as gp

from camera_orchestrator.domain.errors import CameraError
from camera_orchestrator.domain.models.camera import CameraFile, CameraStatus, CaptureSettings
from camera_orchestrator.domain.ports.camera import Camera
from camera_orchestrator.log import get_logger

from .gvfs import unmount_gvfs

log = get_logger("camera_orchestrator.camera")

# Bodies that lock the physical shutter once a PTP session is open — grab-only.
_CAPTURE_LOCKED = frozenset({"m50", "eos m50"})

# Semantic image format -> Canon EOS gphoto2 choice string.
_FORMAT_CHOICES = {"raw": "RAW", "jpeg": "L", "both": "RAW + L"}

# How long to wait for the first file of a shot to arrive (covers write time).
_FIRST_FILE_TIMEOUT_MS = 15000
# Once one file has arrived, how long to wait for siblings (e.g. the RAW+JPEG
# pair). Short, so we don't burn the full timeout after the last file.
_SETTLE_TIMEOUT_MS = 2000


class GphotoCamera(Camera):
    """Camera backend backed by libgphoto2 (python-gphoto2)."""

    def __init__(self, *, release_gvfs: bool = True):
        if release_gvfs:
            unmount_gvfs()
        try:
            self._cam = gp.Camera()
            self._cam.init()
        except gp.GPhoto2Error as exc:
            raise CameraError(f"could not open camera: {exc}") from exc
        self._model = self._get("cameramodel") or self._get("model") or "unknown"

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        try:
            self._cam.exit()
        except gp.GPhoto2Error:
            pass

    # -- config helpers ----------------------------------------------------

    def _get(self, name: str) -> str | None:
        try:
            return str(self._cam.get_config().get_child_by_name(name).get_value())
        except gp.GPhoto2Error:
            return None

    def _set(self, name: str, value: str) -> None:
        try:
            cfg = self._cam.get_config()
            cfg.get_child_by_name(name).set_value(value)
            self._cam.set_config(cfg)
        except gp.GPhoto2Error as exc:
            raise CameraError(f"could not set {name}={value}: {exc}") from exc

    def _require_capture(self) -> None:
        if not self.can_capture:
            raise CameraError(
                f"{self._model} does not support remote capture — use grab instead"
            )

    # -- status / settings -------------------------------------------------

    @property
    def can_capture(self) -> bool:
        m = self._model.lower()
        return not any(locked in m for locked in _CAPTURE_LOCKED)

    def status(self) -> CameraStatus:
        def as_int(v: str | None) -> int | None:
            try:
                return int(v) if v is not None else None
            except ValueError:
                return None

        return CameraStatus(
            model=self._model,
            lens=self._get("lensname"),
            battery=self._get("batterylevel"),
            shutter_count=as_int(self._get("shuttercounter")),
            free_shots=as_int(self._get("availableshots")),
            can_capture=self.can_capture,
        )

    def apply(self, settings: CaptureSettings) -> None:
        if settings.exposure_mode is not None:
            self._set("autoexposuremode", settings.exposure_mode)
        if settings.image_format is not None:
            choice = _FORMAT_CHOICES.get(settings.image_format)
            if choice is None:
                raise CameraError(f"unsupported image_format: {settings.image_format}")
            self._set("imageformat", choice)
        if settings.iso is not None:
            self._set("iso", settings.iso)
        if settings.aperture is not None:
            self._set("aperture", settings.aperture)
        if settings.shutter is not None:
            self._set("shutterspeed", settings.shutter)

    def set_capture_target(self, to_card: bool) -> None:
        # Card-only is required when not downloading, else a shot to Internal RAM
        # is lost. When downloading we leave the target as the camera has it.
        if to_card:
            self._set("capturetarget", "Memory card")

    # -- atomic capture ----------------------------------------------------

    def trigger(self) -> None:
        """Fire the shutter, retrying briefly while the camera reports busy.

        trigger_capture() is asynchronous — if the previous frame is still
        exposing or writing, the camera rejects the next trigger as busy. We let
        that be the pacing mechanism (back off and retry) rather than imposing a
        fixed wait.
        """
        self._require_capture()
        deadline_ms = _FIRST_FILE_TIMEOUT_MS
        waited = 0
        while True:
            try:
                self._cam.trigger_capture()
                return
            except gp.GPhoto2Error as exc:
                if exc.code == gp.GP_ERROR_CAMERA_BUSY and waited < deadline_ms:
                    time.sleep(0.1)
                    waited += 100
                    continue
                raise CameraError(f"capture failed: {exc}") from exc

    def bulb(self, seconds: float) -> None:
        """Hold the shutter open for `seconds`, then close it."""
        self._require_capture()
        self._set("autoexposuremode", "Bulb")
        self._set("bulb", "1")
        try:
            time.sleep(seconds)
        finally:
            self._set("bulb", "0")

    # -- atomic file operations -------------------------------------------

    def wait_for_new_files(self, timeout_ms: int | None = None) -> list[CameraFile]:
        """Return the camera-side files produced since the last trigger.

        A RAW+JPEG body emits one GP_EVENT_FILE_ADDED per file. Waits longer for
        the first file (write time), then only briefly for siblings. Does not
        download. Returns [] if nothing arrives before the timeout.
        """
        found: list[CameraFile] = []
        timeout = timeout_ms if timeout_ms is not None else _FIRST_FILE_TIMEOUT_MS
        while True:
            try:
                event_type, data = self._cam.wait_for_event(timeout)
            except gp.GPhoto2Error as exc:
                raise CameraError(f"waiting for capture failed: {exc}") from exc
            if event_type == gp.GP_EVENT_FILE_ADDED:
                found.append(CameraFile(data.folder, data.name))
                timeout = _SETTLE_TIMEOUT_MS
            elif event_type == gp.GP_EVENT_TIMEOUT:
                break
        return found

    def list_files(self, folder: str = "/") -> list[CameraFile]:
        """Recursively list every file currently on the camera's storage."""
        out: list[CameraFile] = []
        for name, _ in self._cam.folder_list_files(folder):
            out.append(CameraFile(folder, name))
        for name, _ in self._cam.folder_list_folders(folder):
            sub = folder.rstrip("/") + "/" + name
            out.extend(self.list_files(sub))
        return out

    def download(self, ref: CameraFile, out_dir: Path) -> Path:
        """Download one camera-side file to out_dir and return its local path."""
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / ref.name
        try:
            cam_file = self._cam.file_get(ref.folder, ref.name, gp.GP_FILE_TYPE_NORMAL)
            cam_file.save(str(dest))
        except gp.GPhoto2Error as exc:
            raise CameraError(f"download of {ref.name} failed: {exc}") from exc
        log.info("Saved", extra={"dest": str(dest)})
        return dest
