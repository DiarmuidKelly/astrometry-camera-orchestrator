"""Tests for camera_orchestrator.log."""
import json
import logging

from camera_orchestrator.log.logger import (
    _JsonFormatter,
    _TextFormatter,
    get_logger,
    add_file_handler,
)


def _make_record(msg: str = "test message", **extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for k, v in extra.items():
        setattr(record, k, v)
    return record


def test_json_formatter_produces_valid_json():
    record = _make_record("hello world")
    output = _JsonFormatter().format(record)
    data = json.loads(output)
    assert data["message"] == "hello world"
    assert data["level"] == "info"
    assert data["logger"] == "test.logger"
    assert "time" in data


def test_json_formatter_includes_extra_fields():
    record = _make_record("solving", image="IMG_4341.JPG", index=1)
    data = json.loads(_JsonFormatter().format(record))
    assert data["image"] == "IMG_4341.JPG"
    assert data["index"] == 1


def test_json_formatter_excludes_logrecord_internals():
    record = _make_record("msg")
    data = json.loads(_JsonFormatter().format(record))
    for field in ["lineno", "pathname", "thread", "processName", "msecs"]:
        assert field not in data


def test_text_formatter_contains_message():
    record = _make_record("batch complete")
    output = _TextFormatter().format(record)
    assert "batch complete" in output


def test_text_formatter_excludes_logrecord_internals():
    record = _make_record("msg")
    output = _TextFormatter().format(record)
    for field in ["lineno", "pathname", "thread", "processName", "msecs"]:
        assert field not in output


def test_text_formatter_includes_extra_fields():
    record = _make_record("solved", ra=267.73, dec=-29.46)
    output = _TextFormatter().format(record)
    assert "ra=267.73" in output
    assert "dec=-29.46" in output


def test_get_logger_returns_logger():
    log = get_logger("test.get_logger")
    assert isinstance(log, logging.Logger)


def test_get_logger_no_duplicate_handlers():
    name = "test.no_duplicates"
    log1 = get_logger(name)
    log2 = get_logger(name)
    assert log1 is log2
    assert len(log1.handlers) == 1


def test_get_logger_env_level(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    log = get_logger("test.env_level_debug")
    assert log.level == logging.DEBUG


def test_get_logger_env_format_json(monkeypatch, tmp_path):
    monkeypatch.setenv("LOG_FORMAT", "json")
    log = get_logger("test.env_format_json")
    handler = log.handlers[0]
    assert isinstance(handler.formatter, _JsonFormatter)


def test_add_file_handler(tmp_path):
    log = get_logger("test.file_handler")
    log_file = tmp_path / "test.log"
    add_file_handler(log, str(log_file))
    log.info("written to file", extra={"key": "value"})
    content = log_file.read_text()
    data = json.loads(content.strip())
    assert data["message"] == "written to file"
    assert data["key"] == "value"
