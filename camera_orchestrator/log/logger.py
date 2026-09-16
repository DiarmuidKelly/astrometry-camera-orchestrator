"""Structured logger for camera-orchestrator.

Provides a thin wrapper around Python's standard logging with:
- Human-readable console output by default
- Optional JSON line output (LOG_FORMAT=json env var or fmt="json")
- Structured key=value context via the `extra` dict (zerolog-style)
- Rotating file output — call setup_file_logging(path) once at startup

Usage:
    from camera_orchestrator.log import get_logger
    log = get_logger(__name__)
    log.info("solving image", extra={"image": "IMG_4341.JPG", "index": 1, "total": 113})
"""
from __future__ import annotations

import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from . import suppressed  # noqa: F401 — apply suppressions on import

# Default log destination, relative to wherever the app is run from.
DEFAULT_LOG_FILE = "log.log"

# The shared rotating file handler, once setup_file_logging() has run, plus the
# loggers it is attached to. Tracked explicitly because every module's logger
# sets propagate=False, so there is no common parent to hang it off.
_file_handler: RotatingFileHandler | None = None
_configured: list[logging.Logger] = []


def _track(logger: logging.Logger) -> None:
    if logger not in _configured:
        _configured.append(logger)


# Fields that are part of every LogRecord — never treat these as user extras
_LOGRECORD_FIELDS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
})


class _JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record, zerolog-style."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, val in record.__dict__.items():
            if key not in _LOGRECORD_FIELDS and not key.startswith("_"):
                payload[key] = val
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload)


class _TextFormatter(logging.Formatter):
    """Human-readable: timestamp  LEVEL  message  key=value ..."""

    _LEVEL_COLOURS = {
        "DEBUG":    "\033[37m",
        "INFO":     "\033[32m",
        "WARNING":  "\033[33m",
        "ERROR":    "\033[31m",
        "CRITICAL": "\033[35m",
    }
    _RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%H:%M:%S")
        colour = self._LEVEL_COLOURS.get(record.levelname, "")
        level = f"{colour}{record.levelname:<8}{self._RESET}" if sys.stdout.isatty() else f"{record.levelname:<8}"
        base = f"{ts}  {level}  {record.getMessage()}"
        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in _LOGRECORD_FIELDS and not k.startswith("_")
        }
        if extras:
            base += "  " + "  ".join(f"{k}={v}" for k, v in extras.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def get_logger(
    name: str,
    fmt: str | None = None,
    level: str | None = None,
) -> logging.Logger:
    """Return a named logger configured for camera-orchestrator.

    Resolution order for each setting (first wins):
      format: LOG_FORMAT env var → fmt argument → "text"
      level:  LOG_LEVEL env var  → level argument → "INFO"

    Args:
        name: Logger name — pass __name__ from the calling module.
        fmt: "json" or "text". Overridden by LOG_FORMAT env var.
        level: "DEBUG", "INFO", "WARNING", "ERROR". Overridden by LOG_LEVEL env var.

    Returns:
        A standard logging.Logger instance.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    fmt = os.environ.get("LOG_FORMAT", fmt or "text")
    level = os.environ.get("LOG_LEVEL", level or "INFO").upper()

    formatter: logging.Formatter = _JsonFormatter() if fmt == "json" else _TextFormatter()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level, logging.INFO))
    logger.propagate = False

    # Every module builds its own logger with propagate=False, so a file
    # handler has to be attached to each one rather than to a shared parent.
    _track(logger)
    if _file_handler is not None:
        logger.addHandler(_file_handler)
    return logger


def add_file_handler(logger: logging.Logger, path: str, fmt: str = "json") -> None:
    """Attach a plain (non-rotating) file handler to one existing logger.

    Prefer setup_file_logging() — it rotates, and it covers every logger in the
    package rather than the single one passed here.

    Args:
        logger: Logger instance returned by get_logger().
        path: Path to the log file (will be created/appended).
        fmt: "json" (default) or "text".
    """
    formatter: logging.Formatter = _JsonFormatter() if fmt == "json" else _TextFormatter()
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(formatter)
    logger.addHandler(handler)


def setup_file_logging(
    path: str,
    fmt: str = "json",
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
) -> str:
    """Send every camera-orchestrator log to a rotating file as well as stdout.

    Console output goes to the terminal that launched the process, which is no
    use once it has scrolled or when the failure needs reading back afterwards —
    a stalled live view or a capture that died mid-sequence is exactly the case
    where the record has to outlive the terminal.

    Applies to loggers created later *and* to those already built, so it works
    wherever it is called during startup.

    Args:
        path: Log file path. Parent directories are created.
        fmt: "json" (default — greppable) or "text".
        max_bytes: Rotate once the file passes this size.
        backup_count: How many rotated files to keep.

    Returns:
        The resolved path being written to.
    """
    global _file_handler

    dest = Path(path).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)

    formatter: logging.Formatter = _JsonFormatter() if fmt == "json" else _TextFormatter()
    handler = RotatingFileHandler(
        dest, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    handler.setFormatter(formatter)

    if _file_handler is not None:          # replace an earlier destination
        for logger in _configured:
            logger.removeHandler(_file_handler)
        _file_handler.close()

    _file_handler = handler
    for logger in _configured:
        logger.addHandler(handler)
    return str(dest)
