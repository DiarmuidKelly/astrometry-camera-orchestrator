"""Camera grab — pull images from a connected Canon camera via gphoto2.

Unmounts the gvfs gphoto2 mount before listing files so that newly shot
frames are visible (gvfs caches directory listings and misses new files).
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from camera_orchestrator.log import get_logger

log = get_logger("camera_orchestrator.grab")


class GrabError(Exception):
    """Raised when gphoto2 returns a non-zero exit code."""


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def unmount_gvfs() -> None:
    result = run(["gio", "mount", "--list"])
    mounts = [line for line in result.stdout.splitlines() if "gphoto2" in line]
    for mount in mounts:
        uri = mount.strip().split()[-1]
        log.info("Unmounting gvfs mount", extra={"uri": uri})
        run(["gio", "mount", "-u", uri])


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


def _download(num: int, file_name: str, out_dir: Path, force: bool) -> Path | None:
    """Download a single file by number. Returns the destination path on success, None if skipped."""
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


def grab_latest(out_dir: Path, force: bool = False) -> Path | None:
    """Download the latest file from the camera.

    Returns the path of the downloaded file, or None if already exists and force is False.
    Raises GrabError if the camera is unreachable or the download fails.
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
    return _download(num, file_name, out_dir, force)


def poll(out_dir: Path, interval: float, force: bool = False) -> None:
    """Poll the camera for new files and download them as they appear.

    Yields control back to the caller via KeyboardInterrupt — intended for CLI use.
    For programmatic use, call list_files() and _download() directly in your own loop.
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
                _download(num, file_name, out_dir, force)
                seen_max = max(seen_max, num)
    except KeyboardInterrupt:
        log.info("Stopped")
