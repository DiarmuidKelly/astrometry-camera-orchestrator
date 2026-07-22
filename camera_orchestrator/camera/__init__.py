"""Camera drivers — capture and control backends.

Mirrors the solvers package: an abstract Camera base with concrete backends.
GphotoCamera (libgphoto2 via python-gphoto2) is the only backend today.
"""
from .base import Camera, CameraError, CameraFile
from .gphoto import GphotoCamera

__all__ = ["Camera", "CameraError", "CameraFile", "GphotoCamera"]
