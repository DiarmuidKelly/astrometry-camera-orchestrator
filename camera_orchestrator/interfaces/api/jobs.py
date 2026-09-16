"""In-memory job registry — long-running work behind a short HTTP request.

Every capture use-case is synchronous and slow: a sequence of 32 lights plus
darks and bias runs for hours, and an align takes 10-90 s because the plate solve
is a Docker call bounded by `solver.cpulimit`. None of that fits inside a request,
so the routes hand the work to a worker thread and return a `Job` immediately.

Three rules the registry enforces:

- **One camera job at a time.** The USB device is single-user, and queueing
  surprise exposures in the dark is worse than a 409 — a second camera job is
  rejected with JobConflictError. `batch` and `solve` never touch the camera and
  run alongside freely.
- **Confirmation is a blocking callback.** `SequenceService.run` calls
  `before_phase(kind)` and blocks inside it (the CLI passes `input()`). Here the
  callback parks the job in `awaiting_confirmation` on a `threading.Event` until
  `POST /api/jobs/{id}/confirm`, with a generous timeout so a closed browser tab
  fails the job instead of wedging the camera for the night.
- **Cancellation is cooperative.** libgphoto2 calls cannot be interrupted, so a
  cancel sets a flag; runners observe it at their progress callbacks (between
  frames, between images) and raise out of the service.

State is deliberately ephemeral — process memory, no database. CLAUDE.md puts
persistence behind a port, and a job that cannot outlive the process that runs it
has nothing to persist.
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import BaseModel

from camera_orchestrator.interfaces.api.models import (
    ACTIVE_STATES,
    CAMERA_KINDS,
    Job,
    JobKind,
    JobPrompt,
    JobProgress,
    JobState,
)
from camera_orchestrator.log import get_logger

log = get_logger("camera_orchestrator.api.jobs")

# How long a job blocked on a confirmation waits before giving up. Long enough to
# walk to the scope and cap the lens; short enough that a forgotten tab does not
# hold the camera until sunrise.
DEFAULT_CONFIRM_TIMEOUT_S = 1800.0

# Distinguishes "leave the prompt alone" from "clear the prompt" in _update,
# since None is a meaningful value for that field.
_UNSET: Any = object()

# The unit of work: given a context (progress, prompts, cancellation), return a
# JSON-encodable result — typically a Pydantic DTO from a service call.
JobRunner = Callable[["JobContext"], Any]


class JobConflictError(Exception):
    """A second camera job was requested while one is already in flight (409)."""


class JobNotFoundError(Exception):
    """No job with that id exists in this process (404)."""


class JobCancelledError(Exception):
    """Raised inside a runner when the job has been cancelled.

    Propagates out of the service call (a progress callback raising aborts the
    workflow), and the worker turns it into state='cancelled' rather than a
    failure.
    """


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _encode(value: Any) -> Any:
    """Make a service result JSON-safe (DTOs are Pydantic; anything else passes)."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


class _Record:
    """A job plus the thread primitives that drive it."""

    def __init__(self, job: Job):
        self.job = job
        self.version = 0
        self.cancel = threading.Event()
        self.confirm = threading.Event()


class JobContext:
    """The handle a runner uses to report progress and ask questions."""

    def __init__(self, registry: "JobRegistry", record: _Record):
        self._registry = registry
        self._record = record

    @property
    def job_id(self) -> str:
        """The id this job was registered under."""
        return self._record.job.id

    @property
    def cancelled(self) -> bool:
        """True once a cancel has been requested."""
        return self._record.cancel.is_set()

    def check_cancelled(self) -> None:
        """Abort the runner if a cancel is pending. Call from progress callbacks."""
        if self.cancelled:
            raise JobCancelledError(f"job {self.job_id} cancelled")

    def set_progress(self, current: int, total: int, label: str = "") -> None:
        """Publish progress; also a cancellation checkpoint."""
        self._registry._update(
            self._record, progress=JobProgress(current=current, total=total, label=label))
        self.check_cancelled()

    def prompt(self, kind: str, message: str) -> None:
        """Park the job in `awaiting_confirmation` and block until confirmed.

        Raises:
            JobCancelledError: The job was cancelled while waiting.
            TimeoutError: Nobody confirmed within the registry's timeout.
        """
        self.check_cancelled()
        self._registry._update(
            self._record,
            state="awaiting_confirmation",
            prompt=JobPrompt(kind=kind, message=message),
        )
        confirmed = self._record.confirm.wait(self._registry.confirm_timeout)
        self._record.confirm.clear()
        self._registry._update(self._record, state="running", prompt=None)
        self.check_cancelled()
        if not confirmed:
            raise TimeoutError(
                f"no confirmation for '{kind}' within "
                f"{self._registry.confirm_timeout:.0f}s — aborting so the camera is released"
            )


class JobRegistry:
    """Tracks every job this process has run, and runs new ones on worker threads."""

    def __init__(self, confirm_timeout: float = DEFAULT_CONFIRM_TIMEOUT_S):
        """Args:
            confirm_timeout: Seconds a job waits on a confirmation prompt before
                failing. Shortened in tests.
        """
        self.confirm_timeout = confirm_timeout
        self._records: dict[str, _Record] = {}
        self._order: list[str] = []
        # Guards every field of every record, and wakes SSE watchers on change.
        self._cond = threading.Condition()

    # -- queries -----------------------------------------------------------

    def get(self, job_id: str) -> Job:
        """Snapshot of one job. Raises JobNotFoundError if it is unknown."""
        with self._cond:
            return self._require(job_id).job.model_copy(deep=True)

    def list(self) -> list[Job]:
        """Snapshots of every job, newest first."""
        with self._cond:
            return [self._records[i].job.model_copy(deep=True) for i in reversed(self._order)]

    def version_of(self, job_id: str) -> int:
        """Current change counter for a job — the cursor an SSE stream holds."""
        with self._cond:
            return self._require(job_id).version

    def wait(self, job_id: str, version: int, timeout: float) -> tuple[int, Job]:
        """Block until the job changes past `version`, or `timeout` elapses.

        Returns the (possibly unchanged) version and snapshot either way, so the
        caller can use a timeout as a keep-alive tick.
        """
        with self._cond:
            record = self._require(job_id)
            if record.version == version:
                self._cond.wait(timeout)
            return record.version, record.job.model_copy(deep=True)

    # -- lifecycle ---------------------------------------------------------

    def submit(self, kind: JobKind, runner: JobRunner) -> Job:
        """Register a job and start its worker thread.

        Raises:
            JobConflictError: `kind` drives the camera and another camera job is
                already active.
        """
        with self._cond:
            if kind in CAMERA_KINDS:
                busy = self._active_camera_job()
                if busy is not None:
                    raise JobConflictError(
                        f"camera is busy with {busy.kind} job {busy.id} ({busy.state}) — "
                        f"cancel it or wait for it to finish"
                    )
            record = _Record(Job(id=uuid.uuid4().hex, kind=kind,
                                 state="pending", created_at=_utc_now()))
            self._records[record.job.id] = record
            self._order.append(record.job.id)
            snapshot = record.job.model_copy(deep=True)

        thread = threading.Thread(
            target=self._run, args=(record, runner),
            name=f"job-{kind}-{record.job.id[:8]}", daemon=True)
        thread.start()
        return snapshot

    def confirm(self, job_id: str) -> Job:
        """Release a job blocked on a prompt. A no-op for any other state."""
        with self._cond:
            record = self._require(job_id)
        record.confirm.set()
        return self.get(job_id)

    def cancel(self, job_id: str) -> Job:
        """Request cancellation. Cooperative — see the module docstring.

        A job that has not started yet ends immediately; one waiting on a prompt
        is woken and unwinds; a running one stops at its next progress callback.
        Terminal jobs are returned unchanged (cancel is idempotent).
        """
        with self._cond:
            record = self._require(job_id)
            if record.job.state not in ACTIVE_STATES:
                return record.job.model_copy(deep=True)
        record.cancel.set()
        record.confirm.set()  # unblock a prompt so the runner can observe the cancel
        return self.get(job_id)

    # -- worker ------------------------------------------------------------

    def _run(self, record: _Record, runner: JobRunner) -> None:
        """Worker-thread body: run `runner` and record its outcome."""
        if record.cancel.is_set():  # cancelled before we got scheduled
            self._update(record, state="cancelled", ended_at=_utc_now())
            return

        self._update(record, state="running", started_at=_utc_now())
        try:
            result = runner(JobContext(self, record))
        except JobCancelledError:
            log.info("Job cancelled", extra={"job": record.job.id, "kind": record.job.kind})
            self._update(record, state="cancelled", ended_at=_utc_now(), prompt=None)
        except Exception as exc:
            # Every failure mode lands here — a CameraError, a solver timeout, a
            # missing folder. The job records it; the HTTP request that started
            # the job is long gone, so there is nobody to raise to.
            log.error("Job failed", extra={"job": record.job.id, "kind": record.job.kind,
                                           "error": str(exc)})
            self._update(record, state="failed", ended_at=_utc_now(), prompt=None,
                         error=str(exc) or exc.__class__.__name__)
        else:
            self._update(record, state="succeeded", ended_at=_utc_now(), prompt=None,
                         result=_encode(result))

    # -- internals ---------------------------------------------------------

    def _require(self, job_id: str) -> _Record:
        """The record for `job_id`. Caller holds the lock."""
        record = self._records.get(job_id)
        if record is None:
            raise JobNotFoundError(f"no job with id '{job_id}'")
        return record

    def camera_job_holding_device(self) -> Job | None:
        """The camera job that currently owns the hardware, if any.

        Deliberately excludes 'awaiting_confirmation': a sequence paused on the
        lens-cap prompt has released the connection between phases, and live
        view is *wanted* there — it is how you see the cap actually go on.

        Used by the live-view routes to refuse before touching the device.
        CameraSession's lock already makes concurrent access safe, but that lock
        is only held for the frames themselves; between phases, and during the
        card-listing reconnect poll, it is free. Without this check a preview
        could slip in and re-open live view on the body mid-run.
        """
        with self._cond:
            job = self._active_camera_job()
        return job if job is not None and job.state == "running" else None

    def _active_camera_job(self) -> Job | None:
        """The camera job currently in flight, if any. Caller holds the lock."""
        for job_id in self._order:
            job = self._records[job_id].job
            if job.kind in CAMERA_KINDS and job.state in ACTIVE_STATES:
                return job
        return None

    def _update(
        self,
        record: _Record,
        *,
        state: JobState | None = None,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        progress: JobProgress | None = None,
        prompt: Any = _UNSET,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        """Mutate a job and wake every SSE watcher.

        Patch-style: only the arguments passed are applied. `prompt` needs the
        _UNSET sentinel because clearing it (prompt=None on confirm) is a real
        update, not an absence.
        """
        with self._cond:
            job = record.job
            if state is not None:
                job.state = state
            if started_at is not None:
                job.started_at = started_at
            if ended_at is not None:
                job.ended_at = ended_at
            if progress is not None:
                job.progress = progress
            if result is not None:
                job.result = result
            if error is not None:
                job.error = error
            if prompt is not _UNSET:
                job.prompt = prompt
            record.version += 1
            self._cond.notify_all()
