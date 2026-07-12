from pathlib import Path

import pytest

from camera_orchestrator.utils.exif import read_exif
from camera_orchestrator.models import ImageExif

SAMPLE_IMAGE = str(Path(__file__).parent / "fixtures" / "IMG_4341.JPG")


def test_read_exif_returns_image_exif():
    exif = read_exif(SAMPLE_IMAGE)
    assert isinstance(exif, ImageExif)


def test_read_exif_focal_length():
    exif = read_exif(SAMPLE_IMAGE)
    assert exif.focal_mm is not None
    assert exif.focal_mm > 0


def test_read_exif_iso():
    exif = read_exif(SAMPLE_IMAGE)
    assert exif.iso is not None
    assert exif.iso > 0


def test_read_exif_shutter():
    exif = read_exif(SAMPLE_IMAGE)
    assert exif.shutter_sec is not None
    assert exif.shutter_sec > 0


def test_read_exif_missing_file_raises():
    with pytest.raises(Exception):
        read_exif("/nonexistent/file.jpg")
