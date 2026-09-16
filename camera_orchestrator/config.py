"""Typed config — loaded from YAML, validated by Pydantic."""
from __future__ import annotations

import os
import tempfile
from io import StringIO
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, Field
from ruamel.yaml import YAML


def _merge_into(doc: Any, values: dict) -> None:
    """Write `values` into `doc` in place, keeping doc's comments and key order.

    Recurses into nested mappings so a section's comments survive; keys absent
    from doc are appended. Only the scalar values change, which is what keeps
    the hand-written annotations attached to the lines they describe.
    """
    for key, value in values.items():
        if isinstance(value, dict) and isinstance(doc.get(key), dict):
            _merge_into(doc[key], value)
        else:
            doc[key] = value


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
    format: Literal["text", "json"] = Field(default="text", description="Log format for the console. 'json' emits one JSON object per line. Overridden by LOG_FORMAT env var.")
    file: Optional[str] = Field(default="log.log", description="Rotating log file path, relative to where the app is run. Null disables file logging.")
    file_format: Literal["text", "json"] = Field(default="text", description="Format for the log file, independent of the console.")
    max_bytes: int = Field(default=5 * 1024 * 1024, description="Rotate the log file once it passes this size in bytes.")
    backup_count: int = Field(default=5, description="How many rotated log files to keep.")


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

    def save(self, path: str) -> str:
        """Write this config back to a YAML file, preserving comments.

        An existing file is updated in place through a round-trip parser so its
        comments, key order and formatting survive. That matters: this file is
        hand-annotated with the things that silently break solving if forgotten
        — which sensor width belongs to which body, why focal_mm is null, which
        object an RA/Dec hint points at. A plain safe_dump round-trip would
        delete all of it the first time the UI saved.

        Written atomically (temp file in the same directory, then replaced) so a
        crash mid-write can't leave a truncated config — losing solver.index_dir
        or optics.sensor_width_mm degrades every later solve rather than
        erroring, so a half-written file is worse than no write at all.

        Args:
            path: Destination YAML path. Parent directories are created.

        Returns:
            The path written.
        """
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        payload = self._render_yaml(dest)
        # Same directory so os.replace stays on one filesystem (atomic rename).
        with tempfile.NamedTemporaryFile(
            "w", dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp", delete=False
        ) as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = tmp.name
        os.replace(tmp_path, dest)
        return str(dest)

    def _render_yaml(self, dest: Path) -> str:
        """Serialise to YAML, merging into `dest`'s existing document if present."""
        data = self.model_dump(mode="json")
        if not dest.exists():
            return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)

        rt = YAML()  # round-trip mode: retains comments, key order and quoting
        rt.preserve_quotes = True
        try:
            with open(dest) as f:
                doc = rt.load(f)
        except Exception:
            # Unparseable existing file — don't let it block the write.
            return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)

        if doc is None:
            return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)

        _merge_into(doc, data)
        buf = StringIO()
        rt.dump(doc, buf)
        return buf.getvalue()
