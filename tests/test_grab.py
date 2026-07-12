"""Tests for grab.py — all subprocess calls are mocked."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

# grab.py is a top-level script, not a package — import via sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))
import grab


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
        with patch("grab.run", return_value=_proc(LIST_OUTPUT)):
            files = grab.list_files()
        assert files == [(1, "IMG_4001.CR3"), (2, "IMG_4001.JPG"), (3, "IMG_4002.JPG")]

    def test_returns_empty_for_no_matches(self):
        with patch("grab.run", return_value=_proc("There are 0 files on camera\n")):
            files = grab.list_files()
        assert files == []

    def test_exits_on_gphoto2_error(self):
        with patch("grab.run", return_value=_proc_err("Could not claim interface 0")):
            with pytest.raises(SystemExit):
                grab.list_files()


class TestUnmountGvfs:
    def test_unmounts_gphoto2_mounts(self):
        list_output = "Mount(1): Canon EOS M50 Mark II -> gphoto2://[usb:001,006]//\n"
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if "--list" in cmd:
                return _proc(list_output)
            return _proc()

        with patch("grab.run", side_effect=fake_run):
            grab.unmount_gvfs()

        assert any("-u" in c for c in calls)

    def test_no_unmount_when_nothing_mounted(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _proc("No mounts found\n")

        with patch("grab.run", side_effect=fake_run):
            grab.unmount_gvfs()

        assert not any("-u" in c for c in calls)


class TestDownload:
    def test_downloads_file(self, tmp_path):
        with patch("grab.run", return_value=_proc()):
            result = grab._download(3, "IMG_4002.JPG", tmp_path, force=False)
        assert result is True

    def test_skips_existing_file_without_force(self, tmp_path):
        (tmp_path / "IMG_4002.JPG").touch()
        with patch("grab.run") as mock_run:
            result = grab._download(3, "IMG_4002.JPG", tmp_path, force=False)
        assert result is False
        mock_run.assert_not_called()

    def test_force_overwrites_existing_file(self, tmp_path):
        (tmp_path / "IMG_4002.JPG").touch()
        with patch("grab.run", return_value=_proc()) as mock_run:
            result = grab._download(3, "IMG_4002.JPG", tmp_path, force=True)
        assert result is True
        cmd = mock_run.call_args[0][0]
        assert "--force-overwrite" in cmd

    def test_returns_false_on_gphoto2_failure(self, tmp_path):
        with patch("grab.run", return_value=_proc_err("error")):
            result = grab._download(3, "IMG_4002.JPG", tmp_path, force=False)
        assert result is False


class TestGrabLatest:
    def test_downloads_latest_file(self, tmp_path):
        with patch("grab.unmount_gvfs"), \
             patch("grab.list_files", return_value=[(1, "IMG_4001.JPG"), (2, "IMG_4002.JPG")]), \
             patch("grab._download", return_value=True) as mock_dl:
            grab.grab_latest(tmp_path)
        mock_dl.assert_called_once_with(2, "IMG_4002.JPG", tmp_path, False)

    def test_exits_cleanly_when_no_files(self, tmp_path):
        with patch("grab.unmount_gvfs"), \
             patch("grab.list_files", return_value=[]):
            with pytest.raises(SystemExit) as exc:
                grab.grab_latest(tmp_path)
        assert exc.value.code == 0

    def test_exits_when_file_already_exists(self, tmp_path):
        (tmp_path / "IMG_4002.JPG").touch()
        with patch("grab.unmount_gvfs"), \
             patch("grab.list_files", return_value=[(2, "IMG_4002.JPG")]):
            with pytest.raises(SystemExit) as exc:
                grab.grab_latest(tmp_path)
        assert exc.value.code == 0


class TestPoll:
    def test_downloads_new_files_as_they_appear(self, tmp_path):
        call_count = 0

        def fake_list():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [(1, "IMG_4001.JPG")]   # baseline
            if call_count == 2:
                return [(1, "IMG_4001.JPG"), (2, "IMG_4002.JPG")]  # new file
            raise KeyboardInterrupt

        with patch("grab.unmount_gvfs"), \
             patch("grab.list_files", side_effect=fake_list), \
             patch("grab._download", return_value=True) as mock_dl, \
             patch("grab.time.sleep"):
            grab.poll(tmp_path, interval=1.0)

        mock_dl.assert_called_once_with(2, "IMG_4002.JPG", tmp_path, False)

    def test_does_not_re_download_seen_files(self, tmp_path):
        # #2 appears in first poll, #2 appears again in second poll — should only download once
        call_count = 0

        def fake_list():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [(1, "IMG_4001.JPG")]  # baseline
            if call_count == 2:
                return [(1, "IMG_4001.JPG"), (2, "IMG_4002.JPG")]  # new file
            if call_count == 3:
                return [(1, "IMG_4001.JPG"), (2, "IMG_4002.JPG")]  # same, no re-download
            raise KeyboardInterrupt

        with patch("grab.unmount_gvfs"), \
             patch("grab.list_files", side_effect=fake_list), \
             patch("grab._download", return_value=True) as mock_dl, \
             patch("grab.time.sleep"):
            grab.poll(tmp_path, interval=1.0)

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

        with patch("grab.unmount_gvfs"), \
             patch("grab.list_files", side_effect=fake_list), \
             patch("grab._download", return_value=True) as mock_dl, \
             patch("grab.time.sleep"):
            grab.poll(tmp_path, interval=1.0)

        mock_dl.assert_called_once_with(1, "IMG_4001.JPG", tmp_path, False)
