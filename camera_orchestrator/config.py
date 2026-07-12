"""Config for the watch-and-solve pipeline.

Loads a YAML file into typed sections. All fields have sane defaults, so a
partial config (or none at all) still works. CLI flags override config values
in the scripts.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml


@dataclass
class SolverCfg:
    image: str = "diarmuidk/astrometry-dockerised-solver:latest"
    index_dir: str = ""
    cpulimit: int = 60


@dataclass
class OpticsCfg:
    focal_mm: float | None = None          # lens focal length
    sensor_width_mm: float | None = None    # M50 II APS-C ~= 22.3


@dataclass
class SearchCfg:
    ra_deg: float | None = None             # search centre (where you're pointing)
    dec_deg: float | None = None
    radius_deg: float | None = 60.0         # limit the sky to this arc radius


@dataclass
class LocationCfg:
    lat: float | None = None                # for future alt/az -> RA/Dec
    lon: float | None = None


@dataclass
class WatchCfg:
    folder: str = "./incoming"
    poll_interval: float = 1.0
    process_existing: bool = False


@dataclass
class Config:
    solver: SolverCfg = field(default_factory=SolverCfg)
    optics: OpticsCfg = field(default_factory=OpticsCfg)
    search: SearchCfg = field(default_factory=SearchCfg)
    location: LocationCfg = field(default_factory=LocationCfg)
    watch: WatchCfg = field(default_factory=WatchCfg)

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        data: dict = {}
        if path and os.path.exists(path):
            with open(path) as f:
                data = yaml.safe_load(f) or {}

        def section(kind, key):
            return kind(**(data.get(key) or {}))

        return cls(
            solver=section(SolverCfg, "solver"),
            optics=section(OpticsCfg, "optics"),
            search=section(SearchCfg, "search"),
            location=section(LocationCfg, "location"),
            watch=section(WatchCfg, "watch"),
        )
