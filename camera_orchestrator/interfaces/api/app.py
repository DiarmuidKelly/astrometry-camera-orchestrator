"""Application factory — routers, error mapping, static mount.

`create_app()` is the only public entry point. It is a factory rather than a
module-level `app` so tests get an isolated job registry per app and can pass a
Config without touching the filesystem.

**Shutdown is not optional here.** Nothing used to close the camera when the
server stopped, and job runners are daemon threads that CPython does not unwind
at interpreter exit — so a Ctrl-C left the PTP session open with the body in live
view, mirror up and sensor powered. That is the heaviest draw there is, and the
exact drain `release_when_idle` exists to prevent. The lifespan below cancels
active jobs, joins their threads within a bound so their `finally` blocks run,
and then closes the shared session.

Domain errors are mapped to HTTP **once**, here, instead of try/except in every
route: a route calls the service and lets the error out. Starlette walks the
exception's MRO when looking up a handler, so CameraBusyError finds its own 503
before falling back to CameraError's 409.
"""
from __future__ import annotations

import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Type

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from camera_orchestrator.application.browse_service import BrowsePathError as ServiceBrowsePathError
from camera_orchestrator.composition import close_camera_session
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
    JobPromptMismatchError,
    JobRegistry,
)
from camera_orchestrator.log import get_logger

log = get_logger("camera_orchestrator.api")

# The front end is served from inside the package so setuptools' package
# discovery keeps working without a second top-level package.
STATIC_DIR = Path(__file__).resolve().parent / "static"

# How `serve --reload` passes the config path to the reloader's child process.
CONFIG_ENV_VAR = "CAMERA_ORCHESTRATOR_CONFIG"

# Same trick for the bound host, which the child also cannot see (it does not
# parse the CLI args).
HOST_ENV_VAR = "CAMERA_ORCHESTRATOR_HOST"

# Always-acceptable Host headers. '::1' is listed alongside '[::1]' because
# Starlette compares the header with the port split off at the first ':'.
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]", "::1")

# Bind addresses meaning "every interface" — the host header will then be
# whatever the client typed, so the allow-list is built from this machine's own
# addresses instead of from the bind value.
_WILDCARD_BINDS = {"0.0.0.0", "::", ""}

# RFC 5737 TEST-NET-1. Connecting a UDP socket sends no packets; it only asks
# the kernel which local address would be used, so this address is never reached
# and nothing leaves the machine.
_ROUTE_PROBE = ("192.0.2.1", 9)

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
#   JobPromptMismatchError 409 — the confirm answered a superseded prompt.
#   FileNotFoundError 404 — a path that should have existed did not.
_ERROR_STATUS: dict[Type[Exception], int] = {
    CameraBusyError: 503,
    CameraError: 409,
    BrowsePathError: 400,
    ServiceBrowsePathError: 400,
    JobConflictError: 409,
    JobPromptMismatchError: 409,
    JobNotFoundError: 404,
    FileNotFoundError: 404,
}


# Total seconds spent waiting for job threads to unwind on shutdown. Long enough
# for a frame in flight to finish its driver call, short enough that Ctrl-C still
# feels like Ctrl-C — a libgphoto2 call cannot be interrupted, so the wait has to
# be bounded rather than complete.
SHUTDOWN_JOIN_S = 5.0


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Release the camera when the server stops.

    Cancel active jobs first, then join their threads (bounded), then close the
    session — in that order, because the session cannot close while a borrow is
    still held, and the borrow is only released when the runner unwinds.

    The joins run on the event loop rather than through anyio.to_thread: the
    server is already draining, and this must not depend on a thread limiter
    whose slots are exactly what a stuck camera has exhausted.
    """
    yield
    log.info("Shutting down — releasing the camera")
    registry: JobRegistry = app.state.jobs
    registry.shutdown(SHUTDOWN_JOIN_S)
    close_camera_session()


def _local_addresses() -> set[str]:
    """Names and addresses this machine can legitimately be reached on.

    Needed only for a wildcard bind: `--host 0.0.0.0` is exactly the case where
    the Host header is not the bind value but whatever the phone typed (the LAN
    IP, or the mDNS name). Enumerating them keeps that workflow while still
    refusing a name an attacker controls, which is what DNS rebinding needs.
    """
    found: set[str] = set()
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = ""
    if hostname:
        found.update({hostname, f"{hostname}.local"})  # .local: mDNS/Avahi
        try:
            # sockaddr[0] is the address; typed loosely because the tuple shape
            # differs between IPv4 and IPv6.
            found.update(str(info[4][0]) for info in socket.getaddrinfo(hostname, None))
        except OSError:
            pass
    try:
        # The address on the interface holding the default route. On Linux
        # getaddrinfo(hostname) often only yields 127.0.1.1, which is not the
        # address a phone connects to.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(_ROUTE_PROBE)
            found.add(probe.getsockname()[0])
    except OSError:
        pass
    return found


def allowed_hosts(bind_host: str) -> list[str]:
    """The Host headers this server answers to, given what it was bound to.

    Not `*`: without a Host check, a page can point a name it owns at 127.0.0.1
    and issue *same-origin* requests to this service, which turns every
    LAN-adversary finding into one that lands from any web page you open.
    """
    hosts = list(LOOPBACK_HOSTS)
    hosts += sorted(_local_addresses()) if bind_host in _WILDCARD_BINDS else [bind_host]
    return list(dict.fromkeys(hosts))  # de-duplicated, order preserved


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
    browse_root: str | None = None,
    bind_host: str | None = None,
) -> FastAPI:
    """Build the API.

    Args:
        cfg: Loaded configuration. Defaults to `Config()` (all defaults), which
            is what an unconfigured first run gets.
        confirm_timeout: Seconds a job blocked on a lens-cap prompt waits before
            failing. Lowered in tests.
        config_path: YAML file `PUT /api/config` writes back to — the same path
            the server was started with, so the UI edits the file the CLI reads.
        browse_root: Directory every path-taking route is confined to. Defaults
            to `grab.out_dir` **as read now**, and is then frozen.
        bind_host: The interface `serve` bound to. When given, Host headers are
            validated against it (plus loopback); when None the check is not
            installed, which is the embedded/test case where there is no socket
            and therefore no rebinding to do.

    The config lives on `app.state` rather than in a module global because it is
    mutable at runtime (the settings form writes it). Every route resolves it
    per request via `deps.get_config`, so a save takes effect on the next call.

    The **browse root does not work that way, deliberately.** It is resolved once
    here and frozen on `app.state`: `grab.out_dir` is one of the fields the
    settings form can write, so a per-request root could be moved to `/` by a
    request and then read back through `GET /api/files/raw`. A config write moves
    where new captures go; it does not move what the API may touch.
    """
    app = FastAPI(
        title="camera-orchestrator",
        description="Live view, capture control and session browsing for astrophotography.",
        lifespan=_lifespan,
    )
    app.state.config = cfg if cfg is not None else Config()
    app.state.config_path = config_path
    app.state.browse_root = str(
        Path(browse_root or app.state.config.grab.out_dir).expanduser().resolve())
    app.state.jobs = JobRegistry(confirm_timeout=confirm_timeout)

    if bind_host is not None:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts(bind_host))

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
    CLI args. The config path and the bound host travel in the environment
    instead; the host matters because the reloaded child must apply the same
    Host check as a normal run, not a weaker one.
    """
    path = os.environ.get(CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH)
    return create_app(Config.load(path), config_path=path,
                      bind_host=os.environ.get(HOST_ENV_VAR))
