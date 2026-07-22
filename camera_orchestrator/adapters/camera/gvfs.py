"""gvfs / subprocess helpers shared by the gphoto adapters.

The desktop auto-mounts cameras via gvfs, which holds the USB device and blocks
libgphoto2 from claiming it. Release the mount before opening a session.
"""
from __future__ import annotations

import subprocess

from camera_orchestrator.log import get_logger

log = get_logger("camera_orchestrator.adapters.gvfs")


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def unmount_gvfs() -> None:
    result = run(["gio", "mount", "--list"])
    mounts = [line for line in result.stdout.splitlines() if "gphoto2" in line]
    for mount in mounts:
        uri = mount.strip().split()[-1]
        log.info("Unmounting gvfs mount", extra={"uri": uri})
        run(["gio", "mount", "-u", uri])
