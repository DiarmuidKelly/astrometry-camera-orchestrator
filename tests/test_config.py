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
        "location:\n  lat: 47.45\n  lon: 10.43\n"
    )
    cfg = Config.load(str(yaml_file))
    assert cfg.solver.mode == "fast"
    assert cfg.optics.focal_mm == 200.0
    assert cfg.search.ra_deg == 277.5
    assert cfg.location.lat == 47.45
