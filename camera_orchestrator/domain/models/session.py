"""Session-workflow value objects — a multi-phase imaging run."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

PhaseKind = Literal["light", "dark", "bias"]

DEFAULT_ORDER: list[PhaseKind] = ["light", "dark", "bias"]


class SessionRequest(BaseModel):
    """A capture session: lights + optional darks + bias, in a chosen order."""

    out_dir: str = Field(description="Directory to download frames into (unused when download is False).")
    iso: Optional[str] = Field(default=None, description="ISO for lights and darks, e.g. '800'.")
    shutter: Optional[str] = Field(default=None, description="Shutter speed for lights and darks. Ignored when bulb_seconds is set. Bias always uses the fastest shutter.")
    bulb_seconds: Optional[float] = Field(default=None, description="Bulb exposure length for lights and darks. Not used for bias.")
    aperture: Optional[str] = Field(default=None, description="Aperture f-number. None leaves it unchanged.")
    lights: int = Field(default=0, ge=0, description="Number of light frames.")
    darks: int = Field(default=0, ge=0, description="Number of dark frames (same exposure as lights, lens capped).")
    bias: int = Field(default=0, ge=0, description="Number of bias frames (fastest shutter, lens capped).")
    download: bool = Field(default=False, description="Transfer frames over USB (True) or shoot to the card only (False, the default).")
    order: list[PhaseKind] = Field(default_factory=lambda: list(DEFAULT_ORDER), description="Order phases run in. Darks are ideally right after lights (thermal match), but this is configurable.")


class SessionResult(BaseModel):
    """Outcome of a session — frames captured per kind."""

    counts: dict[str, int] = Field(default_factory=dict, description="Frames captured per kind, e.g. {'light': 60, 'dark': 20, 'bias': 50}.")
    frames: list[str] = Field(default_factory=list, description="Local paths of downloaded files (empty when download is False).")
    download: bool = Field(default=False, description="Whether frames were transferred to the host.")
