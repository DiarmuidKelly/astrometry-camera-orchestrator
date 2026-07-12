"""Typed config — loaded from YAML, validated by Pydantic."""
from __future__ import annotations

import os
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field


class SolverConfig(BaseModel):
    """Configuration for the astrometry.net Docker solver."""

    image: str = Field(default="diarmuidk/astrometry-dockerised-solver:latest", description="Docker image to use for plate solving.")
    index_dir: str = Field(default="", description="Path to the directory containing astrometry index files.")
    cpulimit: int = Field(default=60, description="CPU time limit in seconds per solve attempt.")
    mode: Literal["fast", "accurate"] = Field(default="accurate", description="Solver mode: 'fast' downsamples more aggressively; 'accurate' is slower but more reliable.")

    @property
    def solve_args(self) -> list[str]:
        if self.mode == "fast":
            return ["--downsample", "4", "--objs", "100"]
        return ["--downsample", "2"]


class OpticsConfig(BaseModel):
    """Optics configuration used to compute plate scale hints."""

    focal_mm: Optional[float] = Field(default=None, description="Focal length in mm. If set, overridden by EXIF focal length when present.")
    sensor_width_mm: Optional[float] = Field(default=None, description="Sensor width in mm (e.g. 22.3 for APS-C, 35.8 for full-frame).")


class SearchConfig(BaseModel):
    """Sky search region hint to narrow the solver's search space."""

    ra_deg: Optional[float] = Field(default=None, description="Centre RA of the search region in decimal degrees. Null searches the full sky.")
    dec_deg: Optional[float] = Field(default=None, description="Centre Dec of the search region in decimal degrees.")
    radius_deg: float = Field(default=60.0, description="Search radius in degrees around the RA/Dec hint.")


class LocationConfig(BaseModel):
    """Observer location for metadata purposes."""

    lat: Optional[float] = Field(default=None, description="Observer latitude in decimal degrees (positive = north).")
    lon: Optional[float] = Field(default=None, description="Observer longitude in decimal degrees (positive = east).")


class LoggingConfig(BaseModel):
    """Logging output configuration."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO", description="Log verbosity. Overridden by LOG_LEVEL env var.")
    format: Literal["text", "json"] = Field(default="text", description="Log format. 'json' emits one JSON object per line. Overridden by LOG_FORMAT env var.")


class GrabConfig(BaseModel):
    """Configuration for the grab subcommand."""

    out_dir: str = Field(default="./incoming", description="Directory to save downloaded images into.")
    poll_interval: Optional[float] = Field(default=None, description="Poll the camera every N seconds for new files. Null disables polling (one-shot mode).")


class Config(BaseModel):
    solver: SolverConfig = Field(default_factory=SolverConfig)
    optics: OpticsConfig = Field(default_factory=OpticsConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    location: LocationConfig = Field(default_factory=LocationConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    grab: GrabConfig = Field(default_factory=GrabConfig)

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        data: dict = {}
        if path and os.path.exists(path):
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        return cls.model_validate(data)
