"""Tests for the grab adapters/service — all subprocess calls are mocked."""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from camera_orchestrator.adapters.camera import cli_grab, gvfs
from camera_orchestrator.application import grab_service
from camera_orchestrator.domain.errors import GrabError

CLI = "camera_orchestrator.adapters.camera.cli_grab"
GVFS = "camera_orchestrator.adapters.camera.gvfs"
SVC = "camera_orchestrator.application.grab_service"


def _proc(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def _proc_err(stderr: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


LIST_OUTPUT = """\
There are 3 files on camera
#1 IMG_4001.CR3               53 MB  6000x4000 image/x-canon-cr3
#2 IMG_4001.JPG               12 MB  6000x4000 image/jpeg
#3 IMG_4002.JPG               12 MB  6000x4000 image/jpeg
"""


class TestListFiles:
    def test_parses_file_numbers_and_names(self):
        with patch(f"{CLI}.run", return_value=_proc(LIST_OUTPUT)):
            files = cli_grab.list_files()
        assert files == [(1, "IMG_4001.CR3"), (2, "IMG_4001.JPG"), (3, "IMG_4002.JPG")]

    def test_returns_empty_for_no_matches(self):
        with patch(f"{CLI}.run", return_value=_proc("There are 0 files on camera\n")):
            files = cli_grab.list_files()
        assert files == []

    def test_raises_grab_error_on_gphoto2_failure(self):
        with patch(f"{CLI}.run", return_value=_proc_err("Could not claim interface 0")):
            with pytest.raises(GrabError, match="list-files failed"):
                cli_grab.list_files()


class TestUnmountGvfs:
    def test_unmounts_gphoto2_mounts(self):
        list_output = "Mount(1): Canon EOS M50 Mark II -> gphoto2://[usb:001,006]//\n"
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if "--list" in cmd:
                return _proc(list_output)
            return _proc()

        with patch(f"{GVFS}.run", side_effect=fake_run):
            gvfs.unmount_gvfs()

        assert any("-u" in c for c in calls)

    def test_no_unmount_when_nothing_mounted(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _proc("No mounts found\n")

        with patch(f"{GVFS}.run", side_effect=fake_run):
            gvfs.unmount_gvfs()

        assert not any("-u" in c for c in calls)


class TestDownload:
    def test_returns_dest_path_on_success(self, tmp_path):
        with patch(f"{CLI}.run", return_value=_proc()):
            result = cli_grab.download(3, "IMG_4002.JPG", tmp_path, force=False)
        assert result == tmp_path / "IMG_4002.JPG"

    def test_returns_none_for_existing_file_without_force(self, tmp_path):
        (tmp_path / "IMG_4002.JPG").touch()
        with patch(f"{CLI}.run") as mock_run:
            result = cli_grab.download(3, "IMG_4002.JPG", tmp_path, force=False)
        assert result is None
        mock_run.assert_not_called()

    def test_force_overwrites_existing_file(self, tmp_path):
        (tmp_path / "IMG_4002.JPG").touch()
        with patch(f"{CLI}.run", return_value=_proc()) as mock_run:
            result = cli_grab.download(3, "IMG_4002.JPG", tmp_path, force=True)
        assert result == tmp_path / "IMG_4002.JPG"
        cmd = mock_run.call_args[0][0]
        assert "--force-overwrite" in cmd

    def test_raises_grab_error_on_gphoto2_failure(self, tmp_path):
        with patch(f"{CLI}.run", return_value=_proc_err("error")):
            with pytest.raises(GrabError, match="Download failed"):
                cli_grab.download(3, "IMG_4002.JPG", tmp_path, force=False)


class TestGrabLatest:
    def test_returns_path_of_downloaded_file(self, tmp_path):
        dest = tmp_path / "IMG_4002.JPG"
        with patch(f"{SVC}.unmount_gvfs"), \
             patch(f"{SVC}.list_files", return_value=[(1, "IMG_4001.JPG"), (2, "IMG_4002.JPG")]), \
             patch(f"{SVC}.download", return_value=dest) as mock_dl:
            result = grab_service.grab_latest(tmp_path)
        assert result == dest
        mock_dl.assert_called_once_with(2, "IMG_4002.JPG", tmp_path, False)

    def test_returns_none_when_no_files(self, tmp_path):
        with patch(f"{SVC}.unmount_gvfs"), \
             patch(f"{SVC}.list_files", return_value=[]):
            result = grab_service.grab_latest(tmp_path)
        assert result is None

    def test_returns_none_when_file_already_exists(self, tmp_path):
        with patch(f"{SVC}.unmount_gvfs"), \
             patch(f"{SVC}.list_files", return_value=[(2, "IMG_4002.JPG")]), \
             patch(f"{SVC}.download", return_value=None):
            result = grab_service.grab_latest(tmp_path)
        assert result is None

    def test_propagates_grab_error(self, tmp_path):
        with patch(f"{SVC}.unmount_gvfs"), \
             patch(f"{SVC}.list_files", side_effect=GrabError("camera gone")):
            with pytest.raises(GrabError, match="camera gone"):
                grab_service.grab_latest(tmp_path)


class TestPoll:
    def test_downloads_new_files_as_they_appear(self, tmp_path):
        call_count = 0

        def fake_list():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [(1, "IMG_4001.JPG")]
            if call_count == 2:
                return [(1, "IMG_4001.JPG"), (2, "IMG_4002.JPG")]
            raise KeyboardInterrupt

        with patch(f"{SVC}.unmount_gvfs"), \
             patch(f"{SVC}.list_files", side_effect=fake_list), \
             patch(f"{SVC}.download", return_value=tmp_path / "IMG_4002.JPG") as mock_dl, \
             patch(f"{SVC}.time.sleep"):
            grab_service.poll(tmp_path, interval=1.0)

        mock_dl.assert_called_once_with(2, "IMG_4002.JPG", tmp_path, False)

    def test_does_not_re_download_seen_files(self, tmp_path):
        call_count = 0

        def fake_list():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [(1, "IMG_4001.JPG")]
            if call_count == 2:
                return [(1, "IMG_4001.JPG"), (2, "IMG_4002.JPG")]
            if call_count == 3:
                return [(1, "IMG_4001.JPG"), (2, "IMG_4002.JPG")]
            raise KeyboardInterrupt

        with patch(f"{SVC}.unmount_gvfs"), \
             patch(f"{SVC}.list_files", side_effect=fake_list), \
             patch(f"{SVC}.download", return_value=tmp_path / "IMG_4002.JPG") as mock_dl, \
             patch(f"{SVC}.time.sleep"):
            grab_service.poll(tmp_path, interval=1.0)

        mock_dl.assert_called_once_with(2, "IMG_4002.JPG", tmp_path, False)

    def test_empty_camera_at_start(self, tmp_path):
        call_count = 0

        def fake_list():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return []
            if call_count == 2:
                return [(1, "IMG_4001.JPG")]
            raise KeyboardInterrupt

        with patch(f"{SVC}.unmount_gvfs"), \
             patch(f"{SVC}.list_files", side_effect=fake_list), \
             patch(f"{SVC}.download", return_value=tmp_path / "IMG_4001.JPG") as mock_dl, \
             patch(f"{SVC}.time.sleep"):
            grab_service.poll(tmp_path, interval=1.0)

        mock_dl.assert_called_once_with(1, "IMG_4001.JPG", tmp_path, False)
