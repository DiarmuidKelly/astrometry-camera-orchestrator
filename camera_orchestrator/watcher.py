"""Folder watcher — yields new image files as they appear.

Used for the live pipeline: camera drops files into a folder (via gphoto2
download, WiFi transfer, or SD card mount) and the watcher feeds them to the
solver. The same watcher drives the 5D Mark II live tether and the batch
processor — the caller decides what to do with each path yielded.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Iterator

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".cr2", ".cr3", ".tif", ".tiff", ".fits", ".png"}


class FolderWatcher:
    def __init__(self, folder: str, poll_interval: float = 1.0,
                 process_existing: bool = False):
        self.folder = Path(folder)
        self.poll_interval = poll_interval
        self._seen: set[Path] = set()
        if not process_existing:
            self._seen = set(self._scan())

    def _scan(self) -> list[Path]:
        if not self.folder.exists():
            return []
        return [
            p for p in self.folder.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
        ]

    def watch(self) -> Iterator[str]:
        """Yield absolute paths of new image files as they appear."""
        while True:
            current = set(self._scan())
            for path in sorted(current - self._seen):
                self._seen.add(path)
                yield str(path)
            time.sleep(self.poll_interval)
