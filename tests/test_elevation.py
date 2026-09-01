"""Elevation lookup tests - coordinate validation (offline) and a mocked Open-Meteo batch call."""

from __future__ import annotations

import httpx
import pytest

from foray import elevation
from foray.elevation import lookup_batch


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open-Meteo's politeness throttle (see elevation._throttle) only wastes wall-clock here -
    every test's MockTransport is offline."""
    monkeypatch.setattr(elevation._throttle, "min_interval", 0.0)


def test_empty_input_returns_empty() -> None:
    assert lookup_batch([]) == []


def test_rejects_out_of_range_coordinates() -> None:
    with pytest.raises(ValueError):
        lookup_batch([(999.0, 0.0)])


def test_rejects_oversized_batch() -> None:
    with pytest.raises(ValueError):
        lookup_batch([(45.0, -121.0)] * (elevation.MAX_BATCH + 1))


def test_returns_rounded_metres_in_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["latitude"] == "45.300000,47.600000"
        assert request.url.params["longitude"] == "-121.700000,-122.300000"
        return httpx.Response(200, json={"elevation": [1203.6, 12.2]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert lookup_batch([(45.3, -121.7), (47.6, -122.3)], client=client) == [1204, 12]


def test_missing_value_becomes_none() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"elevation": [None, 30.0]}))
    )
    assert lookup_batch([(0.0, 0.0), (1.0, 1.0)], client=client) == [None, 30]


def test_short_response_pads_with_none() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"elevation": [30.0]}))
    )
    assert lookup_batch([(0.0, 0.0), (1.0, 1.0)], client=client) == [30, None]


def test_http_error_propagates() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    with pytest.raises(httpx.HTTPError):
        lookup_batch([(45.3, -121.7)], client=client)


def test_retries_a_429_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"elevation": [500.0]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert lookup_batch([(45.3, -121.7)], client=client) == [500]
    assert calls["n"] == 2


def test_retry_after_honours_http_date_form() -> None:
    from datetime import UTC, datetime
    from email.utils import format_datetime

    from foray.http import retry_after_seconds

    resp = httpx.Response(429, headers={"Retry-After": format_datetime(datetime.now(UTC))})
    # An HTTP-date at (or before) "now" means retry immediately, not fall back to backoff.
    assert retry_after_seconds(resp, 2, cap=120.0) == 0.0


def test_gives_up_after_persistent_429() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(429, headers={"Retry-After": "0"}))
    )
    with pytest.raises(httpx.HTTPStatusError):
        lookup_batch([(45.3, -121.7)], client=client)
