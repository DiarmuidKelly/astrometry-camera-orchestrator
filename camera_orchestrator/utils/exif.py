"""EXIF extraction — returns a typed ImageExif model."""
from __future__ import annotations

import exifread

from ..models import ImageExif


def read_exif(path: str) -> ImageExif:
    """Read EXIF metadata from an image file and return a typed ImageExif model.

    The camera datetime is normalised from EXIF format (YYYY:MM:DD HH:MM:SS)
    to ISO 8601 (YYYY-MM-DDTHH:MM:SS). No timezone is attached since EXIF
    does not carry one.

    Args:
        path: Path to the image file.

    Returns:
        ImageExif with whatever fields the file contains; unknowns are None.

    Raises:
        FileNotFoundError: If the file does not exist.
        Exception: If exifread cannot open or parse the file.
    """
    with open(path, "rb") as f:
        tags = exifread.process_file(f, details=False)

    def rational(tag: str) -> float | None:
        val = tags.get(tag)
        if val is None:
            return None
        r = val.values[0]
        if hasattr(r, "num"):
            return float(r.num) / float(r.den) if r.den else None
        return float(r)

    def integer(tag: str) -> int | None:
        val = tags.get(tag)
        if val is None:
            return None
        v = val.values[0]
        return int(v.num / v.den) if hasattr(v, "num") else int(v)

    def exif_datetime(tag: str) -> str | None:
        val = tags.get(tag)
        if not val:
            return None
        raw = str(val).strip()
        try:
            # EXIF format: "YYYY:MM:DD HH:MM:SS" → ISO 8601: "YYYY-MM-DDTHH:MM:SS"
            date, time = raw.split(" ", 1)
            return f"{date.replace(':', '-')}T{time}"
        except Exception:
            return raw

    return ImageExif(
        focal_mm=rational("EXIF FocalLength"),
        iso=integer("EXIF ISOSpeedRatings"),
        shutter_sec=rational("EXIF ExposureTime"),
        aperture=rational("EXIF FNumber"),
        datetime=exif_datetime("EXIF DateTimeOriginal"),
    )
