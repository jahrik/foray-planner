"""Unit tests for the shared HTTP helpers (foray.http) - all offline."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx

from foray.http import SOURCE_ERRORS, Throttle, retry_after_seconds


def _resp(retry_after: str | None) -> httpx.Response:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return httpx.Response(429, headers=headers)


def test_retry_after_reads_delta_seconds() -> None:
    assert retry_after_seconds(_resp("7"), attempt=1) == 7.0
    assert retry_after_seconds(_resp("2.5"), attempt=1) == 2.5


def test_retry_after_clamps_to_cap() -> None:
    assert retry_after_seconds(_resp("9999"), attempt=1, cap=60.0) == 60.0


def test_retry_after_parses_http_date() -> None:
    soon = datetime.now(UTC) + timedelta(seconds=5)
    value = retry_after_seconds(_resp(format_datetime(soon)), attempt=1, cap=120.0)
    assert 0.0 <= value <= 6.0


def test_retry_after_falls_back_to_exponential_backoff() -> None:
    assert retry_after_seconds(_resp(None), attempt=1, base_delay=2.0) == 2.0
    assert retry_after_seconds(_resp("garbage"), attempt=3, base_delay=2.0) == 8.0


def test_retry_after_never_returns_negative_for_a_past_date() -> None:
    past = datetime.now(UTC) - timedelta(hours=1)
    assert retry_after_seconds(_resp(format_datetime(past)), attempt=1) == 0.0


def test_throttle_disabled_when_interval_non_positive() -> None:
    throttle = Throttle(0.0)
    start = time.monotonic()
    for _ in range(5):
        throttle.wait()
    assert time.monotonic() - start < 0.05


def test_throttle_paces_successive_calls() -> None:
    throttle = Throttle(0.05)
    throttle.wait()  # first call sets the clock, doesn't block
    start = time.monotonic()
    throttle.wait()
    assert time.monotonic() - start >= 0.04


def test_source_errors_covers_transport_and_decode_failures() -> None:
    assert httpx.HTTPError in SOURCE_ERRORS
    assert {ValueError, KeyError, TypeError} <= set(SOURCE_ERRORS)
