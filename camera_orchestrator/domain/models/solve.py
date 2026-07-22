"""Solve-domain value objects — EXIF, hints, results, sidecar records."""
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
        from config, then strips absolute paths from filenames. `cfg` is
        duck-typed (a Config-like object) so the domain keeps no import on the
        infrastructure config.
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
