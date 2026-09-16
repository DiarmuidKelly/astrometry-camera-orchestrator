"""Tests for the browse HTTP routes — real BrowseService over a tmp_path tree.

The service is left real: its job is filesystem-shaped (confinement, sizes,
recorded-versus-present frames) and a fake would fake away everything worth
asserting. `grab.out_dir` points at tmp_path, so the browse root is the tree the
test built and nothing touches the real capture folder.
"""
from __future__ import annotations

from camera_orchestrator.config import Config
from camera_orchestrator.interfaces.api.app import create_app
from fastapi.testclient import TestClient


def _client(root) -> TestClient:
    cfg = Config.model_validate({"grab": {"out_dir": str(root)}})
    return TestClient(create_app(cfg))


def _session(root, folder_name: str) -> None:
    """A minimal session folder: a date-prefixed name and one frame on disk."""
    directory = root / folder_name
    directory.mkdir()
    (directory / "IMG_0001.JPG").write_bytes(b"\xff\xd8frame")


def test_browse_lists_the_root(tmp_path):
    _session(tmp_path, "20260916-m33")
    (tmp_path / "loose.jpg").write_bytes(b"\xff\xd8x")

    body = _client(tmp_path).get("/api/browse").json()
    assert body["is_root"] is True
    assert body["parent"] is None
    assert [s["folder_name"] for s in body["sessions"]] == ["20260916-m33"]
    assert [f["name"] for f in body["files"]] == ["loose.jpg"]


def test_sessions_are_returned_newest_first(tmp_path):
    _session(tmp_path, "20260910-orion")
    _session(tmp_path, "20260916-m33")

    body = _client(tmp_path).get("/api/sessions").json()
    assert [s["folder_name"] for s in body["sessions"]] == ["20260916-m33", "20260910-orion"]
    assert body["sessions"][0]["files_on_disk"] == 1


def test_path_traversal_is_rejected_with_400(tmp_path):
    response = _client(tmp_path).get("/api/browse", params={"path": "../../etc"})
    assert response.status_code == 400               # BrowsePathError, not a 500
    assert "outside the browse root" in response.json()["detail"]


def test_browsing_a_missing_directory_is_400(tmp_path):
    response = _client(tmp_path).get("/api/browse", params={"path": "nope"})
    assert response.status_code == 400               # inside the root, but not a dir


def test_raw_file_is_served_with_a_usable_content_type(tmp_path):
    _session(tmp_path, "20260916-m33")
    response = _client(tmp_path).get(
        "/api/files/raw", params={"path": "20260916-m33/IMG_0001.JPG"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == b"\xff\xd8frame"
    # No attachment disposition — the UI drops solved previews straight into <img>.
    assert "attachment" not in response.headers.get("content-disposition", "")


def test_raw_file_of_a_raw_suffix_gets_a_raw_mime_type(tmp_path):
    (tmp_path / "IMG_0002.CR2").write_bytes(b"raw-bytes")
    response = _client(tmp_path).get("/api/files/raw", params={"path": "IMG_0002.CR2"})
    assert response.headers["content-type"] == "image/x-canon-cr2"


def test_raw_file_missing_is_404(tmp_path):
    response = _client(tmp_path).get("/api/files/raw", params={"path": "absent.jpg"})
    assert response.status_code == 404


def test_raw_file_outside_the_root_is_400(tmp_path):
    response = _client(tmp_path).get("/api/files/raw", params={"path": "/etc/passwd"})
    assert response.status_code == 400               # confinement beats existence
