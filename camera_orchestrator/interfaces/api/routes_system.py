"""Health and config routes — the two things the UI reads before anything else."""
from __future__ import annotations

from importlib import metadata
from pathlib import Path
from typing import Any

import anyio.to_thread
from fastapi import APIRouter, Depends, Request

from camera_orchestrator.config import Config
from camera_orchestrator.interfaces.api.deps import get_config
from camera_orchestrator.interfaces.api.models import ConfigUpdateResponse, HealthResponse
from camera_orchestrator.log import get_logger

log = get_logger("camera_orchestrator.api.system")

router = APIRouter(prefix="/api", tags=["system"])

# VERSION sits at the repo root, four levels up from this module
# (interfaces/api/ -> interfaces/ -> camera_orchestrator/ -> repo root).
_VERSION_FILE = Path(__file__).resolve().parents[3] / "VERSION"


def package_version() -> str:
    """The running version: the VERSION file, or installed metadata as a fallback.

    The file wins because `.github/workflows/auto-release.sh` owns it, so in a
    checkout it is the truth even when the installed distribution lags behind.
    """
    if _VERSION_FILE.is_file():
        return _VERSION_FILE.read_text().strip()
    try:
        return metadata.version("camera-orchestrator")
    except metadata.PackageNotFoundError:
        return "unknown"


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness probe — no hardware touched, always cheap."""
    return HealthResponse(ok=True, version=package_version())


@router.get("/config", response_model=Config)
async def read_config(cfg: Config = Depends(get_config)) -> Config:
    """The loaded configuration, so the UI can show defaults (out_dir, cpulimit…)."""
    return cfg


@router.get("/config/schema")
async def config_schema() -> dict[str, Any]:
    """JSON Schema for Config — labels and help text for the settings form.

    Every Config field carries a Field(description=...), so the UI can render the
    form from this and stay honest when config gains a field.
    """
    return Config.model_json_schema()


@router.put("/config", response_model=ConfigUpdateResponse)
async def write_config(request: Request, body: Config) -> ConfigUpdateResponse:
    """Replace the whole config, persist it, and adopt it without a restart.

    Full-document replace, not a merge: the settings form sends every field
    anyway, and a merge would make "clear this value" ambiguous. Validation is
    FastAPI's — a bad `sensor_width_mm` comes back as a 422 with Pydantic's own
    error detail rather than being written and quietly ruining every later solve.

    Adoption is just swapping `app.state.config`: every route resolves the config
    per request through `get_config`, and the services that capture it
    (`build_shared_align_service`, `build_browse_service`) are rebuilt per request
    too — so the next Align uses the RA/Dec hint you just typed.
    """
    path = await anyio.to_thread.run_sync(body.save, request.app.state.config_path)
    request.app.state.config = body
    log.info("Config saved", extra={"path": path})
    return ConfigUpdateResponse(config=body, path=path)
