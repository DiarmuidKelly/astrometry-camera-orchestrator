"""HTTP-facing value objects — the Job record and the POST request bodies.

These are *transport* shapes, not domain shapes, so they live here rather than in
`domain/models/`: a Job is an artefact of HTTP being request/response while a
capture runs for minutes, and it would mean nothing to the CLI.

The request bodies are deliberately thin: each one carries the fields of an
existing DTO (`CaptureRequest`, `AlignRequest`, `SequenceRequest`) plus the
session `name` the CLI takes as a flag, and a `to_request()` that builds the real
DTO. No parallel request shapes — the DTO stays the single definition of what a
capture is.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from camera_orchestrator.config import Config
from camera_orchestrator.domain.models.align import AlignRequest
from camera_orchestrator.domain.models.browse import SessionEntry
from camera_orchestrator.domain.models.camera import CameraStatus, CaptureRequest, ImageFormat
from camera_orchestrator.domain.models.camera import FrameKind
from camera_orchestrator.domain.models.session import PhaseKind, SequenceRequest

JobKind = Literal["capture", "align", "sequence", "batch", "solve"]

JobState = Literal[
    "pending", "running", "awaiting_confirmation", "succeeded", "failed", "cancelled"
]

# Jobs that drive the camera. Only one of these may be in flight at a time (the
# hardware is single-user); batch/solve are CPU work and run alongside freely.
CAMERA_KINDS: frozenset[str] = frozenset({"capture", "align", "sequence"})

# States a job can still leave — anything else is terminal.
ACTIVE_STATES: frozenset[str] = frozenset({"pending", "running", "awaiting_confirmation"})


class JobProgress(BaseModel):
    """How far through a job is, for a progress bar."""

    current: int = Field(description="Units completed so far (frames, images, phases).")
    total: int = Field(description="Units expected in total. 0 when the total is not yet known.")
    label: str = Field(default="", description="Human description of what is happening now, e.g. 'light frames'.")


class JobPrompt(BaseModel):
    """A question the job is blocked on until POST /api/jobs/{id}/confirm."""

    kind: str = Field(description="What is being asked. For a sequence this is the phase kind: 'dark' or 'bias'.")
    message: str = Field(description="Text to show the user, e.g. 'Cover the lens for dark frames'.")


class Job(BaseModel):
    """A long-running unit of work, polled over HTTP or pushed on the job socket."""

    id: str = Field(description="Opaque job identifier (uuid4 hex).")
    kind: JobKind = Field(description="Which use-case this job runs.")
    state: JobState = Field(description="Lifecycle state. 'awaiting_confirmation' means the job is blocked on `prompt`.")
    created_at: Optional[datetime] = Field(default=None, description="UTC time the job was accepted.")
    started_at: Optional[datetime] = Field(default=None, description="UTC time the worker thread picked it up. Null until it runs.")
    ended_at: Optional[datetime] = Field(default=None, description="UTC time the job reached a terminal state. Null while active.")
    progress: Optional[JobProgress] = Field(default=None, description="Progress, once the job knows its totals. Null before then.")
    prompt: Optional[JobPrompt] = Field(default=None, description="Set only while state is 'awaiting_confirmation'; cleared on confirm.")
    result: Optional[Any] = Field(default=None, description="JSON-encoded service result on success (CaptureResult, AlignResult, SessionManifest, BatchSolveResult, SolveRecord). Null otherwise.")
    error: Optional[str] = Field(default=None, description="Failure message when state is 'failed'. Null otherwise.")


class JobList(BaseModel):
    """Every job this process knows about, newest first."""

    jobs: list[Job] = Field(default_factory=list, description="Jobs, newest first.")


# -- request bodies --------------------------------------------------------


class _SessionBody(BaseModel):
    """Shared session-targeting fields: an explicit folder, or a session name."""

    out_dir: Optional[str] = Field(default=None, description="Parent output directory. Defaults to grab.out_dir from config.")
    name: Optional[str] = Field(default=None, description="Session label. Frames land in <out_dir>/<YYYYMMDD>-<name>/ and a session.json is written. Omit for a loose, unrecorded run.")


class CaptureJobBody(_SessionBody):
    """POST /api/jobs/capture — the fields of CaptureRequest plus a session name."""

    iso: Optional[str] = Field(default=None, description="ISO setting, e.g. '800'. None leaves it unchanged.")
    shutter: Optional[str] = Field(default=None, description="Shutter speed, e.g. '2' or '1/60'. Ignored when bulb_seconds is set.")
    aperture: Optional[str] = Field(default=None, description="Aperture f-number, e.g. '4'. None leaves it unchanged.")
    image_format: Optional[ImageFormat] = Field(default=None, description="'raw', 'jpeg' or 'both'. None keeps the camera's current setting.")
    bulb_seconds: Optional[float] = Field(default=None, description="Bulb exposure length in seconds. Overrides shutter.")
    count: int = Field(default=1, ge=1, description="Number of frames to capture.")
    kind: FrameKind = Field(default="light", description="Frame type label.")
    download: bool = Field(default=False, description="Transfer each frame over USB (True) or shoot to the card only (False, the default).")
    select: Literal["all", "jpeg", "cr2"] = Field(default="all", description="Which of a shot's files to pull down when download is True.")

    def to_request(self, out_dir: str) -> CaptureRequest:
        """Build the domain CaptureRequest, with the resolved output directory."""
        return CaptureRequest(
            out_dir=out_dir,
            iso=self.iso,
            shutter=self.shutter,
            aperture=self.aperture,
            image_format=self.image_format,
            bulb_seconds=self.bulb_seconds,
            count=self.count,
            kind=self.kind,
            download=self.download,
            select=self.select,
        )


class AlignJobBody(_SessionBody):
    """POST /api/jobs/align — the fields of AlignRequest plus name/force."""

    iso: Optional[str] = Field(default=None, description="ISO setting, e.g. '800'.")
    shutter: Optional[str] = Field(default=None, description="Shutter speed, e.g. '2' or '1/60'. Ignored when bulb_seconds is set.")
    aperture: Optional[str] = Field(default=None, description="Aperture f-number, e.g. '4'.")
    bulb_seconds: Optional[float] = Field(default=None, description="Bulb exposure length in seconds. Overrides shutter.")
    force: bool = Field(default=False, description="Overwrite the target of a session that already has sequenced frames.")

    def to_request(self, out_dir: str) -> AlignRequest:
        """Build the domain AlignRequest, with the resolved output directory."""
        return AlignRequest(
            out_dir=out_dir,
            iso=self.iso,
            shutter=self.shutter,
            aperture=self.aperture,
            bulb_seconds=self.bulb_seconds,
        )


class SequenceJobBody(_SessionBody):
    """POST /api/jobs/sequence — the fields of SequenceRequest plus a session name."""

    iso: Optional[str] = Field(default=None, description="ISO for lights and darks, e.g. '800'.")
    shutter: Optional[str] = Field(default=None, description="Shutter for lights and darks. Bias always uses the fastest shutter.")
    aperture: Optional[str] = Field(default=None, description="Aperture f-number, e.g. '4'.")
    bulb_seconds: Optional[float] = Field(default=None, description="Bulb exposure for lights and darks.")
    lights: int = Field(default=0, ge=0, description="Number of light frames.")
    darks: int = Field(default=0, ge=0, description="Number of dark frames (lens capped).")
    bias: int = Field(default=0, ge=0, description="Number of bias frames (fastest shutter, lens capped).")
    download: bool = Field(default=False, description="Transfer frames over USB (True) or shoot to the card only (False, the default).")
    order: Optional[list[PhaseKind]] = Field(default=None, description="Order phases run in. Defaults to lights, darks, bias.")

    def to_request(self, out_dir: str) -> SequenceRequest:
        """Build the domain SequenceRequest, with the resolved output directory."""
        fields: dict[str, Any] = dict(
            out_dir=out_dir,
            iso=self.iso,
            shutter=self.shutter,
            aperture=self.aperture,
            bulb_seconds=self.bulb_seconds,
            lights=self.lights,
            darks=self.darks,
            bias=self.bias,
            download=self.download,
        )
        if self.order is not None:
            fields["order"] = self.order
        return SequenceRequest(**fields)


class BatchJobBody(BaseModel):
    """POST /api/jobs/batch — plate-solve every image in a folder."""

    folder: str = Field(description="Folder of images to solve (non-recursive).")
    annotate: bool = Field(default=False, description="Write annotated overlays into <folder>/annotated/.")
    reprocess: bool = Field(default=False, description="Re-solve images that already have a sidecar record.")
    mode: Optional[Literal["fast", "accurate"]] = Field(default=None, description="Override the solver mode from config for this run only.")
    cpulimit: Optional[int] = Field(default=None, description="Override the solver CPU-time limit (seconds) for this run only.")


class SolveJobBody(BaseModel):
    """POST /api/jobs/solve — plate-solve one image file in place."""

    file: str = Field(description="Path to the image file (JPEG, CR2, …).")
    annotate: bool = Field(default=False, description="Write an annotated overlay to <file>_solved.png alongside the source.")
    force: bool = Field(default=False, description="Re-solve even if a sidecar JSON already exists.")


# -- response envelopes ----------------------------------------------------


class HealthResponse(BaseModel):
    """GET /api/health — liveness plus the running version."""

    ok: bool = Field(default=True, description="Always true; the response arriving is the signal.")
    version: str = Field(description="Package version (the VERSION file).")


class CameraStatusResponse(BaseModel):
    """GET /api/camera/status — never raises, even with no camera plugged in.

    A disconnected camera is the normal state before the user plugs in, not an
    error, so the UI gets connected=False and a message rather than a 5xx.
    """

    connected: bool = Field(description="True if a status read succeeded just now.")
    session_open: bool = Field(description="True if the shared CameraSession currently holds a USB claim.")
    camera: Optional[CameraStatus] = Field(default=None, description="The camera's status when connected, null otherwise.")
    error: Optional[str] = Field(default=None, description="Why the status read failed, when connected is false.")


class SessionListResponse(BaseModel):
    """GET /api/sessions — session folders under the requested path."""

    sessions: list[SessionEntry] = Field(default_factory=list, description="Session folders, newest first.")


class ConfigUpdateResponse(BaseModel):
    """PUT /api/config — the config as saved, and where it was written."""

    config: Config = Field(description="The validated configuration now in force for this process.")
    path: str = Field(description="Absolute path of the YAML file that was written.")


class OkResponse(BaseModel):
    """A bare acknowledgement for side-effecting endpoints."""

    ok: bool = Field(default=True, description="True when the action was performed.")
