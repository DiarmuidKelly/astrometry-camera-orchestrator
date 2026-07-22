"""Pydantic models — single source of truth for all data structures.

Every data object that crosses a boundary (file I/O, solver, sidecar JSON)
is defined here. Import from this module, not from the individual modules.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field


class ImageExif(BaseModel):
    """EXIF metadata extracted from the original image file."""

    focal_mm: Optional[float] = Field(default=None, description="Lens focal length in millimetres as reported by the camera.")
    focal_mm_equiv: Optional[float] = Field(default=None, description="35mm-equivalent focal length, computed from focal_mm × crop factor. Null for full-frame sensors.")
    sensor_width_mm: Optional[float] = Field(default=None, description="Physical sensor width in millimetres, taken from config (not EXIF).")
    iso: Optional[int] = Field(default=None, description="ISO sensitivity setting used for the exposure.")
    shutter_sec: Optional[float] = Field(default=None, description="Shutter speed in seconds (e.g. 0.5 = 1/2 s).")
    aperture: Optional[float] = Field(default=None, description="Aperture f-number (e.g. 5.6 = f/5.6).")
    datetime: Optional[str] = Field(default=None, description="Camera capture time in ISO 8601 format (YYYY-MM-DDTHH:MM:SS). Local time, no timezone — EXIF does not carry one.")


class SolveHints(BaseModel):
    """Optional hints passed to the plate solver to narrow the search space."""

    scale_low: Optional[float] = Field(default=None, description="Lower bound of the image scale in arcseconds per pixel.")
    scale_high: Optional[float] = Field(default=None, description="Upper bound of the image scale in arcseconds per pixel.")
    ra_deg: Optional[float] = Field(default=None, description="Right ascension of the expected field centre in decimal degrees.")
    dec_deg: Optional[float] = Field(default=None, description="Declination of the expected field centre in decimal degrees.")
    radius_deg: Optional[float] = Field(default=None, description="Search radius around the RA/Dec hint in degrees. Wider = slower.")


class SolveResult(BaseModel):
    """Astrometric solution returned by the plate solver."""

    center_ra_deg: float = Field(description="Right ascension of the image centre in decimal degrees (J2000).")
    center_dec_deg: float = Field(description="Declination of the image centre in decimal degrees (J2000).")
    scale_arcsec_per_px: float = Field(description="Image scale in arcseconds per pixel derived from the WCS solution.")
    width_px: int = Field(description="Image width in pixels.")
    height_px: int = Field(description="Image height in pixels.")
    annotated_path: Optional[str] = Field(default=None, description="Filename of the NGC-annotated overlay image, if produced.")


class ObserverInfo(BaseModel):
    """Geographic location of the observer at capture time."""

    lat: Optional[float] = Field(default=None, description="Observer latitude in decimal degrees (positive = north).")
    lon: Optional[float] = Field(default=None, description="Observer longitude in decimal degrees (positive = east).")


class SolveRecord(BaseModel):
    """Canonical schema for a per-image sidecar JSON file.

    Written alongside each solved image as <stem>_solved.json.
    Contains the full provenance of the solve: original file, EXIF,
    astrometric solution, hints used, observer location, and solver mode.
    """

    original_file: str = Field(description="Filename (no path) of the source image.")
    solved_at: str = Field(description="UTC timestamp when the solve completed, in ISO 8601 format with timezone offset.")
    exif: ImageExif = Field(description="EXIF metadata extracted from the source image.")
    solved: bool = Field(description="True if a plate solution was found.")
    solve: Optional[SolveResult] = Field(default=None, description="Astrometric solution. Null if the solve failed.")
    hints_used: SolveHints = Field(description="Scale and position hints that were passed to the solver.")
    observer: ObserverInfo = Field(description="Geographic location of the observer.")
    solver_mode: Literal["fast", "accurate"] = Field(description="Solver mode used: fast (downsampled, fewer objects) or accurate (full resolution).")
    error: Optional[str] = Field(default=None, description="Error message if the solve failed. Null on success.")


class SolveJob(BaseModel):
    """Internal pipeline state for a single image being processed.

    Not serialised directly — call to_record() to get the sidecar-ready SolveRecord.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    path: Optional[str] = Field(default=None, description="Absolute path to the source image file.")
    exif: Optional[ImageExif] = Field(default=None, description="EXIF metadata, or None if extraction failed.")
    hints: SolveHints = Field(default_factory=lambda: SolveHints(), description="Hints passed to the solver for this image.")
    result: Optional[SolveResult] = Field(default=None, description="Solver output. None until solve completes or if it failed.")
    error: Optional[str] = Field(default=None, description="Error message if any stage failed.")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def solved(self) -> bool:
        """True if a SolveResult is present."""
        return self.result is not None

    def to_record(self, cfg) -> SolveRecord:
        """Build the serialisable SolveRecord from this job + config.

        Enriches EXIF with sensor width and crop-factor equivalent focal length
        from config, then strips absolute paths from filenames.
        """
        from pathlib import Path
        is_crop = (
            cfg.optics.sensor_width_mm is not None
            and cfg.optics.sensor_width_mm < 30
        )
        exif = self.exif or ImageExif()
        enriched_exif = exif.model_copy(update={
            "sensor_width_mm": cfg.optics.sensor_width_mm,
            "focal_mm_equiv": (
                round(exif.focal_mm * 1.6, 1)
                if exif.focal_mm and is_crop else None
            ),
        })
        solve = self.result
        if solve and solve.annotated_path:
            solve = solve.model_copy(update={"annotated_path": Path(solve.annotated_path).name})
        return SolveRecord(
            original_file=Path(self.path).name if self.path else "",
            solved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            exif=enriched_exif,
            solved=self.solved,
            solve=solve,
            hints_used=self.hints,
            observer=ObserverInfo(lat=cfg.location.lat, lon=cfg.location.lon),
            solver_mode=cfg.solver.mode,
            error=self.error,
        )


# Semantic image-format choices. The driver maps these to camera-specific
# strings (e.g. GphotoCamera: raw->'RAW', jpeg->'L', both->'RAW + L').
ImageFormat = Literal["raw", "jpeg", "both"]


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
    these and hand it to CaptureService.capture(). Flat by design so it maps
    cleanly to both argparse flags and a JSON body. (Status is a separate
    operation, so there is no status flag here.)
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
