"""gphoto2 CLI file operations used by the grab workflow.

Uses the gphoto2 command-line tool (subprocess) rather than python-gphoto2 —
a separate, simpler path for pulling files that are already on the card. Kept
distinct from the GphotoCamera (python-gphoto2) capture driver.
"""
from __future__ import annotations

import re
from pathlib import Path

from camera_orchestrator.domain.errors import GrabError
from camera_orchestrator.log import get_logger

from .gvfs import run

log = get_logger("camera_orchestrator.adapters.cli_grab")


def list_files() -> list[tuple[int, str]]:
    """Return [(file_number, filename), ...] from gphoto2 --list-files."""
    result = run(["gphoto2", "--list-files"])
    if result.returncode != 0:
        raise GrabError(f"gphoto2 list-files failed: {result.stderr.strip()}")
    files = []
    for line in result.stdout.splitlines():
        m = re.match(r"#(\d+)\s+(\S+)", line.strip())
        if m:
            files.append((int(m.group(1)), m.group(2)))
    return files


def download(num: int, file_name: str, out_dir: Path, force: bool) -> Path | None:
    """Download a single file by number. Returns the path on success, None if skipped."""
    dest = out_dir / file_name
    if dest.exists() and not force:
        log.info("Already exists, skipping  (pass --force to overwrite)", extra={"dest": str(dest)})
        return None
    cmd = ["gphoto2", "--get-file", str(num), "--filename", str(dest)]
    if force:
        cmd.append("--force-overwrite")
    result = run(cmd)
    if result.returncode != 0:
        raise GrabError(f"Download failed for {file_name}: {result.stderr.strip()}")
    log.info("Saved", extra={"dest": str(dest)})
    return dest
