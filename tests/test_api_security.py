"""Tests for the API's security primitives — TestClient over the real app.

Four defences, each with a reproduction of what it stops:

- the `Host` allow-list, which is what makes DNS rebinding fail;
- the capture root being fixed at `serve` time rather than read per request
  from `grab.out_dir`, which `PUT /api/config` can rewrite;
- the job routes confining `out_dir`, `folder` and `file` to that root;
- `solver.image` being a registry reference and nothing else, because it lands
  in a `docker run` argv where docker is still parsing options.

No hardware and no camera: every test here is refused before any service runs,
so a MockCamera is not even needed. `config_path` always points inside
`tmp_path` — a test that writes config must never touch the checked-in one.
"""
from __future__ import annotations

import socket
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from pydantic import ValidationError

from camera_orchestrator.adapters.solvers.docker import DockerSolver
from camera_orchestrator.config import Config
from camera_orchestrator.domain.models.solve import SolveHints
from camera_orchestrator.interfaces.api.app import LOOPBACK_HOSTS, allowed_hosts, create_app
from fastapi.testclient import TestClient


def _app(root: Path, tmp_path: Path, **kwargs):
    """An app whose capture root is `root` and whose config writes stay in tmp_path."""
    cfg = Config.model_validate({"grab": {"out_dir": str(root)}})
    return create_app(cfg, config_path=str(tmp_path / "config.yaml"), **kwargs)


def _client(root: Path, tmp_path: Path, **kwargs) -> TestClient:
    return TestClient(_app(root, tmp_path, **kwargs))


# -- Host validation (DNS rebinding) --------------------------------------


def test_a_host_header_the_server_does_not_answer_to_is_refused(tmp_path):
    """The rebinding step: a name the attacker owns, pointed at 127.0.0.1.

    The browser then treats the requests as same-origin — no preflight, no CORS
    — so without this check every LAN-adversary capability lands from an
    ordinary web page instead.
    """
    app = _app(tmp_path, tmp_path, bind_host="127.0.0.1")
    rebound = TestClient(app, base_url="http://rebind.evil.example")

    response = rebound.get("/api/health")
    assert response.status_code == 400
    assert "Invalid host header" in response.text


def test_loopback_hosts_are_answered(tmp_path):
    app = _app(tmp_path, tmp_path, bind_host="127.0.0.1")
    for base in ("http://localhost:8000", "http://127.0.0.1:8000"):
        assert TestClient(app, base_url=base).get("/api/health").status_code == 200


def test_the_check_is_not_installed_without_a_bound_host(tmp_path):
    # create_app is also used embedded and in tests, where there is no socket and
    # so no rebinding to do. `serve` always passes bind_host.
    assert _client(tmp_path, tmp_path).get("/api/health").status_code == 200


def test_a_wildcard_bind_allows_this_machines_own_addresses_but_not_everything():
    """The phone-over-LAN requirement: `--host 0.0.0.0`, browsed to by LAN IP.

    The bind value is not a Host anyone types, so the allow-list is built from
    the machine's own names and addresses. It must stay an allow-list — '*'
    would silently undo the defence above.
    """
    wildcard = allowed_hosts("0.0.0.0")
    assert "*" not in wildcard
    assert set(LOOPBACK_HOSTS) <= set(wildcard)
    assert socket.gethostname() in wildcard          # the mDNS/hostname route
    # At least one address that is not loopback: what the phone actually types.
    assert [h for h in wildcard if h not in LOOPBACK_HOSTS and h != socket.gethostname()]


def test_an_explicit_bind_allows_that_host_and_loopback():
    # A documentation address (RFC 5737) stands in for a real LAN IP.
    assert allowed_hosts("192.0.2.10") == [*LOOPBACK_HOSTS, "192.0.2.10"]


# -- the capture root is fixed, not config ---------------------------------


def _secret(tmp_path: Path) -> tuple[Path, Path]:
    """A capture root with a file deliberately left *outside* it."""
    root = tmp_path / "captures"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("not yours")
    return root, outside


def test_a_config_write_cannot_move_the_browse_root(tmp_path):
    """The root was read per request from `grab.out_dir`, which the API can set.

    Setting it to '/' (here: the parent of the root) and reading back through
    `GET /api/files/raw` was a full filesystem read for anyone who could reach
    the port. The confinement code was always correct; the root was not.
    """
    root, outside = _secret(tmp_path)
    client = _client(root, tmp_path)

    body = client.get("/api/config").json()
    body["grab"]["out_dir"] = str(tmp_path)          # move the root up one level
    assert client.put("/api/config", json=body).status_code == 200
    assert client.get("/api/config").json()["grab"]["out_dir"] == str(tmp_path)

    leaked = client.get("/api/files/raw", params={"path": str(outside)})
    assert leaked.status_code == 400                 # still confined to the fixed root
    assert "outside the browse root" in leaked.json()["detail"]


def test_the_browse_root_defaults_to_out_dir_as_read_at_startup(tmp_path):
    root, _ = _secret(tmp_path)
    (root / "20260916-m33").mkdir()
    listing = _client(root, tmp_path).get("/api/browse").json()
    assert listing["path"] == str(root) and listing["is_root"] is True


# -- job routes take confined paths ----------------------------------------


def test_a_capture_job_outside_the_root_is_refused_and_creates_nothing(tmp_path):
    # Reproduction: the job routes resolved `out_dir` with a bare Path(), so a
    # POST created and wrote into any directory the process could reach.
    root, _ = _secret(tmp_path)
    target = tmp_path / "elsewhere"

    response = _client(root, tmp_path).post(
        "/api/jobs/capture", json={"out_dir": str(target), "count": 1})

    assert response.status_code == 400
    assert "outside the capture root" in response.json()["detail"]
    assert not target.exists()                       # refused before the runner started


def test_a_session_name_cannot_traverse_out_of_the_root(tmp_path):
    """`name` becomes a folder under the root, so it is a path too.

    The check runs on the folder `resolve_session` produced, not on the raw
    name: the date prefix is glued on ('20260918-..'), which absorbs the first
    '..' and would make a check of the bare name read the wrong string.
    """
    root, _ = _secret(tmp_path)
    escape = "../../../../escape"
    response = _client(root, tmp_path).post(
        "/api/jobs/sequence", json={"name": escape, "lights": 1})

    assert response.status_code == 400
    assert "out_dir" in response.json()["detail"]
    assert not (tmp_path.parent / "escape").exists()


def test_an_align_job_outside_the_root_is_refused(tmp_path):
    root, _ = _secret(tmp_path)
    response = _client(root, tmp_path).post(
        "/api/jobs/align", json={"out_dir": str(tmp_path)})
    assert response.status_code == 400


def test_a_batch_job_outside_the_root_is_refused(tmp_path):
    # batch writes solve_results.json and an annotated/ folder into `folder`, and
    # bind-mounts it into the solver container.
    root, _ = _secret(tmp_path)
    response = _client(root, tmp_path).post("/api/jobs/batch", json={"folder": str(tmp_path)})
    assert response.status_code == 400
    assert "folder" in response.json()["detail"]


def test_a_solve_job_outside_the_root_is_refused(tmp_path):
    root, outside = _secret(tmp_path)
    response = _client(root, tmp_path).post("/api/jobs/solve", json={"file": str(outside)})
    assert response.status_code == 400
    assert "file" in response.json()["detail"]


def test_paths_inside_the_root_still_work(tmp_path):
    # The confinement must not cost the ordinary case: a folder under the root
    # is accepted and runs (an empty folder is 'no_images', not an error).
    root, _ = _secret(tmp_path)
    inside = root / "20260916-m33"
    inside.mkdir()
    accepted = _client(root, tmp_path).post("/api/jobs/batch", json={"folder": str(inside)})
    assert accepted.status_code == 200


# -- solver.image is a docker argv element ---------------------------------


@pytest.mark.parametrize("image", [
    "--privileged",                                  # the reproduction
    "--entrypoint=/bin/sh",
    "-v",
    "--user 0",
    "image --privileged",                            # a flag smuggled after the name
])
def test_a_docker_flag_is_not_a_valid_image(image):
    """`solver.image` is interpolated while docker is still parsing options.

    There is no shell involved — it is an argv list — so this is docker option
    injection rather than shell injection, and the docker group is
    root-equivalent. Validating on the model rejects it at config load *and* on
    `PUT /api/config`, before it is ever written to disk.
    """
    with pytest.raises(ValidationError):
        Config.model_validate({"solver": {"image": image}})


@pytest.mark.parametrize("image", [
    "diarmuidk/astrometry-dockerised-solver:latest",
    "solver",
    "registry.example:5000/team/solver:v1.2.3",
    "solver@sha256:" + "a" * 64,
])
def test_real_image_references_are_accepted(image):
    assert Config.model_validate({"solver": {"image": image}}).solver.image == image


def test_putting_a_docker_flag_as_the_image_is_rejected_by_the_api(tmp_path):
    client = _client(tmp_path, tmp_path)
    body = client.get("/api/config").json()
    body["solver"]["image"] = "--privileged"

    assert client.put("/api/config", json=body).status_code == 422
    # And nothing was persisted: the config the server serves is unchanged.
    assert client.get("/api/config").json()["solver"]["image"] != "--privileged"


def test_the_docker_argv_terminates_its_options_before_the_image(tmp_path):
    """Belt to the validator's braces, for a solver built directly in code."""
    source = tmp_path / "IMG_0001.JPG"
    source.write_bytes(b"\xff\xd8fake")
    solver = DockerSolver(image="solver:latest", index_dir=str(tmp_path))

    with patch("camera_orchestrator.adapters.solvers.docker.subprocess.run") as run:
        run.return_value.returncode = 1              # no solution; we only want the argv
        solver.solve(np.zeros((40, 60, 3), dtype=np.uint8), SolveHints(),
                     source_path=str(source))

    cmd = run.call_args.args[0]
    assert cmd[cmd.index("solver:latest") - 1] == "--"
    assert cmd.index("--") < cmd.index("solve-field")
