"""EXIF extraction from JPEG and CR2/RAW files.

Reads the fields the solver pipeline needs: focal length, ISO, shutter speed,
aperture, and capture datetime. All fields are optional — missing tags return None
rather than raising, so the pipeline degrades gracefully on stripped files.
"""
from __future__ import annotations

from dataclasses import dataclass

import exifread


@dataclass
class ImageExif:
    focal_mm: float | None = None
    iso: int | None = None
    shutter_sec: float | None = None
    aperture: float | None = None
    datetime: str | None = None


def read_exif(path: str) -> ImageExif:
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

    def string(tag: str) -> str | None:
        val = tags.get(tag)
        return str(val).strip() if val else None

    return ImageExif(
        focal_mm=rational("EXIF FocalLength"),
        iso=integer("EXIF ISOSpeedRatings"),
        shutter_sec=rational("EXIF ExposureTime"),
        aperture=rational("EXIF FNumber"),
        datetime=string("EXIF DateTimeOriginal"),
    )
