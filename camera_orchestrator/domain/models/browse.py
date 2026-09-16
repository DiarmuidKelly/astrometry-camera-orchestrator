"""Browse value objects — read-only views of the capture tree.

A UI (web or CLI) needs to answer three questions about `incoming/`: what
sessions exist, what one session recorded, and what files are actually sitting
on disk. These DTOs carry exactly that and nothing else — they reuse
TargetInfo from the session model rather than restating it.

Card-only runs (`download: false`) record camera-side filenames that never land
on disk, so every count here is explicit about *recorded in the manifest* versus
*present on disk*; the UI is expected to show both honestly.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .session import PhaseKind, TargetInfo

FileKind = Literal["raw", "jpeg", "solved_sidecar", "preview", "manifest", "other"]


class FileEntry(BaseModel):
    """One file sitting on disk, as the browser should render it."""

    name: str = Field(description="Basename of the file, e.g. IMG_3846.CR2.")
    path: str = Field(description="Absolute path of the file on the host.")
    size_bytes: int = Field(description="File size in bytes.")
    modified_at: datetime = Field(description="UTC mtime of the file.")
    kind: FileKind = Field(description="Coarse file class derived from the name/suffix: raw, jpeg, solved_sidecar, preview, manifest or other.")


class FrameCounts(BaseModel):
    """Frames recorded in a manifest, summed across repeated phases of a kind."""

    light: int = Field(default=0, description="Light frames recorded, summed over every light phase.")
    dark: int = Field(default=0, description="Dark frames recorded, summed over every dark phase.")
    bias: int = Field(default=0, description="Bias frames recorded, summed over every bias phase.")
    total: int = Field(default=0, description="All recorded frames — light + dark + bias.")


class SessionEntry(BaseModel):
    """A session folder summarised for a listing — manifest facts plus disk facts."""

    session_id: str = Field(description="Session identifier from the manifest; falls back to the folder name when there is no readable manifest.")
    folder_name: str = Field(description="Actual folder name on disk. May disagree with session_id if the folder was renamed by hand.")
    path: str = Field(description="Absolute path of the session folder.")
    name: Optional[str] = Field(default=None, description="Human label (the target) from the manifest, else parsed from the '<YYYYMMDD>-<name>' folder name.")
    session_date: Optional[date] = Field(default=None, description="Date parsed from the folder's YYYYMMDD prefix. None if the folder is not date-prefixed.")
    modified_at: datetime = Field(description="UTC mtime of the session folder itself.")
    has_manifest: bool = Field(description="True if a session.json is present in the folder.")
    manifest_readable: bool = Field(description="True if that session.json parsed. False means a half-written/corrupt manifest — the folder is still listed, with counts zeroed.")
    download: bool = Field(default=False, description="Manifest's download flag: True if frames were transferred to disk, False if they stayed on the card.")
    target: Optional[TargetInfo] = Field(default=None, description="Solved pointing from `align`, if this session has one. None when no align ran or the manifest is unreadable.")
    started_at: Optional[datetime] = Field(default=None, description="UTC start of the first sequence run, from the manifest.")
    ended_at: Optional[datetime] = Field(default=None, description="UTC end of the last sequence run, from the manifest.")
    frames: FrameCounts = Field(default_factory=FrameCounts, description="Recorded frame counts aggregated by kind across all phases.")
    phase_count: int = Field(default=0, description="Number of phase records in the manifest. 0 means align-only (or nothing has run).")
    phase_kinds: list[PhaseKind] = Field(default_factory=list, description="Kinds of each phase in manifest order, so repeats are visible (e.g. ['light','light','dark']).")
    files_recorded: int = Field(default=0, description="Filenames listed across all phases of the manifest — card-side names for a card-only run.")
    files_present: int = Field(default=0, description="How many of those recorded filenames actually exist in the session folder. 0 for a card-only run.")
    files_on_disk: int = Field(default=0, description="Total files present in the session folder (one level deep), manifest included.")
    bytes_on_disk: int = Field(default=0, description="Total size in bytes of the files present in the session folder.")
    subdirectories: list[str] = Field(default_factory=list, description="Names of any nested directories inside the session folder (not descended into).")


class DirectoryListing(BaseModel):
    """One level of a directory: its sessions, its loose files, its other folders."""

    path: str = Field(description="Absolute path of the directory listed.")
    name: str = Field(description="Basename of that directory.")
    is_root: bool = Field(description="True when this listing is the configured browse root itself.")
    parent: Optional[str] = Field(default=None, description="Absolute path of the parent directory, or None when this is the root (there is nothing above it to browse).")
    sessions: list[SessionEntry] = Field(default_factory=list, description="Session folders directly inside, newest first.")
    files: list[FileEntry] = Field(default_factory=list, description="Loose files directly inside, sorted by name.")
    directories: list[str] = Field(default_factory=list, description="Names of non-session subdirectories directly inside.")
