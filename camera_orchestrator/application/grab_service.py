"""Grab workflow — pull files already on the camera card via the gphoto2 CLI.

Composes the CLI-grab adapter (list_files/download) and gvfs release into the
grab-latest and poll use cases. Distinct from the capture driver (python-gphoto2).
"""
from __future__ import annotations

import time
from pathlib import Path

from camera_orchestrator.adapters.camera.cli_grab import download, list_files
from camera_orchestrator.adapters.camera.gvfs import unmount_gvfs
from camera_orchestrator.log import get_logger

log = get_logger("camera_orchestrator.grab")


def grab_latest(out_dir: Path, force: bool = False) -> Path | None:
    """Download the latest file from the camera.

    Returns the path of the downloaded file, or None if it already exists and
    force is False. Raises GrabError if the camera is unreachable or download fails.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info("Releasing gvfs mount")
    unmount_gvfs()

    log.info("Listing camera files")
    files = list_files()
    if not files:
        log.info("No files found on camera")
        return None

    num, file_name = files[-1]
    log.info("Latest file", extra={"file_num": num, "file_name": file_name})
    return download(num, file_name, out_dir, force)


def poll(out_dir: Path, interval: float, force: bool = False) -> None:
    """Poll the camera for new files and download them as they appear.

    Runs until interrupted (KeyboardInterrupt) — intended for CLI use.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info("Releasing gvfs mount")
    unmount_gvfs()

    log.info("Listing camera files")
    files = list_files()
    seen_max = files[-1][0] if files else 0
    log.info("Polling started",
             extra={"baseline_files": len(files), "latest_num": seen_max, "interval_s": interval})

    try:
        while True:
            time.sleep(interval)
            files = list_files()
            new = [(n, fn) for n, fn in files if n > seen_max]
            for num, file_name in new:
                log.info("New file detected", extra={"file_num": num, "file_name": file_name})
                download(num, file_name, out_dir, force)
                seen_max = max(seen_max, num)
    except KeyboardInterrupt:
        log.info("Stopped")
