"""Session path resolution — the `<root>/<YYYYMMDD>-<name>` convention.

The single place that turns a session *name* into the folder it lives in, so
`align` and `sequence` (and a future API route) always agree on where a night's
data lands. Plain arguments only — no argparse, no Config — so any interface can
call it; the CLI keeps its own Namespace adapter.

Inverse of `camera_orchestrator.application.sequence_service.request_name`,
which parses the label back out of a session folder name. Keep the two in step:
`request_name(resolve_session(root, name)[1]) == name`.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

# Folder prefix format: sortable and readable, one folder per night.
DATE_FORMAT = "%Y%m%d"


def resolve_session(
    root: str,
    name: str | None,
    today: date | None = None,
) -> tuple[str, str | None]:
    """Map a parent root + optional session name into (out_dir, session_dir).

    Args:
        root: Parent output directory (CLI `--out` or `grab.out_dir` from config).
        name: Session label, or None for a loose (unrecorded) run.
        today: Date used for the folder prefix. Defaults to `date.today()`;
            injectable so tests do not depend on the calendar.

    Returns:
        (out_dir, session_dir). Named: both are `<root>/<YYYYMMDD>-<name>` — the
        folder is created on write, not here. Unnamed (loose): out_dir is `root`
        and session_dir is None, so no manifest is written.
    """
    if not name:
        return root, None
    day = today or date.today()
    session_dir = str(Path(root) / f"{day:{DATE_FORMAT}}-{name}")
    return session_dir, session_dir
