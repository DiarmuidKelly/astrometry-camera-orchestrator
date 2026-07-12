"""Typed config — loaded from YAML, validated by Pydantic."""
from __future__ import annotations

import os
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field


class SolverConfig(BaseModel):
    image: str = "diarmuidk/astrometry-dockerised-solver:latest"
    index_dir: str = ""
    cpulimit: int = 60
    mode: Literal["fast", "accurate"] = "accurate"

    @property
    def solve_args(self) -> list[str]:
        if self.mode == "fast":
            return ["--downsample", "4", "--objs", "100"]
        return ["--downsample", "2"]


class OpticsConfig(BaseModel):
    focal_mm: Optional[float] = None
    sensor_width_mm: Optional[float] = None


class SearchConfig(BaseModel):
    ra_deg: Optional[float] = None
    dec_deg: Optional[float] = None
    radius_deg: float = 60.0


class LocationConfig(BaseModel):
    lat: Optional[float] = None
    lon: Optional[float] = None


class Config(BaseModel):
    solver: SolverConfig = Field(default_factory=SolverConfig)
    optics: OpticsConfig = Field(default_factory=OpticsConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    location: LocationConfig = Field(default_factory=LocationConfig)

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        data: dict = {}
        if path and os.path.exists(path):
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        return cls.model_validate(data)
