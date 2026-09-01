"""``foray.logging_config.resolve_level`` level resolution."""

from __future__ import annotations

import logging

import pytest

from foray.logging_config import resolve_level


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
