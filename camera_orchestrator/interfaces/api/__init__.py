"""HTTP inbound adapter — FastAPI app serving the browser UI.

Same standing as the CLI: it translates HTTP into application-service calls and
renders the results. It imports `composition.build_*` factories and the domain
DTOs, never an adapter (no gphoto2, cv2, docker, astropy in here).

`create_app()` is the factory; `camera_orchestrator.interfaces.cli serve` runs it
under uvicorn.
"""
from __future__ import annotations

from camera_orchestrator.interfaces.api.app import create_app

__all__ = ["create_app"]
