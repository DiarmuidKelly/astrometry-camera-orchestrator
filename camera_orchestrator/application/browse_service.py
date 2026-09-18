"""Browse service — read-only views over the capture tree for a UI.

Backs a file/directory browser: what sessions exist under the capture root, what
one session recorded, and what is actually on disk. It reads manifests through
the SessionManifestRepository port (never json itself) and reuses
`request_name` for the '<YYYYMMDD>-<name>' folder convention.

Three rules shape the implementation:

- **Confined.** Every incoming path is resolved and must stay inside the root;
  anything that escapes raises BrowsePathError. The UI hands us paths straight
  from a browser, so '../../etc' has to die here.
- **Bounded.** Listings are one level deep, always. Session folders hold
  thousands of RAW files; nothing here walks a tree.
- **Forgiving.** A half-written session.json from a crashed run must not break a
  listing — the folder still appears, flagged `manifest_readable=False`.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from pathlib import Path

from camera_orchestrator.application.sequence_service import request_name
from camera_orchestrator.domain.models.browse import (
    DirectoryEntry,
    DirectoryListing,
    FileEntry,
    FileKind,
    FrameCounts,
    SessionEntry,
)
from camera_orchestrator.domain.models.session import PhaseKind, SessionManifest, TargetInfo
from camera_orchestrator.domain.ports.session_manifest import SessionManifestRepository

MANIFEST_NAME = "session.json"

# Suffix → coarse file class. Anything unlisted is "other".
_RAW_SUFFIXES = {".cr2", ".nef", ".arw", ".raf", ".dng", ".orf", ".rw2"}
_JPEG_SUFFIXES = {".jpg", ".jpeg"}
_PREVIEW_SUFFIXES = {".png", ".tif", ".tiff"}


class BrowsePathError(Exception):
    """A requested path escaped the browse root (or does not exist).

    Lives here rather than in domain/errors.py only to keep this module
    self-contained while the web UI lands; it belongs alongside CameraError.
    """


class BrowseService:
    """Lists sessions and directory contents under a confined capture root."""

    def __init__(self, manifest_repo: SessionManifestRepository, root: str):
        self._repo = manifest_repo
        self._root = Path(root).expanduser().resolve()

    @property
    def root(self) -> str:
        """The absolute, resolved directory every browse call is confined to."""
        return str(self._root)

    def resolve(self, path: str | None = None) -> Path:
        """Resolve `path` (absolute, or relative to the root) inside the root.

        Raises BrowsePathError if the resolved path escapes the root.
        """
        if path is None or path in ("", "."):
            return self._root
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self._root / candidate
        resolved = candidate.resolve()
        if resolved != self._root and self._root not in resolved.parents:
            raise BrowsePathError(f"path '{path}' is outside the browse root {self._root}")
        return resolved

    def list_sessions(self, path: str | None = None) -> list[SessionEntry]:
        """Session folders directly under `path` (the root by default), newest first."""
        directory = self._require_dir(path)
        entries = [
            self._describe_session(child)
            for child in _subdirectories(directory)
            if _is_session_dir(child)
        ]
        return _newest_first(entries)

    def list_directory(self, path: str | None = None) -> DirectoryListing:
        """One level of a directory: its sessions, loose files and other folders."""
        directory = self._require_dir(path)
        sessions: list[SessionEntry] = []
        directories: list[DirectoryEntry] = []
        for child in _subdirectories(directory):
            if _is_session_dir(child):
                sessions.append(self._describe_session(child))
            else:
                directories.append(_directory_entry(child))
        return DirectoryListing(
            path=str(directory),
            name=directory.name,
            is_root=directory == self._root,
            parent=None if directory == self._root else str(directory.parent),
            sessions=_newest_first(sessions),
            files=[_file_entry(f) for f in _files(directory)],
            directories=sorted(directories, key=lambda d: d.name),
        )

    def describe_session(self, path: str) -> SessionEntry:
        """Summarise one session folder — manifest facts plus what is on disk."""
        return self._describe_session(self._require_dir(path))

    def _require_dir(self, path: str | None) -> Path:
        directory = self.resolve(path)
        if not directory.is_dir():
            raise BrowsePathError(f"'{directory}' is not a directory")
        return directory

    def _describe_session(self, directory: Path) -> SessionEntry:
        manifest = self._load_manifest(directory)
        on_disk = {f.name: f for f in _files(directory)}

        recorded: list[str] = []
        kinds: list[PhaseKind] = []
        totals: dict[PhaseKind, int] = {"light": 0, "dark": 0, "bias": 0}
        if manifest is not None:
            for phase in manifest.phases:
                kinds.append(phase.kind)
                # Same kind can appear many times (two 32-light runs) — sum, don't set.
                totals[phase.kind] = totals.get(phase.kind, 0) + phase.count
                recorded.extend(phase.files)
        counts = FrameCounts(
            light=totals["light"], dark=totals["dark"], bias=totals["bias"],
            total=sum(totals.values()),
        )

        return SessionEntry(
            session_id=manifest.session_id if manifest is not None else directory.name,
            folder_name=directory.name,
            path=str(directory),
            name=(manifest.name if manifest is not None and manifest.name else request_name(str(directory))),
            session_date=_date_prefix(directory.name),
            modified_at=_mtime(directory),
            has_manifest=(directory / MANIFEST_NAME).is_file(),
            manifest_readable=manifest is not None,
            download=manifest.download if manifest is not None else False,
            target=_resolved_target(manifest, directory),
            started_at=manifest.started_at if manifest is not None else None,
            ended_at=manifest.ended_at if manifest is not None else None,
            frames=counts,
            phase_count=len(kinds),
            phase_kinds=kinds,
            files_recorded=len(recorded),
            files_present=sum(1 for n in recorded if n in on_disk),
            files_on_disk=len(on_disk),
            bytes_on_disk=sum(f.stat().st_size for f in on_disk.values()),
            subdirectories=[_directory_entry(c) for c in _subdirectories(directory)],
        )

    def _load_manifest(self, directory: Path) -> SessionManifest | None:
        """Manifest for a folder, or None if absent, corrupt or unreadable.

        A crashed run can leave half a session.json behind; the browser must
        degrade to 'session folder, no readable manifest' rather than blow up.
        """
        try:
            return self._repo.load(str(directory))
        except Exception:
            return None


def _is_session_dir(directory: Path) -> bool:
    """A session folder owns a session.json, or is named '<YYYYMMDD>[-name]'."""
    return (directory / MANIFEST_NAME).is_file() or _date_prefix(directory.name) is not None


def _date_prefix(name: str) -> date | None:
    """The YYYYMMDD prefix of a folder name as a date, or None if there isn't one."""
    head = name[:8]
    if len(name) > 8 and name[8] != "-":
        return None
    if not head.isdigit():
        return None
    try:
        return date(int(head[:4]), int(head[4:6]), int(head[6:8]))
    except ValueError:
        return None


def _subdirectories(directory: Path) -> list[Path]:
    """Immediate subdirectories — one level, never recursive."""
    return sorted((p for p in _scan(directory) if p.is_dir()), key=lambda p: p.name)


def _files(directory: Path) -> list[Path]:
    """Immediate files — one level, never recursive."""
    return sorted((p for p in _scan(directory) if p.is_file()), key=lambda p: p.name)


def _scan(directory: Path) -> list[Path]:
    try:
        return [Path(e.path) for e in os.scandir(directory)]
    except OSError:
        return []


def _resolved_target(manifest: SessionManifest | None, directory: Path) -> TargetInfo | None:
    """The manifest's target with `preview`/`frame` as paths a client can fetch.

    A manifest stores them as bare basenames relative to the session folder
    (`align` writes 'IMG_3897_solved.png'). A browser only ever sees the folder
    the name came from via this listing, so handing back the basename makes the
    preview unfetchable — it would be looked up against the browse root instead
    and 404. Absolutising here keeps the manifest format untouched while making
    the browse view self-contained.
    """
    if manifest is None or manifest.target is None:
        return None
    return manifest.target.model_copy(update={
        "preview": _in_session(manifest.target.preview, directory),
        "frame": _in_session(manifest.target.frame, directory),
    })


def _in_session(name: str | None, directory: Path) -> str | None:
    """Absolutise a manifest basename against its session folder."""
    if not name:
        return None
    return str(directory / name)


def _directory_entry(path: Path) -> DirectoryEntry:
    """A subdirectory as name + absolute path, so a UI can navigate straight into it."""
    return DirectoryEntry(name=path.name, path=str(path))


def _file_entry(path: Path) -> FileEntry:
    stat = path.stat()
    return FileEntry(
        name=path.name,
        path=str(path),
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        kind=_file_kind(path),
    )


def _file_kind(path: Path) -> FileKind:
    name, suffix = path.name.lower(), path.suffix.lower()
    if name == MANIFEST_NAME:
        return "manifest"
    if name.endswith("_solved.json"):
        return "solved_sidecar"
    if suffix in _RAW_SUFFIXES:
        return "raw"
    if suffix in _JPEG_SUFFIXES:
        return "jpeg"
    if suffix in _PREVIEW_SUFFIXES:
        return "preview"
    return "other"


def _mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _newest_first(entries: list[SessionEntry]) -> list[SessionEntry]:
    """Sort by the folder's date prefix, falling back to its mtime; ties by name."""
    return sorted(
        entries,
        key=lambda e: (e.session_date or e.modified_at.date(), e.modified_at, e.folder_name),
        reverse=True,
    )
