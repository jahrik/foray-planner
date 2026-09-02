"""Open-Meteo precipitation client (issue #226): offline validation, mocked series parsing,
and the never-partial window-sum rule."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

import httpx
import pytest

from foray.sources import precip

# Captured before conftest's autouse `_no_precip_network` swaps these for network blockers -
# this module exercises the real client against a MockTransport, so it puts them back.
_REAL_ARCHIVE = precip.fetch_archive_precip
_REAL_RECENT = precip.fetch_recent_precip


@pytest.fixture(autouse=True)
def _real_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(precip._throttle, "min_interval", 0.0)
    monkeypatch.setattr(precip, "fetch_archive_precip", _REAL_ARCHIVE)
    monkeypatch.setattr(precip, "fetch_recent_precip", _REAL_RECENT)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_rejects_out_of_range_coordinates() -> None:
    with pytest.raises(ValueError):
        precip.fetch_archive_precip(999.0, 0.0, dt.date(2026, 1, 1), dt.date(2026, 1, 3))


def test_archive_parses_series_and_nulls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["start_date"] == "2026-01-01"
        assert request.url.params["daily"] == "precipitation_sum"
        return httpx.Response(
            200,
            json={"daily": {"time": ["2026-01-01", "2026-01-02", "2026-01-03"], "precipitation_sum": [1.5, None, 0.0]}},
        )

    series = precip.fetch_archive_precip(
        45.0, -122.0, dt.date(2026, 1, 1), dt.date(2026, 1, 3), client=_client(handler)
    )
    assert series == {dt.date(2026, 1, 1): 1.5, dt.date(2026, 1, 2): None, dt.date(2026, 1, 3): 0.0}


def test_recent_passes_past_days() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["past_days"] == "30"
        return httpx.Response(200, json={"daily": {"time": ["2026-01-01"], "precipitation_sum": [2.0]}})

    series = precip.fetch_recent_precip(45.0, -122.0, past_days=30, client=_client(handler))
    assert series == {dt.date(2026, 1, 1): 2.0}


def test_http_error_propagates() -> None:
    with pytest.raises(httpx.HTTPStatusError):
        precip.fetch_archive_precip(
            0.0,
            0.0,
            dt.date(2026, 1, 1),
            dt.date(2026, 1, 2),
            client=_client(lambda request: httpx.Response(503)),
        )


def test_retries_a_429_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"daily": {"time": ["2026-01-01"], "precipitation_sum": [0.4]}})

    series = precip.fetch_archive_precip(0.0, 0.0, dt.date(2026, 1, 1), dt.date(2026, 1, 1), client=_client(handler))
    assert series == {dt.date(2026, 1, 1): 0.4}
    assert calls["n"] == 2


def test_window_sum_totals_the_span() -> None:
    series = {dt.date(2026, 6, 10) - dt.timedelta(days=i): float(i) for i in range(10)}
    # days 2026-06-10, -09, ..., -04  (7 days) -> 0+1+2+3+4+5+6
    assert precip.window_sum(series, dt.date(2026, 6, 10), 7) == 21.0


def test_window_sum_is_none_when_a_day_is_missing() -> None:
    series = {dt.date(2026, 6, 10): 1.0, dt.date(2026, 6, 9): 2.0}  # day -2 absent
    assert precip.window_sum(series, dt.date(2026, 6, 10), 3) is None


def test_window_sum_is_none_when_a_day_is_null() -> None:
    series: dict[dt.date, float | None] = {
        dt.date(2026, 6, 10): 1.0,
        dt.date(2026, 6, 9): None,
        dt.date(2026, 6, 8): 3.0,
    }
    assert precip.window_sum(series, dt.date(2026, 6, 10), 3) is None
