"""Application factory — routers, error mapping, static mount.

`create_app()` is the only public entry point. It is a factory rather than a
module-level `app` so tests get an isolated job registry per app and can pass a
Config without touching the filesystem.

Domain errors are mapped to HTTP **once**, here, instead of try/except in every
route: a route calls the service and lets the error out. Starlette walks the
exception's MRO when looking up a handler, so CameraBusyError finds its own 503
before falling back to CameraError's 409.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Type

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from camera_orchestrator.application.browse_service import BrowsePathError as ServiceBrowsePathError
from camera_orchestrator.config import Config
from camera_orchestrator.domain.errors import BrowsePathError, CameraBusyError, CameraError
from camera_orchestrator.interfaces.api import (
    routes_browse,
    routes_camera,
    routes_jobs,
    routes_system,
    routes_ws,
)
from camera_orchestrator.interfaces.api.jobs import (
    DEFAULT_CONFIRM_TIMEOUT_S,
    JobConflictError,
    JobNotFoundError,
    JobRegistry,
)
from camera_orchestrator.log import get_logger

log = get_logger("camera_orchestrator.api")

# The front end is served from inside the package so setuptools' package
# discovery keeps working without a second top-level package.
STATIC_DIR = Path(__file__).resolve().parent / "static"

# How `serve --reload` passes the config path to the reloader's child process.
CONFIG_ENV_VAR = "CAMERA_ORCHESTRATOR_CONFIG"

# Matches the CLI's --config default, so the UI writes the file the CLI reads.
DEFAULT_CONFIG_PATH = "config.yaml"

# Exception type -> HTTP status. Order does not matter (Starlette matches on the
# raised type's MRO), but the reasoning does:
#   CameraBusyError  503 — transient; the caller should retry, not give up.
#   CameraError      409 — the hardware is in the wrong state (live view off, no
#                          remote capture on this body). Retrying will not help
#                          until the user changes something.
#   BrowsePathError  400 — the client asked for a path outside the root.
#   JobConflictError 409 — a camera job is already in flight.
#   FileNotFoundError 404 — a path that should have existed did not.
_ERROR_STATUS: dict[Type[Exception], int] = {
    CameraBusyError: 503,
    CameraError: 409,
    BrowsePathError: 400,
    ServiceBrowsePathError: 400,
    JobConflictError: 409,
    JobNotFoundError: 404,
    FileNotFoundError: 404,
}


def _handler(status: int):
    """Build a handler rendering an exception as FastAPI's {'detail': ...} shape."""

    async def handle(_request: Request, exc: Exception) -> JSONResponse:
        detail = str(exc) or exc.__class__.__name__
        log.info("Request failed", extra={"status": status, "error": detail})
        return JSONResponse(status_code=status, content={"detail": detail})

    return handle


def create_app(
    cfg: Config | None = None,
    confirm_timeout: float = DEFAULT_CONFIRM_TIMEOUT_S,
    config_path: str = DEFAULT_CONFIG_PATH,
) -> FastAPI:
    """Build the API.

    Args:
        cfg: Loaded configuration. Defaults to `Config()` (all defaults), which
            is what an unconfigured first run gets.
        confirm_timeout: Seconds a job blocked on a lens-cap prompt waits before
            failing. Lowered in tests.
        config_path: YAML file `PUT /api/config` writes back to — the same path
            the server was started with, so the UI edits the file the CLI reads.

    The config lives on `app.state` rather than in a module global because it is
    mutable at runtime (the settings form writes it). Every route resolves it
    per request via `deps.get_config`, so a save takes effect on the next call.
    """
    app = FastAPI(
        title="camera-orchestrator",
        description="Live view, capture control and session browsing for astrophotography.",
    )
    app.state.config = cfg if cfg is not None else Config()
    app.state.config_path = config_path
    app.state.jobs = JobRegistry(confirm_timeout=confirm_timeout)

    for exc_type, status in _ERROR_STATUS.items():
        app.add_exception_handler(exc_type, _handler(status))

    app.include_router(routes_system.router)
    app.include_router(routes_camera.router)
    app.include_router(routes_browse.router)
    app.include_router(routes_jobs.router)
    # The one live-update channel. Registered before the static mount like every
    # other /api route; see routes_ws.py for why it is one socket and not one
    # stream per job.
    app.include_router(routes_ws.router)

    # Mounted last so every /api route matches first (Starlette matches in
    # registration order). Defensive: the front end is a separate deliverable and
    # the app must still boot — and serve the API — before it lands.
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app


def reloadable_app() -> FastAPI:
    """App factory for `serve --reload`.

    uvicorn's reloader re-imports the app in a child process, so it needs an
    import string rather than an object — and the child cannot see the parsed
    CLI args. The config path travels in the environment instead.
    """
    path = os.environ.get(CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH)
    return create_app(Config.load(path), config_path=path)
