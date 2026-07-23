"""Align-workflow value objects — request/result for a pointing check."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class AlignRequest(BaseModel):
    """Input to the align service — one frame, solved, to check pointing."""

    out_dir: str = Field(description="Directory the frame and annotated preview are written to.")
    iso: Optional[str] = Field(default=None, description="ISO setting, e.g. '800'. None leaves it unchanged.")
    shutter: Optional[str] = Field(default=None, description="Shutter speed, e.g. '2' or '1/60'. Ignored when bulb_seconds is set.")
    aperture: Optional[str] = Field(default=None, description="Aperture f-number, e.g. '4'. None leaves it unchanged.")
    bulb_seconds: Optional[float] = Field(default=None, description="Bulb exposure length in seconds. Overrides shutter.")


class AlignResult(BaseModel):
    """Outcome of an align call — where the frame actually landed on the sky."""

    solved: bool = Field(description="True if the frame plate-solved.")
    center_ra_deg: Optional[float] = Field(default=None, description="Right ascension of the frame centre (J2000), if solved.")
    center_dec_deg: Optional[float] = Field(default=None, description="Declination of the frame centre (J2000), if solved.")
    scale_arcsec_per_px: Optional[float] = Field(default=None, description="Image scale in arcsec/pixel, if solved.")
    annotated_path: Optional[str] = Field(default=None, description="Path to the annotated preview (<stem>_solved.png), if produced.")
    frame_path: str = Field(description="Local path of the captured frame that was solved.")
