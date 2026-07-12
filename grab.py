#!/usr/bin/env python3
"""Grab the latest image from the connected Canon camera.

Unmounts the gvfs gphoto2 mount, fetches the most recent file via gphoto2,
saves it to the output directory, then exits. Run this manually after shooting.

Usage:
    python grab.py [--out ./incoming]
    python grab.py --poll 5 [--out ./incoming]   # poll every 5 seconds
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

GVFS_URI = "gphoto2://[usb:]//"

def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def unmount_gvfs() -> None:
    result = run(["gio", "mount", "--list"])
    mounts = [l for l in result.stdout.splitlines() if "gphoto2" in l]
    for mount in mounts:
        uri = mount.strip().split()[-1]
        print(f"Unmounting {uri} ...")
        run(["gio", "mount", "-u", uri])


def list_files() -> list[tuple[int, str]]:
    """Return [(file_number, filename), ...] from gphoto2 --list-files."""
    result = run(["gphoto2", "--list-files"])
    if result.returncode != 0:
        print(f"gphoto2 error: {result.stderr.strip()}")
        sys.exit(1)
    files = []
    for line in result.stdout.splitlines():
        m = re.match(r"#(\d+)\s+(\S+)", line.strip())
        if m:
            files.append((int(m.group(1)), m.group(2)))
    return files


def grab_latest(out_dir: Path, force: bool = False) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Releasing gvfs mount ...")
    unmount_gvfs()

    print("Listing camera files ...")
    files = list_files()
    if not files:
        print("No files found on camera.")
        sys.exit(0)

    num, name = files[-1]
    print(f"Latest file: #{num} {name}")
    if not _download(num, name, out_dir, force):
        sys.exit(0)


def _download(num: int, name: str, out_dir: Path, force: bool) -> bool:
    """Download a single file by number. Returns True on success."""
    dest = out_dir / name
    if dest.exists() and not force:
        print(f"Already exists: {dest}  (pass --force to overwrite)")
        return False
    cmd = ["gphoto2", "--get-file", str(num), "--filename", str(dest)]
    if force:
        cmd.append("--force-overwrite")
    result = run(cmd)
    if result.returncode != 0:
        print(f"Download failed: {result.stderr.strip()}")
        return False
    print(f"Saved → {dest}")
    return True


def poll(out_dir: Path, interval: float, force: bool = False) -> None:
    """Poll the camera for new files and download them as they appear."""
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Releasing gvfs mount ...")
    unmount_gvfs()

    print("Listing camera files ...")
    files = list_files()
    seen_max = files[-1][0] if files else 0
    print(f"Baseline: {len(files)} file(s) on camera, latest #{seen_max}. Polling every {interval}s ...")

    try:
        while True:
            time.sleep(interval)
            files = list_files()
            new = [(n, name) for n, name in files if n > seen_max]
            for num, name in new:
                print(f"New file: #{num} {name}")
                _download(num, name, out_dir, force)
                seen_max = max(seen_max, num)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="grab")
    parser.add_argument("--out", default="./incoming", help="Output directory for downloaded images")
    parser.add_argument("--force", action="store_true", help="Overwrite if file already exists")
    parser.add_argument("--poll", metavar="SECONDS", type=float, default=None,
                        help="Poll camera every N seconds for new files")
    args = parser.parse_args()
    if args.poll is not None:
        poll(Path(args.out), interval=args.poll, force=args.force)
    else:
        grab_latest(Path(args.out), force=args.force)
