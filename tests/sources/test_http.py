"""Unit tests for the shared HTTP helpers (foray.sources.http) - all offline."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from foray.sources.http import SOURCE_ERRORS, HttpRangeReader, Throttle, retry_after_seconds


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


_CONTENT = b"0123456789" * 5


def test_http_range_reader_reads_content_via_valid_206_responses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(_CONTENT))})
        start, end = (int(part) for part in request.headers["Range"].removeprefix("bytes=").split("-"))
        return httpx.Response(
            206,
            headers={"Content-Range": f"bytes {start}-{end}/{len(_CONTENT)}"},
            content=_CONTENT[start : end + 1],
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        reader = HttpRangeReader(client, "https://example.test/file")
        buf = bytearray(4)
        assert reader.readinto(buf) == 4
        assert bytes(buf) == _CONTENT[:4]
        reader.seek(6)
        buf2 = bytearray(4)
        assert reader.readinto(buf2) == 4
        assert bytes(buf2) == _CONTENT[6:10]


def test_http_range_reader_retries_transient_error_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(_CONTENT))})
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("transient", request=request)
        return httpx.Response(206, headers={"Content-Range": f"bytes 0-3/{len(_CONTENT)}"}, content=_CONTENT[:4])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        reader = HttpRangeReader(client, "https://example.test/file")
        buf = bytearray(4)
        assert reader.readinto(buf) == 4
        assert bytes(buf) == _CONTENT[:4]
    assert calls["n"] == 2


def test_http_range_reader_paces_requests_via_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []
    monkeypatch.setattr(Throttle, "wait", lambda self, units=1.0: waits.append(units))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(_CONTENT))})
        start, end = (int(part) for part in request.headers["Range"].removeprefix("bytes=").split("-"))
        return httpx.Response(
            206,
            headers={"Content-Range": f"bytes {start}-{end}/{len(_CONTENT)}"},
            content=_CONTENT[start : end + 1],
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        reader = HttpRangeReader(client, "https://example.test/file", throttle=Throttle(0.05))
        reader.readinto(bytearray(4))
        reader.seek(6)
        reader.readinto(bytearray(4))
    assert len(waits) == 2


def test_http_range_reader_rejects_non_206_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(_CONTENT))})
        return httpx.Response(200, content=_CONTENT)  # ignores Range entirely

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        reader = HttpRangeReader(client, "https://example.test/file", attempts=2)
        with pytest.raises(OSError, match="expected 206"):
            reader.readinto(bytearray(4))
