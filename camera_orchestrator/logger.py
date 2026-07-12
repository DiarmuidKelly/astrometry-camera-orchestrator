"""Structured logger for camera-orchestrator.

Provides a thin wrapper around Python's standard logging with:
- Human-readable console output by default
- Optional JSON line output (LOG_FORMAT=json env var or fmt="json")
- Structured key=value context via the `extra` dict (zerolog-style)
- File handler ready to wire up — call add_file_handler(path) to enable

Usage:
    from camera_orchestrator.logger import get_logger
    log = get_logger(__name__)
    log.info("solving image", extra={"image": "IMG_4341.JPG", "index": 1, "total": 113})
"""
from __future__ import annotations

import json
import logging
import os
import sys
import warnings
from typing import Any

# Suppress noisy astropy WCS warnings that appear on every solve
warnings.filterwarnings("ignore", message=".*FITSFixedWarning.*")
warnings.filterwarnings("ignore", message=".*WCS transformation has more axes.*")

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


def get_logger(name: str, fmt: str | None = None) -> logging.Logger:
    """Return a named logger configured for camera-orchestrator.

    Args:
        name: Logger name — pass __name__ from the calling module.
        fmt: "json" for JSON lines, "text" for human-readable (default).
             Overridden by the LOG_FORMAT environment variable.

    Returns:
        A standard logging.Logger instance.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    fmt = os.environ.get("LOG_FORMAT", fmt or "text")
    formatter: logging.Formatter = _JsonFormatter() if fmt == "json" else _TextFormatter()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger


def add_file_handler(logger: logging.Logger, path: str, fmt: str = "json") -> None:
    """Attach a file handler to an existing logger.

    Args:
        logger: Logger instance returned by get_logger().
        path: Path to the log file (will be created/appended).
        fmt: "json" (default) or "text".
    """
    formatter: logging.Formatter = _JsonFormatter() if fmt == "json" else _TextFormatter()
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
