"""Camera-domain value objects — capture settings, status, request/result."""
from __future__ import annotations

from typing import Literal, NamedTuple, Optional

from pydantic import BaseModel, Field

# Semantic image-format choices. Adapters map these to camera-specific strings
# (e.g. GphotoCamera: raw->'RAW', jpeg->'L', both->'RAW + L').
ImageFormat = Literal["raw", "jpeg", "both"]


class CameraFile(NamedTuple):
    """A file as it exists on the camera (not yet downloaded)."""

    folder: str
    name: str


class CaptureSettings(BaseModel):
    """Exposure settings applied to the camera before a capture.

    iso/shutter/aperture are gphoto2 choice strings passed through as-is
    (e.g. iso="800"). image_format is a semantic enum the driver translates.
    None leaves that setting untouched.
    """

    iso: Optional[str] = Field(default=None, description="ISO sensitivity as a gphoto2 choice string, e.g. '800'.")
    shutter: Optional[str] = Field(default=None, description="Shutter speed as a gphoto2 choice string, e.g. '2' or '1/60'. Ignored for bulb captures.")
    aperture: Optional[str] = Field(default=None, description="Aperture f-number as a gphoto2 choice string, e.g. '4'.")
    image_format: Optional[ImageFormat] = Field(default=None, description="Semantic image format: 'raw', 'jpeg', or 'both'. None keeps the camera's current setting; the driver maps it to a camera-specific choice.")
    exposure_mode: Optional[str] = Field(default=None, description="Canon auto-exposure mode, e.g. 'Manual' or 'Bulb'. Set automatically to 'Bulb' for bulb captures.")


class CameraStatus(BaseModel):
    """Read-only snapshot of the connected camera's state."""

    model: str = Field(description="Camera model name reported over PTP.")
    lens: Optional[str] = Field(default=None, description="Attached lens name, if the camera reports one.")
    battery: Optional[str] = Field(default=None, description="Battery level, e.g. '75%'.")
    shutter_count: Optional[int] = Field(default=None, description="Total shutter actuations, if the camera exposes it.")
    free_shots: Optional[int] = Field(default=None, description="Estimated remaining shots on the storage card.")
    can_capture: bool = Field(description="Whether this body supports remote shutter release over USB (False for bodies that lock the shutter in a PTP session, e.g. Canon M50 II).")


class CaptureRequest(BaseModel):
    """Input to the capture service — built from CLI flags or an API request.

    UI-agnostic: the CLI and a future FastAPI endpoint both construct one of
    these and hand it to CaptureService. Flat by design so it maps cleanly to
    both argparse flags and a JSON body.
    """

    out_dir: str = Field(description="Directory to download frames into (unused when download is False).")
    iso: Optional[str] = Field(default=None, description="ISO setting, e.g. '800'. None leaves it unchanged.")
    shutter: Optional[str] = Field(default=None, description="Shutter speed, e.g. '2' or '1/60'. Ignored when bulb_seconds is set.")
    aperture: Optional[str] = Field(default=None, description="Aperture f-number, e.g. '4'. None leaves it unchanged.")
    image_format: Optional[ImageFormat] = Field(default=None, description="'raw', 'jpeg', or 'both'. None keeps the camera's current setting.")
    bulb_seconds: Optional[float] = Field(default=None, description="Bulb exposure length in seconds. Overrides shutter.")
    count: int = Field(default=1, ge=1, description="Number of frames to capture.")
    kind: Literal["light", "dark", "bias"] = Field(default="light", description="Frame type label, for logging and downstream sorting.")
    download: bool = Field(default=False, description="Transfer each frame over USB (True) or shoot to the card only for faster cadence (False, the default).")

    def to_settings(self) -> "CaptureSettings":
        """Build the camera-facing exposure settings for this request.

        Bulb mode is selected automatically when bulb_seconds is set.
        """
        return CaptureSettings(
            iso=self.iso,
            shutter=self.shutter,
            aperture=self.aperture,
            image_format=self.image_format,
            exposure_mode="Bulb" if self.bulb_seconds else "Manual",
        )


class CaptureResult(BaseModel):
    """Outcome of a capture service call — serialisable for CLI or API."""

    status: CameraStatus = Field(description="Camera status read at the start of the call.")
    frames_captured: int = Field(default=0, description="Number of frames triggered.")
    frames: list[str] = Field(default_factory=list, description="Local paths of downloaded files (empty when download is False).")
    download: bool = Field(default=True, description="Whether frames were transferred to the host.")
