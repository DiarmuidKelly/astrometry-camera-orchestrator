"""Domain errors — raised across port boundaries, caught by callers."""
from __future__ import annotations


class CameraError(Exception):
    """A camera operation failed (connection, claim, capture, download)."""


class GrabError(Exception):
    """A gphoto2 CLI grab operation returned a non-zero exit code."""
