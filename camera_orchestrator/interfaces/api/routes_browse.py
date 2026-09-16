"""Browse routes — read-only views over the capture tree, plus raw file serving.

Thin over `BrowseService`, which owns the confinement rule: every `path` is
resolved and must stay inside the configured root, so `?path=../../etc/passwd`
raises BrowsePathError and the app's handler turns it into a 400. No route here
touches the filesystem without going through `resolve()` first.
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

import anyio.to_thread
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from camera_orchestrator.application.browse_service import BrowseService
from camera_orchestrator.domain.models.browse import DirectoryListing
from camera_orchestrator.interfaces.api.deps import get_browse_service
from camera_orchestrator.interfaces.api.models import SessionListResponse

router = APIRouter(prefix="/api", tags=["browse"])

# mimetypes does not know the camera RAW formats, and the browser needs *a*
# type. Anything unlisted falls back to octet-stream (i.e. download it).
_EXTRA_TYPES = {
    ".cr2": "image/x-canon-cr2",
    ".cr3": "image/x-canon-cr3",
    ".nef": "image/x-nikon-nef",
    ".arw": "image/x-sony-arw",
    ".dng": "image/x-adobe-dng",
    ".fits": "image/fits",
    ".fit": "image/fits",
}

_PATH_QUERY = Query(default=None, description="Path to list — absolute, or relative to the capture root. Defaults to the root.")


def _content_type(path: Path) -> str:
    """Best-effort MIME type for a captured file."""
    suffix = path.suffix.lower()
    if suffix in _EXTRA_TYPES:
        return _EXTRA_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


@router.get("/browse", response_model=DirectoryListing)
async def browse(
    path: str | None = _PATH_QUERY,
    service: BrowseService = Depends(get_browse_service),
) -> DirectoryListing:
    """One level of a directory: its sessions, loose files and other folders."""
    return await anyio.to_thread.run_sync(service.list_directory, path)


@router.get("/sessions", response_model=SessionListResponse)
async def sessions(
    path: str | None = _PATH_QUERY,
    service: BrowseService = Depends(get_browse_service),
) -> SessionListResponse:
    """Session folders directly under `path`, newest first."""
    found = await anyio.to_thread.run_sync(service.list_sessions, path)
    return SessionListResponse(sessions=found)


@router.get("/files/raw")
async def raw_file(
    path: str = Query(description="Path of the file to serve — absolute, or relative to the capture root."),
    service: BrowseService = Depends(get_browse_service),
) -> FileResponse:
    """Serve one file's bytes (previews, JPEGs, session.json, RAWs).

    No Content-Disposition: the UI puts solved previews straight into an <img>,
    and forcing a download would break that.
    """
    resolved = service.resolve(path)  # BrowsePathError -> 400
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail=f"no such file: {path}")
    return FileResponse(resolved, media_type=_content_type(resolved))
