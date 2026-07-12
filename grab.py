#!/usr/bin/env python3
"""Grab the latest image from the connected Canon camera.

Unmounts the gvfs gphoto2 mount, fetches the most recent file via gphoto2,
saves it to the output directory, then exits. Run this manually after shooting.

Usage:
    python grab.py [--out ./incoming]
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
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
    dest = out_dir / name
    print(f"Latest file: #{num} {name}")

    if dest.exists() and not force:
        print(f"Already exists: {dest}  (pass --force to overwrite)")
        sys.exit(0)

    cmd = ["gphoto2", "--get-file", str(num), "--filename", str(dest)]
    if force:
        cmd.append("--force-overwrite")
    result = run(cmd)
    if result.returncode != 0:
        print(f"Download failed: {result.stderr.strip()}")
        sys.exit(1)

    print(f"Saved → {dest}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="grab")
    parser.add_argument("--out", default="./incoming", help="Output directory for downloaded images")
    parser.add_argument("--force", action="store_true", help="Overwrite if file already exists")
    args = parser.parse_args()
    grab_latest(Path(args.out), force=args.force)
