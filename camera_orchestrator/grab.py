"""Camera grab — pull images from a connected Canon camera via gphoto2.

Unmounts the gvfs gphoto2 mount before listing files so that newly shot
frames are visible (gvfs caches directory listings and misses new files).
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

from camera_orchestrator.log import get_logger

log = get_logger("camera_orchestrator.grab")


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
        log.error("gphoto2 list-files failed", extra={"stderr": result.stderr.strip()})
        sys.exit(1)
    files = []
    for line in result.stdout.splitlines():
        m = re.match(r"#(\d+)\s+(\S+)", line.strip())
        if m:
            files.append((int(m.group(1)), m.group(2)))
    return files


def _download(num: int, file_name: str, out_dir: Path, force: bool) -> bool:
    """Download a single file by number. Returns True on success."""
    dest = out_dir / file_name
    if dest.exists() and not force:
        log.info("Already exists, skipping  (pass --force to overwrite)", extra={"dest": str(dest)})
        return False
    cmd = ["gphoto2", "--get-file", str(num), "--filename", str(dest)]
    if force:
        cmd.append("--force-overwrite")
    result = run(cmd)
    if result.returncode != 0:
        log.error("Download failed", extra={"file_name": file_name, "stderr": result.stderr.strip()})
        return False
    log.info("Saved", extra={"dest": str(dest)})
    return True


def grab_latest(out_dir: Path, force: bool = False) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info("Releasing gvfs mount")
    unmount_gvfs()

    log.info("Listing camera files")
    files = list_files()
    if not files:
        log.info("No files found on camera")
        sys.exit(0)

    num, file_name = files[-1]
    log.info("Latest file", extra={"file_num": num, "file_name": file_name})
    if not _download(num, file_name, out_dir, force):
        sys.exit(0)


def poll(out_dir: Path, interval: float, force: bool = False) -> None:
    """Poll the camera for new files and download them as they appear."""
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
