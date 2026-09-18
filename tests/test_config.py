import pytest
from pydantic import ValidationError

from camera_orchestrator.config import Config


def test_defaults_with_no_file():
    cfg = Config.load(None)
    assert cfg.solver.image == "diarmuidk/astrometry-dockerised-solver:latest"
    assert cfg.solver.mode == "accurate"
    assert cfg.solver.cpulimit == 60
    assert cfg.optics.focal_mm is None
    assert cfg.optics.sensor_width_mm is None
    assert cfg.search.radius_deg == 60.0
    assert cfg.location.lat is None


def test_defaults_with_missing_file():
    cfg = Config.load("/nonexistent/path/config.yaml")
    assert cfg.solver.mode == "accurate"


def test_fast_solve_args():
    cfg = Config.load(None)
    cfg.solver.mode = "fast"
    args = cfg.solver.solve_args
    assert "--downsample" in args
    assert "4" in args
    assert "--objs" in args


def test_accurate_solve_args():
    cfg = Config.load(None)
    cfg.solver.mode = "accurate"
    args = cfg.solver.solve_args
    assert "--downsample" in args
    assert "2" in args
    assert "--objs" not in args


def test_invalid_mode_raises():
    with pytest.raises(ValidationError):
        Config.model_validate({"solver": {"mode": "turbo"}})


def test_partial_yaml(tmp_path):
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text("solver:\n  cpulimit: 120\n")
    cfg = Config.load(str(yaml_file))
    assert cfg.solver.cpulimit == 120
    assert cfg.solver.mode == "accurate"
    assert cfg.optics.focal_mm is None


def test_full_yaml(tmp_path):
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(
        "solver:\n  mode: fast\n  cpulimit: 30\n"
        "optics:\n  focal_mm: 200\n  sensor_width_mm: 22.3\n"
        "search:\n  ra_deg: 277.5\n  dec_deg: -6.5\n  radius_deg: 30.0\n"
        "location:\n  lat: 51.4779\n  lon: -0.0015\n"
    )
    cfg = Config.load(str(yaml_file))
    assert cfg.solver.mode == "fast"
    assert cfg.optics.focal_mm == 200.0
    assert cfg.search.ra_deg == 277.5
    assert cfg.location.lat == 51.4779


def test_save_round_trips_through_load(tmp_path):
    cfg = Config.load(None)
    cfg.search.ra_deg = 10.68          # M31 — the values a UI would submit
    cfg.search.dec_deg = 41.27
    cfg.optics.sensor_width_mm = 35.8

    dest = cfg.save(str(tmp_path / "config.yaml"))
    reloaded = Config.load(dest)

    assert reloaded.search.ra_deg == 10.68
    assert reloaded.search.dec_deg == 41.27
    assert reloaded.optics.sensor_width_mm == 35.8


def test_save_preserves_nulls(tmp_path):
    # focal_mm must stay null so EXIF keeps winning; a UI round-trip that
    # silently coerced it to 0.0 would skew every later scale hint.
    cfg = Config.load(None)
    dest = cfg.save(str(tmp_path / "config.yaml"))
    assert Config.load(dest).optics.focal_mm is None


def test_save_creates_parent_directories(tmp_path):
    cfg = Config.load(None)
    dest = cfg.save(str(tmp_path / "nested" / "dir" / "config.yaml"))
    assert Config.load(dest).solver.mode == "accurate"


def test_save_overwrites_atomically(tmp_path):
    path = tmp_path / "config.yaml"
    Config.load(None).save(str(path))

    cfg = Config.load(str(path))
    cfg.search.ra_deg = 297.70
    cfg.save(str(path))

    assert Config.load(str(path)).search.ra_deg == 297.70
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "config.yaml"]
    assert leftovers == []                 # temp file replaced, not left behind


def test_save_preserves_comments_in_an_existing_file(tmp_path):
    # config.yaml is hand-annotated with the things that silently break solving
    # if forgotten — a UI save must not delete them.
    path = tmp_path / "config.yaml"
    path.write_text(
        "optics:\n"
        "  # focal_mm is intentionally null — EXIF takes priority.\n"
        "  focal_mm:\n"
        "  sensor_width_mm: 35.8  # Canon 5D Mark II (full-frame)\n"
        "search:\n"
        "  ra_deg: 10.68\n"
    )
    cfg = Config.load(str(path))
    cfg.search.ra_deg = 297.70
    cfg.save(str(path))

    text = path.read_text()
    assert "# focal_mm is intentionally null — EXIF takes priority." in text
    assert "# Canon 5D Mark II (full-frame)" in text
    assert Config.load(str(path)).search.ra_deg == 297.70   # value still updated
    assert Config.load(str(path)).optics.focal_mm is None   # null survived the trip


def test_save_falls_back_cleanly_on_an_unparseable_file(tmp_path):
    # A corrupt file must not block the write, or the UI could never recover.
    path = tmp_path / "config.yaml"
    path.write_text("solver: [unclosed\n")
    cfg = Config.load(None)
    cfg.search.ra_deg = 1.5
    cfg.save(str(path))
    assert Config.load(str(path)).search.ra_deg == 1.5
