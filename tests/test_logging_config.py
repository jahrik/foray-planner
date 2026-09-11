"""``foray.logging_config.resolve_level`` level resolution + the JSON formatter."""

from __future__ import annotations

import json
import logging
import sys

import pytest

from foray.logging_config import _JsonFormatter, resolve_level


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        ("DEBUG", logging.DEBUG),
        ("debug", logging.DEBUG),
        (" Warning ", logging.WARNING),
        ("", logging.INFO),
        ("bogus", logging.INFO),
        (None, logging.INFO),
    ],
)
def test_env_level_resolution(monkeypatch: pytest.MonkeyPatch, env_value: str | None, expected: int) -> None:
    if env_value is None:
        monkeypatch.delenv("FORAY_LOG_LEVEL", raising=False)
    else:
        monkeypatch.setenv("FORAY_LOG_LEVEL", env_value)
    assert resolve_level(None) == expected


def test_explicit_level_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORAY_LOG_LEVEL", "DEBUG")
    assert resolve_level(logging.ERROR) == logging.ERROR
    assert resolve_level("error") == logging.ERROR


def test_json_formatter_emits_valid_json_with_core_fields() -> None:
    record = logging.LogRecord("foray.jobs", logging.INFO, __file__, 1, "job finished", (), None)
    record.job = "fire"
    payload = json.loads(_JsonFormatter().format(record))
    assert payload["level"] == "INFO"
    assert payload["logger"] == "foray.jobs"
    assert payload["message"] == "job finished"
    assert payload["job"] == "fire"
    assert "timestamp" in payload


def test_json_formatter_includes_exc_info() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord("foray.jobs", logging.ERROR, __file__, 1, "job failed", (), sys.exc_info())
    payload = json.loads(_JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exc_info"]
