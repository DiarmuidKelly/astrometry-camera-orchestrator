"""Tests for BrowseService — real tmp_path tree + the real SidecarSessionRepository.

The unit under test is filesystem-shaped (sizes, mtimes, present-vs-recorded
files, path confinement), so faking the repo would fake away the interesting
half. Everything is written under tmp_path; nothing touches the real capture
tree. FakeRepo from test_sequence_service covers the one case the real repo
cannot produce on demand: a load() that raises.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from camera_orchestrator.adapters.storage.session_manifest import SidecarSessionRepository
from camera_orchestrator.application.browse_service import BrowsePathError, BrowseService
from camera_orchestrator.domain.models.session import (
    PhaseRecord,
    SessionManifest,
    TargetInfo,
)

NOW = datetime(2026, 9, 14, 21, 52, tzinfo=timezone.utc)


def _svc(root) -> BrowseService:
    return BrowseService(SidecarSessionRepository(), str(root))


def _phase(kind: str, count: int, files: list[str] | None = None) -> PhaseRecord:
    return PhaseRecord(kind=kind, count=count, iso="3200", shutter="2",
                       started_at=NOW, ended_at=NOW, files=files or [])


def _manifest(session_id: str, **kw) -> SessionManifest:
    kw.setdefault("name", session_id.partition("-")[2] or None)
    kw.setdefault("started_at", NOW)
    kw.setdefault("ended_at", NOW)
    return SessionManifest(session_id=session_id, **kw)


def _session_dir(root, folder: str, manifest: SessionManifest | None = None, files=()):
    """Create a session folder with an optional manifest and some real files."""
    d = root / folder
    d.mkdir(parents=True, exist_ok=True)
    for name in files:
        (d / name).write_bytes(b"x" * 10)
    if manifest is not None:
        SidecarSessionRepository().save(manifest, str(d))
    return d


def test_empty_root_lists_nothing(tmp_path):
    listing = _svc(tmp_path).list_directory()
    assert listing.sessions == [] and listing.files == []
    assert listing.is_root is True and listing.parent is None   # nothing above the root


def test_repeated_light_phases_aggregate(tmp_path):
    manifest = _manifest("20260914-andromeda", phases=[
        _phase("light", 32), _phase("light", 32), _phase("dark", 16), _phase("bias", 16),
    ])
    _session_dir(tmp_path, "20260914-andromeda", manifest)

    entry = _svc(tmp_path).list_sessions()[0]
    assert entry.frames.light == 64            # two 32-light runs summed, not overwritten
    assert entry.frames.dark == 16 and entry.frames.bias == 16
    assert entry.frames.total == 96
    assert entry.phase_kinds == ["light", "light", "dark", "bias"]  # repeats stay visible
    assert entry.phase_count == 4


def test_card_only_records_files_that_are_not_on_disk(tmp_path):
    manifest = _manifest("20260914-andromeda", download=False,
                         phases=[_phase("light", 3, ["IMG_1.CR2", "IMG_2.CR2", "IMG_3.CR2"])])
    _session_dir(tmp_path, "20260914-andromeda", manifest)

    entry = _svc(tmp_path).describe_session(str(tmp_path / "20260914-andromeda"))
    assert entry.download is False
    assert entry.files_recorded == 3          # the manifest knows three card-side names
    assert entry.files_present == 0           # none of them ever landed on disk
    assert entry.files_on_disk == 1           # only session.json is actually there


def test_downloaded_session_counts_present_files_and_bytes(tmp_path):
    manifest = _manifest("20260915-m33", download=True,
                         phases=[_phase("light", 2, ["IMG_1.CR2", "IMG_2.CR2"])])
    _session_dir(tmp_path, "20260915-m33", manifest, files=["IMG_1.CR2", "IMG_2.CR2"])

    entry = _svc(tmp_path).describe_session(str(tmp_path / "20260915-m33"))
    assert entry.files_recorded == 2 and entry.files_present == 2   # recorded == on disk
    assert entry.bytes_on_disk > 20                                 # frames + manifest bytes


def test_malformed_manifest_degrades_gracefully(tmp_path):
    d = _session_dir(tmp_path, "20260914-crashed")
    (d / "session.json").write_text('{"session_id": "20260914-crash')   # truncated mid-write

    entry = _svc(tmp_path).list_sessions()[0]
    assert entry.has_manifest is True          # the file is there ...
    assert entry.manifest_readable is False    # ... it just does not parse
    assert entry.session_id == "20260914-crashed"   # falls back to the folder name
    assert entry.frames.total == 0 and entry.target is None


def test_repo_error_does_not_break_a_listing(tmp_path):
    class ExplodingRepo:
        def save(self, manifest, session_dir): raise AssertionError("not used")
        def exists(self, session_dir): return True
        def load(self, session_dir): raise OSError("disk went away")

    _session_dir(tmp_path, "20260914-orion")
    entry = BrowseService(ExplodingRepo(), str(tmp_path)).list_sessions()[0]
    assert entry.manifest_readable is False    # a raising port degrades, never 500s the UI


def test_path_traversal_is_rejected(tmp_path):
    root = tmp_path / "incoming"
    root.mkdir()
    (tmp_path / "secrets").mkdir()
    svc = _svc(root)

    with pytest.raises(BrowsePathError):
        svc.list_directory("../secrets")                  # relative escape
    with pytest.raises(BrowsePathError):
        svc.list_directory(str(tmp_path / "secrets"))     # absolute escape
    with pytest.raises(BrowsePathError):
        svc.list_directory("20260914-x/../../secrets")    # escape via a session folder


def test_root_itself_and_children_resolve(tmp_path):
    _session_dir(tmp_path, "20260914-orion")
    svc = _svc(tmp_path)
    assert svc.resolve() == tmp_path.resolve()
    assert svc.resolve("20260914-orion") == (tmp_path / "20260914-orion").resolve()  # relative to root


def test_missing_directory_is_rejected(tmp_path):
    with pytest.raises(BrowsePathError):
        _svc(tmp_path).list_directory("no-such-session")   # inside the root, but not a dir


def test_loose_files_at_the_root_are_listed_and_classified(tmp_path):
    (tmp_path / "IMG_3797.JPG").write_bytes(b"x" * 5)
    (tmp_path / "IMG_3798.CR2").write_bytes(b"x" * 5)
    (tmp_path / "IMG_3797_solved.png").write_bytes(b"x" * 5)
    (tmp_path / "IMG_3797_solved.json").write_bytes(b"x" * 5)
    (tmp_path / "notes.txt").write_bytes(b"x" * 5)
    _session_dir(tmp_path, "20260914-orion")

    listing = _svc(tmp_path).list_directory()
    kinds = {f.name: f.kind for f in listing.files}
    assert kinds == {
        "IMG_3797.JPG": "jpeg", "IMG_3798.CR2": "raw",
        "IMG_3797_solved.png": "preview", "IMG_3797_solved.json": "solved_sidecar",
        "notes.txt": "other",
    }
    assert [s.folder_name for s in listing.sessions] == ["20260914-orion"]  # folders never leak into files
    assert all(f.size_bytes == 5 for f in listing.files)


def test_sessions_are_newest_first(tmp_path):
    for folder in ("20260914-andromeda", "20260916-m33", "20260915-orion"):
        _session_dir(tmp_path, folder, _manifest(folder))

    ids = [s.session_id for s in _svc(tmp_path).list_sessions()]
    assert ids == ["20260916-m33", "20260915-orion", "20260914-andromeda"]  # date prefix, descending


def test_align_only_session_has_target_and_no_phases(tmp_path):
    manifest = _manifest("20260914-andromeda", started_at=None, ended_at=None, phases=[],
                         target=TargetInfo(solved=True, center_ra_deg=11.511,
                                           center_dec_deg=40.597,
                                           preview="IMG_3797_solved.png", frame="IMG_3797.JPG"))
    _session_dir(tmp_path, "20260914-andromeda", manifest)

    entry = _svc(tmp_path).list_sessions()[0]
    assert entry.phase_count == 0 and entry.frames.total == 0   # align ran, sequence never did
    assert entry.target is not None and entry.target.center_ra_deg == 11.511
    assert entry.session_date.isoformat() == "2026-09-14"       # parsed from the folder prefix


def test_target_preview_and_frame_come_back_as_resolvable_paths(tmp_path):
    """The manifest stores basenames; a browse client only has the listing.

    A bare 'IMG_3797_solved.png' resolves against the browse *root*, not the
    session folder, so a UI putting it straight into an <img> gets a 404.
    """
    manifest = _manifest("20260914-andromeda", phases=[],
                         target=TargetInfo(solved=True, preview="IMG_3797_solved.png",
                                           frame="IMG_3797.JPG"))
    d = _session_dir(tmp_path, "20260914-andromeda", manifest,
                     files=["IMG_3797_solved.png", "IMG_3797.JPG"])

    svc = _svc(tmp_path)
    target = svc.list_sessions()[0].target
    assert target.preview == str(d / "IMG_3797_solved.png")
    assert target.frame == str(d / "IMG_3797.JPG")
    # And the path survives confinement, i.e. it is fetchable via /api/files/raw.
    assert svc.resolve(target.preview).is_file()


def test_target_preview_stays_none_when_the_align_never_solved(tmp_path):
    _session_dir(tmp_path, "20260914-orion",
                 _manifest("20260914-orion", phases=[],
                           target=TargetInfo(solved=False, frame="IMG_1.JPG")))

    target = _svc(tmp_path).list_sessions()[0].target
    assert target.solved is False and target.preview is None   # nothing to render


def test_sequence_without_align_has_null_target(tmp_path):
    _session_dir(tmp_path, "20260914-orion",
                 _manifest("20260914-orion", phases=[_phase("light", 5)]))

    entry = _svc(tmp_path).list_sessions()[0]
    assert entry.target is None                 # no align ever ran against this session
    assert entry.frames.light == 5


def test_renamed_folder_keeps_both_names(tmp_path):
    _session_dir(tmp_path, "20260914-renamed-by-hand", _manifest("20260914-andromeda"))

    entry = _svc(tmp_path).list_sessions()[0]
    assert entry.session_id == "20260914-andromeda"          # what the manifest claims
    assert entry.folder_name == "20260914-renamed-by-hand"   # what the disk says


def test_nested_stray_directory_is_reported_not_descended(tmp_path):
    d = _session_dir(tmp_path, "20260914-orion", _manifest("20260914-orion"),
                     files=["IMG_1.CR2"])
    (d / "rejects").mkdir()
    (d / "rejects" / "IMG_bad.CR2").write_bytes(b"x" * 10)

    entry = _svc(tmp_path).describe_session(str(d))
    assert [s.name for s in entry.subdirectories] == ["rejects"]
    assert entry.subdirectories[0].path == str(d / "rejects")   # navigable, not just a label
    assert entry.files_on_disk == 2      # IMG_1.CR2 + session.json — the nested file is not counted


def test_undated_folder_with_manifest_still_counts_as_a_session(tmp_path):
    _session_dir(tmp_path, "scratch", _manifest("scratch", name=None))

    listing = _svc(tmp_path).list_directory()
    assert [s.folder_name for s in listing.sessions] == ["scratch"]
    assert listing.sessions[0].session_date is None      # no YYYYMMDD prefix to parse
    assert listing.directories == []                     # it is a session, not a plain folder


def test_plain_directory_is_not_a_session(tmp_path):
    (tmp_path / "tools").mkdir()

    listing = _svc(tmp_path).list_directory()
    assert [d.name for d in listing.directories] == ["tools"] and listing.sessions == []


def test_plain_directory_carries_an_absolute_path_the_ui_can_browse_to(tmp_path):
    """The browser navigates by handing `path` straight back to list_directory().

    A bare name would force the client to rebuild the path by concatenation, which
    guesses wrong as soon as the listed folder is not the browse root itself.
    """
    nested = tmp_path / "raw-dumps"
    (nested / "annotated").mkdir(parents=True)

    entry = _svc(tmp_path).list_directory(str(nested)).directories[0]
    assert entry.name == "annotated"
    assert entry.path == str(nested / "annotated")
    # And the path round-trips: feeding it back lists that directory.
    assert _svc(tmp_path).list_directory(entry.path).path == str(nested / "annotated")


def test_listing_a_session_folder_shows_its_files(tmp_path):
    d = _session_dir(tmp_path, "20260914-orion", _manifest("20260914-orion"),
                     files=["IMG_1.CR2", "IMG_2.CR2"])

    listing = _svc(tmp_path).list_directory(str(d))
    assert listing.is_root is False and listing.parent == str(tmp_path.resolve())
    assert [f.name for f in listing.files] == ["IMG_1.CR2", "IMG_2.CR2", "session.json"]  # name order
