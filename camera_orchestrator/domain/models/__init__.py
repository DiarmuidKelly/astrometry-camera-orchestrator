"""Domain value objects. Import from here rather than the submodules."""
from .camera import (
    CameraFile,
    CameraStatus,
    CaptureRequest,
    CaptureResult,
    CaptureSettings,
    ImageFormat,
)
from .solve import (
    ImageExif,
    ObserverInfo,
    SolveHints,
    SolveJob,
    SolveRecord,
    SolveResult,
)

__all__ = [
    "CameraFile",
    "CameraStatus",
    "CaptureRequest",
    "CaptureResult",
    "CaptureSettings",
    "ImageFormat",
    "ImageExif",
    "ObserverInfo",
    "SolveHints",
    "SolveJob",
    "SolveRecord",
    "SolveResult",
]
