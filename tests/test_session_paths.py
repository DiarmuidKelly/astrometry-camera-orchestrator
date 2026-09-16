"""Tests for resolve_session — pure path maths, date injected for determinism."""
from __future__ import annotations

from datetime import date

from camera_orchestrator.application.sequence_service import request_name
from camera_orchestrator.application.session_paths import resolve_session

DAY = date(2026, 7, 23)


def test_named_session_is_date_prefixed_under_root():
    out_dir, session_dir = resolve_session("/data", "orion", today=DAY)
    assert session_dir == "/data/20260723-orion"
    assert out_dir == session_dir                      # frames land in the session folder


def test_unnamed_session_is_loose_in_the_root():
    out_dir, session_dir = resolve_session("/data", None, today=DAY)
    assert out_dir == "/data"
    assert session_dir is None                         # None => no manifest is written


def test_empty_name_is_treated_as_unnamed():
    # argparse hands through "" for `--name ""`; that is not a session label.
    assert resolve_session("/data", "", today=DAY) == ("/data", None)


def test_date_is_zero_padded():
    _, session_dir = resolve_session("/data", "m42", today=date(2026, 1, 5))
    assert session_dir.endswith("20260105-m42")        # YYYYMMDD, never Y-M-D


def test_relative_root_is_preserved():
    _, session_dir = resolve_session("./incoming", "orion", today=DAY)
    assert session_dir == "incoming/20260723-orion"    # Path normalises the leading ./


def test_same_name_and_day_resolves_to_one_folder():
    # align and sequence must agree, or a night's data splits across folders.
    align = resolve_session("/data", "orion", today=DAY)
    sequence = resolve_session("/data", "orion", today=DAY)
    assert align == sequence


def test_round_trips_through_request_name():
    # resolve_session is the inverse of sequence_service.request_name.
    _, session_dir = resolve_session("/data", "orion", today=DAY)
    assert request_name(session_dir) == "orion"


def test_round_trips_with_a_hyphenated_name():
    _, session_dir = resolve_session("/data", "orion-wide", today=DAY)
    assert request_name(session_dir) == "orion-wide"   # only the date prefix is stripped


def test_defaults_to_today_when_no_date_injected():
    _, session_dir = resolve_session("/data", "orion")
    assert session_dir == f"/data/{date.today():%Y%m%d}-orion"
