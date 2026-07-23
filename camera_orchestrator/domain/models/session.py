"""Session-workflow value objects.

A *session* is a folder that owns a session.json manifest. Two verbs write into
it: `align` records the solved target (TargetInfo); `sequence` fires the phases
and appends PhaseRecords. SequenceRequest is the input to the sequence run.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

PhaseKind = Literal["light", "dark", "bias"]

DEFAULT_ORDER: list[PhaseKind] = ["light", "dark", "bias"]


class SequenceRequest(BaseModel):
    """A capture sequence: lights + optional darks + bias, in a chosen order."""

    out_dir: str = Field(description="Directory to write frames into (the session folder, or the parent for a loose run).")
    iso: Optional[str] = Field(default=None, description="ISO for lights and darks, e.g. '800'.")
    shutter: Optional[str] = Field(default=None, description="Shutter speed for lights and darks. Ignored when bulb_seconds is set. Bias always uses the fastest shutter.")
    bulb_seconds: Optional[float] = Field(default=None, description="Bulb exposure length for lights and darks. Not used for bias.")
    aperture: Optional[str] = Field(default=None, description="Aperture f-number. None leaves it unchanged.")
    lights: int = Field(default=0, ge=0, description="Number of light frames.")
    darks: int = Field(default=0, ge=0, description="Number of dark frames (same exposure as lights, lens capped).")
    bias: int = Field(default=0, ge=0, description="Number of bias frames (fastest shutter, lens capped).")
    download: bool = Field(default=False, description="Transfer frames over USB (True) or shoot to the card only (False, the default).")
    order: list[PhaseKind] = Field(default_factory=lambda: list(DEFAULT_ORDER), description="Order phases run in. Darks are ideally right after lights (thermal match), but this is configurable.")


class TargetInfo(BaseModel):
    """The solved pointing recorded by `align` for a session's target."""

    solved: bool = Field(description="Whether the align frame plate-solved.")
    center_ra_deg: Optional[float] = Field(default=None, description="Solved centre RA in decimal degrees.")
    center_dec_deg: Optional[float] = Field(default=None, description="Solved centre Dec in decimal degrees.")
    scale_arcsec_per_px: Optional[float] = Field(default=None, description="Plate scale in arcseconds per pixel.")
    preview: Optional[str] = Field(default=None, description="Filename of the annotated preview saved in the session folder.")
    frame: Optional[str] = Field(default=None, description="Filename of the align frame the solve ran on.")


class PhaseRecord(BaseModel):
    """What one phase (a lights/darks/bias sub-run) actually produced."""

    kind: PhaseKind = Field(description="Frame type: light, dark, or bias.")
    count: int = Field(description="Frames actually captured in this phase.")
    iso: Optional[str] = Field(default=None, description="ISO applied for this phase.")
    shutter: Optional[str] = Field(default=None, description="Shutter speed applied (bias forces the fastest, e.g. 1/4000).")
    aperture: Optional[str] = Field(default=None, description="Aperture applied, if set.")
    bulb_seconds: Optional[float] = Field(default=None, description="Bulb duration in seconds, if this phase used bulb.")
    started_at: datetime = Field(description="UTC timestamp when the phase's first shot fired.")
    ended_at: datetime = Field(description="UTC timestamp when the phase's last frame landed.")
    files: list[str] = Field(default_factory=list, description="Frame filenames (basenames). Camera-side names for a card-only run; local names when downloaded.")


class SessionManifest(BaseModel):
    """A record of one imaging session — written as session.json in the session folder."""

    schema_version: int = Field(default=1, description="Manifest format version, for forward migration.")
    session_id: str = Field(description="The session folder name, e.g. 20260723-orion.")
    name: Optional[str] = Field(default=None, description="Optional human label passed via --name (the target). None if unlabelled.")
    download: bool = Field(default=False, description="True if frames were downloaded into the session folder; False means files[] are card-side names and the frames remain on the card.")
    target: Optional[TargetInfo] = Field(default=None, description="The solved target, written by `align`. None until an align has run against this session.")
    started_at: Optional[datetime] = Field(default=None, description="UTC timestamp when the first sequence run began. None until a sequence has run.")
    ended_at: Optional[datetime] = Field(default=None, description="UTC timestamp when the last sequence run finished.")
    phases: list[PhaseRecord] = Field(default_factory=list, description="Per-phase records, in the order they ran, appended across sequence runs.")
