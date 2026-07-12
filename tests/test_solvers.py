import math

import pytest

from camera_orchestrator.solvers import scale_hint_from_optics


def test_scale_hint_200mm_apsc():
    low, high = scale_hint_from_optics(focal_mm=200, sensor_width_mm=22.3, frame_width_px=6000)
    fov = math.degrees(2 * math.atan(11.15 / 200))
    expected = fov * 3600 / 6000
    assert abs((low + high) / 2 - expected) < 0.01
    assert low < expected < high


def test_scale_hint_70mm_apsc():
    low, high = scale_hint_from_optics(focal_mm=70, sensor_width_mm=22.3, frame_width_px=6000)
    fov = math.degrees(2 * math.atan(11.15 / 70))
    expected = fov * 3600 / 6000
    assert abs((low + high) / 2 - expected) < 0.01
    assert low < expected < high


def test_scale_hint_tolerance():
    low, high = scale_hint_from_optics(focal_mm=200, sensor_width_mm=22.3,
                                        frame_width_px=6000, tol=0.3)
    mid = (low + high) / 2
    assert abs(low - mid * 0.7) < 0.001
    assert abs(high - mid * 1.3) < 0.001


def test_scale_hint_fullframe_vs_apsc():
    low_apsc, high_apsc = scale_hint_from_optics(200, 22.3, 6000)
    low_ff, high_ff = scale_hint_from_optics(200, 35.8, 6000)
    assert (low_ff + high_ff) / 2 > (low_apsc + high_apsc) / 2


def test_scale_hint_longer_focal_is_narrower():
    low_200, high_200 = scale_hint_from_optics(200, 22.3, 6000)
    low_70, high_70 = scale_hint_from_optics(70, 22.3, 6000)
    assert (low_200 + high_200) / 2 < (low_70 + high_70) / 2
