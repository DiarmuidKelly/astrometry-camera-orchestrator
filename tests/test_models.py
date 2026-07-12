import json

from camera_orchestrator.models import (
    ImageExif,
    ObserverInfo,
    SolveHints,
    SolveJob,
    SolveRecord,
    SolveResult,
)


def test_solve_job_solved_false():
    job = SolveJob()
    assert job.solved is False


def test_solve_job_solved_true():
    result = SolveResult(
        center_ra_deg=277.5,
        center_dec_deg=-6.5,
        scale_arcsec_per_px=3.97,
        width_px=6000,
        height_px=4000,
    )
    job = SolveJob(result=result)
    assert job.solved is True


def test_solve_record_serialises_to_json():
    record = SolveRecord(
        original_file="IMG_4341.JPG",
        solved_at="2026-07-12T00:00:00+00:00",
        exif=ImageExif(focal_mm=200.0, iso=3200),
        solved=True,
        solve=SolveResult(
            center_ra_deg=267.73,
            center_dec_deg=-29.46,
            scale_arcsec_per_px=3.97,
            width_px=6000,
            height_px=4000,
        ),
        hints_used=SolveHints(ra_deg=277.5, dec_deg=-6.5, radius_deg=60.0),
        observer=ObserverInfo(lat=47.45, lon=10.43),
        solver_mode="accurate",
    )
    data = json.loads(record.model_dump_json())
    assert data["original_file"] == "IMG_4341.JPG"
    assert data["solved"] is True
    assert data["solve"]["center_ra_deg"] == 267.73
    assert data["exif"]["focal_mm"] == 200.0
    assert data["observer"]["lat"] == 47.45


def test_solve_record_unsolved():
    record = SolveRecord(
        original_file="IMG_bad.JPG",
        solved_at="2026-07-12T00:00:00+00:00",
        exif=ImageExif(),
        solved=False,
        solve=None,
        hints_used=SolveHints(),
        observer=ObserverInfo(),
        solver_mode="fast",
        error="solver returned None",
    )
    data = json.loads(record.model_dump_json())
    assert data["solved"] is False
    assert data["solve"] is None
    assert data["error"] == "solver returned None"


def test_image_exif_all_optional():
    exif = ImageExif()
    assert exif.focal_mm is None
    assert exif.iso is None
    assert exif.shutter_sec is None


def test_solve_hints_all_optional():
    hints = SolveHints()
    assert hints.scale_low is None
    assert hints.ra_deg is None
