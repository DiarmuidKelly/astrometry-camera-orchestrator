"""Domain errors — raised across port boundaries, caught by callers."""
from __future__ import annotations


class CameraError(Exception):
    """A camera operation failed (connection, claim, capture, download)."""


class CameraBusyError(CameraError):
    """The shared camera is held by another operation and did not free up.

    Raised when a borrow times out — e.g. a live-view stream asking for a frame
    while a capture sequence holds the connection. Callers should back off and
    retry rather than treat it as a hardware fault; an API maps it to 503.
    """


class GrabError(Exception):
    """A gphoto2 CLI grab operation returned a non-zero exit code."""


class BrowsePathError(Exception):
    """A browse path escaped the configured root, or is not a directory.

    Paths reaching the browse service come from a UI, so traversal attempts are
    rejected rather than clamped; an API maps this to 400.
    """
